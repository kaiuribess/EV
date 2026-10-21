"""Offline cross-checks of captured account history and durable native records.

A matching historical order is not an OMS lifecycle certificate. In particular,
current SQLite rows do not prove that an intent committed before transmission,
and two database copies do not prove a process restarted. Those gaps stay visible.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
import hashlib
import json

from .native_accounting import NativeAccounting, NativeFill
from .native_oms import NativeOrder, NativeSettlement
from .native_trace import review_application_trace
from ..venues.reconciliation import normalize_kalshi_order, normalize_novig_fill, normalize_novig_order


def artifact_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _table(capture, name):
    table = capture.get("local_oms", {}).get("tables", {}).get(name, {})
    if (table.get("available") is not True or table.get("complete") is not True or not isinstance(table.get("rows"), list)
            or any(not isinstance(row, dict) for row in table["rows"])):
        return None
    return table["rows"]


def _collection(record, name):
    collection = record.get("collections", {}).get(name, {})
    rows = collection.get("rows")
    return rows if collection.get("complete") is True and isinstance(rows, list) and all(isinstance(row, dict) for row in rows) else None


def _check(checks, name, passed, *, absent=False, detail=""):
    checks[name] = {"status": "missing" if absent else "matched" if passed else "mismatch", "detail": detail}


def _economics(order, fills):
    accounting = NativeAccounting(order.intent, order.venue_order_id)
    for fill in fills:
        accounting.apply_fill(fill)
    expected = (order.filled_quantity, order.filled_cost_usd, order.gross_payout_usd, order.fees)
    actual = tuple(getattr(accounting.totals, field) for field in ("quantity", "cost_usd", "gross_payout_usd", "fee_usd"))
    if actual != expected:
        raise ValueError("Individual native fill totals do not match durable order")
    # Replaying identical evidence must leave accounting unchanged. This is an
    # offline invariant check, explicitly not observed production idempotency.
    before = accounting.totals
    for fill in fills:
        if not accounting.apply_fill(fill).duplicate:
            raise ValueError("Repeated native fill changed accounting")
    if accounting.totals != before:
        raise ValueError("Native fill replay changed totals")
    return accounting.totals


def _remote_report(order, record, at):
    raw_orders = _collection(record, "orders")
    if order.venue == "novig":
        collections = [_collection(record, "orders_" + state) for state in ("pending", "open", "filled", "canceled", "rejected")]
        raw_orders = None if any(rows is None for rows in collections) else [row for rows in collections for row in rows]
    if raw_orders is None:
        return None
    key = "orderId" if order.venue == "novig" else "order_id"
    matches = [row for row in raw_orders if row.get(key) == order.venue_order_id]
    if len(matches) != 1:
        raise ValueError("Native history does not uniquely identify the durable order")
    if order.venue == "kalshi":
        return normalize_kalshi_order(order.intent, {"order": matches[0]}, expected_order_id=order.venue_order_id, observed_at=at)
    if order.venue == "novig":
        raw_fills = _collection(record, "fills")
        if raw_fills is None:
            return None
        fills = tuple(normalize_novig_fill(order.intent, row, expected_order_id=order.venue_order_id)
                      for row in raw_fills if row.get("orderId") == order.venue_order_id)
        return normalize_novig_order(order.intent, matches[0], expected_order_id=order.venue_order_id,
                                      observed_at=at, fills=fills, history_complete=True)
    # ProphetX financial proof additionally needs synchronized per-fill rows and
    # matching wallet-ledger fee attribution. A history summary cannot replace it.
    return None


def verify_native_lifecycle(capture: dict, *, expected_build_id: str | None = None,
                            previous_capture: dict | None = None) -> dict:
    if (capture.get("schema_version") != 1 or capture.get("collection_method") != "authenticated_read_only"
            or not isinstance(capture.get("venues"), list)):
        raise ValueError("Expected a read-only native account capture")
    at = datetime.fromisoformat(capture["captured_at"])
    if at.tzinfo is None or not isinstance(capture.get("build_id"), str) or not capture["build_id"]:
        raise ValueError("Capture requires an aware clock and build identity")
    records = {}
    for record in capture["venues"]:
        key = record["venue"], record["environment"], record.get("account_fingerprint")
        if key in records:
            raise ValueError("Duplicate account capture scope")
        records[key] = record
    native_rows = _table(capture, "native_orders")
    fill_rows = _table(capture, "native_fill_evidence")
    settlement_rows = _table(capture, "native_settlements")
    conflicts = _table(capture, "native_fill_conflicts")
    invalidations = _table(capture, "native_settlement_invalidations")
    seen, results = set(), []
    for row in native_rows or ():
        order = NativeOrder.model_validate_json(row["data"])
        if order.id in seen or row.get("id") != order.id:
            raise ValueError("Duplicate or mismatched local order identity")
        seen.add(order.id)
        checks = {}
        record = records.get((order.venue, order.mode, order.account_fingerprint))
        _check(checks, "authenticated_account_scope", bool(record and record.get("authenticated") is True
               and record.get("identity_verified") is True), absent=record is None)
        _check(checks, "build_scope", expected_build_id is not None and capture["build_id"] == expected_build_id,
               absent=expected_build_id is None, detail="Capture build does not prove the build that originally submitted the order")
        _check(checks, "canonical_market_scope", order.market_snapshot.identity() == order.market_fingerprint)
        _check(checks, "acknowledgement_identity", bool(order.venue_order_id and order.submitted_at and order.acknowledged_at
               and order.created_at <= order.submitted_at <= order.acknowledged_at <= at),
               absent=order.submitted_at is None or order.acknowledged_at is None)
        blocked = conflicts is None or invalidations is None or any(x.get("order_id") == order.id for x in (conflicts or []) + (invalidations or []))
        _check(checks, "no_invalidated_financial_evidence", not blocked)
        try:
            fills = [NativeFill.model_validate_json(r["data"]) for r in fill_rows or () if r.get("order_id") == order.id]
            totals = _economics(order, fills) if fills and order.venue_order_id else None
            _check(checks, "local_individual_fill_accounting", totals is not None,
                   absent=not fills or fill_rows is None, detail="Offline replay; not production restart proof")
        except ValueError:
            _check(checks, "local_individual_fill_accounting", False)
        try:
            remote = _remote_report(order, record, at) if record and order.venue_order_id else None
            snapshot = remote.snapshot if remote else None
            matched = bool(remote and remote.financial_complete and snapshot and
                (snapshot.cumulative_quantity, snapshot.cumulative_cost_usd, snapshot.cumulative_gross_payout_usd, snapshot.cumulative_fee_usd)
                == (order.filled_quantity, order.filled_cost_usd, order.gross_payout_usd, order.fees))
            _check(checks, "native_financial_history_matches", matched, absent=remote is None)
        except ValueError:
            _check(checks, "native_financial_history_matches", False)
        settlements = [NativeSettlement.model_validate_json(r["data"]) for r in settlement_rows or () if r.get("order_id") == order.id]
        matching_settlement = len(settlements) == 1 and all(s.order_id == order.id and (s.venue, s.environment, s.account_fingerprint, s.market_fingerprint)
            == (order.venue, order.mode, order.account_fingerprint, order.market_fingerprint) and s.settled_at <= at for s in settlements)
        _check(checks, "local_settlement_scope", matching_settlement, absent=not settlements)
        # These require event-time authenticated artifacts; a current table or
        # one historical balance cannot establish them, even if its numbers fit.
        for name, detail in (
            ("durable_intent_before_transmission", "A current intent row is not a timestamped durable-commit receipt"),
            ("native_settlement_cash_and_fees", "Need exact native settlement/ledger entries and before/after cash reconciliation"),
            ("position_release", "Need account-bound pre/post settlement positions and durable release evidence"),
            ("partial_fill_cancel_race", "Requires genuine observed partial-fill/cancel sequence; empty account is not evidence"),
            ("actual_restart_recovery", "Two read snapshots do not prove an OMS restart with this order"),
            ("production_settlement_idempotency", "Offline duplicate replay is not evidence of a genuine repeated settlement delivery"),
        ):
            _check(checks, name, False, absent=True, detail=detail)
        if previous_capture is not None:
            previous_rows = _table(previous_capture, "native_orders")
            old = next((NativeOrder.model_validate_json(r["data"]) for r in previous_rows or () if r.get("id") == order.id), None)
            compatible = previous_capture.get("build_id") == capture["build_id"] and datetime.fromisoformat(previous_capture["captured_at"]) < at
            _check(checks, "read_snapshot_identity_continuity", bool(old and compatible and
                old.intent == order.intent and old.account_fingerprint == order.account_fingerprint
                and old.market_fingerprint == order.market_fingerprint), absent=old is None,
                detail="Continuity comparison only; not restart certification")
        results.append({"local_order_id": order.id, "client_order_id": order.client_order_id,
            "native_order_id": order.venue_order_id, "venue": order.venue, "environment": order.mode,
            "account_fingerprint": order.account_fingerprint, "market_fingerprint": order.market_fingerprint,
            "checks": checks, "application_trace": review_application_trace(_table(capture, "native_lifecycle_trace"), order),
            "lifecycle_certified": False})
    return {"schema_version": 1, "capture_sha256": artifact_digest(capture), "build_id": capture["build_id"],
        "captured_at": capture["captured_at"], "previous_capture_sha256": artifact_digest(previous_capture) if previous_capture else None,
        "native_order_count": len(results), "local_inventory_complete": native_rows is not None,
        "orders": results, "status": "no_native_oms_orders_to_validate" if not results else "partial_evidence_review_required",
        "certifies_current_oms": False, "launch_evidence_issued": False, "execution_enabled": False,
        "missing_checks": ["durable_intent_before_transmission", "genuine_partial_fill_cancel_race",
            "actual_restart_recovery", "native_settlement_cash_and_fees", "position_release", "production_settlement_idempotency"],
        "note": "This read-only report distinguishes matching financial evidence from missing event-time lifecycle proof. It cannot issue launch approval."}
