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
from decimal import Decimal

from ..core.native_pricing import NativeFeeBound, instrument_fingerprint
from .reconciliation import ReconciliationError, _number, _object, _text, _time


def kalshi_taker_fee_bound(instrument, series_response, changes_response, *,
                          market_series_ticker: str, observed_at: datetime,
                          ttl_seconds: int = 60, direct_member_verified: bool = False):
    """Build a bound after independently verifying market→event→series identity.

    Inputs must be current official GET /series/{ticker} and GET
    /series/fee_changes?series_ticker=...&show_historical=false responses parsed
    with Decimal. Flat schedules, FCM extras and maker orders are unsupported.
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
    if series.get("fee_type") not in {"quadratic", "quadratic_with_maker_fees", "quadratic_with_combo_maker_fees"}:
        raise ReconciliationError("Unsupported Kalshi fee type; no general formula may be inferred")
    if isinstance(series.get("fee_multiplier"), float):
        raise ReconciliationError("Fee multiplier must retain the original decimal JSON value")
    multiplier = _number(series.get("fee_multiplier"), "series fee multiplier")
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
        source=f"Kalshi official series {ticker}; {series['fee_type']}; taker multiplier {multiplier}; "
               f"worst price and .01-contract fill rounding; balance grid {precision}; "
               "GET /series and /series/fee_changes; fee schedule effective 2026-07-07")
