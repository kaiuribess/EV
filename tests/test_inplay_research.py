"""In-play prices can inform research without inheriting pregame order authority."""
from datetime import UTC, datetime, timedelta

import pytest

from evplatform.core.config import PricingConfig, TrustedPilotMarketScope
from evplatform.core.multi_book_consensus import consensus_fair_value
from evplatform.core.pricing import NoSignal
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
