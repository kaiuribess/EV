"""Authenticated Kalshi ordinary binary settlement, with exact cash attribution.

Only primary direct-member buy-only positions with one durable order per native
market are admitted. Scalar/FMV, netting, shared positions and archived cash
records are deliberately not inferred from public market results.

Official contracts checked 2026-10-21:
https://docs.kalshi.com/api-reference/portfolio/get-settlements
https://docs.kalshi.com/api-reference/portfolio/get-fills
https://docs.kalshi.com/api-reference/historical/get-historical-fills
https://docs.kalshi.com/api-reference/historical/get-historical-positions
https://kalshi.com/docs/kalshi-fee-schedule.pdf
"""
from __future__ import annotations

from decimal import Decimal
import json

from ..core.models import OrderStatus, utcnow
from .account_probe import kalshi_account_fingerprint, _kalshi_pages
from .kalshi import KalshiClient
from .reconciliation import ReconciliationError, _number, _object, _text, _time


POLICY = "kalshi_ioc_binary_halt_on_exception"
ZERO = Decimal(0)


def supports_kalshi_settlement_scope(profile, adapter, mappings, managed_orders) -> bool:
    """Capability only: genuine lifecycle evidence and rule review stay required."""
    from ..core.readiness import LaunchProfile
    from .bridge import NativeExecutionVenue
    from .book_normalization import NativeBookMapper, NativeMarketMapping

    if (not isinstance(profile, LaunchProfile) or profile.settlement_policy != POLICY
            or profile.venue != "kalshi" or not isinstance(adapter, NativeExecutionVenue)
            or not isinstance(adapter.client, KalshiClient)
            or adapter.client.settings.environment != profile.environment):
        return False
    try:
        reviewed = NativeBookMapper(tuple(NativeMarketMapping.model_validate(m.model_dump()) for m in mappings))
        now = utcnow()
        family = profile.family_rule
        requested = [m for m in reviewed.mappings if m.instrument.venue == "kalshi"
            and (m.market.id in profile.market_ids or family is not None
                 and family.matches(m.market)
                 and m.instrument.source.startswith(f"family:{family.digest()}:")
                 and m.market.start_time > now
                 and m.fee_bound.observed_at <= now < m.fee_bound.valid_until)]
        if family is None:
            if {m.market.id for m in requested} != set(profile.market_ids):
                return False
        else:
            complete = {market_id for market_id in {m.market.id for m in requested}
                if {m.instrument.outcome for m in requested if m.market.id == market_id}
                   == set(next(m.market for m in requested if m.market.id == market_id).outcomes)}
            requested = [m for m in requested if m.market.id in complete]
            if not requested or not family.approved_at <= now < family.expires_at:
                return False
        for mapping in requested:
            instrument = mapping.instrument
            execution = adapter.mappings.get((mapping.market.id, instrument.outcome))
            if (instrument.quantity_unit != "contracts" or instrument.payout_per_unit_usd != 1
                    or instrument.native_outcome not in {"yes", "no"}
                    or instrument.quantity_step < Decimal(".01") or instrument.quantity_step % Decimal(".01")
                    or instrument.price_format != "probability"
                    or not mapping.fee_bound.observed_at <= now < mapping.fee_bound.valid_until
                    or execution is None or not execution.verified or not execution.fee_schedule_verified
                    or execution.canonical_market_id != mapping.market.id
                    or execution.canonical_outcome != instrument.outcome
                    or execution.native_instrument_id != instrument.instrument_id
                    or execution.native_outcome != instrument.native_outcome
                    or execution.settlement_fingerprint != mapping.market.identity()
                    or execution.native_quantity_unit != instrument.quantity_unit
                    or execution.native_payout_usd != instrument.payout_per_unit_usd):
                return False
        historical = set()
        for order in managed_orders:
            if (order.venue != "kalshi" or order.mode != profile.environment
                    or order.account_fingerprint != profile.account_fingerprint):
                continue
            ticker = order.intent.instrument.instrument_id
            if ticker in historical:
                return False
            historical.add(ticker)
        return True
    except (AttributeError, KeyError, TypeError, ValueError):
        return False


async def _settlements(client, ticker):
    # The official settlement response may omit its optional cursor at EOF.
    rows, cursor, seen = [], None, set()
    for _ in range(20):
        page = _object(await client.request("GET", "/portfolio/settlements", params={
            "ticker": ticker, "subaccount": 0, "limit": 200, **({"cursor": cursor} if cursor else {})}), "settlements")
        if not isinstance(page.get("settlements"), list):
            raise ReconciliationError("Incomplete Kalshi settlement history")
        rows.extend(page["settlements"])
        if "cursor" not in page or page["cursor"] == "":
            return rows
        cursor = page["cursor"]
        if not isinstance(cursor, str) or cursor in seen:
            raise ReconciliationError("Invalid Kalshi settlement cursor")
        seen.add(cursor)
    raise ReconciliationError("Kalshi settlement history exceeds page bound")


async def reconcile_kalshi_settlement(client, order, managed_orders):
    """GET-only actual cash reconciliation; exceptions must retain exposure.

    Supply ALL durable orders, including settled ones. Rechecking a previous
    payment that disappears or changes requires durable invalidation by OMS.
    No fill or no settlement row is proof of no payment or cancellation.
    """
    from ..core.native_oms import NativeSettlement

    if not isinstance(client, KalshiClient) or order.venue != "kalshi":
        raise ReconciliationError("Kalshi settlement requires the actual native client")
    instrument = order.intent.instrument
    if (order.mode != client.settings.environment or not order.venue_order_id
            or not order.reconciliation_complete or order.filled_quantity <= 0
            or order.status not in {OrderStatus.FILLED, OrderStatus.CANCELLED}
            or instrument.quantity_unit != "contracts" or instrument.payout_per_unit_usd != 1
            or instrument.native_outcome not in {"yes", "no"}
            or instrument.settlement_rules_fingerprint != order.market_fingerprint
            or order.market_snapshot.identity() != order.market_fingerprint):
        raise ReconciliationError("Terminal, fully reconciled exact Kalshi contract required")
    related = [o for o in managed_orders if o.venue == "kalshi" and o.mode == order.mode
               and o.account_fingerprint == order.account_fingerprint
               and o.intent.instrument.instrument_id == instrument.instrument_id]
    if len(related) != 1 or related[0].id != order.id:
        raise ReconciliationError("Shared Kalshi native positions need a cash allocation adapter")

    keys = _object(await client.request("GET", "/api_keys"), "authenticated keys").get("api_keys")
    if not isinstance(keys, list):
        raise ReconciliationError("Authenticated Kalshi identity is missing")
    matching = [r for r in keys if _object(r, "key").get("api_key_id") == client.settings.api_key_id]
    if (len(matching) != 1 or matching[0].get("subaccount") not in {None, 0}
            or isinstance(matching[0].get("subaccount"), bool) or matching[0].get("fcm_subtrader_id")
            or kalshi_account_fingerprint(order.mode, client.settings.api_key_id) != order.account_fingerprint):
        raise ReconciliationError("Kalshi settlement primary account identity mismatch")
    rows = await _settlements(client, instrument.instrument_id)
    if not rows:
        return None  # Includes archive migration: caller must invalidate previous credit.
    if len(rows) != 1:
        raise ReconciliationError("Multiple Kalshi settlement records/shards are ambiguous")
    row = _object(rows[0], "settlement")
    shard = row.get("exchange_index")
    result = row.get("market_result")
    if (row.get("ticker") != instrument.instrument_id or type(shard) is not int or shard < 0
            or result not in {"yes", "no"}):
        raise ReconciliationError("Scalar, unknown or mismatched Kalshi settlement requires cash review")
    _text(row.get("event_ticker"), "settlement event ticker")
    settled_at = _time(row.get("settled_time"))
    if not order.created_at <= settled_at <= utcnow():
        raise ReconciliationError("Kalshi settlement timestamp is outside order lifetime")
    for side in ("yes", "no"):
        quantity = _number(row.get(f"{side}_count_fp"), "settled quantity")
        cost = _number(row.get(f"{side}_total_cost_dollars"), "settled cost")
        if (quantity != (order.filled_quantity if side == instrument.native_outcome else ZERO)
                or cost != (order.filled_cost_usd if side == instrument.native_outcome else ZERO)):
            raise ReconciliationError("Kalshi settled quantity/cost differs from durable fills")
    cents = row.get("revenue")
    if type(cents) is not int or cents < 0:
        raise ReconciliationError("Kalshi actual settlement revenue cents are required")
    payout = Decimal(cents) / 100
    value = row.get("value")
    if value is not None and (type(value) is not int or value != (100 if result == "yes" else 0)):
        raise ReconciliationError("Kalshi binary settlement value conflicts with result")
    if (order.gross_payout_usd != order.filled_quantity
            or payout != (order.gross_payout_usd if result == instrument.native_outcome else ZERO)
            or _number(row.get("fee_cost"), "settled fee") != order.fees):
        raise ReconciliationError("Kalshi settlement cash/fees disagree with the filled position")

    # Query both partitions and the entire market, not just this order: unknown
    # external fills or offsetting trades must not disappear behind net quantity.
    fills = {}
    for path in ("/portfolio/fills", "/historical/fills"):
        for raw in await _kalshi_pages(client, path, "fills", ticker=instrument.instrument_id, subaccount=0):
            fill = _object(raw, "settled fill")
            fid = _text(fill.get("fill_id"), "fill ID")
            if (fill.get("trade_id") != fid or fill.get("order_id") != order.venue_order_id
                    or fill.get("ticker") != instrument.instrument_id
                    or fill.get("market_ticker") != instrument.instrument_id
                    or type(fill.get("exchange_index")) is not int or fill["exchange_index"] != shard
                    or fill.get("outcome_side") != instrument.native_outcome
                    or fill.get("book_side") != ("bid" if instrument.native_outcome == "yes" else "ask")
                    or fill.get("action") not in {None, "buy"}
                    or fill.get("side") not in {None, instrument.native_outcome}
                    or type(fill.get("subaccount_number")) is not int or fill["subaccount_number"] != 0
                    or fill.get("is_taker") is not True):
                raise ReconciliationError("Unmanaged, maker or mismatched Kalshi settlement fill")
            yes = _number(fill.get("yes_price_dollars"), "YES fill price", positive=True)
            no = _number(fill.get("no_price_dollars"), "NO fill price", positive=True)
            if yes + no != 1:
                raise ReconciliationError("Conflicting Kalshi complementary fill prices")
            price = yes if instrument.native_outcome == "yes" else no
            quantity = _number(fill.get("count_fp"), "filled contracts", positive=True)
            instrument.validate_quantity(quantity)
            if not instrument.price_satisfies_limit(price, order.intent.limit_price):
                raise ReconciliationError("Kalshi fill violated the durable price limit")
            fee = _number(fill.get("fee_cost"), "actual fill fee")
            when = _time(fill.get("created_time"))
            if not order.created_at <= when <= settled_at:
                raise ReconciliationError("Kalshi fill timestamp outside order/settlement lifetime")
            economic = (quantity, quantity * price, fee, when, shard)
            if fid in fills and fills[fid] != economic:
                raise ReconciliationError("Conflicting Kalshi duplicate fill history")
            fills[fid] = economic
    if (not fills or sum((r[0] for r in fills.values()), ZERO) != order.filled_quantity
            or sum((r[1] for r in fills.values()), ZERO) != order.filled_cost_usd
            or sum((r[2] for r in fills.values()), ZERO) != order.fees):
        raise ReconciliationError("Complete Kalshi fill quantity/cost/fees must match durable order")
    return NativeSettlement(order_id=order.id, venue="kalshi", environment=order.mode,
        account_fingerprint=order.account_fingerprint, market_fingerprint=order.market_fingerprint,
        settlement_id=f"kalshi:{instrument.instrument_id}:{shard}:{settled_at.isoformat()}",
        payout_usd=payout, additional_fee_usd=ZERO, fee_refund_usd=ZERO, settled_at=settled_at,
        evidence=json.dumps({"source": "kalshi.authenticated.portfolio.settlements", "result": result,
            "exchange_index": shard, "revenue_cents": cents, "filled_quantity": str(order.filled_quantity),
            "filled_cost_usd": str(order.filled_cost_usd), "fees_usd": str(order.fees),
            "fill_ids": sorted(fills)}, sort_keys=True))
