"""Kalshi occurrence/expiration clocks must never become NBA tipoff clocks."""
from copy import deepcopy
from datetime import datetime, timezone

import pytest

from evplatform.integrations.kalshi_event_crosswalk import fixture_candidates, moneyline_candidates


MARKETS = {"cursor": "", "markets": [
    {"ticker": "KXNBAGAME-26OCT22CLEPHI-PHI", "event_ticker": "KXNBAGAME-26OCT22CLEPHI",
     "market_type": "binary", "status": "active", "occurrence_datetime": "2026-10-23T02:00:00Z",
     "rules_primary": "If Philadelphia wins the Cleveland vs Philadelphia Pro Basketball game originally scheduled for Oct 22, 2026, then the market resolves to Yes.",
     "rules_secondary": "If postponed but begins within 48 hours, resolve official result. Otherwise resolve to a fair price."},
    {"ticker": "KXNBAGAME-26OCT22CLEPHI-CLE", "event_ticker": "KXNBAGAME-26OCT22CLEPHI",
     "market_type": "binary", "status": "active", "occurrence_datetime": "2026-10-23T02:00:00Z",
     "rules_primary": "If Cleveland wins the Cleveland vs Philadelphia Pro Basketball game originally scheduled for Oct 22, 2026, then the market resolves to Yes.",
     "rules_secondary": "If postponed but begins within 48 hours, resolve official result. Otherwise resolve to a fair price."},
]}
SOURCE = {"items": [{"identity": {"provider": "oddize_rest", "event_id": "game-1",
    "bookmaker": "circa", "context": {"event_sport": "nba",
    "participants": ["PHI", "CLE"], "scheduled_start": "2026-10-22T23:00:00Z"}}}]}


def test_matching_fixture_retains_distinct_tipoff_and_expiration():
    result = fixture_candidates(MARKETS, SOURCE)
    assert result["match_count"] == 1
    candidate = result["matches"][0]
    assert candidate["source_start"] == "2026-10-22T23:00:00+00:00"
    assert candidate["native_occurrence_times"] == ["2026-10-23T02:00:00+00:00"]
    assert candidate["occurrence_is_tipoff"] is False
    assert candidate["native_fair_price_on_cancel"] is True
    assert candidate["native_outcome_rules_verified"] is True
    assert all(len(row["text_sha256"]) == 64 for row in candidate["native_rule_evidence"].values())
    assert not candidate["settlement_equivalence_verified"]
    assert not result["execution_authorized"]


def _source_moneyline():
    source = deepcopy(SOURCE)
    source["items"][0]["signature"] = "a" * 64
    source["items"][0]["identity"].update(
        market_key="ml", player_key=None, line=None, period=None,
        outcome_points={"PHI": None, "CLE": None})
    source["items"][0]["observation"] = {
        "decimal_odds": {"PHI": "2.10", "CLE": "1.80"},
        "source_observed_at": "2026-10-22T22:59:59Z",
        "source_price_changed_at": "2026-10-22T22:30:00Z",
        "observation_verified": True, "observation_complete": True,
        "market_status_verified": True, "status": "active"}
    return source


def test_exact_moneyline_sides_and_quote_evidence_are_review_only():
    source = _source_moneyline()
    fixtures = fixture_candidates(MARKETS, source)
    result = moneyline_candidates(fixtures, source,
                                  now=datetime(2026, 10, 22, 23, tzinfo=timezone.utc))
    assert result["candidate_count"] == result["quote_evidence_ready_count"] == 1
    candidate = result["candidates"][0]
    assert candidate["source_outcomes"] == {"PHI": "Philadelphia 76ers", "CLE": "Cleveland Cavaliers"}
    assert candidate["native_outcomes"] == {
        "Philadelphia 76ers": "KXNBAGAME-26OCT22CLEPHI-PHI",
        "Cleveland Cavaliers": "KXNBAGAME-26OCT22CLEPHI-CLE"}
    assert candidate["quote_evidence_ready"] is True
    assert candidate["source_price_changed_at"] == "2026-10-22T22:30:00Z"
    assert candidate["candidate_only"] and not candidate["settlement_equivalence_verified"]
    assert not result["execution_authorized"]


def test_oddspapi_moneyline_uses_documented_home_away_ids_and_catalog():
    source = _source_moneyline()
    identity = source["items"][0]["identity"]
    identity.update(provider="oddspapi_rest", market_key="111", period="result", line="0",
                    outcome_points={"111": None, "112": None})
    identity["context"] = {
        "event_sport": 11, "tournament_id": 132, "event_league": "NBA",
        "participants": ["Philadelphia 76ers", "Cleveland Cavaliers"],
        "scheduled_start": "2026-10-22T23:00:00Z",
        "market_catalog": {"marketType": "moneyline", "marketName": "Winner (incl. overtime)",
                           "period": "result", "playerProp": False, "handicap": 0}}
    source["items"][0]["observation"]["decimal_odds"] = {"111": "2.10", "112": "1.80"}
    result = moneyline_candidates(fixture_candidates(MARKETS, source), source,
                                  now=datetime(2026, 10, 22, 23, tzinfo=timezone.utc))
    assert result["candidate_count"] == 1
    assert result["candidates"][0]["source_outcomes"] == {
        "111": "Philadelphia 76ers", "112": "Cleveland Cavaliers"}


@pytest.mark.parametrize("change", [
    lambda row: row["identity"].update(period="first_half"),
    lambda row: row["identity"].update(line="1.5"),
    lambda row: row["identity"].update(outcome_points={"PHI": 0, "CLE": None}),
    lambda row: row["observation"].update(decimal_odds={"PHI": "2.10"}),
    lambda row: row["observation"].update(decimal_odds={"PHI": "NaN", "CLE": "1.80"}),
    lambda row: row["observation"].update(decimal_odds={"PHI": "2.10", "BOS": "1.80"}),
])
def test_nonmatching_source_moneyline_is_not_a_native_side_lead(change):
    source = _source_moneyline()
    change(source["items"][0])
    result = moneyline_candidates(fixture_candidates(MARKETS, source), source,
                                  now=datetime(2026, 10, 22, 23, tzinfo=timezone.utc))
    assert result["candidate_count"] == 0


@pytest.mark.parametrize("old,new", [
    ("If Philadelphia wins", "If Cleveland wins"),
    ("Cleveland vs Philadelphia", "Boston vs Philadelphia"),
    ("Oct 22, 2026", "Oct 23, 2026"),
])
def test_changed_native_winner_rule_stays_a_fixture_lead_only(old, new):
    native, source = deepcopy(MARKETS), _source_moneyline()
    native["markets"][0]["rules_primary"] = native["markets"][0]["rules_primary"].replace(old, new)
    fixtures = fixture_candidates(native, source)
    assert fixtures["match_count"] == 1
    assert fixtures["matches"][0]["native_outcome_rules_verified"] is False
    result = moneyline_candidates(fixtures, source,
                                  now=datetime(2026, 10, 22, 23, tzinfo=timezone.utc))
    assert result["candidate_count"] == 0


def test_old_source_observation_is_visible_but_not_quote_ready():
    source = _source_moneyline()
    source["items"][0]["observation"]["source_observed_at"] = "2026-10-22T22:30:00Z"
    result = moneyline_candidates(fixture_candidates(MARKETS, source), source,
                                  now=datetime(2026, 10, 22, 23, tzinfo=timezone.utc))
    assert result["candidate_count"] == 1
    assert result["quote_evidence_ready_count"] == 0
    assert "fresh_quote_observation" in result["candidates"][0]["quote_evidence_gaps"]


def test_oddspapi_nba_fixture_lead_requires_exact_sport_tournament_and_names():
    source = deepcopy(SOURCE)
    identity = source["items"][0]["identity"]
    identity["provider"] = "oddspapi_rest"
    identity["event_id"] = "fixture-1"
    identity["bookmaker"] = "pinnacle"
    identity["context"] = {
        "event_sport": 11, "tournament_id": 132, "event_league": "NBA",
        "participants": ["Philadelphia 76ers", "Cleveland Cavaliers"],
        "scheduled_start": "2026-10-22T23:00:00Z",
    }
    matched = fixture_candidates(MARKETS, source)
    assert matched["match_count"] == 1
    assert matched["matches"][0]["provider"] == "oddspapi_rest"
    assert matched["execution_authorized"] is False
    for field, bad in (("event_sport", 10), ("tournament_id", 133),
                       ("event_league", "EuroLeague")):
        changed = deepcopy(source)
        changed["items"][0]["identity"]["context"][field] = bad
        assert fixture_candidates(MARKETS, changed)["match_count"] == 0


@pytest.mark.parametrize("mutation", [
    lambda native, source: native["markets"].pop(),
    lambda native, source: native["markets"][0].update(status="suspended"),
    lambda native, source: native["markets"][0].update(ticker="KXNBAGAME-26OCT22CLEPHI-DEN"),
    lambda native, source: source["items"][0]["identity"]["context"].update(
        scheduled_start="2026-10-23T23:00:00Z"),
    lambda native, source: source["items"][0]["identity"]["context"].update(
        participants=["PHI", "BOS"]),
])
def test_incomplete_or_different_fixture_remains_unmatched(mutation):
    native, source = deepcopy(MARKETS), deepcopy(SOURCE)
    mutation(native, source)
    assert fixture_candidates(native, source)["match_count"] == 0


def test_conflicting_source_schedule_is_quarantined():
    source = deepcopy(SOURCE)
    source["items"].append(deepcopy(source["items"][0]))
    source["items"][1]["identity"]["context"]["scheduled_start"] = "2026-10-22T23:30:00Z"
    result = fixture_candidates(MARKETS, source)
    assert result["match_count"] == 0 and result["conflicting_source_events"] == 1


def test_different_provider_starts_remain_visible_but_disputed():
    source = deepcopy(SOURCE)
    source["items"].append(deepcopy(source["items"][0]))
    source["items"][1]["identity"]["provider"] = "parlay_ws"
    source["items"][1]["identity"]["context"]["scheduled_start"] = "2026-10-22T23:10:00Z"
    result = fixture_candidates(MARKETS, source)
    assert result["match_count"] == 2
    assert result["cross_provider_start_conflicts"] == 1
    assert all(row["source_start_conflict"] for row in result["matches"])


def test_missing_catalog_cursor_is_not_complete():
    native = deepcopy(MARKETS)
    native.pop("cursor")
    with pytest.raises(ValueError):
        fixture_candidates(native, SOURCE)


def test_conflicting_native_market_identity_is_not_reviewed():
    native = deepcopy(MARKETS)
    native["markets"].append(deepcopy(native["markets"][0]))
    native["markets"][2]["rules_secondary"] = "Changed terms"
    result = fixture_candidates(native, SOURCE)
    assert result["match_count"] == 0 and result["conflicting_native_events"] == 1


@pytest.mark.parametrize("native_code,source_code,expected", [
    ("PHX", "PHO", "Phoenix Suns"), ("BKN", "BRK", "Brooklyn Nets"),
    ("CHA", "CHO", "Charlotte Hornets"),
])
def test_live_kalshi_code_aliases_remain_venue_specific(native_code, source_code, expected):
    native, source = deepcopy(MARKETS), deepcopy(SOURCE)
    original = "KXNBAGAME-26OCT22CLEPHI"
    replacement = f"KXNBAGAME-26OCT22CLE{native_code}"
    for market in native["markets"]:
        market["event_ticker"] = replacement
        market["ticker"] = market["ticker"].replace(original, replacement).replace("-PHI", f"-{native_code}")
    source["items"][0]["identity"]["context"]["participants"][0] = source_code
    result = fixture_candidates(native, source)
    assert result["match_count"] == 1
    assert result["matches"][0]["home"] == expected
    assert result["unknown_native_team_codes"] == []
