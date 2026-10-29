"""Strict projection of Parlay PBP state, separate from sportsbook prices.

The PBP match ID is not the odds event ID. A unique team/start match gives a
research association; only an explicit source crosswalk verifies identity.
"""
from __future__ import annotations

from datetime import datetime, timezone

from ..core.models import GameState, Market
from .base import ProviderEnvelope

SPORT_LEAGUE_KEYS = {
    "NBA": "basketball_nba", "NFL": "americanfootball_nfl",
    "MLB": "baseball_mlb", "NHL": "icehockey_nhl",
    "EPL": "soccer_epl", "ATP": "tennis_atp",
}


def _clock(value):
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return result.astimezone(timezone.utc) if result.utcoffset() is not None else None


def project_live_points(frame: ProviderEnvelope, markets: list[Market]) -> list[GameState]:
    """Keep actual PBP capture time; reject ambiguous or incomplete fixtures."""
    payload = frame.payload
    if not isinstance(payload, dict) or not isinstance(payload.get("matches"), list):
        return []
    result = []
    seen_events = set()
    for row in payload["matches"]:
        if not isinstance(row, dict) or row.get("in_play") is not True:
            continue
        match_id, state = row.get("match_id"), row.get("state")
        if not isinstance(match_id, str) or not isinstance(state, dict):
            continue
        start = _clock(state.get("commence_time"))
        captured = row.get("last_event_at_ms")
        period = state.get("period")
        teams = (row.get("team_or_player_a"), row.get("team_or_player_b"))
        if (start is None or type(captured) is not int or captured <= 0
                or not isinstance(period, (int, str)) or not str(period).strip()
                or not all(isinstance(team, str) and team for team in teams)
                or teams[0] == teams[1]):
            continue
        try:
            source_time = datetime.fromtimestamp(captured / 1000, timezone.utc)
            score = {str(team): int(row[key]) for team, key in zip(teams, ("score_a", "score_b"))}
        except (ValueError, OverflowError, TypeError, OSError):
            continue
        candidates = {market.event_id: market for market in markets
            if market.status == "live" and market.start_time == start
            and set(market.participants) == set(teams)
            and SPORT_LEAGUE_KEYS.get(market.league) == row.get("sport_key")}
        if len(candidates) != 1:
            continue
        event_id, market = next(iter(candidates.items()))
        if event_id in seen_events:
            # Two PBP matches mapped to one event: no trustworthy state.
            result = [item for item in result if item.event_id != event_id]
            continue
        seen_events.add(event_id)
        result.append(GameState(event_id=event_id, status="live", score=score,
            clock=state.get("clock") if isinstance(state.get("clock"), str) else None,
            period=str(period), source_match_id=match_id,
            identity_verified=market.original_ids.get("parlay_pbp_match_id") == match_id,
            source_time=source_time, received_at=frame.received_at))
    return result
