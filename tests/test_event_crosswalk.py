"""Event candidates stay separate from wager/settlement approval."""
from copy import deepcopy

import pytest

from evplatform.integrations.event_crosswalk import event_candidates, moneyline_candidates


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


def _market_review():
    return {"truncated": False, "markets": [{"id": "219", "name": "Moneyline",
        "type": "moneyline", "sub_type": "moneyline", "status": "active", "period": None,
        "selection_shape": "nested", "selection_review_truncated": False,
        "selection_review": [
            {"group": 0, "name": "Los Angeles Lakers", "strike_id": "lakers", "outcome_id": "4"},
            {"group": 0, "name": "Los Angeles Lakers", "strike_id": "lakers", "outcome_id": "4"},
            {"group": 1, "name": "Golden State Warriors", "strike_id": "warriors", "outcome_id": "5"},
        ]}]}


def _source_moneylines():
    source = deepcopy(SOURCE)
    source["items"][0].update(signature="a" * 64, observation={
        "decimal_odds": {"home": "1.5", "away": "2.7"},
        "source_observed_at": None, "observation_verified": False,
        "market_status_verified": False})
    source["items"][0]["identity"].update(market_key="h2h", player_key=None,
        line=None, period=None, outcome_points={"home": None, "away": None})
    source["items"][1].update(signature="b" * 64, observation={
        "decimal_odds": {"LAL": "1.6", "GSW": "2.5"},
        "source_observed_at": "2026-10-22T01:00:00Z", "observation_verified": True,
        "market_status_verified": False})
    source["items"][1]["identity"].update(market_key="ml", player_key=None,
        line=None, period=None, outcome_points={"LAL": None, "GSW": None})
    return source


def test_moneyline_review_preserves_exact_strikes_and_source_gaps_without_mapping():
    source = _source_moneylines()
    result = moneyline_candidates(event_candidates(NATIVE, source), source, _market_review(),
                                  native_event_id="1700099993")
    assert result["candidate_count"] == 2
    assert {row["bookmaker"] for row in result["candidates"]} == {"pinnacle", "circa"}
    assert result["candidates"][0]["native_outcomes"]["Los Angeles Lakers"]["strike_id"] == "lakers"
    assert result["candidates"][0]["source_market_status_verified"] is False
    assert result["market_mapping_verified"] is False
    assert result["settlement_equivalence_verified"] is False
    assert result["execution_authorized"] is False


@pytest.mark.parametrize("change", [
    lambda s, m: s["items"][0]["identity"].update(line="1.5"),
    lambda s, m: s["items"][0]["identity"].update(period="first_half"),
    lambda s, m: s["items"][0]["identity"].update(outcome_points={"home": "1.5", "away": None}),
    lambda s, m: s["items"][0]["observation"].update(decimal_odds={"home": "1.5"}),
    lambda s, m: m["markets"][0].update(status="suspended"),
    lambda s, m: m["markets"][0].update(selection_review_truncated=True),
    lambda s, m: m["markets"][0]["selection_review"][0].update(strike_id="other"),
])
def test_moneyline_review_rejects_changed_or_ambiguous_shape(change):
    source, market = _source_moneylines(), _market_review()
    change(source, market)
    result = moneyline_candidates(event_candidates(NATIVE, source), source, market,
                                  native_event_id="1700099993")
    assert not any(row["provider"] == "parlay_rest" for row in result["candidates"])
