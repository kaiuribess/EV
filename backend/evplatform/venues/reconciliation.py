"""Strict native order evidence, separate from routing and financial accounting.

Sources are the current official Kalshi Get Order/User Fills, Novig Order
Lifecycle/List Fills and ProphetX Direct Link Order Lifecycle contracts. A
placement response proves identity only. Missing fees, a paginated partial
history, or eventually consistent absence never proves zero exposure.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict

from ..core.native_accounting import NativeCumulativeSnapshot, NativeFill, NativeOrderIntent
from .common import UnknownOrderOutcome, decimal_value


class ReconciliationError(ValueError):
    """Evidence is invalid or ambiguous; retain the previous exposure."""


class NativeOrderReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    native_order_id: str
    client_order_id: str
    status: Literal["acknowledged", "open", "partial", "filled", "cancelled", "rejected", "unknown", "cancel_pending"]
    remaining_quantity: Decimal | None = None
    terminal: bool = False
    financial_complete: bool = False
    snapshot: NativeCumulativeSnapshot | None = None
    fills: tuple[NativeFill, ...] = ()
    reason: str = ""


class NativeCancelReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    native_order_id: str
    client_order_id: str
    status: Literal["cancel_pending", "unknown"] = "cancel_pending"
    matching_stopped: bool = False
    retry_cancel_allowed: bool = False
    financial_complete: bool = False
    reason: str = "Fill reconciliation is still required"


def _object(value, name: str) -> dict:
    if not isinstance(value, dict):
        raise ReconciliationError(f"Expected {name} object")
    return value


def _text(value, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReconciliationError(f"Missing {name}")
    return value


def _number(value, name: str, *, positive: bool = False, whole: bool = False) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ReconciliationError(f"Invalid {name}")
    try:
        number = decimal_value(value)
    except (ValueError, TypeError):
        raise ReconciliationError(f"Invalid {name}") from None
    if number < 0 or (positive and number == 0) or (whole and number != number.to_integral_value()):
        raise ReconciliationError(f"Invalid {name}")
    return number


def _time(value, *, milliseconds=False) -> datetime:
    try:
        if isinstance(value, datetime):
            parsed = value
        elif milliseconds:
            parsed = datetime.fromtimestamp(float(_number(value, "timestamp")) / 1000, timezone.utc)
        else:
            parsed = datetime.fromisoformat(_text(value, "timestamp").replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        return parsed
    except (ValueError, TypeError, OverflowError, OSError):
        raise ReconciliationError("An explicit timezone-aware source timestamp is required") from None


def _identity(intent: NativeOrderIntent, data: dict, order_key: str, client_key: str,
              expected_order_id: str | None, *, require_client=True) -> str:
    native_id = _text(data.get(order_key), "native order ID")
    if expected_order_id is not None and native_id != expected_order_id:
        raise ReconciliationError("Native order identity changed")
    if require_client or client_key in data:
        if data.get(client_key) != intent.client_order_id:
            raise ReconciliationError("Native client order ID does not match durable intent")
    return native_id


def normalize_ack(venue: str, payload: dict, intent: NativeOrderIntent) -> NativeOrderReport:
    """Never promote synchronous placement fields to fill evidence."""
    try:
        data = _object(payload, "placement")
        if venue == "kalshi":
            data = _object(data.get("order", data), "order")
            native_id = _identity(intent, data, "order_id", "client_order_id", None, require_client=False)
        elif venue == "novig":
            native_id = _identity(intent, data, "orderId", "clientId", None)
        elif venue == "prophetx":
            data = _object(data.get("data"), "data")
            if data.get("success") is not True:
                raise ReconciliationError("Placement not positively acknowledged")
            data = _object(data.get("order"), "order")
            native_id = _identity(intent, data, "order_id", "external_id", None)
        else:
            raise ReconciliationError("Unsupported native venue")
    except (ValueError, TypeError, KeyError):
        # A malformed success body may still represent an accepted order.
        raise UnknownOrderOutcome(intent.client_order_id) from None
    return NativeOrderReport(native_order_id=native_id, client_order_id=intent.client_order_id,
                             status="acknowledged", reason="Identity acknowledged; fills require independent evidence")


def _snapshot(intent, native_id, status, quantity, cost, payout, fees, observed_at, source, revision=None):
    if quantity > intent.quantity or cost > payout or (quantity == 0 and (cost or payout or fees)):
        raise ReconciliationError("Native cumulative accounting is inconsistent")
    return NativeCumulativeSnapshot(
        order_id=native_id, client_order_id=intent.client_order_id,
        cumulative_quantity=quantity, cumulative_cost_usd=cost,
        cumulative_gross_payout_usd=payout, cumulative_fee_usd=fees,
        observed_at=observed_at, status=status, authoritative=True,
        source=source, revision=revision,
    )


def normalize_kalshi_order(intent: NativeOrderIntent, payload: dict, *, expected_order_id: str,
                           observed_at: datetime) -> NativeOrderReport:
    data = _object(_object(payload, "response").get("order"), "order")
    native_id = _identity(intent, data, "order_id", "client_order_id", expected_order_id)
    if data.get("ticker") != intent.instrument.instrument_id or data.get("action") != "buy":
        raise ReconciliationError("Kalshi contract/action does not match intent")
    if intent.instrument.exchange_index is not None and (
            type(data.get("exchange_index")) is not int
            or data["exchange_index"] != intent.instrument.exchange_index
            or data.get("subaccount_number") != 0):
        raise ReconciliationError("Kalshi order shard or primary subaccount differs from durable intent")
    side = data.get("outcome_side", data.get("side"))
    if side != intent.instrument.native_outcome:
        raise ReconciliationError("Kalshi outcome does not match intent")
    if "side" in data and "outcome_side" in data and data["side"] != data["outcome_side"]:
        raise ReconciliationError("Conflicting Kalshi outcome aliases")
    if _number(data.get(f"{side}_price_dollars"), "native limit price") != intent.limit_price:
        raise ReconciliationError("Kalshi limit price changed outside the durable intent")
    filled = _number(data.get("fill_count_fp"), "filled quantity")
    remaining = _number(data.get("remaining_count_fp"), "remaining quantity")
    initial = _number(data.get("initial_count_fp"), "initial quantity")
    if initial != intent.quantity or filled + remaining > initial:
        raise ReconciliationError("Kalshi order quantities do not match intent")
    raw_status = data.get("status")
    if raw_status == "executed" and filled == initial and remaining == 0:
        status = "filled"
    elif raw_status == "canceled" and remaining == 0:
        status = "cancelled"
    elif raw_status == "resting" and remaining > 0 and filled + remaining == initial:
        status = "partial" if filled else "open"
    else:
        raise ReconciliationError("Unrecognized or inconsistent Kalshi order state")
    cost = _number(data.get("taker_fill_cost_dollars"), "taker cost") + _number(data.get("maker_fill_cost_dollars"), "maker cost")
    fees = _number(data.get("taker_fees_dollars"), "taker fees") + _number(data.get("maker_fees_dollars"), "maker fees")
    source_time = _time(data.get("last_update_time"))
    if source_time > observed_at:
        raise ReconciliationError("Kalshi source timestamp is in the future")
    snapshot = _snapshot(intent, native_id, status, filled, cost, filled, fees, source_time, "kalshi.rest.order")
    return NativeOrderReport(native_order_id=native_id, client_order_id=intent.client_order_id,
                             status=status, remaining_quantity=remaining,
                             terminal=status in {"filled", "cancelled"}, financial_complete=True, snapshot=snapshot)


def normalize_kalshi_fill(intent: NativeOrderIntent, payload: dict, *, expected_order_id: str) -> NativeFill:
    frame = _object(payload, "fill frame")
    if frame.get("type") != "fill":
        raise ReconciliationError("Expected a private Kalshi fill frame")
    data = _object(frame.get("msg"), "fill")
    native_id = _identity(intent, data, "order_id", "client_order_id", expected_order_id)
    if data.get("market_ticker") != intent.instrument.instrument_id or data.get("action") != "buy":
        raise ReconciliationError("Kalshi fill contract/action does not match")
    side = data.get("purchased_side", data.get("outcome_side", data.get("side")))
    if side != intent.instrument.native_outcome:
        raise ReconciliationError("Kalshi fill side does not match")
    yes_price = _number(data.get("yes_price_dollars"), "yes fill price", positive=True)
    price = yes_price if side == "yes" else Decimal(1) - yes_price
    if not 0 < price < 1:
        raise ReconciliationError("Invalid Kalshi fill price")
    qty = _number(data.get("count_fp"), "fill quantity", positive=True)
    return NativeFill(fill_id=_text(data.get("trade_id"), "trade ID"), order_id=native_id,
                      client_order_id=intent.client_order_id, quantity=qty, price=price,
                      cost_usd=qty * price, gross_payout_usd=qty,
                      fee_usd=_number(data.get("fee_cost"), "fill fee"),
                      filled_at=_time(data.get("ts_ms"), milliseconds=True),
                      source="kalshi.private.fill")


def normalize_novig_fill(intent: NativeOrderIntent, data: dict, *, expected_order_id: str) -> NativeFill:
    data = _object(data, "fill")
    native_id = _identity(intent, data, "orderId", "clientId", expected_order_id)
    if (data.get("outcomeId") != intent.instrument.native_outcome or
            data.get("marketId") != intent.instrument.instrument_id):
        raise ReconciliationError("Novig fill market/outcome does not match intent")
    qty = _number(data.get("qty"), "fill quantity", positive=True, whole=True)
    cost = _number(data.get("cost"), "fill cost", positive=True)
    payout = qty * Decimal(".01")
    price = cost / payout
    if not 0 < price < 1:
        raise ReconciliationError("Invalid Novig fill cost")
    return NativeFill(fill_id=_text(data.get("fillId"), "fill ID"), order_id=native_id,
                      client_order_id=intent.client_order_id, quantity=qty, price=price,
                      cost_usd=cost, gross_payout_usd=payout,
                      # The REST schema expressly omits fee on an uncharged
                      # fill. This rule does not apply to private WS fills.
                      fee_usd=_number(data.get("fee", "0"), "fill fee"),
                      filled_at=_time(data.get("ts"), milliseconds=True),
                      source="novig.rest.fill")


def normalize_novig_order(intent: NativeOrderIntent, payload: dict, *, expected_order_id: str,
                          observed_at: datetime, fills: tuple[NativeFill, ...] = (),
                          history_complete: bool = False,
                          confirmed_filled_quantity: Decimal | None = None) -> NativeOrderReport:
    data = _object(payload, "order")
    native_id = _identity(intent, data, "orderId", "clientId", expected_order_id)
    if data.get("outcomeId") != intent.instrument.native_outcome:
        raise ReconciliationError("Novig outcome does not match intent")
    if data.get("marketId") != intent.instrument.instrument_id:
        raise ReconciliationError("Novig market does not match intent")
    if _number(data.get("price"), "native limit price") != intent.limit_price:
        raise ReconciliationError("Novig limit price changed outside the durable intent")
    original = _number(data.get("qty"), "original quantity", positive=True, whole=True)
    remaining = _number(data.get("remaining"), "remaining quantity", whole=True)
    if original != intent.quantity or remaining > original:
        raise ReconciliationError("Novig quantities do not match intent")
    status = {"PENDING": "acknowledged", "OPEN": "open", "FILLED": "filled",
              "CANCELED": "cancelled", "REJECTED": "rejected"}.get(data.get("status"))
    if status is None:
        raise ReconciliationError("Unknown Novig order state")
    # On cancellation, qty - remaining need not describe fills: only the fill
    # ledger establishes the executed quantity and actual fees.
    fills = _unique_fills(intent, native_id, fills)
    quantity, cost, payout, fees = _totals(fills)
    if quantity > original or (status in {"open", "filled"} and quantity + remaining != original):
        history_complete = False
    if status == "rejected" and quantity:
        raise ReconciliationError("Rejected Novig order unexpectedly contains fills")
    if status == "filled" and remaining:
        raise ReconciliationError("Filled Novig order still has remaining quantity")
    if status == "open" and quantity:
        status = "partial"
    terminal = status in {"filled", "cancelled", "rejected"}
    snapshot = None
    if status == "cancelled" and (confirmed_filled_quantity is None or quantity != confirmed_filled_quantity):
        # A canceled order's `remaining` is no longer resting liquidity. Do not
        # declare a lagging empty fill page complete without a private-stream
        # cumulative watermark retained from the contiguous order channel.
        history_complete = False
    if history_complete and status != "acknowledged":
        snapshot = _snapshot(intent, native_id, status, quantity, cost, payout, fees,
                             observed_at, "novig.rest.order_and_fills")
    return NativeOrderReport(native_order_id=native_id, client_order_id=intent.client_order_id,
                             status=status, remaining_quantity=remaining, terminal=terminal,
                             financial_complete=snapshot is not None, snapshot=snapshot, fills=fills,
                             reason="" if snapshot else "A complete, fee-bearing native fill history is required")


def _unique_fills(intent, native_id, fills):
    by_id = {}
    for fill in fills:
        if fill.order_id != native_id or fill.client_order_id != intent.client_order_id:
            raise ReconciliationError("Fill history contains a different order")
        # Re-reading the same match changes local observation time only.
        if (fill.fill_id in by_id and
                by_id[fill.fill_id].model_dump(exclude={"observed_at"}) != fill.model_dump(exclude={"observed_at"})):
            raise ReconciliationError("Conflicting duplicate fill evidence")
        by_id[fill.fill_id] = fill
    return tuple(by_id.values())


def _totals(fills):
    return tuple(sum((getattr(f, field) for f in fills), Decimal(0)) for field in
                 ("quantity", "cost_usd", "gross_payout_usd", "fee_usd"))


def normalize_prophetx_order(intent: NativeOrderIntent, payload: dict, *, expected_order_id: str,
                            observed_at: datetime, fills: tuple[NativeFill, ...] = (),
                            history_complete: bool = False, websocket: bool = False) -> NativeOrderReport:
    data = _object(_object(payload, "response").get("info" if websocket else "data"), "order")
    native_id = _identity(intent, data, "id" if websocket else "order_id", "external_id", expected_order_id)
    if data.get("strike_id") != intent.instrument.instrument_id:
        raise ReconciliationError("ProphetX strike does not match intent")
    if decimal_value(data.get("price")) != intent.limit_price:
        raise ReconciliationError("ProphetX limit price changed outside the durable intent")
    filled = _number(data.get("total_filled_quantity" if websocket else "filled_quantity"), "cumulative stake")
    remaining = _number(data.get("open_quantity"), "open stake")
    if filled + remaining > intent.quantity:
        raise ReconciliationError("ProphetX quantities exceed original stake")
    if "original_quantity" in data and _number(data["original_quantity"], "original stake") != intent.quantity:
        raise ReconciliationError("ProphetX original stake changed")
    raw_status, matching = data.get("status"), data.get("matching_status")
    if raw_status == "open" and _number(data.get("quantity"), "current stake") != filled + remaining:
        raise ReconciliationError("ProphetX current quantity differs from executed/open stake")
    if raw_status == "inactive":
        status = "acknowledged"
    elif raw_status == "canceled" and filled == 0 and remaining == 0:
        status = "cancelled"
    elif raw_status == "open" and remaining == 0 and matching == "fully_matched":
        status = "filled" if filled == intent.quantity else "cancelled"
    elif raw_status == "open" and remaining > 0 and matching in {"unmatched", "partially_matched"}:
        status = "partial" if filled else "open"
    else:
        # Settlement fields require a dedicated settlement event, not a guessed
        # translation into an execution state or released liability.
        raise ReconciliationError("ProphetX settlement/unknown order state requires review")
    fills = _unique_fills(intent, native_id, fills)
    quantity, cost, payout, fees = _totals(fills)
    terminal = status in {"filled", "cancelled"}
    complete = history_complete and quantity == filled and status != "acknowledged"
    source_time = observed_at
    if not websocket:
        source_time = _time(data.get("updated_at"))
        if source_time > observed_at:
            raise ReconciliationError("ProphetX source timestamp is in the future")
    snapshot = _snapshot(intent, native_id, status, quantity, cost, payout, fees,
                         source_time, "prophetx.private.order" if websocket else "prophetx.rest.order_and_fills") if complete else None
    return NativeOrderReport(native_order_id=native_id, client_order_id=intent.client_order_id,
                             status=status, remaining_quantity=remaining, terminal=terminal,
                             financial_complete=complete, snapshot=snapshot, fills=fills,
                             reason="" if complete else "Matched-price and actual-fee evidence must reconcile with cumulative stake")


def normalize_prophetx_cancel(native_id: str, intent: NativeOrderIntent, *, status_code=200,
                             error_code: str | None = None) -> NativeCancelReport:
    stopped = status_code == 200 or (status_code == 400 and error_code in {"order_already_filled", "order_already_cancelled"})
    retry = status_code == 409 and error_code in {"order_is_placing", "order_is_matching"}
    return NativeCancelReport(native_order_id=native_id, client_order_id=intent.client_order_id,
                              status="cancel_pending" if stopped or retry else "unknown",
                              matching_stopped=stopped, retry_cancel_allowed=retry,
                              reason="Matching stopped; executed stake and fees still require reconciliation" if stopped
                              else "Transient matching race; only cancellation may be retried" if retry
                              else "Cancellation outcome unknown; retain all exposure")


def normalize_prophetx_fill(intent: NativeOrderIntent, payload: dict, *, expected_order_id: str,
                           published_at: datetime) -> NativeFill:
    """Only private matching events with actual fees can become fill evidence.

    `profit` belongs to the whole order and is not a per-fill payout. The
    published time is recorded as publication, never as execution latency.
    Repeating American payouts require an explicit cash settlement quantum;
    the conservative lower bound is retained until account reconciliation.
    """
    data = _object(_object(payload, "private event").get("info"), "order")
    native_id = _identity(intent, data, "id", "external_id", expected_order_id)
    if data.get("strike_id") != intent.instrument.instrument_id or data.get("update_type") != "matching":
        raise ReconciliationError("Expected an owned ProphetX matching event")
    quantity = _number(data.get("filled_quantity"), "fill stake", positive=True)
    cumulative = _number(data.get("total_filled_quantity"), "cumulative stake", positive=True)
    if cumulative < quantity or cumulative > intent.quantity:
        raise ReconciliationError("Invalid ProphetX cumulative fill watermark")
    try:
        price = decimal_value(data.get("fill_price"))
        intent.instrument.validate_price(price)
    except (ValueError, TypeError):
        raise ReconciliationError("Invalid ProphetX fill price") from None
    matching_id = data.get("matching_id")
    if isinstance(matching_id, bool) or not isinstance(matching_id, int) or matching_id <= 0:
        raise ReconciliationError("Stable ProphetX matching ID required")
    # Do not invent a cash payout by rounding a repeating rational. The native
    # fee/settlement contract must agree with the account's payout precision.
    lower = intent.instrument.payout_bound(quantity, price)
    upper = intent.instrument.payout_bound(quantity, price, upper=True)
    if lower != upper:
        raise ReconciliationError("ProphetX fractional cash payout needs matched-detail profit evidence")
    return NativeFill(fill_id=str(matching_id), order_id=native_id, client_order_id=intent.client_order_id,
                      quantity=quantity, price=price, cost_usd=quantity, gross_payout_usd=lower,
                      fee_usd=_number(data.get("fee"), "fill fee"), filled_at=None,
                      observed_at=_time(published_at), timestamp_kind="publication",
                      source="prophetx.private.orders", cumulative_quantity_after=cumulative)


def match_client_order(venue: str, intent: NativeOrderIntent, rows: list[dict]) -> str | None:
    """Resolve identity only; absence never licenses a placement retry."""
    keys = {"kalshi": ("client_order_id", "order_id"), "novig": ("clientId", "orderId"),
            "prophetx": ("external_id", "order_id")}
    if venue not in keys:
        raise ReconciliationError("Unsupported native venue")
    client_key, native_key = keys[venue]
    candidates = [row for row in rows if _object(row, "history row").get(client_key) == intent.client_order_id]
    if venue == "kalshi" and intent.instrument.exchange_index is not None and any(
            row.get("ticker") != intent.instrument.instrument_id
            or row.get("exchange_index") != intent.instrument.exchange_index
            or row.get("subaccount_number") != 0 for row in candidates):
        raise ReconciliationError("Kalshi recovered order escaped durable shard/account/market identity")
    matches = {_text(row.get(native_key), "native order ID") for row in candidates}
    if len(matches) > 1:
        raise ReconciliationError("Multiple native orders share this client ID; manual review required")
    return next(iter(matches), None)
