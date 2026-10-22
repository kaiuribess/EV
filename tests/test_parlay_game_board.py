"""The dedicated Parlay baseline cannot turn a bounded sample into a full board."""
from __future__ import annotations

import json
from datetime import UTC, datetime
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
    assert query["bookmakers"] == ["pinnacle,fanduel,draftkings,caesars,betonline,bet365"]
    assert query["kinds"] == ["game"]
    assert query["markets"] == ["h2h,spreads,totals"]
    assert query["limit"] == ["1000"]


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
        params={"limit": 5000, "includeLinks": True,
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
