"""Bounded native subscriptions prefer source-rich exact identity leads."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from evplatform.plugins.external import NativeMonitor

NOW = datetime(2026, 10, 23, 18, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_polymarket_exact_source_market_outside_baseline_enters_stream_scope(monkeypatch):
    monkeypatch.setattr("evplatform.plugins.external.utcnow", lambda: NOW)
    calls = []
    events = []
    for index in range(10):
        slug = f"game-{index}"
        market = {"id": index, "slug": slug, "sportsMarketTypeV2": "SPORTS_MARKET_TYPE_MONEYLINE",
                  "status": "MARKET_STATUS_OPEN", "active": True, "closed": False,
                  "hidden": False, "gameStartTime": (NOW+timedelta(hours=index+1)).isoformat(),
                  "description": "Full game includes overtime", "question": f"Game {index}",
                  "feeCoefficient": "0.0695", "orderPriceMinTickSize": "0.01",
                  "marketSides": []}
        events.append({"slug": f"event-{index}", "active": True,
                       "closed": False, "markets": [market]})

    class Client:
        async def discover_league_events(self, league):
            calls.append(("catalog", league))
            return [{"league": {"slug": league}, "events": events, "bounded": False}]

        async def public_market_metadata(self, slug):
            raise AssertionError("Complete listed metadata needs no per-slug request")

        async def fetch_snapshot(self, slug):
            calls.append(("snapshot", slug))
            return {}

    async def exact_matches():
        # This is the existing read-only matcher, after it sees the full
        # bounded discovery pool, and before the stream limit is applied.
        assert len(monitor.research_catalog_rows) == 10
        assert any(row["slug"] == "game-9" for row in monitor.research_catalog_rows)
        return ["game-9"]

    monitor = SimpleNamespace(client=Client(), context=SimpleNamespace(config={
        "discovery_leagues": ["nba"], "max_discovered_markets": 8}),
        discovery_status="disabled", discovered_market_ids=[],
        _refresh_research_matches=exact_matches)
    await NativeMonitor._refresh_polymarket_discovery(monitor)
    assert monitor.discovered_market_ids[0] == "game-9"
    assert len(monitor.discovered_market_ids) == 8
    assert sum(call[0] == "snapshot" for call in calls) == 2


@pytest.mark.asyncio
async def test_prophetx_source_rich_later_event_gets_bounded_event_slot(monkeypatch):
    monkeypatch.setattr("evplatform.plugins.external.utcnow", lambda: NOW)
    async def no_matching_side_effects(_):
        return []
    monkeypatch.setattr(NativeMonitor, "_refresh_research_matches", no_matching_side_effects)
    first = {"event_id": 101, "name": "Unrelated at Game", "status": "not_started",
             "scheduled": (NOW+timedelta(hours=1)).isoformat()}
    target = {"event_id": 202, "name": "Away at Home", "status": "not_started",
              "scheduled": (NOW+timedelta(hours=2)).isoformat()}
    calls = []

    class Client:
        async def discover_events(self, *, tournament_id):
            return {"data": {"sport_events": [first, target]}}

        async def discover_markets(self, event_ids):
            calls.append(event_ids)
            return {"data": {event_ids[0]: []}}

    source = SimpleNamespace(event_name="Away at Home", league="NBA", start_time=NOW+timedelta(hours=2))
    engine = SimpleNamespace(consensus_estimates={"source": {"independent_book_count": 3}},
                             markets={"source": source})
    monitor = SimpleNamespace(client=Client(), engine=engine,
        context=SimpleNamespace(config={"discovery_tournament_ids": [132],
            "max_discovered_events": 1, "max_discovered_prop_subscriptions": 0}),
        discovery_status="disabled", discovered_event_ids=[])
    await NativeMonitor._refresh_discovery(monitor)
    assert monitor.discovery_status == "current"
    assert monitor.discovered_event_ids == ["202"]
    assert calls == [["202"]]


@pytest.mark.asyncio
async def test_prophetx_exact_prop_subtype_wins_single_subscription_slot(monkeypatch):
    monkeypatch.setattr("evplatform.plugins.external.utcnow", lambda: NOW)
    async def no_matching_side_effects(_):
        return []
    monkeypatch.setattr(NativeMonitor, "_refresh_research_matches", no_matching_side_effects)
    event = {"event_id": 202, "name": "Away at Home", "status": "not_started",
             "scheduled": (NOW+timedelta(hours=2)).isoformat()}
    def prop(subtype, statistic):
        return {"id": subtype, "sub_type": subtype, "status": "active",
                "player_id": "Player A", "statistic": statistic,
                "period": "full_game", "line": "24.5", "selections": [[{"strike_id": "x"}]]}

    class Client:
        async def discover_events(self, *, tournament_id):
            return {"data": {"sport_events": [event]}}

        async def discover_markets(self, event_ids):
            return {"data": {"202": [prop("player_rebounds", "rebounds"),
                                     prop("player_points", "points")]}}

    source = SimpleNamespace(event_name="Away at Home", league="NBA",
        start_time=NOW+timedelta(hours=2), market_type="player_prop",
        player_id="Player A", statistic="points", line="24.5")
    engine = SimpleNamespace(consensus_estimates={"source": {"independent_book_count": 3}},
                             markets={"source": source})
    monitor = SimpleNamespace(client=Client(), engine=engine,
        context=SimpleNamespace(config={"discovery_tournament_ids": [132],
            "max_discovered_events": 1, "max_discovered_prop_subscriptions": 1}),
        discovery_status="disabled", discovered_event_ids=[])
    await NativeMonitor._refresh_discovery(monitor)
    assert monitor.discovered_prop_subtypes == {"202": ["player_points"]}


@pytest.mark.asyncio
async def test_prophetx_missing_rest_prop_selections_still_get_research_channel(monkeypatch):
    monkeypatch.setattr("evplatform.plugins.external.utcnow", lambda: NOW)
    async def no_matching_side_effects(_):
        return []
    monkeypatch.setattr(NativeMonitor, "_refresh_research_matches", no_matching_side_effects)
    event = {"event_id": 202, "name": "Away at Home", "status": "not_started",
             "scheduled": (NOW+timedelta(hours=2)).isoformat()}

    class Client:
        async def discover_sports(self, *, active_events_only):
            return {"data": {"tournaments": [{"id": 31, "name": "NFL"}]}}

        async def discover_events(self, *, tournament_id):
            assert tournament_id == 31
            return {"data": {"sport_events": [event]}}

        async def discover_markets(self, event_ids):
            return {"data": {"202": [
                {"id": 300, "sub_type": "player_total_receptions", "status": "active",
                 "selections": [[{"strike_id": "receptions"}]]},
                {"id": 301, "sub_type": "player_total_receiving_yards", "status": "active",
                 "selections": None}]}}

    source = SimpleNamespace(event_name="Away at Home", league="NFL",
        start_time=NOW+timedelta(hours=2), market_type="player_prop",
        player_id="player-1", statistic="receiving_yards", line="54.5")
    engine = SimpleNamespace(consensus_estimates={"source": {"independent_book_count": 3}},
                             markets={"source": source})
    monitor = SimpleNamespace(client=Client(), engine=engine,
        context=SimpleNamespace(config={"discovery_tournament_ids": [],
            "discovery_leagues": ["NFL"], "max_discovered_events": 1,
            "max_discovered_prop_subscriptions": 1}),
        discovery_status="disabled", discovered_event_ids=[])
    await NativeMonitor._refresh_discovery(monitor)
    assert monitor.discovered_prop_subtypes == {"202": ["player_total_receiving_yards"]}
