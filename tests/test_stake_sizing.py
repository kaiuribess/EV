from datetime import timedelta
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from evplatform.core.config import RiskConfig
from evplatform.core.models import utcnow
from evplatform.core.native_pricing import instrument_fingerprint, native_opportunity, native_revalidate_intent
from evplatform.core.pricing import NoSignal
from evplatform.core.readiness import LiveLimits
from test_native_pricing import setup_native
from test_native_runtime_readiness import runtime


def sizing_fixture(market, quotes, **changes):
    m, q, book, fair, config, now = setup_native(market, quotes, venue="kalshi", price="0.500", quantity="10000",
        per_unit_usd="0", per_fill_usd="0", fixed_order_usd="0")
    ins = book.instrument.model_copy(update=dict(minimum_quantity=D("1"), minimum_order_cost_usd=D(0),
        order_minimum_verified=True, order_minimum_source="OFFLINE verified minimum fixture") | changes)
    book = book.model_copy(update={"instrument": ins, "fee_bound": book.fee_bound.model_copy(update={
        "instrument_fingerprint": instrument_fingerprint(ins)})})
    fair = fair.model_copy(update={"probabilities": {"home": D(".6"), "away": D(".4")}})
    return m, q, book, fair, config, now


def size(data, cash="100", **kwargs):
    m, q, b, f, c, now = data
    return native_opportunity(m, f, b, c, now=now, max_stake_usd=None, mode="sandbox",
        stake_sizing="quarter_kelly", available_cash_usd=D(cash), **kwargs)


def test_quarter_kelly_uses_cash_and_five_dollar_unit_is_not_cap(market, quotes):
    small = size(sizing_fixture(market, quotes), "100")
    large = size(sizing_fixture(market, quotes), "1000")
    assert small.sizing.full_kelly_fraction == D(".2")
    assert small.intent.reserved_cost_usd == 5
    assert large.intent.reserved_cost_usd == 50
    assert large.sizing.unit_size_usd == 5
    assert large.intent.reserved_cost_usd / large.sizing.unit_size_usd == 10


def test_below_cash_minimum_skips_and_never_rounds_kelly_up(market, quotes):
    with pytest.raises(NoSignal, match="below the verified native minimum"):
        size(sizing_fixture(market, quotes, minimum_order_cost_usd=D("2")), "25")
    accepted = size(sizing_fixture(market, quotes, minimum_order_cost_usd=D("2")), "40")
    assert accepted.intent.reserved_cost_usd == 2


def test_unknown_minimum_cannot_size_live_order(market, quotes):
    for change in ({"order_minimum_verified": False}, {"minimum_order_cost_usd": None}, {"order_minimum_source": None}):
        with pytest.raises(NoSignal, match="minimum stake"):
            size(sizing_fixture(market, quotes, **change))


def test_fees_and_sensitivity_reduce_kelly(market, quotes):
    m,q,b,f,c,now = sizing_fixture(market, quotes)
    f = f.model_copy(update={"sensitivity_probabilities": {"home": D(".55"), "away": D(".45")}})
    result = size((m,q,b,f,c,now))
    assert result.sizing.budget_usd == D("2.5")
    b = b.model_copy(update={"fee_bound": b.fee_bound.model_copy(update={"fixed_order_usd": D(".01")})})
    result = size((m,q,b,f,c,now))
    assert result.sizing.budget_usd < D("2.5")
    assert result.intent.reserved_cost_usd <= result.sizing.budget_usd


def test_negative_edge_and_zero_cash_skip(market, quotes):
    m,q,b,f,c,now = sizing_fixture(market, quotes)
    f = f.model_copy(update={"probabilities": {"home": D(".49"), "away": D(".51")}})
    with pytest.raises(NoSignal, match="below the verified"):
        size((m,q,b,f,c,now))
    with pytest.raises(NoSignal, match="cash"):
        size(sizing_fixture(market, quotes), "0")


def test_native_grid_floors_and_liquidity_never_increases_budget(market, quotes):
    data = sizing_fixture(market, quotes, quantity_step=D("1"))
    result = size(data, "31")
    assert result.sizing.budget_usd == D("1.55")
    assert result.intent.quantity == 3
    assert result.intent.reserved_cost_usd == D("1.5")


def test_partial_remainder_is_not_subject_to_new_order_minimum(market, quotes):
    m,q,b,f,c,now = sizing_fixture(market, quotes, minimum_order_cost_usd=D("2"))
    original = size((m,q,b,f,c,now))
    checked = native_revalidate_intent(original.intent, m, f, b, c, now=now, remaining_quantity=D("1"))
    assert checked.intent == original.intent
    assert checked.expected_profit_usd == D(".1")


def test_uncapped_policy_requires_cash_and_quarter_kelly():
    with pytest.raises(ValueError):
        RiskConfig(max_order_stake=None)
    with pytest.raises(ValueError):
        RiskConfig(max_order_stake=None, available_cash_only=True)
    config = RiskConfig(max_order_stake=None, available_cash_only=True, stake_sizing="quarter_kelly")
    assert config.unit_size_usd == 5 and not config.live_stake_authorized
    limits = LiveLimits(max_order_stake_usd=None, max_event_exposure_usd=None, max_player_exposure_usd=None,
        max_venue_exposure_usd=None, max_correlated_exposure_usd=None, max_total_exposure_usd=None,
        daily_loss_limit_usd=None, available_cash_only=True, stake_sizing="quarter_kelly",
        max_open_orders=20, max_orders_per_minute=30)
    assert limits.unit_size_usd == 5


def ready_cash(runtime, balance="25"):
    snapshot = runtime.account_snapshots["novig"]
    runtime.account_snapshots["novig"] = snapshot.model_copy(update={"balance_usd": D(balance), "cash_available_verified": True})
    return runtime


def test_cash_for_one_venue_does_not_pool_other_accounts(runtime):
    ready_cash(runtime)
    runtime.account_snapshots["kalshi"] = runtime.account_snapshots["novig"].model_copy(update={"venue": "kalshi", "balance_usd": D("10000")})
    assert runtime.sizing_cash("novig") == 25
    with pytest.raises(NoSignal):
        runtime.sizing_cash("kalshi")


@pytest.mark.parametrize("change", [{"observed_at": utcnow()-timedelta(seconds=31)}, {"reconciled": False},
    {"cash_available_verified": False}, {"identity_verified": False}, {"account_fingerprint": "other"}, {"environment": "production"}])
def test_cash_requires_current_exact_account(runtime, change):
    ready_cash(runtime)
    runtime.account_snapshots["novig"] = runtime.account_snapshots["novig"].model_copy(update=change)
    with pytest.raises(NoSignal):
        runtime.sizing_cash("novig")
