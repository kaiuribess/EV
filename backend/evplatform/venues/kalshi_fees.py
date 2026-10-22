"""Conservative IOC taker fee bounds from current official series metadata.

Sources checked 2026-10-21:
https://docs.kalshi.com/api-reference/market/get-series
https://docs.kalshi.com/api-reference/exchange/get-series-fee-changes
https://docs.kalshi.com/getting_started/fee_rounding
https://kalshi.com/docs/kalshi-fee-schedule.pdf (effective July 7, 2026)

This is a maximum reserve, not an expected fee estimate. It includes worst-case
quadratic price, six-decimal trade-fee rounding and balance-grid rounding for
EVERY possible .01-contract fill, without relying on future accumulator rebates.
"""
from datetime import datetime, timedelta
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from ..core.native_pricing import NativeFeeBound, instrument_fingerprint
from .reconciliation import ReconciliationError, _number, _object, _text, _time


@dataclass(frozen=True)
class KalshiFillFee:
    """One documented taker fill; accumulator is carried to the next fill."""
    model_fee_usd: Decimal
    trade_fee_usd: Decimal
    rounding_fee_usd: Decimal
    rebate_usd: Decimal
    net_fee_usd: Decimal
    debit_usd: Decimal
    rounding_accumulator_usd: Decimal


def kalshi_taker_fill_fee(price: Decimal, quantity: Decimal, multiplier: Decimal, *,
                          balance_grid_usd: Decimal, rounding_accumulator_usd: Decimal = Decimal(0)) -> KalshiFillFee:
    """Apply Kalshi's six-decimal trade fee and per-order balance-grid rebate.

    This is an exact model only when series/event multiplier and the account's
    direct-member status have been independently verified. It does not replace
    the conservative pre-order debit reserve below.
    """
    values = (price, quantity, multiplier, rounding_accumulator_usd)
    if (any(type(value) is not Decimal or not value.is_finite() for value in values)
            or not Decimal(0) < price < Decimal(1) or quantity <= 0 or multiplier < 0
            or rounding_accumulator_usd < 0
            or balance_grid_usd not in (Decimal(".0001"), Decimal(".01"))):
        raise ValueError("Exact Kalshi fill inputs and known account balance grid are required")
    model = multiplier * Decimal(".07") * quantity * price * (1-price)
    trade = model.quantize(Decimal(".000001"), rounding=ROUND_CEILING)
    revenue = -price * quantity
    aligned = ((revenue-trade)/balance_grid_usd).to_integral_value(rounding=ROUND_FLOOR)*balance_grid_usd
    rounding = revenue-trade-aligned
    accumulated = rounding_accumulator_usd + rounding
    rebate = min((accumulated/balance_grid_usd).to_integral_value(rounding=ROUND_FLOOR),
                 ((trade+rounding)/balance_grid_usd).to_integral_value(rounding=ROUND_FLOOR))*balance_grid_usd
    net = trade+rounding-rebate
    return KalshiFillFee(model, trade, rounding, rebate, net, -revenue+net, accumulated-rebate)


def kalshi_taker_fee_bound(instrument, series_response, changes_response, *,
                          market_response, event_response, event_changes_response,
                          market_series_ticker: str, market_event_ticker: str, observed_at: datetime,
                          ttl_seconds: int = 60, direct_member_verified: bool = False):
    """Build a bound after independently verifying market→event→series identity.

    Inputs must include current official market→event→series listings and
    both series/event fee-change feeds. Flat schedules, FCM extras and maker
    orders are unsupported.
    The caller must refresh before expiry and never reuse across instruments.
    """
    if (instrument.venue != "kalshi" or instrument.quantity_unit != "contracts"
            or instrument.payout_per_unit_usd != 1 or not instrument.verified
            or instrument.quantity_step < Decimal(".01") or instrument.quantity_step % Decimal(".01")
            or type(ttl_seconds) is not int or not 0 < ttl_seconds <= 300
            or type(direct_member_verified) is not bool
            or observed_at.tzinfo is None or observed_at.utcoffset() is None):
        raise ReconciliationError("Verified Kalshi instrument and bounded fee observation required")
    series = _object(_object(series_response, "series response").get("series"), "series")
    ticker = _text(series.get("ticker"), "series ticker")
    if ticker != market_series_ticker:
        raise ReconciliationError("Kalshi fee series does not match verified market lineage")
    market = _object(_object(market_response, "market response").get("market"), "market")
    event = _object(_object(event_response, "event response").get("event"), "event")
    if (market.get("ticker") != instrument.instrument_id
            or market.get("event_ticker") != market_event_ticker
            or event.get("event_ticker") != market_event_ticker
            or event.get("series_ticker") != ticker
            or type(market.get("exchange_index")) is not int
            or market["exchange_index"] < 0
            or event.get("exchange_index") != market["exchange_index"]):
        raise ReconciliationError("Kalshi fee market/event/series or exchange index lineage differs")
    override_type, override_multiplier = event.get("fee_type_override"), event.get("fee_multiplier_override")
    if (override_type is None) != (override_multiplier is None):
        raise ReconciliationError("Partial Kalshi event fee override")
    fee_type = override_type if override_type is not None else series.get("fee_type")
    if fee_type not in {"quadratic", "quadratic_with_maker_fees", "quadratic_with_combo_maker_fees"}:
        raise ReconciliationError("Unsupported Kalshi fee type; no general formula may be inferred")
    raw_multiplier = override_multiplier if override_multiplier is not None else series.get("fee_multiplier")
    if isinstance(raw_multiplier, float):
        raise ReconciliationError("Fee multiplier must retain the original decimal JSON value")
    multiplier = _number(raw_multiplier, "effective fee multiplier")
    changes = _object(changes_response, "fee changes").get("series_fee_change_arr")
    if not isinstance(changes, list):
        raise ReconciliationError("Current scheduled Kalshi fee changes are required")
    valid_until = observed_at + timedelta(seconds=ttl_seconds)
    seen = set()
    for raw in changes:
        change = _object(raw, "fee change")
        identity = _text(change.get("id"), "fee change ID")
        scheduled = _time(change.get("scheduled_ts"))
        if (change.get("series_ticker") != ticker or identity in seen or scheduled <= observed_at):
            raise ReconciliationError("Ambiguous, duplicate or already-effective fee change")
        seen.add(identity)
        # Any upcoming change, even an unknown future formula, ends this bound.
        valid_until = min(valid_until, scheduled)
    event_changes = _object(event_changes_response, "event fee changes")
    if event_changes.get("cursor") != "" or not isinstance(event_changes.get("event_fee_changes"), list):
        raise ReconciliationError("Complete scoped Kalshi event fee changes are required")
    for raw in event_changes["event_fee_changes"]:
        change = _object(raw, "event fee change")
        identity = _text(change.get("id"), "event fee change ID")
        scheduled = _time(change.get("scheduled_ts"))
        if (change.get("event_ticker") != market_event_ticker or change.get("series_ticker") != ticker
                or identity in seen or scheduled <= observed_at):
            raise ReconciliationError("Ambiguous or already-effective event fee change")
        seen.add(identity)
        valid_until = min(valid_until, scheduled)
    # Without confirmed direct membership choose the less precise (more costly)
    # documented balance grid. This does not admit FCM-specific added fees.
    precision = Decimal(".0001") if direct_member_verified else Decimal(".01")
    # Pricing's maximum_fill_count is quantity / instrument.quantity_step. The
    # exchange may split an order finer than its minimum order grid, so scale
    # that count to the native minimum .01 fill granularity explicitly.
    split_factor = instrument.quantity_step / Decimal(".01")
    return NativeFeeBound(instrument_fingerprint=instrument_fingerprint(instrument),
        per_unit_usd=Decimal(".07") * multiplier * Decimal(".25"),
        per_fill_usd=(precision + Decimal(".000001")) * split_factor,
        fixed_order_usd=Decimal(0), verified=True, observed_at=observed_at, valid_until=valid_until,
        source=f"Kalshi official event {market_event_ticker} / series {ticker}; {fee_type}; taker multiplier {multiplier}; "
               f"worst price and .01-contract fill rounding; balance grid {precision}; "
               "account precision conservative unless direct membership verified; "
               "GET /markets, /events, /series and both fee-change feeds; fee schedule effective 2026-07-07")
