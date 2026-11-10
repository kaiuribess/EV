"""Offline conservative fee arithmetic, not a provider fee certification."""
from datetime import timedelta
from decimal import Decimal as D, ROUND_CEILING

import pytest

from evplatform.core.models import utcnow
from evplatform.venues.kalshi_fees import kalshi_taker_fee_bound, kalshi_taker_fill_fee
from evplatform.venues.reconciliation import ReconciliationError
from test_native_book_normalization import mapping


def bound(market, *, multiplier=D(1), fee_type="quadratic", changes=None, direct=False, step=None,
          event_override=None, event_changes=None):
    instrument = mapping(market).instrument
    if step is not None:
        instrument = instrument.model_copy(update={"quantity_step": D(step)})
    now = utcnow()
    fee = kalshi_taker_fee_bound(instrument, {"series": {"ticker": "SERIES", "fee_type": fee_type,
        "fee_multiplier": multiplier}}, {"series_fee_change_arr": [] if changes is None else changes},
        market_response={"market": {"ticker": instrument.instrument_id, "event_ticker": "EVENT", "exchange_index": 3}},
        event_response={"event": {"event_ticker": "EVENT", "series_ticker": "SERIES", "exchange_index": 3,
            "fee_type_override": event_override[0] if event_override else None,
            "fee_multiplier_override": event_override[1] if event_override else None}},
        event_changes_response={"event_fee_changes": [] if event_changes is None else event_changes, "cursor": ""},
        market_series_ticker="SERIES", market_event_ticker="EVENT", observed_at=now,
        direct_member_verified=direct)
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
                              {"series_fee_change_arr": []},
                              market_response={"market": {"ticker": instrument.instrument_id,
                                  "event_ticker": "EVENT", "exchange_index": 3}},
                              event_response={"event": {"event_ticker": "EVENT", "series_ticker": "SERIES",
                                  "exchange_index": 3}}, event_changes_response={"event_fee_changes": [], "cursor": ""},
                              market_event_ticker="EVENT", **args)
    with pytest.raises(ReconciliationError, match="already-effective"):
        bound(market, changes=[{"id": "old", "series_ticker": "SERIES", "scheduled_ts": utcnow().isoformat()}])


@pytest.mark.parametrize("price,model,direct_debit,other_debit", [
    (".27", ".013797", ".2838", ".29"),
    (".74", ".013468", ".7535", ".76"),
])
def test_documented_one_contract_fee_and_account_precision(price, model, direct_debit, other_debit):
    direct = kalshi_taker_fill_fee(D(price), D(1), D(1), balance_grid_usd=D(".0001"))
    other = kalshi_taker_fill_fee(D(price), D(1), D(1), balance_grid_usd=D(".01"))
    assert direct.model_fee_usd == other.model_fee_usd == D(model)
    assert direct.debit_usd == D(direct_debit)
    assert other.debit_usd == D(other_debit)


def test_split_fills_carry_rounding_accumulator_instead_of_new_whole_cent_each_time():
    first = kalshi_taker_fill_fee(D(".27"), D(".5"), D(1), balance_grid_usd=D(".01"))
    second = kalshi_taker_fill_fee(D(".27"), D(".5"), D(1), balance_grid_usd=D(".01"),
                                   rounding_accumulator_usd=first.rounding_accumulator_usd)
    assert first.rebate_usd == 0 and second.rebate_usd == D(".01")
    assert first.debit_usd + second.debit_usd == D(".29")
    assert first.net_fee_usd >= 0 and second.net_fee_usd >= 0


def test_conservative_debit_reserve_is_not_replaced_by_model_fee(market):
    instrument, fee, now = bound(market)
    model = kalshi_taker_fill_fee(D(".27"), D(1), D(1), balance_grid_usd=D(".01"))
    assert fee.maximum_fee(instrument, D(1), now) >= model.net_fee_usd
    assert D(".27") + fee.maximum_fee(instrument, D(1), now) >= model.debit_usd


def test_one_dollar_debit_cap_cannot_be_authorized_by_a_cheaper_one_fill_example(market):
    instrument, fee, now = bound(market)
    one_fill = kalshi_taker_fill_fee(D(".27"), D(1), D(1), balance_grid_usd=D(".01"))
    cap = D(1)
    assert one_fill.debit_usd == D(".29") < cap
    assert instrument.cost_for(D(1), D(".27")) + fee.maximum_fee(instrument, D(1), now) > cap


def test_event_fee_override_and_scheduled_change_are_bound_to_exact_market(market):
    change_at = utcnow()+timedelta(seconds=10)
    _, fee, _ = bound(market, event_override=("quadratic", D("1.5")), event_changes=[
        {"id": "event-change", "event_ticker": "EVENT", "series_ticker": "SERIES",
         "scheduled_ts": change_at.isoformat()}])
    assert fee.per_unit_usd == D(".02625")
    assert fee.valid_until == change_at
    with pytest.raises(ReconciliationError, match="event fee change"):
        bound(market, event_changes=[{"id": "foreign", "event_ticker": "OTHER",
            "series_ticker": "SERIES", "scheduled_ts": change_at.isoformat()}])
