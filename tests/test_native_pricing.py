from datetime import timedelta
from decimal import Decimal as D
from types import SimpleNamespace
from unittest.mock import AsyncMock
import asyncio

import pytest
from pydantic import ValidationError

from evplatform.core.config import PricingConfig
from evplatform.core.manual_native import ManualNativeService
from evplatform.core.models import utcnow
from evplatform.core.native_accounting import NativeInstrument, NativePriceBand
from evplatform.core.native_pricing import (
    NativeBook, NativeFeeBound, NativeLevel, instrument_fingerprint, native_opportunity,
    native_revalidate_intent, settlement_fingerprint,
)
from evplatform.core.pricing import NoSignal, fair_value
from evplatform.core.risk import RiskEngine, RiskRejected
from evplatform.core.config import RiskConfig
from evplatform.plugins.native_positive_ev import NativePositiveEV
from evplatform.venues.common import PreflightRequired


async def test_manual_prophetx_order_terms_use_fresh_ladder_without_dispatch():
    refresh = AsyncMock(return_value={-164, 160})
    encoded = []
    adapter = SimpleNamespace(client=SimpleNamespace(refresh_verified_price_ladder=refresh),
                              build_native_request=lambda intent: encoded.append(intent))
    service = ManualNativeService(SimpleNamespace(native=SimpleNamespace(
        manager=SimpleNamespace(adapters={"prophetx": adapter}))))
    intent = object()
    await service._verify_prophetx_request("prophetx", SimpleNamespace(intent=intent))
    refresh.assert_awaited_once()
    assert encoded == [intent]


async def test_manual_prophetx_unverified_request_blocks_review():
    refresh = AsyncMock(return_value={-164, 160})
    adapter = SimpleNamespace(client=SimpleNamespace(refresh_verified_price_ladder=refresh),
                              build_native_request=lambda _: (_ for _ in ()).throw(
                                  PreflightRequired("Limit is absent from current ladder")))
    service = ManualNativeService(SimpleNamespace(native=SimpleNamespace(
        manager=SimpleNamespace(adapters={"prophetx": adapter}))))
    with pytest.raises(RiskRejected, match="order terms cannot be verified"):
        await service._verify_prophetx_request("prophetx", SimpleNamespace(intent=object()))
    refresh.assert_awaited_once()


def setup_native(market, quotes, *, venue="novig", price="0.450", quantity="100", **fees):
    now = utcnow()
    market = market.model_copy(update={"settlement": market.settlement.model_copy(update={"void_rule": "not_applicable"})})
    quotes = [q.model_copy(update={"synthetic": False, "source_time": now - timedelta(milliseconds=100)}) for q in quotes]
    fields = dict(venue=venue, instrument_id="native-market", canonical_market_id=market.id,
                  outcome="home", native_outcome="native-home", quantity_unit="whole_cent_contracts",
                  price_format="probability", payout_per_unit_usd="0.01", quantity_step="1",
                  minimum_quantity="1", cash_quantum_usd="0.00001", verified=True, source="reviewed fixture",
                  settlement_rules_fingerprint=settlement_fingerprint(market),
                  price_bands=(NativePriceBand(minimum="0.005", maximum="0.995", step="0.005"),))
    if venue == "kalshi":
        fields.update(quantity_unit="contracts", quantity_step="0.01", minimum_quantity="0.01",
                      payout_per_unit_usd="1", cash_quantum_usd="0.000001")
    elif venue == "prophetx":
        fields.update(quantity_unit="usd_stake", quantity_step="0.01", minimum_quantity="1",
                      payout_per_unit_usd=None, price_format="american", cash_quantum_usd="0.01", price_bands=())
    instrument = NativeInstrument(**fields)
    fee_bound = NativeFeeBound(**(dict(instrument_fingerprint=instrument_fingerprint(instrument),
        per_unit_usd="0.00001", per_fill_usd="0.00001", fixed_order_usd="0.001", verified=True,
        source="explicit verified fixture fee cap", observed_at=now - timedelta(minutes=1),
        valid_until=now + timedelta(hours=1)) | fees))
    book = NativeBook(instrument=instrument, market_fingerprint=market.identity(),
        asks=(NativeLevel(price=price, quantity=quantity),), fee_bound=fee_bound,
        source_time=now - timedelta(milliseconds=50), received_at=now, revision=1,
        synchronized=True)
    config = PricingConfig()
    fair = fair_value(market, quotes, config, now)
    return market, quotes, book, fair, config, now


def candidate(data, *, max_stake_usd=D("10"), **changes):
    market, quotes, book, fair, config, now = data
    return native_opportunity(market, fair, book, config, max_stake_usd=max_stake_usd,
                              mode="sandbox", now=now, **changes)


async def test_manual_review_is_one_use_and_does_not_enable_automation(market, quotes):
    data = setup_native(market, quotes)
    first, second = candidate(data), candidate(data)
    risk = RiskEngine(RiskConfig())
    reviewed = []
    def check(order):
        assert not risk.manual_order_active(order.id)  # Preview has no dispatch authority.
        reviewed.append(order.id)
    async def place(found, *, execution_origin):
        assert execution_origin == "manual" and risk.manual_order_active(found.intent.id)
        return SimpleNamespace(model_dump=lambda **_: {"id": found.intent.id, "status": "open"})
    native = SimpleNamespace(preview_check=check,
        books={(first.market_id, first.venue, first.outcome): data[2]},
        build_order=lambda found, **_: SimpleNamespace(id=found.intent.id, account_fingerprint="a" * 64),
        execute=place)
    engine = SimpleNamespace(native=native, risk=risk, oms=SimpleNamespace(lock=asyncio.Lock()),
                             markets={data[0].id: data[0]}, settings=SimpleNamespace(mode="sandbox"))
    service = ManualNativeService(engine)
    service._gate = lambda **_: "reviewed-profile"
    service._fresh_candidate = AsyncMock(side_effect=[first, second])
    preview = await service.preview(first.market_id, first.venue, first.outcome, D("1"))
    assert preview["candidate"]["intent"]["id"] == first.intent.id
    result = await service.submit(preview["review_token"], preview["terms_digest"], D("1"), preview["proposal_digest"])
    assert result["order"]["id"] == second.intent.id
    assert reviewed == [first.intent.id]
    assert not risk.auto_trade and not risk.manual_order_active(second.intent.id)
    with pytest.raises(RiskRejected, match="already used"):
        await service.submit(preview["review_token"], preview["terms_digest"], D("1"), preview["proposal_digest"])


async def test_manual_review_rejects_changed_price_without_placement(market, quotes):
    data = setup_native(market, quotes)
    first = candidate(data)
    changed = first.model_copy(update={"roi": first.roi - D(".01")})
    risk = RiskEngine(RiskConfig())
    execute = AsyncMock()
    native = SimpleNamespace(preview_check=lambda _: None,
        books={(first.market_id, first.venue, first.outcome): data[2]},
        build_order=lambda found, **_: SimpleNamespace(id=found.intent.id, account_fingerprint="a" * 64),
        execute=execute)
    engine = SimpleNamespace(native=native, risk=risk, oms=SimpleNamespace(lock=asyncio.Lock()),
                             markets={data[0].id: data[0]}, settings=SimpleNamespace(mode="sandbox"))
    service = ManualNativeService(engine)
    service._gate = lambda **_: "reviewed-profile"
    service._fresh_candidate = AsyncMock(side_effect=[first, changed])
    preview = await service.preview(first.market_id, first.venue, first.outcome, D("1"))
    with pytest.raises(RiskRejected, match="approved bounds"):
        await service.submit(preview["review_token"], preview["terms_digest"], D("1"), preview["proposal_digest"])
    execute.assert_not_awaited()


async def test_manual_review_binds_explicit_owner_confirmation_and_never_places_on_mismatch(market, quotes):
    data = setup_native(market, quotes)
    found = candidate(data)
    risk = RiskEngine(RiskConfig())
    risk.kill_switch = True
    execute = AsyncMock()
    native = SimpleNamespace(preview_check=lambda _: None,
        books={(found.market_id, found.venue, found.outcome): data[2]},
        build_order=lambda item, **_: SimpleNamespace(id=item.intent.id, account_fingerprint="a" * 64),
        execute=execute)
    engine = SimpleNamespace(native=native, risk=risk, oms=SimpleNamespace(lock=asyncio.Lock()),
        markets={data[0].id: data[0]}, settings=SimpleNamespace(mode="sandbox"))
    service = ManualNativeService(engine)
    service._gate = lambda **_: "reviewed-profile"
    service._fresh_candidate = AsyncMock(return_value=found)
    review = await service.preview(found.market_id, found.venue, found.outcome, D("1"))
    assert review["unsigned"] and review["risk_decisions"]["kill_switch_on"]
    with pytest.raises(RiskRejected, match="confirmation"):
        await service.submit(review["review_token"], "0" * 64, D("1"), review["proposal_digest"])
    execute.assert_not_awaited()


async def test_manual_proposal_digest_and_account_recheck_block_changed_terms(market, quotes):
    data = setup_native(market, quotes)
    found = candidate(data)
    risk = RiskEngine(RiskConfig())
    execute = AsyncMock()
    accounts = iter(("a" * 64, "b" * 64))
    native = SimpleNamespace(preview_check=lambda _: None,
        books={(found.market_id, found.venue, found.outcome): data[2]},
        build_order=lambda item, **_: SimpleNamespace(id=item.intent.id,
            account_fingerprint=next(accounts)), execute=execute)
    engine = SimpleNamespace(native=native, risk=risk, oms=SimpleNamespace(lock=asyncio.Lock()),
        markets={data[0].id: data[0]}, settings=SimpleNamespace(mode="sandbox"))
    service = ManualNativeService(engine)
    service._gate = lambda **_: "reviewed-profile"
    service._fresh_candidate = AsyncMock(return_value=found)
    preview = await service.preview(found.market_id, found.venue, found.outcome, D("1"))
    with pytest.raises(RiskRejected, match="Account, market"):
        await service.submit(preview["review_token"], preview["terms_digest"],
                             D("1"), preview["proposal_digest"])
    execute.assert_not_awaited()
    assert risk.kill_switch


async def test_manual_proposal_expiry_and_duplicate_digest_fail_without_placement(market, quotes):
    data = setup_native(market, quotes)
    found = candidate(data)
    risk = RiskEngine(RiskConfig())
    execute = AsyncMock()
    native = SimpleNamespace(preview_check=lambda _: None,
        books={(found.market_id, found.venue, found.outcome): data[2]},
        build_order=lambda item, **_: SimpleNamespace(id=item.intent.id,
            account_fingerprint="a" * 64), execute=execute)
    engine = SimpleNamespace(native=native, risk=risk, oms=SimpleNamespace(lock=asyncio.Lock()),
        markets={data[0].id: data[0]}, settings=SimpleNamespace(mode="sandbox"))
    service = ManualNativeService(engine)
    service._gate = lambda **_: "reviewed-profile"
    service._fresh_candidate = AsyncMock(return_value=found)
    review = await service.preview(found.market_id, found.venue, found.outcome, D("1"))
    with pytest.raises(RiskRejected, match="confirmation"):
        await service.submit(review["review_token"], review["terms_digest"], D("1"), "0" * 64)
    with pytest.raises(RiskRejected, match="already used"):
        await service.submit(review["review_token"], review["terms_digest"],
                             D("1"), review["proposal_digest"])
    execute.assert_not_awaited()


async def test_manual_reverification_accepts_fresh_clock_and_harmless_depth_change(market, quotes):
    data = setup_native(market, quotes)
    first = candidate(data)
    later = utcnow()
    refreshed_refs = tuple({**row, "source_time": later.isoformat(),
                            "source_observed_at": later.isoformat()} for row in first.references)
    refreshed = first.model_copy(update={"references": refreshed_refs, "calculated_at": later,
                                        "book_revision": first.book_revision + 1})
    risk = RiskEngine(RiskConfig())
    execute = AsyncMock(return_value=SimpleNamespace(model_dump=lambda **_: {"id": first.intent.id}))
    native = SimpleNamespace(preview_check=lambda _: None,
        books={(first.market_id, first.venue, first.outcome): data[2]},
        build_order=lambda found, **_: SimpleNamespace(id=found.intent.id, account_fingerprint="a" * 64),
        execute=execute, build_id="build-1")
    storage = SimpleNamespace(event=AsyncMock())
    engine = SimpleNamespace(native=native, risk=risk, oms=SimpleNamespace(lock=asyncio.Lock()),
        markets={data[0].id: data[0]}, settings=SimpleNamespace(mode="sandbox"), storage=storage)
    service = ManualNativeService(engine)
    service._gate = lambda **_: "reviewed-profile"
    service._fresh_candidate = AsyncMock(side_effect=[first, refreshed])
    preview = await service.preview(first.market_id, first.venue, first.outcome, D("1"))
    native.books[(first.market_id, first.venue, first.outcome)] = data[2].model_copy(update={
        "revision": 2, "source_time": later})
    result = await service.submit(preview["review_token"], preview["terms_digest"], D("1"),
                                  preview["proposal_digest"])
    assert result["final_validation_digest"] != preview["proposal_digest"]
    event = storage.event.await_args.args[0]
    assert event.payload["original_proposal_digest"] == preview["proposal_digest"]
    assert event.payload["final_validation_digest"] == result["final_validation_digest"]
    assert storage.event.await_args.kwargs == {"durable": True}
    execute.assert_awaited_once()
    assert not risk.auto_trade and risk.kill_switch


@pytest.mark.parametrize("change", ["policy", "build", "quantity", "debit", "fee", "edge"])
async def test_manual_reverification_rejects_scope_or_worse_economics(market, quotes, change):
    data = setup_native(market, quotes)
    first = candidate(data)
    updated = {"policy": {"reference_policy_digest": "different"},
        "quantity": {"intent": first.intent.model_copy(update={"quantity": first.intent.quantity + D(1)})},
        "debit": {"intent": first.intent.model_copy(update={"limit_price": first.intent.limit_price + D(".005")})},
        "fee": {"intent": first.intent.model_copy(update={"max_fee_usd": first.intent.max_fee_usd + D(".001")})},
        "edge": {"ev_per_usd_payout": first.ev_per_usd_payout - D(".01")}}
    second = first.model_copy(update=updated.get(change, {}))
    risk = RiskEngine(RiskConfig())
    execute = AsyncMock()
    native = SimpleNamespace(preview_check=lambda _: None,
        books={(first.market_id, first.venue, first.outcome): data[2]}, build_id="build-1",
        build_order=lambda found, **_: SimpleNamespace(id=found.intent.id, account_fingerprint="a" * 64),
        execute=execute)
    engine = SimpleNamespace(native=native, risk=risk, oms=SimpleNamespace(lock=asyncio.Lock()),
        markets={data[0].id: data[0]}, settings=SimpleNamespace(mode="sandbox"))
    service = ManualNativeService(engine)
    service._gate = lambda **_: "reviewed-profile"
    service._fresh_candidate = AsyncMock(side_effect=[first, second])
    preview = await service.preview(first.market_id, first.venue, first.outcome, D("1"))
    if change == "build":
        native.build_id = "build-2"
    with pytest.raises(RiskRejected, match="changed|approved bounds"):
        await service.submit(preview["review_token"], preview["terms_digest"], D("1"),
                             preview["proposal_digest"])
    execute.assert_not_awaited()
    assert risk.kill_switch and not risk.auto_trade


async def test_manual_max_cost_resizes_candidate_before_review(market, quotes):
    data = setup_native(market, quotes, quantity="2000")
    market, observations, book, _, config, _ = data
    base = candidate(data)
    native = SimpleNamespace(books={(market.id, base.venue, base.outcome): book},
                             candidates={}, sizing_cash=lambda venue: D("10"))
    async def recalculate(market_id):
        native.candidates[(market_id, base.venue, base.outcome)] = base
    native.recalculate = recalculate
    engine = SimpleNamespace(native=native, risk=RiskEngine(RiskConfig()),
        registry=SimpleNamespace(records={"native_positive_ev": SimpleNamespace(enabled=True)}),
        settings=SimpleNamespace(mode="sandbox", pricing=config),
        markets={market.id: market}, quotes={market.id: {(q.provider, q.bookmaker): q for q in observations}})
    service = ManualNativeService(engine)
    limited = await service._fresh_candidate(market.id, base.venue, base.outcome, D("1"))
    assert limited.intent.reserved_cost_usd <= D("1")
    assert limited.intent.quantity < base.intent.quantity
    with pytest.raises(RiskRejected, match="budget|minimum|quantity"):
        await service._fresh_candidate(market.id, base.venue, base.outcome, D("0.0001"))


def test_native_cent_contract_ev_uses_usd_payout_and_cost(market, quotes):
    data = setup_native(market, quotes)
    found = candidate(data)
    assert found.intent.quantity == 100
    assert found.intent.maximum_cost_usd == D("0.45")
    assert found.intent.minimum_gross_payout_usd == 1
    assert found.intent.max_fee_usd == D("0.003")
    assert found.expected_profit_usd == found.fair_probability - D("0.453")
    assert found.ev_per_usd_payout == found.expected_profit_usd
    assert found.qualified
    assert not found.synthetic


def test_native_stake_sizing_reserves_every_partial_fill_fee(market, quotes):
    data = setup_native(market, quotes, quantity="2000", per_fill_usd="0.00003", fixed_order_usd="0.01")
    found = candidate(data, max_stake_usd=D("0.50"))
    assert found.intent.quantity == 107
    assert found.intent.max_fee_usd == D("0.01428")
    assert found.intent.reserved_cost_usd == D("0.49578")
    assert found.intent.reserved_cost_usd <= D("0.50")


def test_fractional_kalshi_and_prophetx_stakes_keep_native_quantity(market, quotes):
    fractional = candidate(setup_native(market, quotes, venue="kalshi", quantity="3.17"))
    assert fractional.intent.quantity == D("3.17")
    assert fractional.intent.maximum_cost_usd == D("1.4265")
    stake = candidate(setup_native(market, quotes, venue="prophetx", price="125", quantity="4.17"))
    assert stake.intent.quantity == D("4.17")
    assert stake.intent.maximum_cost_usd == D("4.17")
    assert stake.intent.minimum_gross_payout_usd == D("8.34")  # each .01 match may round profit down


def test_negative_native_observations_are_visible_but_not_qualified(market, quotes):
    found = candidate(setup_native(market, quotes, price="0.650"))
    assert found.expected_profit_usd < 0
    assert not found.qualified


@pytest.mark.parametrize("change", [
    {"source_time": None}, {"synchronized": False}, {"status": "suspended"},
    {"status": "closed"}, {"synthetic": True}, {"market_fingerprint": "different-rules"}, {"asks": ()},
])
def test_native_book_fail_closed_states(market, quotes, change):
    m, q, b, f, c, now = setup_native(market, quotes)
    with pytest.raises(NoSignal):
        candidate((m, q, b.model_copy(update=change), f, c, now))


def test_recent_arrival_does_not_refresh_old_book_source_timestamp(market, quotes):
    m, q, b, f, c, now = setup_native(market, quotes)
    with pytest.raises(NoSignal, match="timestamp"):
        candidate((m, q, b.model_copy(update={"source_time": now - timedelta(seconds=3)}), f, c, now))
    with pytest.raises(NoSignal, match="timestamp"):
        candidate((m, q, b.model_copy(update={"source_time": now + timedelta(seconds=1)}), f, c, now))


def test_reference_ages_are_recomputed_at_native_submission(market, quotes):
    m, q, b, f, c, now = setup_native(market, quotes)
    future = now + timedelta(seconds=6)
    b = b.model_copy(update={"source_time": future, "received_at": future})
    f = f.model_copy(update={"calculated_at": future})  # Ref source clocks deliberately remain old.
    with pytest.raises(NoSignal, match="reference expired"):
        candidate((m, q, b, f, c, future))


@pytest.mark.parametrize("changes", [{"verified": False}, {"instrument_fingerprint": "other-instrument"}])
def test_unverified_or_cross_instrument_fee_bound_rejected(market, quotes, changes):
    data = setup_native(market, quotes, **changes)
    with pytest.raises(NoSignal, match="fee bound"):
        candidate(data)


def test_expired_fee_cap_and_too_small_budget_fail_closed(market, quotes):
    m, q, b, f, c, now = setup_native(market, quotes)
    b = b.model_copy(update={"fee_bound": b.fee_bound.model_copy(update={"valid_until": now})})
    with pytest.raises(NoSignal, match="stale"):
        candidate((m, q, b, f, c, now))
    with pytest.raises(NoSignal, match="budget"):
        candidate(setup_native(market, quotes), max_stake_usd=D("0.0001"))


def test_duplicate_reference_books_and_synthetic_fair_cannot_execute(market, quotes):
    m, q, b, f, c, now = setup_native(market, quotes)
    with pytest.raises(NoSignal, match="double-counts"):
        candidate((m, q, b, f.model_copy(update={"references": (f.references[0], f.references[0])}), c, now))
    with pytest.raises(NoSignal, match="Synthetic"):
        candidate((m, q, b, f.model_copy(update={"synthetic": True}), c, now))


def remap_book(book, market):
    ins = book.instrument.model_copy(update={"settlement_rules_fingerprint": settlement_fingerprint(market)})
    return book.model_copy(update={"instrument": ins, "market_fingerprint": market.identity(),
        "fee_bound": book.fee_bound.model_copy(update={"instrument_fingerprint": instrument_fingerprint(ins)})})


def test_unknown_push_void_probabilities_cannot_be_treated_as_zero(market, quotes):
    m, q, b, f, c, now = setup_native(market, quotes)
    m = m.model_copy(update={"settlement": m.settlement.model_copy(update={"void_rule": "cancelled_event_void"})})
    b = remap_book(b, m)
    with pytest.raises(NoSignal, match="push/void"):
        candidate((m, q, b, f, c, now))
    m = m.model_copy(update={"extensions": {"native_settlement_probabilities_v1": {
        "verified": True, "source": "validated historical cancellation model fixture",
        "push_probability_upper_bound": "0", "void_probability_upper_bound": "0.10",
        "refund_cost_on_push": True, "refund_cost_on_void": True,
    }}})
    found = candidate((m, q, b, f, c, now))
    expected = (found.fair_probability - D("0.45")) * D("0.9") - D("0.003")
    assert found.expected_profit_usd == expected


def test_standing_intent_reuses_original_price_despite_cheaper_current_ask(market, quotes):
    m, q, b, f, c, now = setup_native(market, quotes)
    original = candidate((m, q, b, f, c, now))
    better = b.model_copy(update={"asks": (NativeLevel(price="0.400", quantity="100"),)})
    current = native_revalidate_intent(original.intent, m, f, better, c, now=now)
    assert current.intent.limit_price == D("0.450")
    assert current.expected_profit_usd == original.expected_profit_usd
    assert current.id == original.id


def test_standing_intent_blocks_fee_growth_lost_edge_and_lost_depth(market, quotes):
    m, q, b, f, c, now = setup_native(market, quotes)
    original = candidate((m, q, b, f, c, now))
    raised = b.model_copy(update={"fee_bound": b.fee_bound.model_copy(update={"per_fill_usd": D("0.01")})})
    with pytest.raises(NoSignal, match="reservation"):
        native_revalidate_intent(original.intent, m, f, raised, c, now=now)
    with pytest.raises(NoSignal, match="edge"):
        native_revalidate_intent(original.intent, m, f.model_copy(update={"probabilities": {"home": D("0.40"), "away": D("0.60")}}), b, c, now=now)
    with pytest.raises(NoSignal, match="liquidity"):
        native_revalidate_intent(original.intent, m, f, b.model_copy(update={"asks": (NativeLevel(price="0.455", quantity="100"),)}), c, now=now)


def test_book_ladder_must_be_native_sorted_and_on_grid(market, quotes):
    m, q, b, f, c, now = setup_native(market, quotes)
    with pytest.raises(ValidationError):
        NativeBook.model_validate(b.model_dump() | {"asks": [dict(price="0.45", quantity="10"), dict(price="0.40", quantity="10")]})
    with pytest.raises(ValidationError):
        NativeBook.model_validate(b.model_dump() | {"asks": [dict(price="0.451", quantity="10")]})


@pytest.mark.asyncio
async def test_native_strategy_plugin_emits_standard_candidate_without_router(market, quotes):
    m, q, b, f, c, now = setup_native(market, quotes)
    strategy = NativePositiveEV()
    view = dict(market=m, quotes=q, native_books=[b], config=c, mode="sandbox", max_stake_usd=D("10"))
    candidates = await strategy.evaluate(None, view)
    assert len(candidates) == 1
    assert candidates[0].qualified
    assert strategy.manifest.capabilities["direct_order_access"] is False
    validated = strategy.revalidate(candidates[0], view | {"native_book": b})
    assert validated.intent.client_order_id == candidates[0].intent.client_order_id


@pytest.mark.parametrize("budget", [None, "invalid", "NaN", "Infinity", D("0"), True, 1.0])
def test_missing_or_invalid_native_budget_is_no_signal(market, quotes, budget):
    with pytest.raises(NoSignal):
        candidate(setup_native(market, quotes), max_stake_usd=budget)


@pytest.mark.parametrize("mode", ["demo", "paper", None])
def test_mode_must_be_explicitly_native(market, quotes, mode):
    m, q, b, f, c, now = setup_native(market, quotes)
    with pytest.raises(NoSignal):
        native_opportunity(m, f, b, c, max_stake_usd=D("1"), mode=mode, now=now)


async def test_native_plugin_preserves_no_signal_diagnostic(market, quotes):
    m, q, b, f, c, now = setup_native(market, quotes)
    with pytest.raises(NoSignal, match="timestamp"):
        await NativePositiveEV().evaluate(None, dict(market=m, quotes=q,
            native_books=[b.model_copy(update={"source_time": None})], config=c,
            max_stake_usd=D("1"), mode="sandbox"))


@pytest.mark.parametrize("policy,error", [
    ({"eligible_books": ["pinnacle", "circa"]}, "no longer eligible"),
    ({"eligible_books": []}, "no longer eligible"),
    ({"weights": {"pinnacle": D("2")}}, "weight changed"),
    ({"devig_method": "power"}, "method changed"),
    ({"required_books": ["fanduel"]}, "requirements"),
])
def test_native_submission_rechecks_current_reference_policy(market, quotes, policy, error):
    m, q, b, f, config, now = setup_native(market, quotes)
    changed = PricingConfig.model_validate(config.model_dump() | policy)
    with pytest.raises(NoSignal, match=error):
        candidate((m, q, b, f, changed, now))
