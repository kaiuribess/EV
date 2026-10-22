"""A native strike lead is not a market mapping or trade approval."""
from copy import deepcopy

import httpx

from evplatform.integrations.prophetx_event_crosswalk import fixture_candidates
from evplatform.integrations.event_crosswalk import moneyline_candidates
from scripts.review_prophetx_nba import collect_market_catalogs


EVENTS = {"read_only": True, "truncated": False, "events": [
    {"event_id": "1700099998", "name": "Cleveland Cavaliers at Philadelphia 76ers",
     "scheduled": "2026-10-22T23:00:00Z", "status": "not_started"}]}
MARKETS = {"1700099998": {"read_only": True, "truncated": False,
    "filter_sub_type": "moneyline", "markets": [
    {"id": "219", "name": "Moneyline", "type": "moneyline", "sub_type": "moneyline",
     "status": "active", "selection_shape": "nested", "selection_count": 2,
     "selection_review_truncated": False, "selection_review": [
        {"group": 0, "strike_id": "a"*32, "outcome_id": "4", "name": "Philadelphia 76ers"},
        {"group": 0, "strike_id": "a"*32, "outcome_id": "4", "name": "Philadelphia 76ers"},
        {"group": 1, "strike_id": "b"*32, "outcome_id": "5", "name": "Cleveland Cavaliers"}]}
]}}
SOURCE = {"items": [{"identity": {"provider": "oddize_rest", "event_id": "game-1",
    "bookmaker": "circa", "context": {"event_sport": "nba", "participants": ["PHI", "CLE"],
    "scheduled_start": "2026-10-22T23:00:00Z"}}}], "collection": {"truncated": False}}


def test_two_strikes_link_to_exact_fixture_without_trade_approval():
    result = fixture_candidates(EVENTS, MARKETS, SOURCE)
    assert result["match_count"] == 1
    match = result["matches"][0]
    assert match["native_market_id"] == "219"
    assert {row["team"] for row in match["native_outcomes"]} == {
        "Philadelphia 76ers", "Cleveland Cavaliers"}
    assert match["native_start_informational"] and not match["schedule_verified"]
    assert match["candidate_only"] and not match["settlement_equivalence_verified"]
    assert result["native_market_catalogs_missing"] == 0
    assert not result["execution_authorized"]


def test_oddspapi_scope_and_cross_provider_start_conflict():
    source = deepcopy(SOURCE)
    extra = deepcopy(source["items"][0])
    extra["identity"].update(provider="oddspapi_rest", event_id="fixture-1", bookmaker="pinnacle")
    source["items"][0]["identity"]["context"]["scheduled_start"] = "2026-10-22T22:58:00Z"
    extra["identity"]["context"] = {"event_sport": 11, "tournament_id": 132,
        "event_league": "NBA", "participants": ["Philadelphia 76ers", "Cleveland Cavaliers"],
        "scheduled_start": "2026-10-22T23:02:00Z"}
    source["items"].append(extra)
    result = fixture_candidates(EVENTS, MARKETS, source)
    assert result["match_count"] == 2
    assert result["cross_provider_start_conflicts"] == 1
    assert all(item["source_start_conflict"] for item in result["matches"])
    source["items"][1]["identity"]["context"]["scheduled_start"] = "2026-10-22T23:02:01Z"
    result = fixture_candidates(EVENTS, MARKETS, source)
    assert result["match_count"] == 1
    assert result["schedule_disagreements"][0]["difference_seconds"] == 121
    assert result["matches"][0]["source_start_conflict"] is True
    source["items"][1]["identity"]["context"]["tournament_id"] = 133
    assert fixture_candidates(EVENTS, MARKETS, source)["match_count"] == 1


def test_bad_or_truncated_native_outcome_cannot_become_candidate():
    for field, value in (("selection_review_truncated", True), ("status", "suspended"),
                         ("selection_count", 1), ("selection_shape", "unknown")):
        markets = deepcopy(MARKETS)
        markets["1700099998"]["markets"][0][field] = value
        assert fixture_candidates(EVENTS, markets, SOURCE)["match_count"] == 0
    markets = deepcopy(MARKETS)
    markets["1700099998"]["markets"][0]["selection_review"][2]["strike_id"] = "a"*32
    assert fixture_candidates(EVENTS, markets, SOURCE)["match_count"] == 0


def test_exact_two_sided_source_moneyline_remains_review_only():
    source = deepcopy(SOURCE)
    item = source["items"][0]
    item["signature"] = "c" * 64
    item["identity"].update(market_key="ml", player_key=None, line=None, period=None,
                            outcome_points={"PHI": None, "CLE": None})
    item["observation"] = {"decimal_odds": {"PHI": "1.8", "CLE": "2.1"},
                           "source_observed_at": "2026-10-22T22:00:00Z",
                           "observation_verified": True, "market_status_verified": False}
    report = fixture_candidates(EVENTS, MARKETS, source)
    review = moneyline_candidates(report, source, MARKETS["1700099998"],
                                  native_event_id="1700099998")
    assert review["candidate_count"] == 1
    lead = review["candidates"][0]
    assert lead["source_outcomes"] == {
        "PHI": "Philadelphia 76ers", "CLE": "Cleveland Cavaliers"}
    assert lead["source_market_status_verified"] is False
    assert review["market_mapping_verified"] is False
    assert review["settlement_equivalence_verified"] is False
    assert review["execution_authorized"] is False


def test_documented_oddspapi_nba_moneyline_outcomes_only():
    source = deepcopy(SOURCE)
    item = source["items"][0]
    item["identity"].update(provider="oddspapi_rest", bookmaker="pinnacle",
                            market_key="111", player_key=None, line="0", period="result",
                            outcome_points={"111": None, "112": None})
    item["identity"]["context"].update(event_sport=11, tournament_id=132, event_league="NBA",
        participants=["Philadelphia 76ers", "Cleveland Cavaliers"],
        market_catalog={"marketType": "moneyline", "marketName": "Winner (incl. overtime)",
                        "period": "result", "playerProp": False, "handicap": 0.0})
    item["observation"] = {"decimal_odds": {"111": "1.8", "112": "2.1"},
                           "source_observed_at": "2026-10-22T22:00:00Z",
                           "observation_verified": True, "market_status_verified": True}

    def leads(snapshot):
        report = fixture_candidates(EVENTS, MARKETS, snapshot)
        return moneyline_candidates(report, snapshot, MARKETS["1700099998"],
                                    native_event_id="1700099998")["candidates"]

    assert leads(source)[0]["source_outcomes"] == {
        "111": "Philadelphia 76ers", "112": "Cleveland Cavaliers"}
    for field, value in (("marketName", "Regular Time Winner"), ("period", "p1"),
                         ("playerProp", True), ("handicap", 1.5)):
        modified = deepcopy(source)
        modified["items"][0]["identity"]["context"]["market_catalog"][field] = value
        assert leads(modified) == []
    modified = deepcopy(source)
    modified["items"][0]["identity"]["market_key"] = "113"
    assert leads(modified) == []
    modified = deepcopy(source)
    modified["items"][0]["observation"]["decimal_odds"] = {"111": "1.8", "113": "2.1"}
    modified["items"][0]["identity"]["outcome_points"] = {"111": None, "113": None}
    assert leads(modified) == []


def test_source_team_or_time_disagreement_has_no_match():
    source = deepcopy(SOURCE)
    source["items"][0]["identity"]["context"]["participants"] = ["PHI", "BOS"]
    assert fixture_candidates(EVENTS, MARKETS, source)["match_count"] == 0
    source = deepcopy(SOURCE)
    source["items"][0]["identity"]["context"]["scheduled_start"] = "2026-10-22T23:10:00Z"
    assert fixture_candidates(EVENTS, MARKETS, source)["match_count"] == 0


def test_read_only_batch_catalog_review_stops_after_provider_error():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(502, json={"detail": {"provider_status": 429}})

    events = deepcopy(EVENTS)
    events["events"].extend([
        {"event_id": "1700100447", "status": "not_started"},
        {"event_id": "1700100999", "status": "not_started"},
    ])
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        catalog, errors = collect_market_catalogs(client, "http://local.test/review", events,
                                                   {"X-EV-Control": "local"})
    assert catalog == {}
    assert len(calls) == 1
    assert errors == [{"event_ids": ["1700099998", "1700100447", "1700100999"],
                       "reason": "native_batch_catalog_unavailable",
                       "http_status": 502}]
    assert fixture_candidates(events, catalog, SOURCE)["native_market_catalogs_missing"] == 3


def test_read_only_batch_catalog_review_requires_every_event():
    events = deepcopy(EVENTS)
    events["events"].append({"event_id": "1700100447", "status": "not_started"})
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"read_only": True, "by_event": {
            "1700099998": MARKETS["1700099998"]}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        catalog, errors = collect_market_catalogs(client, "http://local.test/review", events,
                                                   {"X-EV-Control": "local"})
    assert len(calls) == 1 and catalog == {}
    assert errors[0]["reason"] == "native_batch_catalog_unavailable"
