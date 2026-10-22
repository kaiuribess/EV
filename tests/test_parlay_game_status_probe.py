"""The bounded game probe reports only evidence the normalized engine accepts."""
import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path

from evplatform.integrations.base import ProviderEnvelope


spec = importlib.util.spec_from_file_location(
    "parlay_game_status_probe", Path(__file__).parents[1] / "scripts/probe_parlay_game_status.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def _board(*, age_seconds=2, headers=None):
    now = datetime(2026, 10, 22, 17, tzinfo=UTC)
    event = {"id": "event", "home_team": "Home", "away_team": "Away", "bookmakers": [
        {"key": book, "markets": [{"key": "h2h",
            "last_observed": (now-timedelta(seconds=age_seconds)).isoformat(),
            "last_update_type": "collector_observation",
            "outcomes": [
                {"name": "Home", "price": 1.8,
                 **({"link": "https://sportsbook.fanduel.com/home"} if book == "fanduel" else {})},
                {"name": "Away", "price": 2.1,
                 **({"link": "https://sportsbook.fanduel.com/away"} if book == "fanduel" else {})},
            ]}]} for book in ("fanduel", "pinnacle")],
    }
    return ProviderEnvelope("parlay_rest", "snapshot", [event], now, 1,
                            metadata={"headers": headers or {}})


def test_probe_keeps_recent_clock_and_native_status_separate():
    result = probe.summarize(_board())
    assert result["offer_count"] == 2
    assert result["by_book"]["fanduel"] == {
        "offers": 1, "two_sided": 1, "clock_verified": 1,
        "status_verified": 1, "both_verified": 1}
    assert result["by_book"]["pinnacle"]["clock_verified"] == 1
    assert result["by_book"]["pinnacle"]["status_verified"] == 0
    assert result["both_status_and_recent_observation_count"] == 1


def test_probe_does_not_count_stale_or_partial_response_as_current():
    stale = probe.summarize(_board(age_seconds=121))
    assert stale["by_book"]["fanduel"]["status_verified"] == 1
    assert stale["by_book"]["fanduel"]["clock_verified"] == 0
    assert stale["both_status_and_recent_observation_count"] == 0
    partial = probe.summarize(_board(headers={"x-result-truncated": "true"}))
    assert partial["both_status_and_recent_observation_count"] == 0
