"""Independent offline adversarial review of native quarter-Kelly sizing.

These synthetic fixtures certify arithmetic and rejection behavior only.
They are not evidence of a venue's production minimum or rounding rules.
"""
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from evplatform.core.models import OrderStatus
from evplatform.core.conflicts import reviewed_correlations, risk_family
from evplatform.core.native_oms import NativeOrder
from evplatform.core.native_pricing import instrument_fingerprint, native_opportunity, _settlement_bounds
from evplatform.core.pricing import NoSignal
from evplatform.core.pricing import fair_value
from evplatform.core.risk import RiskRejected
from evplatform.core.stake_sizing import quarter_kelly_budget
from test_native_pricing import setup_native
from test_native_runtime_readiness import runtime
from test_stake_sizing import ready_cash, size, sizing_fixture


def test_reporting_unit_does_not_change_order_size(market, quotes):
    fixture = sizing_fixture(market, quotes)
    one = size(fixture, unit_size_usd=D("1"))
    five = size(fixture, unit_size_usd=D("5"))
    assert one.intent.quantity == five.intent.quantity
    assert one.intent.reserved_cost_usd == five.intent.reserved_cost_usd


def test_pending_same_account_reserves_reduce_bankroll(runtime):
    ready_cash(runtime, "100")
    # Unsent local intents are excluded from the remote inventory fence but
    # must still reserve cash in the shared ledger.
    pending = SimpleNamespace(id="pending", venue="novig", mode="sandbox",
        account_fingerprint=runtime.profile.account_fingerprint,
        status=OrderStatus.INTENT, venue_order_id=None, filled_quantity=D(0),
        reserved_cost=D("20"))
    runtime.engine.risk.native_orders[pending.id] = pending
    assert runtime.sizing_cash("novig") == 80
    assert runtime.sizing_cash("novig", exclude_order_id="pending") == 100


def test_an_inflight_account_probe_invalidates_sizing(runtime):
    ready_cash(runtime, "100")
    runtime._account_probes = {"novig"}
    with pytest.raises(NoSignal, match="current reconciled cash"):
        runtime.sizing_cash("novig")


def test_changed_ledger_fence_invalidates_sizing(runtime):
    ready_cash(runtime, "100")
    runtime.engine.risk.settled.add("newly-settled")
    with pytest.raises(NoSignal, match="current reconciled cash"):
        runtime.sizing_cash("novig")


def test_final_dispatch_rechecks_current_cash_after_candidate_sizing(runtime):
    ready_cash(runtime, "100")
    runtime.engine.risk.config = runtime.engine.risk.config.model_copy(update={
        "stake_sizing": "quarter_kelly", "available_cash_only": True})
    key, book = next(iter(runtime.books.items()))
    ins = book.instrument.model_copy(update={"minimum_order_cost_usd": D(0),
        "order_minimum_verified": True, "order_minimum_source": "OFFLINE minimum fixture"})
    book = book.model_copy(update={"instrument": ins, "fee_bound": book.fee_bound.model_copy(update={
        "instrument_fingerprint": instrument_fingerprint(ins), "per_unit_usd": D(0),
        "per_fill_usd": D(0), "fixed_order_usd": D(0)})})
    runtime.books[key] = book
    market = runtime.engine.markets[key[0]]
    fair = fair_value(market, list(runtime.engine.quotes[market.id].values()), runtime.engine.settings.pricing)
    candidate = native_opportunity(market, fair, book, runtime.engine.settings.pricing,
        mode="sandbox", max_stake_usd=D("1"), stake_sizing="quarter_kelly",
        available_cash_usd=runtime.sizing_cash("novig"))
    order = NativeOrder(intent=candidate.intent, candidate_id=candidate.id,
        strategy="native_positive_ev", account_fingerprint=runtime.profile.account_fingerprint,
        market_snapshot=market, market_fingerprint=market.identity(), risk_family_id=risk_family(market),
        reviewed_correlation_groups=reviewed_correlations(market), status=OrderStatus.SUBMITTING)
    runtime.engine.risk.native_orders[order.id] = order
    # Other guard layers have dedicated integration tests. Leave the real
    # final-dispatch -> revalidate_order -> sizing_cash path under test here.
    runtime.require_preflight = lambda current: None
    runtime.engine.risk.check_enabled = lambda *_: None
    runtime.engine.risk.check_native_reservation = lambda *args, **kwargs: None
    runtime.dispatch_guard(order.intent)
    ready_cash(runtime, ".1")
    with pytest.raises(RiskRejected, match="quarter-Kelly budget"):
        runtime.dispatch_guard(order.intent)


def test_manual_preflight_needs_exact_request_permit_and_launch_gate(runtime):
    ready_cash(runtime, "100")
    key, book = next(iter(runtime.books.items()))
    market = runtime.engine.markets[key[0]]
    fair = fair_value(market, list(runtime.engine.quotes[market.id].values()), runtime.engine.settings.pricing)
    found = native_opportunity(market, fair, book, runtime.engine.settings.pricing,
                               mode="sandbox", max_stake_usd=D("1"))
    manual = runtime.build_order(found, execution_origin="manual")
    automatic = runtime.build_order(found)
    base = runtime.report()
    runtime.report = lambda *args: base.model_copy(update={"safe_to_arm": True})
    runtime.engine.settings.live_enabled = True
    runtime.engine.risk.kill_switch = False
    runtime.engine.risk.manual_order_active = lambda order_id: order_id == manual.id
    runtime.require_preflight(manual)
    assert runtime.manager.adapters["novig"].preflight.native_trading_enabled
    with pytest.raises(RiskRejected, match="release gate"):
        runtime.require_preflight(automatic)
    runtime.engine.risk.manual_order_active = lambda order_id: False
    with pytest.raises(RiskRejected, match="release gate"):
        runtime.require_preflight(manual)
    runtime.engine.risk.manual_order_active = lambda order_id: True
    runtime.engine.settings.live_enabled = False
    with pytest.raises(RiskRejected, match="release gate"):
        runtime.require_preflight(manual)


def test_push_void_uncertainty_can_only_reduce_kelly(market, quotes):
    m, q, book, fair, config, now = sizing_fixture(market, quotes)
    baseline = _settlement_bounds(m)
    uncertain = baseline.model_copy(update={"push_probability_upper_bound": D(".05"),
        "void_probability_upper_bound": D(".02")})
    options = dict(available_cash_usd=D("100"), unit_size_usd=D("5"), now=now)
    ordinary = quarter_kelly_budget(book, D(".6"), baseline, **options)
    conservative = quarter_kelly_budget(book, D(".6"), uncertain, **options)
    assert conservative.conservative_probability == D(".558")
    assert 0 < conservative.budget_usd < ordinary.budget_usd


def test_payout_bound_accounts_for_every_permitted_partial_fill(market, quotes):
    m, q, book, fair, config, now = setup_native(market, quotes, venue="prophetx",
        price="-150", quantity="100", per_unit_usd="0", per_fill_usd="0", fixed_order_usd="0")
    ins = book.instrument.model_copy(update={"minimum_order_cost_usd": D("1"),
        "order_minimum_verified": True, "order_minimum_source": "OFFLINE minimum fixture"})
    book = book.model_copy(update={"instrument": ins, "fee_bound": book.fee_bound.model_copy(update={
        "instrument_fingerprint": instrument_fingerprint(ins)})})
    # Every row below satisfies the currently allowed native fill rounding
    # interval. Their aggregate cannot guarantee the one-rounding-per-order
    # payout assumed by the previous sizing formula.
    step_payout = ins.payout_bound(ins.quantity_step, D("-150"))
    worst_gross_per_unit = step_payout / ins.quantity_step
    assert worst_gross_per_unit == 1
    try:
        sizing = quarter_kelly_budget(book, D(".7"), _settlement_bounds(m),
            available_cash_usd=D("100"), unit_size_usd=D("5"), now=now)
    except NoSignal:
        return  # Unknown payout-rounding scope may fail closed instead.
    assert sizing.budget_usd == 0, (
        "A positive budget assumes aggregate payout rounding although allowed "
        "minimum-size partial fills can return only the stake on a win")
