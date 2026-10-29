"""Live state and native trading permission are separate from price timestamps."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
import asyncio

import httpx

from evplatform.core.config import TrustedPilotMarketScope
from evplatform.core.inplay import inplay_execution_rejections
from evplatform.core.models import GameState, Market, Settlement
from evplatform.integrations.base import ProviderEnvelope
from evplatform.integrations.parlay_live import project_live_points
from evplatform.integrations.parlay import ParlayRest


NOW = datetime(2026, 10, 29, 0, 27, tzinfo=UTC)
START = NOW - timedelta(minutes=27)


def market(*, pbp_id=None, period="FULL", status="live"):
    return Market(id="research:parlay:test", event_id="research:parlay:nba-event",
        event_name="Chicago Bulls at New York Knicks", sport="basketball", league="NBA",
        participants=("New York Knicks", "Chicago Bulls"), start_time=START,
        status=status, market_type="moneyline", outcomes=("New York Knicks", "Chicago Bulls"),
        settlement=Settlement(period=period),
        original_ids={"parlay_pbp_match_id": pbp_id} if pbp_id else {})


def frame(*, match_id="401909886", captured=None, period=1):
    captured = captured or NOW - timedelta(seconds=1)
    row = {"match_id": match_id, "sport_key": "basketball_nba", "in_play": True,
        "team_or_player_a": "New York Knicks", "team_or_player_b": "Chicago Bulls",
        "score_a": "18", "score_b": "17", "last_event_at_ms": int(captured.timestamp()*1000),
        "state": {"period": period, "clock": "4:57", "commence_time": START.isoformat()}}
    return ProviderEnvelope("parlay_rest", "live_points", {"matches": [row]}, NOW, 1)


def book(*, age_ms=500, status="active", synchronized=True, asks=(1,)):
    return SimpleNamespace(status=status, synchronized=synchronized, asks=asks,
                           age_ms=lambda now: age_ms)


def test_pbp_match_is_research_association_without_explicit_id_crosswalk():
    states = project_live_points(frame(), [market()])
    assert len(states) == 1
    assert states[0].score == {"New York Knicks": 18, "Chicago Bulls": 17}
    assert states[0].period == "1" and states[0].source_match_id == "401909886"
    assert not states[0].identity_verified
    assert "attributable" in "; ".join(inplay_execution_rejections(market(), states[0], book(),
        now=NOW, max_state_age_ms=2000, max_book_age_ms=2000,
        native_permission=True, native_permission_source="active live catalog"))


def test_explicit_crosswalk_live_status_and_native_depth_all_required():
    m = market(pbp_id="401909886")
    state = project_live_points(frame(), [m])[0]
    assert state.identity_verified and state.source_time == NOW - timedelta(seconds=1)
    assert not inplay_execution_rejections(m, state, book(), now=NOW,
        max_state_age_ms=2000, max_book_age_ms=2000,
        native_permission=True, native_permission_source="active live catalog")
    assert "permission" in "; ".join(inplay_execution_rejections(m, state, book(),
        now=NOW, max_state_age_ms=2000, max_book_age_ms=2000))
    assert "stale" in "; ".join(inplay_execution_rejections(m, state, book(age_ms=3000),
        now=NOW, max_state_age_ms=2000, max_book_age_ms=2000,
        native_permission=True, native_permission_source="active live catalog"))


def test_suspended_native_or_delayed_game_state_blocks():
    m = market(pbp_id="401909886")
    state = project_live_points(frame(captured=NOW-timedelta(seconds=4)), [m])[0]
    reasons = inplay_execution_rejections(m, state, book(status="suspended"),
        now=NOW, max_state_age_ms=2000, max_book_age_ms=2000,
        native_permission=True, native_permission_source="active live catalog")
    assert any("stale" in reason for reason in reasons)
    assert any("active synchronized" in reason for reason in reasons)


def test_pbp_ambiguous_or_wrong_start_does_not_establish_identity():
    assert not project_live_points(frame(), [market(), market().model_copy(update={"event_id": "duplicate"})])
    assert not project_live_points(frame(), [market().model_copy(update={"start_time": START+timedelta(minutes=5)})])


def test_pregame_scope_does_not_inherit_live_permission():
    scope = TrustedPilotMarketScope(venue="prophetx", sport="basketball", league="NBA",
        market_type="moneyline", reviewed_normal_rule_id="NBA-rule-v1",
        source_normal_rule_evidence="Reviewed normal winner and overtime")
    assert scope.phase == "pregame" and not scope.matches(market())
    assert "phase" not in scope.model_dump(mode="json")
    assert TrustedPilotMarketScope.model_validate(scope.model_dump(mode="json")).phase == "pregame"


def test_live_points_uses_one_authenticated_bounded_snapshot_request():
    seen = []
    def respond(request):
        seen.append((request.url.path, request.headers.get("X-API-Key")))
        return httpx.Response(200, json={"sport_key": "basketball_nba", "matches": []})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            provider = ParlayRest({"enabled": True, "sports": ["basketball_nba"],
                "session_request_budget": 1}, client=client, credential="fixture-secret")
            result = await provider.fetch_live_points("basketball_nba")
            assert result.kind == "snapshot" and result.payload["matches"] == []
            assert provider.quota.stats()["reserved_cost"] == 1
    asyncio.run(run())
    assert seen == [("/v1/sports/basketball_nba/live/points", "fixture-secret")]
