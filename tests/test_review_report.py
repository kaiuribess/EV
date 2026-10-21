from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest

from evplatform.integrations.base import ProviderEnvelope
from evplatform.integrations.normalization import extract_offers
from evplatform.integrations.review_report import (
    ReviewPairs, build_review_report, fetch_pending_reviews, source_freshness,
)

NOW = datetime(2026, 10, 21, 4, 0, tzinfo=timezone.utc)


def record(signature="a" * 64, source_time=NOW.isoformat(), **context):
    return {"signature": signature, "provider": "offline_provider", "identity": {
        "provider": "offline_provider", "bookmaker": "offline_book", "event_id": "offline_event",
        "market_key": "h2h", "player_key": None, "line": None, "period": None,
        "outcome_points": {"Team A": None, "Team B": None}, "context": {
            "participants": ["Team A", "Team B"], "scheduled_start": NOW.isoformat(), **context}},
        "observation": {"source_time": source_time, "provider_time": NOW.isoformat(),
                        "status": "active", "decimal_odds": {"Team A": "1.9", "Team B": "2.0"}}}


def test_freshness_does_not_use_fresh_delivery_and_unknown_rules_stay_missing():
    item = record(source_time=(NOW-timedelta(days=4)).isoformat())
    before = deepcopy(item)
    report = build_review_report({"items": [item]}, now=NOW)
    row = report["observations"][0]
    assert row["freshness"] == "stale" and row["source_age_seconds"] == 345600
    assert {"period", "void_rule", "overtime", "equivalent_settlement_rules"} <= set(row["evidence_gaps"])
    assert not report["execution_authorized"] and report["summary"]["authorized_mappings"] == 0
    assert item == before


@pytest.mark.parametrize("value,state", [(None, "unknown"), ("bad", "unknown"),
    ("2026-10-21T04:00:00", "unknown"), ("2026-10-21T04:00:01Z", "future"),
    ("2026-10-21T03:59:55Z", "within_limit"), ("2026-10-21T03:59:54Z", "stale")])
def test_source_clock_classification(value, state):
    assert source_freshness(value, now=NOW, max_age_seconds=5)["freshness"] == state


def test_bad_prices_suspension_and_raw_settlement_do_not_create_a_usable_mapping():
    item = record(settlement_rules="A document that still requires review")
    item["observation"].update(status="suspended", decimal_odds={"Team A": "NaN", "Team B": "2"})
    row = build_review_report({"items": [item]}, now=NOW)["observations"][0]
    assert row["raw_settlement_supplied"]
    assert {"complete_valid_prices", "active_market", "equivalent_settlement_rules"} <= set(row["evidence_gaps"])


def test_duplicates_invalid_rows_and_provider_book_aliases_never_inflate_evidence():
    first, other = record(), record(signature="b"*64)
    other["identity"].update(provider="second_provider", bookmaker="PIN")
    report = build_review_report({"items": [first, first, other, {"identity": None}]}, now=NOW)
    assert report["summary"]["duplicate_signatures"] == 1
    assert report["summary"]["source_freshness"]["invalid_identity"] == 1
    assert report["summary"]["reviewed_observations"] == 2
    assert {row["bookmaker"] for row in report["observations"]} == {"offline_book", "PIN"}
    assert "independent_book_count" not in report["summary"]


def test_rest_start_and_both_provider_ids_preserved_without_period_defaults():
    event = {"id": "source-id", "eventId": "source-event-id", "commence_time": NOW.isoformat(),
        "sport_key": "basketball_nba", "home_team": "Team A", "away_team": "Team B",
        "bookmakers": [{"key": "offline_book", "markets": [{"key": "spreads", "last_update": NOW.isoformat(),
            "outcomes": [{"name": "Team A", "price": 1.9, "point": -3.5,
                          "alternateLines": [{"point": -4.5, "price": 2.1, "last_update": NOW.isoformat()}]},
                         {"name": "Team B", "price": 2.0, "point": 3.5,
                          "alternateLines": [{"point": 4.5, "price": 1.8, "last_update": NOW.isoformat()}]}]}]}]}
    offers = extract_offers(ProviderEnvelope("parlay_rest", "snapshot", [event], NOW, 1))
    assert len(offers) == 2
    for offer in offers:
        assert offer.event_id == "source-event-id"
        assert offer.context["provider_event_ids"] == {"id": "source-id", "eventId": "source-event-id"}
        assert offer.context["scheduled_start"] == NOW.isoformat()
        assert offer.period is None and "overtime" not in offer.context
    changed = deepcopy(event)
    changed["commence_time"] = (NOW+timedelta(days=1)).isoformat()
    assert extract_offers(ProviderEnvelope("parlay_rest", "snapshot", [changed], NOW, 1))[0].signature != offers[0].signature


@pytest.mark.parametrize("url", ["https://127.0.0.1:8001", "http://evil.test", "http://user:secret@localhost", "http://localhost/path", "http://localhost?secret=x"])
def test_remote_or_credential_bearing_urls_rejected_before_any_request(url):
    with pytest.raises(ValueError):
        fetch_pending_reviews(url)


def test_bounded_get_only_pagination_reports_nonatomic_truncation():
    seen = []
    def handle(request):
        seen.append(request)
        offset, limit = int(request.url.params["offset"]), int(request.url.params["limit"])
        rows = [record(signature=f"{index:064x}") for index in range(offset, offset + limit)]
        return httpx.Response(200, json={"items": rows, "total": 5000, "offset": offset, "observed_at": NOW.isoformat()})
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = fetch_pending_reviews("http://127.0.0.1:8001", max_rows=1001, client=client)
    assert len(result["items"]) == 1001
    assert result["collection"] == {"pages": 2, "atomic": False, "total_changed": False, "truncated": True}
    assert all(r.method == "GET" and r.url.path == "/api/market-review" and not r.content for r in seen)


def test_contract_rule_mismatch_and_line_mismatch_remain_explicit():
    values = {"source": "offline", "source_market_id": "offline-market", "event_id": "event",
        "sport": "basketball", "league": "nba", "participant_ids": ["A", "B"], "start_time": NOW.isoformat(),
        "market_type": "total", "line": "200.5", "outcomes": ["over", "under"], "period": "full_game",
        "scope": "game", "overtime": "included", "participation": "not_applicable", "void_rule": "refund",
        "push_rule": "not_applicable", "currency": "USD", "contract_type": "binary",
        "evidence_urls": ["https://example.test/offline-fixture"]}
    pairs = ReviewPairs.model_validate({"comparisons": [{"left": values, "right": {**values, "line": "201.5", "void_rule": "fair_value"}}]})
    result = build_review_report({"items": []}, now=NOW, pairs=pairs)["contract_comparisons"][0]
    assert not result["structurally_equivalent"] and not result["execution_authorized"]
    assert {"field": "line", "status": "mismatch"} in result["differences"]
    assert {"field": "void_rule", "status": "mismatch"} in result["differences"]
