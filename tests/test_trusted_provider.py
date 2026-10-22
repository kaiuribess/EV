"""Provider-trusted probabilities are real-source research, never order permission."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from evplatform.core.config import PricingConfig, TrustedPilotPolicy
from evplatform.core.config import Settings
from evplatform.core.models import Envelope, utcnow
from evplatform.core.native_accounting import NativeInstrument, NativePriceBand
from evplatform.core.native_pricing import (NativeBook, NativeFeeBound, NativeLevel,
    instrument_fingerprint, native_opportunity, research_native_estimate)
from evplatform.core.pricing import NoSignal, fair_value
from evplatform.core.reference_profiles import ProfileSelector, ReferenceProfile
from evplatform.engine import Engine
from evplatform.integrations.base import ProviderEnvelope
from evplatform.integrations.normalization import SourceOffer, TrustedSourceProjector, VerifiedProviderMapper


NOW = datetime(2026, 10, 22, 20, tzinfo=UTC)


def config(mode="trusted_provider"):
    profile = ReferenceProfile(selectors=(ProfileSelector(
        sport="basketball", league="NBA", market_type="moneyline"),),
        weights_percent={"pinnacle": Decimal(60), "fanduel": Decimal(40)},
        required_any_anchor=("pinnacle",), min_distinct_books=2,
        status="research_prior", live_execution_allowed=False)
    return PricingConfig(reference_profile_mode="strict", reference_quality_mode=mode,
        normal_outcome_policy="winner_only_binary", reference_profiles={"nba": profile},
        enabled_reference_profiles=["nba"], max_quote_age_ms=5000,
        trusted_max_delivery_age_ms=30000, trusted_max_indicator_age_ms=30000)


def frame(*, suspended=False, pinnacle=True, fanduel=True, incomplete=False,
          poll_age=1, received=NOW, start=None):
    start = start or NOW + timedelta(hours=1)
    books = []
    if pinnacle:
        books.append({"key": "pinnacle", "verified_at": (received-timedelta(seconds=poll_age)).isoformat(),
            "is_current": True, "markets": [{"key": "h2h", "last_update": (NOW-timedelta(hours=1)).isoformat(),
                "outcomes": [{"name": "Home", "price": 1.85}, {"name": "Away", "price": 2.05}]}]})
    if fanduel:
        books.append({"key": "fanduel", "markets": [{"key": "h2h", "suspended": suspended,
            "last_update": (NOW-timedelta(hours=1)).isoformat(),
            "outcomes": ([{"name": "Home", "price": 1.91}] if incomplete else
                         [{"name": "Home", "price": 1.91}, {"name": "Away", "price": 1.91}])}]})
    return ProviderEnvelope("parlay_rest", "snapshot", [{"id": "event-1", "sport_key": "basketball_nba",
        "home_team": "Home", "away_team": "Away", "commence_time": start.isoformat(),
        "bookmakers": books}], received, 1, published_at=received,
        metadata={"sport": "basketball_nba"})


def projected(envelope, policy=None):
    mapper = VerifiedProviderMapper()
    mapper.research_projector = TrustedSourceProjector(lambda: policy or config())
    assert mapper(envelope) == []  # No reviewed source-to-native identity.
    return mapper, mapper.research_updates


def test_trusted_two_book_probability_keeps_unknown_quote_age_and_status():
    _, updates = projected(frame())
    assert len(updates) == 2
    market = updates[0][0]
    assert market.id.startswith("research:parlay:") and market.extensions["native_mapping_reviewed"] is False
    assert all(m.id == market.id for m, _ in updates)
    fair = fair_value(market, [q for _, q in updates], config(), NOW)
    assert set(fair.probabilities) == {"Home", "Away"}
    assert fair.probabilities["Home"] > Decimal("0.50")
    assert [r["effective_weight_percent"] for r in fair.references] == ["40.0", "60.0"]
    assert all(r["source_observed_at"] is None and r["age_ms"] is None for r in fair.references)
    assert any(r["status"] == "unknown" for r in fair.references)
    assert not fair.execution_eligible and not fair.live_execution_allowed
    assert any("Exact selection observation age unknown" in reason for reason in fair.ineligibility_reasons)
    assert all(r["source_price_changed_at"] is None or r["source_price_changed_at"] != r["source_observed_at"]
               for r in fair.references)


def test_trusted_delivery_expires_without_relabeling_cached_price():
    _, updates = projected(frame())
    market = updates[0][0]
    with pytest.raises(NoSignal, match="Insufficient"):
        fair_value(market, [q for _, q in updates], config(), NOW+timedelta(seconds=31))


def test_trusted_rejects_suspension_incomplete_pair_and_stale_poll():
    for case in (frame(suspended=True), frame(incomplete=True), frame(poll_age=31)):
        _, updates = projected(case)
        assert len(updates) >= 1
        market = updates[0][0]
        with pytest.raises(NoSignal):
            fair_value(market, [q for _, q in updates], config(), NOW)


def test_trusted_keeps_strict_mode_and_source_mapping_separate():
    _, updates = projected(frame(), config("strict_verified"))
    assert updates == []
    _, updates = projected(frame())
    market = updates[0][0]
    with pytest.raises(NoSignal, match="Insufficient"):
        fair_value(market, [q for _, q in updates], config("strict_verified"), NOW)


def test_duplicate_book_from_rest_and_websocket_counts_once():
    _, updates = projected(frame())
    market = updates[0][0]
    copies = [q for _, q in updates]
    copies.append(copies[0].model_copy(update={"provider": "parlay_ws"}))
    fair = fair_value(market, copies, config(), NOW)
    assert len(fair.references) == 2
    assert {r["bookmaker"] for r in fair.references} == {"pinnacle", "fanduel"}


def test_malformed_line_is_isolated_from_other_research_markets():
    projector = TrustedSourceProjector(config)
    envelope = frame()
    valid = SourceOffer(provider="parlay_rest", bookmaker="pinnacle", event_id="event-1",
        market_key="h2h", player_key=None, line=None, period="FULL", outcome_points={},
        prices={"home": Decimal("1.85"), "away": Decimal("2.05")}, observed_at=None,
        provider_time=NOW, context={"event_sport": "basketball_nba",
            "participants": ["Home", "Away"],
            "scheduled_start": (NOW + timedelta(hours=1)).isoformat()})
    broken = SourceOffer(provider="parlay_rest", bookmaker="pinnacle", event_id="event-1",
        market_key="spreads", player_key=None, line="bad", period="FULL",
        outcome_points={"Home": "bad", "Away": "-1.5"},
        prices={"Home": Decimal("1.9"), "Away": Decimal("1.9")}, observed_at=None,
        provider_time=NOW, context=valid.context)
    updates = projector.apply(envelope, [valid, broken])
    assert len(updates) == 1
    assert updates[0][0].market_type == "moneyline"


def test_full_game_periods_coalesce_but_quarter_market_does_not():
    from dataclasses import replace

    projector = TrustedSourceProjector(config)
    envelope = frame()
    base = SourceOffer(provider="parlay_rest", bookmaker="pinnacle", event_id="event-1",
        market_key="h2h", player_key=None, line=None, period=None, outcome_points={},
        prices={"home": Decimal("1.85"), "away": Decimal("2.05")}, observed_at=None,
        provider_time=NOW, context={"event_sport": "basketball_nba",
            "participants": ["Home", "Away"],
            "scheduled_start": (NOW + timedelta(hours=1)).isoformat()})
    rest = projector.apply(envelope, [base])[0][0]
    full = projector.apply(envelope, [replace(base, provider="parlay_game_ws", period="FULL")])[0][0]
    assert full.id == rest.id
    assert projector.apply(envelope, [replace(base, provider="parlay_game_ws", period="Q1")]) == []


def test_opposite_spread_orientation_has_a_distinct_research_identity():
    from dataclasses import replace

    projector = TrustedSourceProjector(config)
    envelope = frame()
    base = SourceOffer(provider="parlay_rest", bookmaker="pinnacle", event_id="event-1",
        market_key="spreads", player_key=None, line="3.5", period="FULL",
        outcome_points={"Home": "-3.5", "Away": "3.5"},
        prices={"Home": Decimal("1.9"), "Away": Decimal("1.9")}, observed_at=None,
        provider_time=NOW, context={"event_sport": "basketball_nba",
            "participants": ["Home", "Away"],
            "scheduled_start": (NOW + timedelta(hours=1)).isoformat()})
    home_favorite = projector._market_quote(envelope, base)[0]
    away_favorite = projector._market_quote(envelope, replace(base,
        outcome_points={"Home": "3.5", "Away": "-3.5"}))[0]
    assert home_favorite.id != away_favorite.id
    assert "Home -3.5" in home_favorite.outcomes
    assert "Home +3.5" in away_favorite.outcomes


def test_complete_game_websocket_frame_pairs_current_sides_without_quote_clock():
    rows = []
    for book in ("pinnacle", "fanduel"):
        for team in ("Home", "Away"):
            rows.append({"kind": "game", "bookmaker": book, "market_key": "h2h",
                "event_id": "event-1", "team": team, "outcome": team,
                "price": -110, "over_price": -110, "under_price": None,
                "home_team": "Home", "away_team": "Away", "period": "FULL",
                "commence_time": (NOW + timedelta(hours=1)).isoformat(),
                "last_update": int((NOW - timedelta(minutes=2)).timestamp() * 1000)})
    envelope = ProviderEnvelope("parlay_game_ws", "initial_state",
        {"data": rows, "snapshot_scope": "current_game_board",
         "snapshot_complete": True, "truncated": False}, NOW, 1,
        published_at=NOW, snapshot_complete=True, metadata={"sport": "basketball_nba"})
    mapper = VerifiedProviderMapper()
    mapper.research_projector = TrustedSourceProjector(config)
    mapper(envelope)
    assert len(mapper.research_updates) == 2
    market = mapper.research_updates[0][0]
    fair = fair_value(market, [quote for _, quote in mapper.research_updates], config(), NOW)
    assert fair.probabilities["Home"] == Decimal("0.5")
    assert all(ref["source_observed_at"] is None for ref in fair.references)
    assert not fair.execution_eligible


def test_trusted_pilot_policy_is_disabled_and_requires_scoped_owner_review():
    assert not PricingConfig().trusted_pilot_policy.enabled
    with pytest.raises(ValueError):
        TrustedPilotPolicy(enabled=True, exact_market_id="nba-1", provider_id="parlay_rest",
            trusted_books=["pinnacle", "fanduel"], max_indicator_age_ms=30000,
            independent_crosscheck="two_distinct_books",
            uncertainty_margin_probability=Decimal("0.02"), max_debit_usd=Decimal("1"),
            max_loss_usd=Decimal("1"), approval_evidence="owner-review")
    with pytest.raises(ValueError):
        TrustedPilotPolicy(enabled=True, owner_approved=True, exact_market_id="nba-1",
            provider_id="parlay_rest", trusted_books=["pinnacle", "fanduel"],
            max_indicator_age_ms=30000, independent_crosscheck="two_distinct_books",
            uncertainty_margin_probability=Decimal("0.02"), max_debit_usd=Decimal("2"),
            max_loss_usd=Decimal("1"), approval_evidence="owner-review")


def trusted_native_fixture(at=NOW):
    _, updates = projected(frame(received=at, start=at + timedelta(hours=1)))
    source_market = updates[0][0]
    market = source_market.model_copy(update={"id": "reviewed-fixture",
        "settlement": source_market.settlement.model_copy(update={"push_rule": "not_applicable"}),
        "extensions": {"reviewed_fixture": True}})
    quotes = [q.model_copy(update={"market_id": market.id,
        "market_fingerprint": market.identity()}) for _, q in updates]
    instrument = NativeInstrument(venue="kalshi", instrument_id="KX-REVIEWED-FIXTURE",
        exchange_index=3, canonical_market_id=market.id, outcome="Home",
        native_outcome="yes", quantity_unit="contracts", price_format="probability",
        payout_per_unit_usd="1", quantity_step="1", minimum_quantity="1",
        minimum_order_cost_usd="0", order_minimum_verified=True,
        order_minimum_source="fixture", cash_quantum_usd="0.0001",
        price_bands=(NativePriceBand(minimum="0.01", maximum="0.99", step="0.01"),),
        settlement_rules_fingerprint=market.identity(), verified=True, source="fixture")
    fee = NativeFeeBound(instrument_fingerprint=instrument_fingerprint(instrument),
        per_unit_usd="0.01", per_fill_usd="0", fixed_order_usd="0", verified=True,
        source="fixture", observed_at=at-timedelta(seconds=1),
        valid_until=at+timedelta(minutes=1))
    book = NativeBook(instrument=instrument, market_fingerprint=market.identity(),
        asks=(NativeLevel(price="0.40", quantity="10"),), fee_bound=fee,
        source_time=at-timedelta(milliseconds=100), received_at=at,
        revision=1, synchronized=True)
    return market, quotes, book


def test_synthetic_trusted_positive_ev_is_research_only_without_order_intent():
    market, quotes, book = trusted_native_fixture()
    fair = fair_value(market, quotes, config(), NOW)
    estimate = research_native_estimate(market, fair, book, config(), now=NOW)
    assert estimate["positive_estimate"] is True
    assert Decimal(estimate["expected_profit_usd"]) > 0
    assert estimate["execution_eligible"] is False
    with pytest.raises(NoSignal, match="research-only"):
        native_opportunity(market, fair, book, config(), max_stake_usd=Decimal("1"),
            mode="production", now=NOW)


@pytest.mark.asyncio
async def test_provisional_native_estimate_reaches_reference_dashboard_without_order(tmp_path):
    market, quotes, book = trusted_native_fixture(utcnow())
    engine = Engine(Settings(_env_file=None, mode="production", live_enabled=False,
        auto_trade=False, sample_feed_enabled=False, database_path=str(tmp_path / "native-research.db"),
        pricing=config()))
    engine.risk.kill_switch = True
    await engine.process_event(Envelope(schema_name="sports.market", source="test", payload=market))
    for quote in quotes:
        await engine.process_event(Envelope(schema_name="odds.quote", source="test", payload=quote))
    await engine.process_event(Envelope(schema_name="exchange.native_book", source="test", payload=book))
    state = engine.state()
    assert len(state["reference_estimates"]) == 1
    estimate = state["reference_estimates"][0]
    assert estimate["status"] == "PROVISIONAL POSITIVE EV"
    assert estimate["native_research_estimates"][0]["execution_eligible"] is False
    assert not state["native_opportunities"] and not state["native_orders"]
    assert not state["auto_trade"] and state["kill_switch"]


def test_source_prop_uses_exact_event_metadata_and_rejects_unknown_period():
    from evplatform.core.profile_import import STATISTICS
    assert STATISTICS["player_points"] == "points"
    profile = ReferenceProfile(selectors=(ProfileSelector(sport="basketball", league="NBA",
        market_type="player_prop", statistic="points"),),
        weights_percent={"pinnacle": Decimal(20), "fanduel": Decimal(50),
                         "draftkings": Decimal(20), "caesars": Decimal(10)},
        required_any_anchor=("fanduel",), min_distinct_books=3,
        status="research_prior", live_execution_allowed=False)
    policy = config().model_copy(update={"reference_profiles": {"props": profile},
                                 "enabled_reference_profiles": ["props"]})
    mapper = VerifiedProviderMapper()
    mapper.research_projector = TrustedSourceProjector(lambda: policy)
    mapper(frame())  # Same provider event ID supplies exact teams and start.
    rows = []
    for book in ("fanduel", "pinnacle", "draftkings"):
        rows.append({"event_id": "event-1", "bookmaker": book, "market_key": "player_points",
            "player": "Named Player", "period": "FULL", "line": 24.5,
            "over_price": -110, "under_price": -110, "last_update": int((NOW-timedelta(minutes=1)).timestamp()*1000)})
    prop = ProviderEnvelope("parlay_rest", "props_snapshot", rows, NOW, 1,
        published_at=NOW, metadata={"sport": "basketball_nba"})
    mapper(prop)
    assert len(mapper.research_updates) == 3
    market = mapper.research_updates[0][0]
    assert market.player == "Named Player" and market.line == Decimal("24.5")
    assert len({m.id for m, _ in mapper.research_updates}) == 1
    fair = fair_value(market, [q for _, q in mapper.research_updates], policy, NOW)
    assert len(fair.references) == 3 and not fair.execution_eligible
    mapper(ProviderEnvelope("parlay_rest", "props_snapshot", [{**rows[0], "period": "UNKNOWN"}],
        NOW, 2, published_at=NOW, metadata={"sport": "basketball_nba"}))
    assert mapper.research_updates == []


@pytest.mark.asyncio
async def test_research_reference_reaches_dashboard_without_native_order(tmp_path):
    now = utcnow()
    engine = Engine(Settings(_env_file=None, mode="production", live_enabled=False,
        auto_trade=False, sample_feed_enabled=False, database_path=str(tmp_path / "research.db"),
        pricing=config()))
    engine.risk.kill_switch = True
    _, updates = projected(frame(received=now, start=now+timedelta(hours=1)))
    for market, quote in updates:
        await engine.process_event(Envelope(schema_name="sports.market", source="test", payload=market))
        await engine.process_event(Envelope(schema_name="odds.quote", source="test", payload=quote))
    state = engine.state()
    assert len(state["reference_estimates"]) == 1
    assert state["reference_estimates"][0]["quality_mode"] == "trusted_provider"
    assert state["reference_estimates"][0]["native_market_matched"] is False
    assert state["opportunities"] == [] and state["native_opportunities"] == []
    assert state["auto_trade"] is False and state["kill_switch"] is True
    assert state["orders"] == [] and state["native_orders"] == []
