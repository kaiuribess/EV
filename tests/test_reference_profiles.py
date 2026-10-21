"""Offline tests for research priors, exact selection and executable evidence.

The TTLs/status assertions below are explicit test fixtures, not provider claims.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest
from pydantic import ValidationError

from evplatform.core.config import PricingConfig
from evplatform.core.models import FeeSchedule, Level, Market, OrderBook, Quote, Settlement
from evplatform.core.native_accounting import NativeInstrument, NativePriceBand
from evplatform.core.native_pricing import (
    NativeBook, NativeFeeBound, NativeLevel, instrument_fingerprint,
    native_opportunity, native_revalidate_intent,
)
from evplatform.core.pricing import (
    NoSignal, devig, fair_value, opportunity, validate_candidate_size, validate_fair_references,
)
from evplatform.core.reference_profiles import (
    ProfileSelector, ReferenceFreshnessRule, ReferenceProfile, quote_eligibility,
)

NOW = datetime(2026, 10, 20, 18, tzinfo=timezone.utc)
SELECTOR = dict(sport="basketball", league="NBA", market_type="player_prop", statistic="points")
WEIGHTS = {"fanduel": D(50), "pinnacle": D(20), "draftkings": D(20), "caesars": D(10)}


def make_profile(**changes):
    return ReferenceProfile(**(dict(selectors=(SELECTOR,), weights_percent=WEIGHTS,
        required_any_anchor=("fanduel",), min_distinct_books=3,
        status="research_prior", live_execution_allowed=False) | changes))


@pytest.fixture
def profile_data():
    market = Market(id="nba-points-25.5", event_id="fixture-event", event_name="A v B",
        **SELECTOR, participants=("A", "B"), start_time=NOW + timedelta(hours=1),
        status="scheduled", player="Fixture Player", player_id="fixture-player", line="25.5",
        outcomes=("over", "under"), settlement=Settlement(void_rule="not_applicable"))
    quotes = [Quote(market_id=market.id, provider=f"feed-{book}", bookmaker=book,
        decimal_odds={"over": D("1.5"), "under": D("2.8")},
        source_time=NOW - timedelta(hours=2), source_price_changed_at=NOW - timedelta(hours=2),
        source_observed_at=NOW - timedelta(milliseconds=100), provider_time=NOW, received_at=NOW,
        observation_verified=True, observation_complete=True, market_status_verified=True)
        for book in WEIGHTS]
    rules = [ReferenceFreshnessRule(**SELECTOR, provider=q.provider, bookmaker=q.bookmaker,
        ttl_seconds="2", verified=True, evidence="offline exact-source fixture") for q in quotes]
    config = PricingConfig(reference_profile_mode="strict", reference_profiles={"nba": make_profile()},
        enabled_reference_profiles=["nba"], profile_freshness_rules=rules, min_ev="0", min_roi="0")
    book = OrderBook(market_id=market.id, venue="fixture_paper", outcome="over",
        asks=(Level(price="0.50", quantity=50),), sequence=1, source_time=NOW, received_at=NOW,
        fee=FeeSchedule(model="none", verified=True, source="offline fixture"))
    return market, quotes, config, book


def native_book(market, *, price="0.50", venue="fixture_exchange"):
    instrument = NativeInstrument(venue=venue, instrument_id="native-points",
        canonical_market_id=market.id, outcome="over", native_outcome="native-over",
        quantity_unit="contracts", price_format="probability", payout_per_unit_usd="1",
        quantity_step="1", minimum_quantity="1", cash_quantum_usd="0.000001", verified=True,
        source="offline fixture", settlement_rules_fingerprint=market.identity(),
        price_bands=(NativePriceBand(minimum="0.0001", maximum="0.9999", step="0.0001"),))
    fee = NativeFeeBound(instrument_fingerprint=instrument_fingerprint(instrument),
        per_unit_usd="0", per_fill_usd="0", fixed_order_usd="0", verified=True,
        source="offline zero fee fixture", observed_at=NOW - timedelta(seconds=1),
        valid_until=NOW + timedelta(hours=1))
    return NativeBook(instrument=instrument, market_fingerprint=market.identity(),
        asks=(NativeLevel(price=price, quantity="50"),), fee_bound=fee, source_time=NOW,
        received_at=NOW, revision=1, synchronized=True)


@pytest.mark.parametrize("changes", [
    {"league": "WNBA"}, {"sport": "football"}, {"statistic": "assists"},
    {"statistic": "unknown"}, {"market_type": "moneyline", "statistic": None},
    {"status": "live"}, {"status": "suspended"}, {"status": "closed"},
    {"start_time": NOW}, {"start_time": NOW - timedelta(seconds=1)},
])
def test_no_broad_profile_inheritance_or_inplay_use(profile_data, changes):
    market, quotes, config, _ = profile_data
    with pytest.raises(NoSignal):
        fair_value(market.model_copy(update=changes), quotes, config, NOW)


def test_disabled_and_ambiguous_profiles_cannot_select(profile_data):
    market, quotes, config, _ = profile_data
    with pytest.raises(NoSignal, match="No enabled"):
        fair_value(market, quotes, config.model_copy(update={"enabled_reference_profiles": []}), NOW)
    with pytest.raises(ValidationError, match="overlap"):
        PricingConfig(reference_profiles={"one": make_profile(), "two": make_profile()},
                      enabled_reference_profiles=["one", "two"])


@pytest.mark.parametrize("changes", [
    {"weights_percent": {"fanduel": "99"}}, {"weights_percent": {"fanduel": "100", "pinnacle": "0"}},
    {"required_any_anchor": ("circa",)}, {"required_any_anchor": ("fanduel", "fanduel")},
    {"min_distinct_books": 5}, {"selectors": (SELECTOR, SELECTOR)},
])
def test_invalid_prior_contract_fails_validation(changes):
    with pytest.raises(ValidationError):
        make_profile(**changes)


@pytest.mark.parametrize("changes", [{"league": "*"}, {"statistic": None}, {"sport": " basketball"}])
def test_selectors_require_canonical_exact_scope(changes):
    with pytest.raises(ValidationError):
        ProfileSelector(**(SELECTOR | changes))


@pytest.mark.parametrize("field,value", [
    ("reference_profile_mode", "legacy"), ("reference_profiles", {}),
    ("enabled_reference_profiles", []), ("profile_freshness_rules", []),
])
def test_broad_override_cannot_replace_exact_profile_policy(field, value):
    with pytest.raises(ValidationError, match="exact selectors"):
        PricingConfig(market_overrides={"basketball:player_prop": {field: value}})


def test_profile_weights_apply_after_distinct_anchor_quorum(profile_data):
    market, quotes, config, _ = profile_data
    quotes[1] = quotes[1].model_copy(update={"decimal_odds": {"over": D("1.6"), "under": D("2.5")}})
    fair = fair_value(market, quotes[:3], config, NOW)
    assert fair.model == "power" and fair.sensitivity_model == "proportional"
    for method, actual in (("power", fair.probabilities), ("proportional", fair.sensitivity_probabilities)):
        expected = sum(devig(q.decimal_odds, method)["over"] * WEIGHTS[q.bookmaker] for q in quotes[:3]) / 90
        assert abs(actual["over"] - expected) < D("1e-27")
    assert fair.execution_eligible and not fair.live_execution_allowed
    assert {r["bookmaker"] for r in fair.references} == {"fanduel", "pinnacle", "draftkings"}
    with pytest.raises(NoSignal, match="anchor"):
        fair_value(market, quotes[1:], config, NOW)
    with pytest.raises(NoSignal, match="Insufficient"):
        fair_value(market, quotes[:2] + [quotes[0].model_copy(update={"provider": "another-api"})], config, NOW)


def test_anchor_is_any_not_all_and_excluded_books_have_no_vote(profile_data):
    market, quotes, config, _ = profile_data
    profile = make_profile(required_any_anchor=("fanduel", "pinnacle"))
    config = config.model_copy(update={"reference_profiles": {"nba": profile}})
    exchange = quotes[0].model_copy(update={"bookmaker": "kalshi", "decimal_odds": {"over": D("1.01"), "under": D("99")}})
    fair = fair_value(market, quotes[1:] + [exchange], config, NOW)
    assert fair.execution_eligible
    assert "kalshi" not in {r["bookmaker"] for r in fair.references}


def test_old_price_change_fresh_verified_observation_is_current(profile_data):
    market, quotes, config, _ = profile_data
    fair = fair_value(market, quotes, config, NOW)
    assert fair.execution_eligible
    assert all(r["age_ms"] == 100 and r["freshness_factor"] == "1" for r in fair.references)
    assert all(r["source_price_changed_at"] != r["source_observed_at"] for r in fair.references)


@pytest.mark.parametrize("changes,reason", [
    ({"source_observed_at": None}, "observation is missing"),
    ({"observation_verified": False}, "observation is missing"),
    ({"observation_complete": False}, "complete outcome"),
    ({"market_status_verified": False}, "market status"),
    ({"source_observed_at": NOW - timedelta(seconds=3)}, "expired"),
])
def test_unverified_or_stale_anchor_remains_research_with_both_models(profile_data, changes, reason):
    market, quotes, config, book = profile_data
    quotes[0] = quotes[0].model_copy(update=changes)
    fair = fair_value(market, quotes, config, NOW)
    assert not fair.execution_eligible and fair.sensitivity_probabilities
    assert any(reason in r for r in fair.ineligibility_reasons)
    candidate = opportunity(market, fair, book, config, NOW)
    assert not candidate.execution_eligible
    assert not candidate.reference_inputs_eligible
    assert candidate.ev_per_contract > 0 and candidate.sensitivity_ev_per_contract > 0
    with pytest.raises(NoSignal, match="research-only"):
        validate_candidate_size(candidate, book.fee, book.asks[0].price, 5, config)
    with pytest.raises(NoSignal, match="research-only"):
        native_opportunity(market, fair, native_book(market), config, max_stake_usd=D(5), mode="sandbox", now=NOW)


@pytest.mark.parametrize("rule_changes", [None, {"verified": False}, {"provider": "different-feed"},
    {"bookmaker": "other-book"}, {"league": "WNBA"}, {"statistic": "rebounds"}])
def test_missing_or_nonexact_ttl_is_research_only(profile_data, rule_changes):
    market, quotes, config, _ = profile_data
    rules = [] if rule_changes is None else [r.model_copy(update=rule_changes) for r in config.profile_freshness_rules]
    config = config.model_copy(update={"profile_freshness_rules": rules})
    fair = fair_value(market, quotes, config, NOW)
    assert not fair.execution_eligible
    assert all(r["freshness_factor"] == "0" for r in fair.references)
    assert any("TTL is missing" in reason for reason in fair.ineligibility_reasons)


def test_exact_ttl_has_hard_boundary_and_no_decay(profile_data):
    market, quotes, config, _ = profile_data
    quote = quotes[0].model_copy(update={"source_observed_at": NOW - timedelta(seconds=2)})
    assert quote_eligibility(config, market, quote, NOW)[0]
    assert not quote_eligibility(config, market, quote, NOW + timedelta(microseconds=1))[0]
    assert not quote_eligibility(config, market, quote.model_copy(update={"source_observed_at": NOW + timedelta(microseconds=1)}), NOW)[0]


def test_invalid_nonanchor_can_be_excluded_if_current_quorum_still_exists(profile_data):
    market, quotes, config, _ = profile_data
    quotes[-1] = quotes[-1].model_copy(update={"observation_complete": False})
    fair = fair_value(market, quotes, config, NOW)
    assert fair.execution_eligible
    assert len(fair.references) == 3
    assert "caesars" not in {r["bookmaker"] for r in fair.references}


def test_dedup_uses_observation_clock_not_arrival_or_best_price(profile_data):
    market, quotes, config, _ = profile_data
    stale = quotes[0].model_copy(update={"provider": "late-relay", "source_observed_at": NOW - timedelta(seconds=1),
        "source_time": NOW, "decimal_odds": {"over": D("1.3"), "under": D("3.5")}})
    config = config.model_copy(update={"profile_freshness_rules": config.profile_freshness_rules + [
        config.profile_freshness_rules[0].model_copy(update={"provider": "late-relay"})]})
    for inputs in (quotes + [stale], [stale] + quotes):
        fair = fair_value(market, inputs, config, NOW)
        assert next(r for r in fair.references if r["bookmaker"] == "fanduel")["provider"] == "feed-fanduel"
        assert fair.execution_eligible


def test_equal_observation_conflict_cannot_choose_favorable_price(profile_data):
    market, quotes, config, _ = profile_data
    conflict = quotes[0].model_copy(update={"decimal_odds": {"over": D("1.4"), "under": D("3.0")}})
    fair = fair_value(market, quotes + [conflict], config, NOW)
    assert not fair.execution_eligible
    assert any("Conflicting" in reason for reason in fair.ineligibility_reasons)


def test_sensitivity_rejection_retains_both_estimates_and_blocks_size(profile_data):
    market, quotes, config, book = profile_data
    fair = fair_value(market, quotes, config, NOW)
    assert fair.probabilities["over"] > fair.sensitivity_probabilities["over"]
    price = (fair.probabilities["over"] + fair.sensitivity_probabilities["over"]) / 2
    book = book.model_copy(update={"asks": (Level(price=price, quantity=50),)})
    candidate = opportunity(market, fair, book, config, NOW)
    assert candidate.ev_per_contract > 0 > candidate.sensitivity_ev_per_contract
    assert candidate.reference_inputs_eligible
    assert not candidate.execution_eligible
    assert not candidate.size_curve[0]["execution_eligible"]
    with pytest.raises(NoSignal):
        validate_candidate_size(candidate, book.fee, price, 5, config)


def test_actual_size_rounding_rechecks_both_methods(profile_data):
    market, quotes, config, book = profile_data
    fair = fair_value(market, quotes, config, NOW)
    price = fair.sensitivity_probabilities["over"] - D("0.005")
    fee = FeeSchedule(model="fixed", per_contract="0.0001", rounding="0.01", verified=True)
    book = book.model_copy(update={"asks": (Level(price=price, quantity=50),), "fee": fee})
    candidate = opportunity(market, fair, book, config, NOW)
    assert candidate.execution_eligible
    assert validate_candidate_size(candidate, fee, price, 50, config)[0] > 0
    with pytest.raises(NoSignal, match="actual size"):
        validate_candidate_size(candidate, fee, price, 1, config)


def test_target_self_reference_is_an_input_rejection(profile_data):
    market, quotes, config, book = profile_data
    fair = fair_value(market, quotes, config, NOW)
    candidate = opportunity(market, fair, book.model_copy(update={"venue": "fanduel"}), config, NOW)
    assert not candidate.reference_inputs_eligible and not candidate.execution_eligible
    assert any("own independent reference" in reason for reason in candidate.ineligibility_reasons)
    with pytest.raises(NoSignal, match="own independent reference"):
        native_opportunity(market, fair, native_book(market, venue="fanduel"), config,
                           max_stake_usd=D(5), mode="sandbox", now=NOW)


@pytest.mark.parametrize("change", ["weights", "ttl", "disabled", "legacy", "live_permission"])
def test_cached_fair_invalidated_by_profile_policy_change(profile_data, change):
    market, quotes, config, _ = profile_data
    fair = fair_value(market, quotes, config, NOW)
    if change == "weights":
        config = config.model_copy(update={"reference_profiles": {"nba": make_profile(weights_percent={
            "fanduel": D(40), "pinnacle": D(30), "draftkings": D(20), "caesars": D(10)})}})
    elif change == "ttl":
        config = config.model_copy(update={"profile_freshness_rules": [r.model_copy(update={"ttl_seconds": D(3)}) for r in config.profile_freshness_rules]})
    elif change == "disabled":
        config = config.model_copy(update={"enabled_reference_profiles": []})
    elif change == "legacy":
        config = config.model_copy(update={"reference_profile_mode": "legacy"})
    else:
        config = config.model_copy(update={"reference_profiles": {"nba": make_profile(live_execution_allowed=True)}})
    with pytest.raises(NoSignal):
        validate_fair_references(market, fair, config, NOW)


def test_cached_calculation_timestamp_cannot_refresh_observation_ttl(profile_data):
    market, quotes, config, _ = profile_data
    fair = fair_value(market, quotes, config, NOW)
    later = NOW + timedelta(seconds=3)
    with pytest.raises(NoSignal, match="observation/TTL"):
        validate_fair_references(market, fair.model_copy(update={"calculated_at": later}), config, later)


@pytest.mark.parametrize("flag", ["observation_verified", "observation_complete", "market_status_verified", "execution_eligible"])
def test_cached_reference_flags_cannot_be_lost(profile_data, flag):
    market, quotes, config, _ = profile_data
    fair = fair_value(market, quotes, config, NOW)
    refs = list(fair.references)
    refs[0] = refs[0] | {flag: False}
    with pytest.raises(NoSignal, match="observation/TTL"):
        validate_fair_references(market, fair.model_copy(update={"references": tuple(refs)}), config, NOW)


def test_native_profiles_block_production_and_permission_tampering(profile_data):
    market, quotes, config, _ = profile_data
    fair = fair_value(market, quotes, config, NOW)
    book = native_book(market)
    found = native_opportunity(market, fair, book, config, max_stake_usd=D(5), mode="sandbox", now=NOW)
    assert found.qualified and found.sensitivity_expected_profit_usd > 0
    assert not found.live_execution_allowed
    assert found.sensitivity_expected_profit_usd == found.sensitivity_fair_probability * found.intent.minimum_gross_payout_usd - found.intent.maximum_cost_usd
    with pytest.raises(NoSignal, match="production execution"):
        native_opportunity(market, fair, book, config, max_stake_usd=D(5), mode="production", now=NOW)
    with pytest.raises(NoSignal, match="production execution"):
        native_revalidate_intent(found.intent.model_copy(update={"mode": "production"}), market, fair, book, config, now=NOW)
    with pytest.raises(NoSignal, match="permission or status"):
        native_opportunity(market, fair.model_copy(update={"live_execution_allowed": True}), book, config,
                           max_stake_usd=D(5), mode="production", now=NOW)


def test_native_sensitivity_failure_is_visible_and_cannot_revalidate(profile_data):
    market, quotes, config, _ = profile_data
    fair = fair_value(market, quotes, config, NOW)
    price = ((fair.probabilities["over"] + fair.sensitivity_probabilities["over"]) / 2).quantize(D("0.0001"))
    book = native_book(market, price=price)
    found = native_opportunity(market, fair, book, config, max_stake_usd=D(5), mode="sandbox", now=NOW)
    assert found.expected_profit_usd > 0 > found.sensitivity_expected_profit_usd
    assert not found.qualified and not found.execution_eligible
    with pytest.raises(NoSignal, match="no longer meets"):
        native_revalidate_intent(found.intent, market, fair, book, config, now=NOW)


def test_duplicate_ttl_and_naive_observation_are_invalid(profile_data):
    _, quotes, config, _ = profile_data
    with pytest.raises(ValidationError, match="unique exact"):
        PricingConfig.model_validate(config.model_dump() | {"profile_freshness_rules": config.profile_freshness_rules * 2})
    with pytest.raises(ValidationError, match="timezone"):
        Quote.model_validate(quotes[0].model_dump() | {"source_observed_at": NOW.replace(tzinfo=None)})
