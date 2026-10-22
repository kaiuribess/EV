"""Offline fixtures exercise conditional normal WIN/LOSS pricing, never live evidence."""
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError

from evplatform.core.config import PricingConfig, RiskConfig
from evplatform.core.manual_native import ManualNativeService
from evplatform.core.models import Market, Quote, Settlement
from evplatform.core.native_accounting import NativeInstrument, NativePriceBand
from evplatform.core.native_pricing import (
    NativeBook, NativeFeeBound, NativeLevel, instrument_fingerprint,
    WinnerOnlyNormalOutcome, _settlement_bounds, native_opportunity, settlement_fingerprint,
)
from evplatform.core.pricing import NoSignal, fair_value, validate_fair_references
from evplatform.core.reference_profiles import ProfileSelector, ReferenceFreshnessRule, ReferenceProfile
from evplatform.core.risk import RiskEngine
from evplatform.integrations.normalization import VerifiedOfferMapping

NOW = datetime(2026, 10, 22, 18, tzinfo=timezone.utc)


def market_case(sport="basketball", league="NBA", market_type="moneyline", *,
                statistic=None, player=None, line=None, outcomes=("home", "away"),
                period="full_game", overtime="included", push="not_applicable",
                void="cancelled_event_void"):
    return Market(id=f"fixture:{sport}:{league}:{market_type}:{statistic}:{line}:{period}",
        event_id=f"fixture:{sport}:{league}", event_name="Fixture Home v Fixture Away",
        sport=sport, league=league, market_type=market_type,
        participants=("Fixture Home", "Fixture Away"),
        start_time=NOW + timedelta(hours=2), status="scheduled",
        player=player, player_id="fixture-player" if player else None,
        statistic=statistic, line=line, outcomes=outcomes,
        settlement=Settlement(period=period, overtime=overtime, void_rule=void, push_rule=push))


def strict_case(market, *, policy="winner_only_binary", odds=None, books=("pinnacle", "fanduel"),
                verified=True, status="active", observed=NOW - timedelta(seconds=1)):
    selector = ProfileSelector(sport=market.sport, league=market.league,
        market_type=market.market_type, statistic=market.statistic)
    weights = {book: D(100) / len(books) for book in books}
    profile = ReferenceProfile(selectors=(selector,), weights_percent=weights,
        required_any_anchor=(books[0],), min_distinct_books=2,
        status="offline_fixture_only", live_execution_allowed=True)
    rules = [ReferenceFreshnessRule(**selector.model_dump(), provider=f"fixture-{book}",
        bookmaker=book, ttl_seconds="5", verified=True,
        evidence="OFFLINE source-contract fixture") for book in books]
    config = PricingConfig(normal_outcome_policy=policy, reference_profile_mode="strict",
        reference_profiles={"fixture": profile}, enabled_reference_profiles=["fixture"],
        profile_freshness_rules=rules, min_ev="0.01", min_roi="0.01")
    odds = odds or {outcome: D("1.8") if i == 0 else D("2.1")
                    for i, outcome in enumerate(market.outcomes)}
    quotes = [Quote(market_id=market.id, market_fingerprint=market.identity(),
        provider=f"fixture-{book}", bookmaker=book,
        decimal_odds=odds, status=status, source_time=NOW - timedelta(hours=1),
        source_price_changed_at=NOW - timedelta(hours=1),
        source_observed_at=observed, provider_time=NOW, received_at=NOW,
        observation_verified=verified, observation_complete=verified,
        market_status_verified=verified, settlement_equivalence_verified=False,
        normal_outcome_verified=True, normal_outcome_evidence="OFFLINE exact normal-condition review")
        for book in books]
    return quotes, config


def native_book(market, *, venue="kalshi", outcome=None, price="0.40", quantity="20"):
    outcome = outcome or market.outcomes[0]
    instrument = NativeInstrument(venue=venue, instrument_id=f"fixture-{venue}-{market.id}",
        canonical_market_id=market.id, outcome=outcome, native_outcome="yes",
        quantity_unit="contracts" if venue == "kalshi" else "whole_cent_contracts",
        price_format="probability", payout_per_unit_usd="1" if venue == "kalshi" else ".01",
        quantity_step="1", minimum_quantity="1", cash_quantum_usd=".000001",
        minimum_order_cost_usd=".01", order_minimum_verified=True,
        order_minimum_source="OFFLINE fixture minimum", verified=True,
        source="OFFLINE reviewed identity fixture",
        settlement_rules_fingerprint=settlement_fingerprint(market),
        price_bands=(NativePriceBand(minimum=".005", maximum=".995", step=".005"),))
    fee = NativeFeeBound(instrument_fingerprint=instrument_fingerprint(instrument),
        per_unit_usd=".00001", per_fill_usd="0", fixed_order_usd=".001",
        verified=True, source="OFFLINE fee-bound fixture",
        observed_at=NOW - timedelta(minutes=1), valid_until=NOW + timedelta(hours=1))
    return NativeBook(instrument=instrument, market_fingerprint=market.identity(),
        asks=(NativeLevel(price=price, quantity=quantity),), fee_bound=fee,
        source_time=NOW - timedelta(milliseconds=100), received_at=NOW,
        revision=1, synchronized=True)


@pytest.mark.parametrize("market", [
    market_case(),
    market_case("football", "NFL"),
    market_case("baseball", "MLB", "player_prop", statistic="pitcher_strikeouts",
                player="Fixture Pitcher", line=D("6.5"), outcomes=("over", "under")),
    market_case("basketball", "NBA", "player_prop", statistic="points",
                player="Fixture Player", line=D("24.5"), outcomes=("over", "under")),
    market_case("hockey", "NHL", "player_prop", statistic="shots_on_goal",
                player="Fixture Skater", line=D("2.5"), outcomes=("over", "under")),
    market_case("football", "NFL", "player_prop", statistic="anytime_touchdown",
                player="Fixture Receiver", line=D("0.5"), outcomes=("yes", "no")),
    market_case("tennis", "ATP", void="retirement_void"),
])
def test_normal_binary_families_use_same_pricing_engine(market):
    quotes, config = strict_case(market)
    fair = fair_value(market, quotes, config, NOW)
    book = native_book(market)
    candidate = native_opportunity(market, fair, book, config,
        max_stake_usd=D("2"), mode="production", now=NOW)
    assert fair.execution_eligible and candidate.qualified
    assert candidate.normal_outcome_policy == "winner_only_binary"
    assert candidate.estimate_label.startswith("Winner-only estimated EV")
    assert "exceptional settlements excluded" in candidate.estimate_label
    assert candidate.expected_profit_usd == (
        candidate.fair_probability * candidate.intent.minimum_gross_payout_usd
        - candidate.intent.maximum_cost_usd - candidate.intent.max_fee_usd)
    assert candidate.roi == candidate.expected_profit_usd / candidate.intent.reserved_cost_usd
    assert all(not row["settlement_equivalence_verified"] for row in fair.references)


def test_three_way_soccer_draw_is_in_probability_distribution():
    market = market_case("soccer", "EPL", outcomes=("home", "draw", "away"),
        overtime="excluded")
    quotes, config = strict_case(market, odds={"home": D("2"), "draw": D("3"), "away": D("4")})
    fair = fair_value(market, quotes, config, NOW)
    assert set(fair.probabilities) == {"home", "draw", "away"}
    assert sum(fair.probabilities.values()) == pytest.approx(D(1))
    assert fair.probabilities["home"] < D(".5")  # A draw consumes probability mass.
    candidate = native_opportunity(market, fair, native_book(market, price=".35"), config,
        max_stake_usd=D("2"), mode="production", now=NOW)
    assert candidate.qualified


def test_native_payout_is_venue_specific_not_one_dollar_for_novig():
    market = market_case()
    quotes, config = strict_case(market)
    fair = fair_value(market, quotes, config, NOW)
    payouts = {}
    for venue in ("kalshi", "novig"):
        candidate = native_opportunity(market, fair, native_book(market, venue=venue,
            quantity="1000"), config, max_stake_usd=D("1"), mode="production", now=NOW)
        payouts[venue] = candidate.intent.instrument.payout_per_unit_usd
        assert candidate.expected_profit_usd == (
            candidate.fair_probability * candidate.intent.minimum_gross_payout_usd
            - candidate.intent.maximum_cost_usd - candidate.intent.max_fee_usd)
    assert payouts == {"kalshi": D("1"), "novig": D(".01")}


def test_integer_normal_push_is_not_discarded_as_exception():
    market = market_case(market_type="total", line=D("220"),
        outcomes=("over", "under"), push="integer_total_refund")
    quotes, config = strict_case(market)
    fair = fair_value(market, quotes, config, NOW)
    with pytest.raises(NoSignal, match="Normal-completion push"):
        native_opportunity(market, fair, native_book(market), config,
            max_stake_usd=D("2"), mode="production", now=NOW)


def test_winner_only_basis_does_not_claim_verified_zero_exception_probability():
    basis = _settlement_bounds(market_case(void="retirement_void"), winner_only=True)
    assert isinstance(basis, WinnerOnlyNormalOutcome)
    assert not hasattr(basis, "void_probability_upper_bound")
    assert not hasattr(basis, "refund_cost_on_void")
    assert "exceptional settlements excluded" in basis.source


def test_wrong_threshold_period_player_and_missing_opposite_fail():
    market = market_case(market_type="player_prop", statistic="points",
        player="Fixture Player", line=D("24.5"), outcomes=("over", "under"))
    quotes, config = strict_case(market)
    for change in ({"line": D("25.5")}, {"player_id": "other"},
                   {"statistic": "rebounds"},
                   {"settlement": market.settlement.model_copy(update={"period": "first_half"})},
                   {"settlement": market.settlement.model_copy(update={"overtime": "excluded"})},
                   {"outcomes": ("yes", "no")}):
        other = market.model_copy(update=change)
        with pytest.raises(NoSignal):
            fair_value(other, quotes, config, NOW)
    with pytest.raises(ValidationError):
        Quote.model_validate(quotes[0].model_dump() | {"decimal_odds": {"yes": D("2")}})


def test_stale_suspended_duplicate_and_strict_policy_guards():
    market = market_case()
    quotes, config = strict_case(market)
    extra = quotes[0].model_copy(update={"provider": "second-api"})
    fair = fair_value(market, [*quotes, extra], config, NOW)
    assert len(fair.references) == 2 and {r["bookmaker"] for r in fair.references} == {"pinnacle", "fanduel"}
    stale = [q.model_copy(update={"source_observed_at": NOW - timedelta(seconds=6)}) for q in quotes]
    assert not fair_value(market, stale, config, NOW).execution_eligible
    with pytest.raises(NoSignal, match="Insufficient"):
        fair_value(market, [quotes[0], quotes[1].model_copy(update={"status": "suspended"})],
            config, NOW)
    unverified = [q.model_copy(update={"normal_outcome_verified": False}) for q in quotes]
    assert not fair_value(market, unverified, config, NOW).execution_eligible
    strict = config.model_copy(update={"normal_outcome_policy": "strict_settlement_equivalence"})
    assert not fair_value(market, quotes, strict, NOW).execution_eligible
    with pytest.raises(NoSignal, match="policy changed"):
        validate_fair_references(market, fair, strict, NOW)


def test_mapping_requires_separate_normal_condition_provenance():
    market = market_case()
    base = dict(signature="0" * 64, market=market, outcome_map={"A": "home", "B": "away"},
                bookmaker="pinnacle", verified=True, evidence="OFFLINE fixture source identity")
    with pytest.raises(ValidationError, match="own source-to-target evidence"):
        VerifiedOfferMapping(**base, settlement_equivalence_verified=False,
            normal_outcome_verified=True)
    mapping = VerifiedOfferMapping(**base, settlement_equivalence_verified=False,
        normal_outcome_verified=True, normal_outcome_evidence="OFFLINE reviewed ordinary winner")
    assert not mapping.settlement_equivalence_verified and mapping.normal_outcome_verified


@pytest.mark.asyncio
async def test_winner_only_candidate_reaches_unsigned_manual_preview_without_order():
    market = market_case()
    quotes, config = strict_case(market)
    book = native_book(market)
    candidate = native_opportunity(market, fair_value(market, quotes, config, NOW), book, config,
        max_stake_usd=D("1"), mode="production", now=NOW)
    risk = RiskEngine(RiskConfig())
    risk.kill_switch = True
    preview_check, execute = Mock(), AsyncMock()
    native = SimpleNamespace(books={(market.id, "kalshi", "home"): book},
        build_id="OFFLINE-build", preview_check=preview_check,
        build_order=lambda found, **_: SimpleNamespace(id=found.intent.id,
            account_fingerprint="a" * 64), execute=execute)
    engine = SimpleNamespace(native=native, risk=risk, oms=SimpleNamespace(lock=asyncio.Lock()),
        markets={market.id: market}, settings=SimpleNamespace(mode="production", pricing=config))
    service = ManualNativeService(engine)
    service._gate = lambda **_: "OFFLINE-profile"
    service._fresh_candidate = AsyncMock(return_value=candidate)
    proposal = await service.preview(market.id, "kalshi", "home", D("1"))
    assert proposal["unsigned"] and proposal["candidate"]["normal_outcome_policy"] == "winner_only_binary"
    assert proposal["risk_decisions"]["kill_switch_on"] is True
    preview_check.assert_called_once()
    execute.assert_not_awaited()
    assert not risk.auto_trade and risk.kill_switch
