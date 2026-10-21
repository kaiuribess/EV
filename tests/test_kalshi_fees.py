"""Offline conservative fee arithmetic, not a provider fee certification."""
from datetime import timedelta
from decimal import Decimal as D, ROUND_CEILING

import pytest

from evplatform.core.models import utcnow
from evplatform.venues.kalshi_fees import kalshi_taker_fee_bound
from evplatform.venues.reconciliation import ReconciliationError
from test_native_book_normalization import mapping


def bound(market, *, multiplier=D(1), fee_type="quadratic", changes=None, direct=False, step=None):
    instrument = mapping(market).instrument
    if step is not None:
        instrument = instrument.model_copy(update={"quantity_step": D(step)})
    now = utcnow()
    fee = kalshi_taker_fee_bound(instrument, {"series": {"ticker": "SERIES", "fee_type": fee_type,
        "fee_multiplier": multiplier}}, {"series_fee_change_arr": [] if changes is None else changes},
        market_series_ticker="SERIES", observed_at=now, direct_member_verified=direct)
    return instrument, fee, now


@pytest.mark.parametrize("fee_type", ["quadratic", "quadratic_with_maker_fees", "quadratic_with_combo_maker_fees"])
def test_taker_multiplier_and_default_conservative_balance_grid(market, fee_type):
    instrument, fee, now = bound(market, fee_type=fee_type, multiplier=D("1.5"))
    assert fee.per_unit_usd == D(".02625")
    assert fee.per_fill_usd == D(".010001")
    assert fee.maximum_fee(instrument, D("2.50"), now) == D(".065625") + 250*D(".010001")
    assert fee.valid_until == now+timedelta(seconds=60)


@pytest.mark.parametrize("direct", [False, True])
def test_worst_partial_fill_fee_bound_covers_every_price_and_avoids_float_rounding(market, direct):
    instrument, fee, now = bound(market, direct=direct)
    grid = D(".0001") if direct else D(".01")
    for price in (D(".001"), D(".111"), D(".5"), D(".999")):
        for quantity in (D(".01"), D(".23"), D("2.5")):
            # Maximum fragmentation, no assumed rebate. Amount debit after fee
            # is rounded UP to the balance grid as documented for buys.
            model = D(".07")*D(".01")*price*(1-price)
            trade = model.quantize(D(".000001"), rounding=ROUND_CEILING)
            debit = ((D(".01")*price+trade)/grid).to_integral_value(rounding=ROUND_CEILING)*grid
            actual_max = (debit-D(".01")*price)*(quantity/D(".01"))
            assert actual_max <= fee.maximum_fee(instrument, quantity, now)


def test_large_order_grid_does_not_hide_finer_native_partial_fills(market):
    instrument, fee, now = bound(market, direct=True, step="1")
    assert fee.per_fill_usd == D(".0101")
    assert fee.maximum_fee(instrument, D(1), now) == D(".0276")


def test_scheduled_change_shortens_validity_even_when_future_formula_unknown(market):
    at = utcnow()+timedelta(seconds=10)
    _, fee, _ = bound(market, changes=[{"id": "change", "series_ticker": "SERIES",
                                      "scheduled_ts": at.isoformat(), "fee_type": "future_unknown"}])
    assert fee.valid_until == at


@pytest.mark.parametrize("fee_type", ["flat", "unknown", None])
def test_unreviewed_fee_formulas_rejected(market, fee_type):
    with pytest.raises(ReconciliationError, match="fee type"):
        bound(market, fee_type=fee_type)


@pytest.mark.parametrize("multiplier", [True, -1, None, "nan", .7])
def test_missing_nonfinite_negative_or_float_metadata_rejected(market, multiplier):
    with pytest.raises((ReconciliationError, ValueError)):
        bound(market, multiplier=multiplier)


def test_series_binding_and_current_change_evidence_required(market):
    instrument = mapping(market).instrument
    args = dict(market_series_ticker="OTHER", observed_at=utcnow())
    with pytest.raises(ReconciliationError, match="lineage"):
        kalshi_taker_fee_bound(instrument, {"series": {"ticker": "SERIES", "fee_type": "quadratic", "fee_multiplier": D(1)}},
                              {"series_fee_change_arr": []}, **args)
    with pytest.raises(ReconciliationError, match="already-effective"):
        bound(market, changes=[{"id": "old", "series_ticker": "SERIES", "scheduled_ts": utcnow().isoformat()}])
