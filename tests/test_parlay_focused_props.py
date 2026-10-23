from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest

from evplatform.integrations.base import ProtocolError, ProviderEnvelope
from evplatform.integrations.normalization import VerifiedProviderMapper, extract_offers
from evplatform.integrations.parlay import ParlayRest
from evplatform.integrations.parlay_focused_props import (FocusedCreditWindow,
    exact_parlay_event_id, needs_focused_references)


EVENT = "472de3df50fe86e9508e60fc83c21388"
NOW = datetime(2026, 10, 23, 22, 24, tzinfo=UTC)


def row(book="fanduel", **updates):
    return {"event_id": EVENT, "bookmaker": book,
        "market_key": "player_receptions", "player": "Aaron Jones", "line": 2.5,
        "period": "FULL", "home_team": "Minnesota Vikings",
        "away_team": "Indianapolis Colts", "commence_time": "2026-10-25T17:00:00Z",
        "over_price": -110, "under_price": -120,
        "last_update": "2026-10-23T22:23:35Z", **updates}


def provider(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_focused_novig_prop_scope_uses_free_coverage_and_one_paid_page():
    calls = []

    def handle(request):
        assert request.method == "GET"  # focused discovery has no placement path
        calls.append((request.url.path, dict(request.url.params)))
        if request.url.path.endswith("/coverage"):
            return httpx.Response(200, json={"sources": [
                {"bookmaker": book, "api_visible_rows": 1, "status": "api_visible"}
                for book in ("fanduel", "draftkings", "caesars")]})
        return httpx.Response(200, json=[row(book) for book in
            ("fanduel", "draftkings", "caesars")], headers={
                "x-result-has-more": "false", "x-requests-last": "3"})

    async with provider(handle) as http:
        rest = ParlayRest({"enabled": True, "session_request_budget": 3},
                          client=http, credential="fixture-secret")
        pages, detail = await rest.fetch_focused_props(event_id=EVENT, player="Aaron Jones",
            market="player_receptions", line="2.5",
            bookmakers=["fanduel", "draftkings", "caesars"], limit=200)
    assert len(calls) == 2 and calls[0][0].endswith("/props/coverage")
    assert calls[1][0].endswith("/props")
    assert calls[1][1]["eventId"] == EVENT and calls[1][1]["player"] == "Aaron Jones"
    assert calls[1][1]["includeSids"] == "true"
    assert detail["credits_reserved"] == 3 and len(pages) == 1
    offers = extract_offers(pages[0])
    assert len(offers) == 3 and {o.bookmaker for o in offers} == {
        "fanduel", "draftkings", "caesars"}
    assert all(o.prices["over"] > 1 and o.prices["under"] > 1 for o in offers)
    assert all(o.source_stored_at == datetime(2026, 10, 23, 22, 23, 35, tzinfo=UTC)
               for o in offers)
    assert all(not o.observation_verified and not o.market_status_verified for o in offers)
    assert all(o.verification_reason == "unverified_observation_clock" for o in offers)


@pytest.mark.asyncio
async def test_focused_props_rejects_ignored_event_filter_before_publishing():
    def handle(request):
        if request.url.path.endswith("/coverage"):
            return httpx.Response(200, json={"sources": [
                {"bookmaker": book, "api_visible_rows": 1, "status": "api_visible"}
                for book in ("fanduel", "draftkings")]})
        return httpx.Response(200, json=[row(event_id="02002f0e4dd420f9a66b6f278dafe587")])

    async with provider(handle) as http:
        rest = ParlayRest({"enabled": True}, client=http, credential="fixture-secret")
        with pytest.raises(ProtocolError, match="ignored the exact event"):
            await rest.fetch_focused_props(event_id=EVENT, player="Aaron Jones",
                market="player_receptions", line="2.5", bookmakers=["fanduel", "draftkings"])


@pytest.mark.asyncio
async def test_focused_props_pages_remain_nonatomic_and_keep_adverse_headers():
    def handle(request):
        if request.url.path.endswith("/coverage"):
            return httpx.Response(200, json={"sources": [
                {"bookmaker": book, "api_visible_rows": 1, "status": "api_visible"}
                for book in ("fanduel", "draftkings")]})
        if request.url.params["offset"] == "0":
            return httpx.Response(200, json=[row()], headers={
                "x-result-has-more": "true", "x-next-offset": "1",
                "x-result-truncated": "true"})
        return httpx.Response(200, json=[row("draftkings")], headers={
            "x-result-has-more": "false"})

    async with provider(handle) as http:
        rest = ParlayRest({"enabled": True, "session_request_budget": 6},
                          client=http, credential="fixture-secret")
        pages, detail = await rest.fetch_focused_props(event_id=EVENT, player="Aaron Jones",
            market="player_receptions", line="2.5", bookmakers=["fanduel", "draftkings"])
    assert detail["credits_reserved"] == 6 and len(pages) == 2
    assert all(page.metadata["headers"]["x-result-has-more"] == "true" for page in pages)
    assert extract_offers(pages[0])[0].verification_reason == "truncated_response"
    assert extract_offers(pages[1])[0].verification_reason == "paginated_response_not_atomic"


def test_rest_collector_observation_and_selection_ids_do_not_verify_active_status():
    observed = NOW - timedelta(seconds=2)
    raw = row(last_observed=observed.isoformat(), last_update_type="collector_observation",
              over_sid="sid-over", under_sid="sid-under")
    frame = ProviderEnvelope("parlay_rest", "props_snapshot", [raw], NOW, 1)
    offer, = extract_offers(frame)
    assert offer.observation_verified and offer.observation_complete
    assert offer.source_observed_at == observed
    assert offer.source_selection_ids == {"over": "sid-over", "under": "sid-under"}
    assert not offer.market_status_verified


def test_focused_status_and_opposing_side_fail_closed():
    suspended, = extract_offers(ProviderEnvelope("parlay_rest", "props_snapshot",
        [row(status="SUSPENDED")], NOW, 1))
    assert suspended.status == "suspended" and not suspended.market_status_verified
    assert VerifiedProviderMapper()(ProviderEnvelope("parlay_rest", "props_snapshot",
        [row(under_price=None)], NOW, 1)) == []


def test_focused_credit_window_deduplicates_and_rolls_off():
    budget = FocusedCreditWindow(6, 120)
    scope = (EVENT, "Aaron Jones", "player_receptions", "2.5")
    assert budget.available(100, scope) == 6
    budget.charge(100, scope, 3)
    assert budget.available(101, scope) == 0
    assert budget.available(221, scope) == 3
    budget.charge(221, scope, 3)
    assert budget.available(500, scope) == 0
    assert budget.available(3700, scope) == 3


def test_two_book_fair_research_from_truncated_or_old_rows_still_needs_focus():
    def quote(book, reason="unverified_observation_clock", age=1):
        return SimpleNamespace(provider="parlay_rest", bookmaker=book, status="unknown",
            verification_reason=reason, decimal_odds={"over": Decimal("1.9"), "under": Decimal("1.9")},
            source_observed_at=None, observation_verified=False,
            source_stored_at=NOW-timedelta(seconds=age))

    assert needs_focused_references([quote("fanduel", "truncated_response"),
                                     quote("draftkings", "truncated_response")],
                                    now=NOW, max_indicator_age_ms=30000)
    assert needs_focused_references([quote("fanduel", age=61), quote("draftkings", age=61)],
                                    now=NOW, max_indicator_age_ms=30000)
    assert not needs_focused_references([quote("fanduel"), quote("draftkings")],
                                       now=NOW, max_indicator_age_ms=30000)
    assert needs_focused_references([quote("fanduel"), quote("fanduel")],
                                    now=NOW, max_indicator_age_ms=30000)


def test_exact_event_resolution_never_uses_player_or_team_name_alone():
    market = SimpleNamespace(participants=("Minnesota Vikings", "Indianapolis Colts"),
                             start_time=datetime(2026, 10, 25, 17, tzinfo=UTC))
    correct = {"id": EVENT, "home_team": market.participants[0],
               "away_team": market.participants[1], "commence_time": market.start_time.isoformat()}
    assert exact_parlay_event_id(market, [correct]) == EVENT
    assert exact_parlay_event_id(market, [correct, dict(correct)]) is None
    assert exact_parlay_event_id(market, [{**correct, "commence_time": "2026-10-25T17:05:00Z"}]) is None
    assert exact_parlay_event_id(market, [{**correct, "away_team": "Other"}]) is None
