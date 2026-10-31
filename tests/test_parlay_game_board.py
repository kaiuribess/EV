"""The dedicated Parlay baseline cannot turn a bounded sample into a full board."""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import pytest
from pydantic import ValidationError

from evplatform.integrations import PROVIDER_FACTORIES
from evplatform.integrations.base import ProviderEnvelope
from evplatform.integrations.normalization import VerifiedProviderMapper, extract_offers
from evplatform.integrations.parlay import (
    ParlayConfig, ParlayGameBoardConfig, ParlayGameBoardWebSocket, ParlayRest,
    ParlayWebSocket,
)
from evplatform.plugins.external import ProviderBridge


@pytest.mark.parametrize("override", [
    {"bookmakers": []},
    {"bookmakers": ["pinnacle", "pinnacle"]},
    {"markets": ["h2h", "spreads"]},
    {"markets": ["h2h", "spreads", "totals", "player_points"]},
    {"stream_kinds": ["game", "prop"]},
    {"include_props": True},
])
def test_baseline_config_rejects_incomplete_or_broad_filters(override):
    with pytest.raises(ValidationError):
        ParlayGameBoardConfig.model_validate(override)


def test_baseline_is_independent_provider_with_exact_documented_filters():
    assert PROVIDER_FACTORIES["parlay_game_ws"] is ParlayGameBoardWebSocket
    provider = ParlayGameBoardWebSocket({"sports": ["basketball_nba"]}, credential="")
    query = parse_qs(urlsplit(provider.stream_url("basketball_nba")).query)
    assert query["bookmakers"] == ["pinnacle,fanduel,draftkings,caesars,bet365"]
    assert query["kinds"] == ["game"]
    assert query["markets"] == ["h2h,spreads,totals"]
    assert query["limit"] == ["1000"]
    # snapshot=complete is a separately restricted native-prop scope. The
    # complete game board is selected by these explicit book/market filters.
    assert "snapshot" not in query
    assert urlsplit(provider.stream_url("basketball_nba")).path == "/v1/ws/odds/basketball_nba"


@pytest.mark.asyncio
async def test_complete_game_board_reconnect_refreshes_baseline_without_rest():
    connections = []

    class Socket:
        def __init__(self, number):
            self.frames = [
                {"type": "connected", "tier": "business"},
                {"type": "initial_state", "snapshot_scope": "current_game_board",
                 "snapshot_complete": True, "truncated": False,
                 "data": [{"bookmaker": "draftkings", "last_update": f"fixture-{number}"}]},
            ]

        async def recv(self):
            if self.frames:
                return json.dumps(self.frames.pop(0))
            await asyncio.Event().wait()

        async def close(self):
            pass

    class Connection:
        def __init__(self, number):
            self.socket = Socket(number)

        async def __aenter__(self):
            return self.socket

        async def __aexit__(self, *ignored):
            return False

    def factory(*args, **kwargs):
        connections.append((args, kwargs))
        return Connection(len(connections))

    provider = ParlayGameBoardWebSocket({"sports": ["basketball_nba"]},
        credential="fixture-secret", ws_factory=factory)
    provider.config = provider.config.model_copy(update={"baseline_refresh_seconds": .05})
    provider._subscriptions.add("basketball_nba")
    queue = asyncio.Queue()
    task = asyncio.create_task(provider._run_sport("basketball_nba", queue))
    try:
        frames = [await asyncio.wait_for(queue.get(), 2) for _ in range(5)]
        assert [frame.kind for frame in frames] == [
            "connected", "initial_state", "feed_invalidated", "connected", "initial_state"]
        assert frames[1].snapshot_complete and frames[4].snapshot_complete
        assert frames[1].payload["data"][0]["last_update"] != frames[4].payload["data"][0]["last_update"]
        assert len(connections) == 2 and provider.reconnects == 1
        assert all("fixture-secret" not in args[0] for args, _ in connections)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_betonline_is_requested_only_for_supported_pregame_game_sports():
    books = ["fanduel", "betonline"]
    rest = ParlayRest({"bookmakers": books}, credential="")
    rest.request = AsyncMock(return_value=ProviderEnvelope(
        "parlay_rest", "snapshot", [], datetime.now(UTC), 1))
    await rest.fetch_snapshot("basketball_nba")
    assert rest.request.await_args.kwargs["params"]["bookmakers"] == "fanduel"
    await rest.fetch_snapshot("americanfootball_nfl")
    assert rest.request.await_args.kwargs["params"]["bookmakers"] == "fanduel,betonline"
    await rest.fetch_props("americanfootball_nfl")
    assert rest.request.await_args.kwargs["params"]["bookmakers"] == "fanduel"
    props_stream = ParlayWebSocket({"bookmakers": books, "stream_kinds": ["prop"]}, credential="")
    assert parse_qs(urlsplit(props_stream.stream_url("americanfootball_nfl")).query)["bookmakers"] == ["fanduel"]
    game_stream = ParlayGameBoardWebSocket({"bookmakers": books}, credential="")
    assert parse_qs(urlsplit(game_stream.stream_url("baseball_mlb")).query)["bookmakers"] == ["fanduel,betonline"]


@pytest.mark.parametrize("limit", [0, 10001])
def test_props_page_limit_rejects_undocumented_values(limit):
    with pytest.raises(ValidationError):
        ParlayConfig(props_limit=limit)


async def test_rest_props_supports_large_scoped_page_without_extra_charge():
    provider = ParlayRest({"props_limit": 5000, "bookmakers": ["fanduel"]}, credential="")
    provider.request = AsyncMock(return_value=ProviderEnvelope(
        "parlay_rest", "snapshot", [], datetime.now(UTC), 1))
    await provider.fetch_props("basketball_nba")
    provider.request.assert_awaited_once_with(
        "/v1/sports/basketball_nba/props",
        params={"limit": 5000, "includeLinks": True, "includeSids": True,
                "bookmakers": "fanduel"}, cost=3,
    )


async def test_rest_props_can_explicitly_filter_price_change_age_without_changing_ws_filter():
    provider = ParlayRest({"props_max_age_seconds": 120, "max_age_seconds": 30}, credential="")
    provider.request = AsyncMock(return_value=ProviderEnvelope(
        "parlay_rest", "snapshot", [], datetime.now(UTC), 1))
    await provider.fetch_props("basketball_nba", markets="player_points")
    params = provider.request.await_args.kwargs["params"]
    assert params["maxAgeSec"] == 120
    assert params["markets"] == "player_points"
    stream = ParlayWebSocket({"props_max_age_seconds": 120, "max_age_seconds": 30}, credential="")
    query = parse_qs(urlsplit(stream.stream_url("basketball_nba")).query)
    assert query["max_age_s"] == ["30"]


def test_complete_flag_needs_provider_confirmation_and_never_certifies_quote():
    provider = ParlayGameBoardWebSocket(credential="")
    row = {"event_id": "game-1", "home_team": "A", "away_team": "B",
           "bookmaker": "pinnacle", "kind": "game", "market_key": "h2h",
           "home_ml": -115, "away_ml": 105, "last_update": "2026-10-21T18:00:00Z"}
    frame = {"type": "initial_state", "snapshot_scope": "current_game_board",
             "snapshot_complete": True, "truncated": False, "data": [row]}
    complete = provider.parse_frame(json.dumps(frame), has_snapshot=False)
    assert complete.snapshot_complete is True
    offer, = extract_offers(complete)
    assert offer.provider == "parlay_game_ws"
    assert offer.market_key == "h2h"
    assert not offer.market_status_verified
    assert not offer.observation_verified
    mapper = VerifiedProviderMapper()
    assert mapper(complete) == []
    assert len(mapper.quarantine) == 1

    for change in ({"truncated": True}, {"snapshot_scope": "recent_rows"},
                   {"snapshot_complete": False}):
        bounded = provider.parse_frame(json.dumps({**frame, **change}), has_snapshot=False)
        assert bounded.snapshot_complete is False


@pytest.mark.asyncio
async def test_rest_startup_deduplicates_only_complete_identical_game_stream_scope():
    provider = ParlayRest({"deduplicate_complete_game_stream": True,
                           "startup_stream_wait_seconds": 0}, credential="")
    frame = ProviderEnvelope("parlay_rest", "snapshot", [], datetime.now(UTC), 1)
    provider.fetch_snapshot = AsyncMock(return_value=frame)
    provider.fetch_props = AsyncMock(return_value=frame)
    provider.game_stream_state = lambda _: (True, True)
    assert len([row async for row in provider._poll_sport("basketball_nba")]) == 1
    provider.fetch_snapshot.assert_not_awaited()
    provider.fetch_props.assert_awaited_once()
    provider.game_stream_state = lambda _: (True, False)
    assert len([row async for row in provider._poll_sport("basketball_nba")]) == 2
    provider.fetch_snapshot.assert_awaited_once()


def test_rest_stream_dedup_needs_exact_book_market_region_and_sport_scope():
    rest = ParlayRest({"sports": ["basketball_nba"], "bookmakers": ["fanduel"],
        "markets": ["h2h", "spreads", "totals"]}, credential="")
    stream = ParlayGameBoardWebSocket({"sports": ["basketball_nba"],
        "bookmakers": ["fanduel"], "markets": ["h2h", "spreads", "totals"]}, credential="")
    stream._ready_sports.add("basketball_nba")
    stream._complete_baseline_sports.add("basketball_nba")
    bridge = ProviderBridge.__new__(ProviderBridge)
    bridge.client = rest
    bridge.engine = SimpleNamespace(registry=SimpleNamespace(records={
        "parlay_game_ws": SimpleNamespace(enabled=True,
            plugin=SimpleNamespace(client=stream))}))
    assert bridge._parlay_game_stream_state("basketball_nba") == (True, True)
    assert bridge._parlay_game_stream_state("americanfootball_nfl") == (False, False)
    stream.config.bookmakers = ["draftkings"]
    assert bridge._parlay_game_stream_state("basketball_nba") == (False, False)
