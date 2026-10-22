"""Event candidates stay separate from wager/settlement approval."""
from copy import deepcopy

import pytest

from evplatform.integrations.event_crosswalk import event_candidates


NATIVE = {"events": [{"event_id": "1700099993", "name": "Golden State Warriors at Los Angeles Lakers",
                      "scheduled": "2026-10-22T02:00:00Z", "status": "not_started"}],
          "truncated": False}
SOURCE = {"items": [
    {"identity": {"provider": "parlay_rest", "event_id": "parlay-game", "bookmaker": "pinnacle",
                   "context": {"event_sport": "basketball_nba",
                               "participants": ["Los Angeles Lakers", "Golden State Warriors"],
                               "scheduled_start": "2026-10-22T02:00:00Z"}}},
    {"identity": {"provider": "oddize_rest", "event_id": "202610210LAL", "bookmaker": "circa",
                   "context": {"event_sport": "nba", "participants": ["LAL", "GSW"],
                               "scheduled_start": "2026-10-21T22:00:00-04:00"}}},
], "collection": {"truncated": False}}


def test_exact_event_candidates_preserve_source_ids_without_authorizing_market():
    result = event_candidates(NATIVE, SOURCE)
    assert {(m["provider"], m["source_event_id"]) for m in result["matches"]} == {
        ("parlay_rest", "parlay-game"), ("oddize_rest", "202610210LAL")}
    assert all(m["native_event_id"] == "1700099993" and m["start_delta_seconds"] == 0
               and m["candidate_only"] for m in result["matches"])
    assert result["event_identity_verified"] is False
    assert result["market_mapping_verified"] is False
    assert result["settlement_equivalence_verified"] is False
    assert result["execution_authorized"] is False


def test_unknown_team_start_or_conflicting_source_identity_never_matches():
    source = deepcopy(SOURCE)
    source["items"][1]["identity"]["context"]["participants"] = ["LAL", "UNKNOWN"]
    source["items"].append({"identity": {**source["items"][0]["identity"],
        "context": {"event_sport": "basketball_nba",
                    "participants": ["Los Angeles Lakers", "Golden State Warriors"],
                    "scheduled_start": "2026-10-22T03:00:00Z"}}})
    result = event_candidates(NATIVE, source)
    assert result["matches"] == [] and result["conflicting_source_events"] == 1
    source = deepcopy(SOURCE)
    source["items"][0]["identity"]["context"]["scheduled_start"] = "2026-10-22T02:10:00Z"
    assert len(event_candidates(NATIVE, source)["matches"]) == 1


def test_duplicate_native_fixture_is_ambiguous_not_assumed_equivalent():
    native = deepcopy(NATIVE)
    native["events"].append({**native["events"][0], "event_id": "1700099994"})
    result = event_candidates(native, SOURCE)
    assert result["matches"] == [] and result["ambiguous_source_events"] == 2
    with pytest.raises(ValueError):
        event_candidates(NATIVE, SOURCE, max_start_delta_seconds=901)
