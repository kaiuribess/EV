"""Read-only owned-order capture and offline financial comparison.

Preserves the documented get-order, matched-detail and wallet responses. A
successful comparison never certifies submission, restart, settlement or access.
"""
from datetime import timedelta
import math
import re

from ..core.models import utcnow
from .prophetx_financial import (
    ProphetXHistory, authenticated_identity, normalize_financial_order,
    order_with_history_fee, prophetx_fingerprint, reporting_cursor, source_time,
)
from .reconciliation import ReconciliationError, _identity, _number, _object


async def capture_owned_orders(client, orders, *, max_pages=4, max_orders=20):
    from ..core.lifecycle_capture import _pages, sanitize
    if type(max_orders) is not int or not 1 <= max_orders <= 100 or type(max_pages) is not int or not 1 <= max_pages <= 20:
        raise ValueError("Bounded owned-order capture required")
    user, fingerprint = authenticated_identity(client)
    selected = [order for order in orders if order.venue == "prophetx" and order.mode == client.settings.environment]
    # Never silently truncate owned inventory or query a different account.
    if len(selected) > max_orders or len({o.id for o in selected}) != len(selected):
        raise ValueError("Owned-order capture bound or duplicate identity")
    for order in selected:
        if (order.account_fingerprint != fingerprint or not isinstance(order.venue_order_id, str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", order.venue_order_id)):
            raise ValueError("Owned order has no matching authenticated native identity")
    result = []
    for order in selected:
        bundle = {"local_order_id": order.id, "native_order_id": order.venue_order_id,
                  "started_at": utcnow().isoformat(), "complete": False, "errors": []}
        try:
            raw = await client.reconcile_order(order.venue_order_id)
            bundle["order"] = {"received_at": utcnow().isoformat(), "response": sanitize(raw)}
            row = _object(_object(raw, "order response").get("data"), "order")
            _identity(order.intent, row, "order_id", "external_id", order.venue_order_id)
            if row.get("user_id") != user or row.get("strike_id") != order.intent.instrument.instrument_id:
                raise ReconciliationError("Owned order account or strike mismatch")
            created = source_time(row.get("created_at"), observed_at=utcnow(), name="Order creation")
            market = str(_number(row.get("market_id"), "market identity", positive=True, whole=True))
            specs = (
                ("history", "/v4/mm/get_order_history", "orders", {"from": math.floor(created.timestamp()),
                    "to": math.ceil(utcnow().timestamp()), "market_id": market}),
                ("matched", "/v4/mm/get_order_matched_detail", "matching_details", {"order_id": order.venue_order_id}),
                ("wallet", "/v4/mm/get_transactions", "transactions", {"trade_id": order.venue_order_id}),
            )
            for name, path, collection, params in specs:
                bundle[name] = await _pages(client, path, collection, params=params, envelope="data",
                    cursor_key="next_cursor", cursor_param="next_cursor", max_pages=max_pages)
            bundle["complete"] = all(bundle[name].get("complete") is True for name, *_ in specs)
            if authenticated_identity(client) != (user, fingerprint):
                raise ReconciliationError("Authenticated account changed during capture")
        except Exception as exc:
            bundle["complete"] = False
            bundle["errors"].append({"type": type(exc).__name__, "http_status": getattr(exc, "status_code", None)})
        bundle["finished_at"] = utcnow().isoformat()
        result.append(bundle)
    return result


def _history(bundle, name, endpoint, collection, *, start, end, require_clock=True):
    value = _object(bundle.get(name), "captured collection")
    pages = value.get("pages")
    if value.get("complete") is not True or not isinstance(pages, list) or not 1 <= len(pages) <= 20:
        raise ReconciliationError("Missing complete captured pages")
    rows, clocks, cursors, previous = [], [], set(), start
    for index, page in enumerate(pages):
        page = _object(page, "captured page")
        received = source_time(page.get("received_at"), observed_at=end, name="Captured page receipt")
        if received < previous:
            raise ReconciliationError("Captured reporting pages are out of order")
        previous = received
        raw = _object(page.get("response"), "captured response")
        body = _object(raw.get("data"), "captured data")
        items = body.get(collection)
        if not isinstance(items, list):
            raise ReconciliationError("Missing captured collection")
        rows.extend(_object(item, "captured row") for item in items)
        if require_clock:
            clock = source_time(raw.get("last_synced_at"), observed_at=received,
                name="Captured reporting clock", max_age=timedelta(seconds=10))
            # A later long-running capture cannot silently refresh this clock.
            source_time(clock, observed_at=end, name="Reporting coverage", max_age=timedelta(seconds=10))
            clocks.append(clock)
        cursor, _ = reporting_cursor(body, endpoint=endpoint, collection=collection)
        if cursor is None:
            if index != len(pages)-1:
                raise ReconciliationError("Extra pages after history exhaustion")
        elif index == len(pages)-1 or cursor in cursors:
            raise ReconciliationError("Incomplete or repeated captured cursor")
        cursors.add(cursor)
    if rows != value.get("rows"):
        raise ReconciliationError("Captured collection differs from raw pages")
    return ProphetXHistory(tuple(rows), min(clocks) if clocks else None)


def normalize_captured_order(order, record, *, observed_at):
    bundles = record.get("owned_order_evidence")
    if not isinstance(bundles, list):
        return None
    matches = [item for item in bundles if isinstance(item, dict) and item.get("local_order_id") == order.id]
    if not matches:
        return None
    if len(matches) != 1:
        raise ReconciliationError("Duplicate owned-order evidence")
    bundle = matches[0]
    if bundle.get("complete") is not True:
        return None
    if bundle.get("errors") != [] or bundle.get("native_order_id") != order.venue_order_id:
        raise ReconciliationError("Invalid owned-order evidence scope")
    end = source_time(bundle.get("finished_at"), observed_at=observed_at, name="Capture completion")
    start = source_time(bundle.get("started_at"), observed_at=end, name="Capture start")
    raw = _object(bundle.get("order"), "captured order")
    receipt = source_time(raw.get("received_at"), observed_at=end, name="Order receipt")
    if receipt < start:
        raise ReconciliationError("Order response precedes capture")
    row = _object(_object(raw.get("response"), "order response").get("data"), "owned order")
    user = row.get("user_id")
    if (record.get("authenticated") is not True or record.get("identity_verified") is not True
            or record.get("venue") != "prophetx" or record.get("environment") != order.mode
            or prophetx_fingerprint(order.mode, user) != order.account_fingerprint
            or record.get("account_fingerprint") != order.account_fingerprint):
        raise ReconciliationError("Captured financial account scope mismatch")
    history = _history(bundle, "history", "/v4/mm/get_order_history", "orders", start=receipt, end=end)
    matched = _history(bundle, "matched", "/v4/mm/get_order_matched_detail", "matching_details", start=receipt, end=end)
    wallet = _history(bundle, "wallet", "/v4/mm/get_transactions", "transactions", start=receipt, end=end, require_clock=False)
    row = order_with_history_fee(row, history, order.venue_order_id, observed_at=end)
    return normalize_financial_order(order.intent, row, matched.rows, wallet.rows, user_id=user,
        expected_order_id=order.venue_order_id, observed_at=end, reporting_watermark=matched.last_synced_at)
