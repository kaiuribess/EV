from datetime import timedelta
from decimal import Decimal as D

import pytest
from pydantic import ValidationError

from evplatform.core.config import PricingConfig
from evplatform.core.models import utcnow
from evplatform.core.native_accounting import NativeInstrument, NativePriceBand
from evplatform.core.native_pricing import (
    NativeBook, NativeFeeBound, NativeLevel, instrument_fingerprint, native_opportunity,
    native_revalidate_intent, settlement_fingerprint,
)
from evplatform.core.pricing import NoSignal, fair_value
from evplatform.plugins.native_positive_ev import NativePositiveEV


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
