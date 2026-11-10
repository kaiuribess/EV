"""In-play prices can inform research without inheriting pregame order authority."""
from datetime import UTC, datetime, timedelta

import pytest

from decimal import Decimal

from evplatform.core.config import PricingConfig, TrustedPilotMarketScope, TrustedPilotPolicy
from evplatform.core.multi_book_consensus import consensus_fair_value, native_live_pricing
from evplatform.core.pricing import NoSignal, fair_value
from evplatform.integrations.base import ProviderEnvelope
from evplatform.integrations.normalization import TrustedSourceProjector, VerifiedProviderMapper


NOW = datetime(2026, 10, 28, 23, 10, tzinfo=UTC)
START = NOW - timedelta(minutes=2)


def mapper():
    config = PricingConfig(reference_profile_mode="strict",
        reference_quality_mode="trusted_provider", multi_book_consensus_enabled=True,
        trusted_max_delivery_age_ms=30000, trusted_max_indicator_age_ms=30000)
    result = VerifiedProviderMapper()
    result.research_projector = TrustedSourceProjector(lambda: config)
    return result, config


def frame(rows, *, kind="odds_update", received=NOW, sport="basketball_nba"):
    return ProviderEnvelope("parlay_ws", kind, {"data": rows}, received, 1,
        published_at=received, metadata={"sport": sport})


def game(book, *, changed=NOW, **updates):
    return {"event_id": "atl-cha-live", "home_team": "Atlanta Hawks",
        "away_team": "Charlotte Hornets", "commence_time": START.isoformat(),
        "bookmaker": book, "kind": "game", "market_key": "h2h",
        "home_ml": -120, "away_ml": 110, "last_update": changed.isoformat(),
        **updates}


def test_post_start_price_changes_make_labeled_research_fair_value_only():
    source, config = mapper()
    source(frame([game(book) for book in ("fanduel", "draftkings", "caesars")]))
    rows = source.research_updates
    assert len(rows) == 3
    market = rows[0][0]
    assert market.status == "live" and market.extensions["inplay_price_after_start"]
    assert market.extensions["event_state_verified"] is False
    assert all(item.id == market.id and quote.status == "unknown" for item, quote in rows)
    fair, _, _ = consensus_fair_value(market, [quote for _, quote in rows], config, NOW)
    assert len(fair.references) == 3
    assert not fair.execution_eligible and not fair.live_execution_allowed
    scope = TrustedPilotMarketScope(venue="prophetx", sport="basketball", league="NBA",
        market_type="moneyline", reviewed_normal_rule_id="NBA-live-fixture",
        source_normal_rule_evidence="Fixture full-game rule only")
    assert not scope.matches(market)


def test_old_prematch_price_cannot_be_replayed_after_start_or_heartbeat():
    source, _ = mapper()
    source(frame([game("fanduel", changed=START-timedelta(seconds=5))]))
    assert source.research_updates == []
    source(frame([], kind="heartbeat"))
    assert source.research_updates == []


def test_expired_inplay_price_is_not_consensus_eligible():
    source, config = mapper()
    source(frame([game(book, changed=NOW-timedelta(seconds=31))
        for book in ("fanduel", "draftkings", "caesars")]))
    rows = source.research_updates
    assert len(rows) == 3
    with pytest.raises(NoSignal, match="Fewer than two complete"):
        consensus_fair_value(rows[0][0], [quote for _, quote in rows], config, NOW)


def test_explicit_removal_withdraws_live_research_quote():
    source, _ = mapper()
    source(frame([game("fanduel")]))
    assert len(source.research_projector.active) == 1
    source(frame([{"event_id": "atl-cha-live", "bookmaker": "fanduel",
        "market_key": "h2h", "action": "remove"}], received=NOW+timedelta(seconds=1)))
    assert not source.research_projector.active
    assert len(source.research_updates) == 1
    assert source.research_updates[0][1].status == "withdrawn"


def test_delayed_inplay_frame_cannot_overwrite_newer_price():
    source, _ = mapper()
    source(frame([game("fanduel")]))
    source(frame([game("fanduel", changed=NOW-timedelta(seconds=2),
        home_ml=-300)], received=NOW+timedelta(seconds=1)))
    assert source.research_updates == []
    retained = next(iter(source.research_projector.active.values()))[1]
    assert retained.source_time == NOW


def test_inplay_rest_row_is_not_a_fresh_websocket_price():
    source, _ = mapper()
    source(ProviderEnvelope("parlay_rest", "snapshot", [{"id": "atl-cha-live",
        "sport_key": "basketball_nba", "home_team": "Atlanta Hawks",
        "away_team": "Charlotte Hornets", "commence_time": START.isoformat(),
        "bookmakers": [{"key": "fanduel", "markets": [{"key": "h2h",
            "outcomes": [{"name": "Atlanta Hawks", "price": 1.83},
                         {"name": "Charlotte Hornets", "price": 2.1}]}]}]}],
        NOW, 1, metadata={"sport": "basketball_nba"}))
    assert source.research_updates == []


def test_tennis_match_and_soccer_total_are_research_only_not_three_way_winners():
    source, _ = mapper()
    tennis = {**game("fanduel"), "event_id": "tennis-live",
        "home_team": "Player A", "away_team": "Player B"}
    source(frame([tennis], sport="tennis_atp"))
    assert len(source.research_updates) == 1
    assert source.research_updates[0][0].league == "ATP"
    assert source.research_updates[0][0].extensions["research_only"]
    soccer = {**game("draftkings"), "event_id": "soccer-live",
        "home_team": "Club A", "away_team": "Club B"}
    source(frame([soccer], sport="soccer_epl"))
    assert source.research_updates == []
    total = {**soccer, "market_key": "totals", "line": "2.5",
        "home_ml": None, "away_ml": None, "over_price": -105, "under_price": -115}
    source(frame([total], sport="soccer_epl"))
    assert len(source.research_updates) == 1
    assert source.research_updates[0][0].market_type == "total"
    assert source.research_updates[0][0].status == "live"


def test_separate_live_scope_promotes_normal_question_but_not_unknown_selection_status():
    scope = TrustedPilotMarketScope(venue="prophetx", sport="basketball", league="NBA",
        market_type="moneyline", phase="inplay", reviewed_normal_rule_id="NBA-live-fixture",
        source_normal_rule_evidence="OFFLINE reviewed full-game ordinary winner")
    policy = TrustedPilotPolicy(enabled=True, owner_approved=True, market_scope=scope,
        provider_id="parlay_ws", trusted_books=["fanduel", "draftkings", "caesars"],
        reference_weights_percent={"fanduel": Decimal(40), "draftkings": Decimal(35),
                                   "caesars": Decimal(25)},
        min_distinct_books=3, max_indicator_age_ms=3000,
        inplay_max_row_age_ms=2000,
        inplay_latency_evidence="OFFLINE measured latency fixture; not production approval",
        independent_crosscheck="two_distinct_books",
        uncertainty_margin_probability=Decimal("0.01"), max_debit_usd=Decimal(25),
        max_loss_usd=Decimal(25), pregame_only=False, recurring_authorization=True,
        maximum_native_attempts=None, automatic_followups=True,
        approval_evidence="OFFLINE fixture only")
    config = PricingConfig(reference_profile_mode="strict", reference_quality_mode="trusted_provider",
        normal_outcome_policy="winner_only_binary", multi_book_consensus_enabled=True,
        live_multi_book_enabled=True, trusted_pilot_policy=policy,
        trusted_max_delivery_age_ms=3000, trusted_max_indicator_age_ms=3000)
    source = VerifiedProviderMapper()
    source.research_projector = TrustedSourceProjector(lambda: config)
    source(frame([game(book, period="FULL") for book in policy.trusted_books]))
    rows = source.research_updates
    assert len(rows) == 3
    market, quotes = rows[0][0], [quote for _, quote in rows]
    assert market.id.startswith("reviewed:") and scope.matches(market)
    assert all(quote.normal_outcome_verified for quote in quotes)
    research, _, _ = consensus_fair_value(market, quotes, config, NOW)
    assert not research.live_execution_allowed and not research.execution_eligible
    fair = fair_value(market, quotes, native_live_pricing(market, quotes, config, NOW), NOW)
    assert not fair.live_execution_allowed
    assert any("current in-play selection status" in reason
               for reason in fair.ineligibility_reasons)
    accepted_policy = policy.model_copy(update={
        "accept_unknown_active_status": True,
        "accept_unknown_quote_observation_age": True})
    accepted_config = config.model_copy(update={"trusted_pilot_policy": accepted_policy})
    trusted = fair_value(market, quotes,
        native_live_pricing(market, quotes, accepted_config, NOW), NOW)
    assert trusted.live_execution_allowed and trusted.execution_eligible
    assert all(reference["source_observed_at"] is None and reference["status"] == "unknown"
               for reference in trusted.references)
    assert accepted_policy.model_dump(mode="json")["inplay_max_row_age_ms"] == 2000
    # Affirmative source evidence also qualifies under the restrictive policy.
    # This evidence is a fixture, not a current production observation.
    affirmed = [quote.model_copy(update={"status": "active", "market_status_verified": True,
        "observation_verified": True, "source_observed_at": NOW,
        "source_time": NOW}) for quote in quotes]
    qualified = fair_value(market, affirmed, native_live_pricing(market, affirmed, config, NOW), NOW)
    assert qualified.live_execution_allowed and qualified.execution_eligible
    stale = [quote.model_copy(update={"source_observed_at": NOW-timedelta(seconds=3),
        "source_time": NOW-timedelta(seconds=3)}) for quote in affirmed]
    old = fair_value(market, stale, native_live_pricing(market, stale, config, NOW), NOW)
    assert not old.live_execution_allowed
    suspended = [quote.model_copy(update={"status": "suspended"}) for quote in affirmed]
    with pytest.raises(NoSignal):
        native_live_pricing(market, suspended, config, NOW)


def test_inplay_source_scope_needs_distinct_latency_policy():
    scope = TrustedPilotMarketScope(venue="prophetx", sport="basketball", league="NBA",
        market_type="moneyline", phase="inplay", reviewed_normal_rule_id="NBA-live-fixture",
        source_normal_rule_evidence="OFFLINE full-game rule")
    with pytest.raises(ValueError, match="source-row age policy"):
        TrustedPilotPolicy(enabled=True, owner_approved=True, market_scope=scope,
            provider_id="parlay_ws", trusted_books=["fanduel", "draftkings", "caesars"],
            min_distinct_books=3, max_indicator_age_ms=30000,
            independent_crosscheck="two_distinct_books",
            uncertainty_margin_probability=Decimal("0.01"), max_debit_usd=Decimal(25),
            max_loss_usd=Decimal(25), pregame_only=False, recurring_authorization=True,
            maximum_native_attempts=None, automatic_followups=True,
            approval_evidence="OFFLINE fixture only")
    assert "inplay_max_row_age_ms" not in PricingConfig().trusted_pilot_policy.model_dump(mode="json")
