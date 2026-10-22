"""Offline Oddize protocol fixtures, not evidence of live connectivity."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from pydantic import ValidationError

from evplatform.core.models import Market, Settlement
from evplatform.integrations import PROVIDER_FACTORIES
from evplatform.integrations.base import ProtocolError, ProviderDisabled, ProviderEnvelope, QuotaExceeded
from evplatform.integrations.normalization import VerifiedOfferMapping, VerifiedProviderMapper, extract_offers
from evplatform.integrations.oddize import OddizeConfig, OddizeRest, prop_event_plan, prop_event_refs


NOW = datetime(2026, 10, 20, 18, tzinfo=UTC)


def price(side, *, book="pin", offer_type="ml", line=None, **updates):
    return {"book": book, "book_name": "Fixture Book", "offer_type": offer_type,
            "market": "moneyline", "team": side, "line": line, "american_odds": "-110",
            "ts": int(NOW.timestamp()), "timestamp": NOW.isoformat(), **updates}


def raw(rows=None, *, kind="snapshot", **updates):
    payload = {"sport": "nba", "events": [{"event_id": "event-1", "sport": "nba", "team1": "AWAY",
               "team2": "HOME", "start_date": (NOW + timedelta(hours=1)).isoformat(),
               "odds": rows if rows is not None else [price("AWAY"), price("HOME")]}]}
    return ProviderEnvelope("oddize_rest", kind, updates.pop("payload", payload), NOW + timedelta(seconds=5),
                            100, metadata={"sport": "nba"}, **updates)


def props_row(subject="Example Player", side="Over", line=24.5, **updates):
    return {"market": "player_pts", "offer_type": "player_pts", "subject": subject,
            "subject_type": "player", "side": side, "line": line, "book": "pin",
            "american_odds": "-110", "ts": int(NOW.timestamp()), "timestamp": NOW.isoformat(), **updates}


def raw_props(rows):
    return raw(kind="props_snapshot", payload={"event_id": "event-1", "sport": "nba", "props": rows})


def test_prop_event_refs_require_exact_current_provider_event_identity():
    future = (NOW + timedelta(hours=1)).isoformat()
    later = (NOW + timedelta(hours=2)).isoformat()
    snapshot = {"sport": "nba", "events": [
        {"event_id": "one", "sport": "nba", "team1": "AWAY", "team2": "HOME", "start_date": future},
        {"event_id": "two", "sport": "nba", "team1": "A", "team2": "B", "start_date": later},
        {"event_id": "bad", "sport": "nba", "team1": "A", "team2": "B", "start_date": future},
        {"event_id": "bad", "sport": "nba", "team1": "C", "team2": "D", "start_date": future},
        {"event_id": "malformed", "sport": "nba", "team1": "A", "team2": "B", "start_date": future},
        {"event_id": "malformed", "sport": "nba", "team1": None, "team2": "B", "start_date": future},
        {"event_id": "past", "sport": "nba", "team1": "A", "team2": "B", "start_date": NOW.isoformat()},
        {"event_id": "other", "sport": "nfl", "team1": "A", "team2": "B", "start_date": future},
        {"event_id": "../traversal", "sport": "nba", "team1": "A", "team2": "B", "start_date": future},
    ]}
    refs = prop_event_refs(snapshot, "nba", now=NOW, lookahead_hours=24)
    assert set(refs) == {"one", "two"}
    assert prop_event_plan(refs, ["two", "missing", "two"], 1) == ["two", "one"]
    assert prop_event_plan(refs, [], 1) == ["one"]
    assert prop_event_plan(refs, [], 0) == []
    with pytest.raises(ValueError):
        prop_event_plan(refs, [], 4)
    with pytest.raises(ProtocolError):
        prop_event_refs({"sport": "nfl", "events": []}, "nba", now=NOW, lookahead_hours=24)


async def test_event_props_bind_exact_same_cycle_event_ref_without_claiming_status():
    ref = {"event_id": "event-1", "sport": "nba", "team1": "AWAY", "team2": "HOME",
           "start_date": (NOW + timedelta(hours=1)).isoformat()}

    def handle(request):
        assert request.url.path == "/api/v1/events/nba/event-1/props"
        return httpx.Response(200, json={"event_id": "event-1", "sport": "nba", "props": [
            props_row(side="Over"), props_row(side="Under")], "count": 2})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        provider = OddizeRest({"enabled": True}, client=client, credential="offline-secret")
        frame = await provider.fetch_event_props("nba", "event-1", prop_types=["player_pts"], event_ref=ref)
    offer, = extract_offers(frame)
    assert offer.context["participants"] == ["HOME", "AWAY"]
    assert offer.context["scheduled_start"] == ref["start_date"]
    assert offer.line == "24.5" and set(offer.prices) == {"Over", "Under"}
    assert offer.observation_verified and offer.observation_complete
    assert not offer.market_status_verified
    assert frame.metadata["event_ref"] == ref


async def test_event_props_reject_wrong_served_event_and_reference():
    ref = {"event_id": "event-1", "sport": "nba", "team1": "AWAY", "team2": "HOME",
           "start_date": (NOW + timedelta(hours=1)).isoformat()}
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200,
            json={"event_id": "wrong", "sport": "nba", "props": []}))) as client:
        provider = OddizeRest({"enabled": True}, client=client, credential="offline-secret")
        with pytest.raises(ProtocolError):
            await provider.fetch_event_props("nba", "event-1", prop_types=["player_pts"], event_ref=ref)
        calls = provider.quota.calls
        with pytest.raises(ProtocolError):
            await provider.fetch_event_props("nba", "event-1", prop_types=["player_pts"],
                                             event_ref={**ref, "team1": "HOME"})
        assert provider.quota.calls == calls


async def test_auto_prop_cycle_uses_current_snapshot_identity_and_one_metered_event():
    future = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
    paths = []

    def handle(request):
        paths.append(request.url.path)
        if request.url.path == "/api/v1/account/usage":
            return httpx.Response(200, json={"plan_id": "api_developer", "credits_remaining": 50})
        if request.url.path == "/api/v1/sports":
            return httpx.Response(200, json={"sports": []})
        if request.url.path == "/api/v1/books":
            return httpx.Response(200, json={"books": []})
        if request.url.path == "/api/v1/offer-types":
            return httpx.Response(200, json={"offer_types": []})
        if request.url.path == "/api/v1/odds/latest":
            return httpx.Response(200, json={"sport": "nba", "events": [
                {"event_id": "first", "sport": "nba", "team1": "AWAY", "team2": "HOME",
                 "start_date": future, "odds": []}]})
        if request.url.path == "/api/v1/events/nba/first/props":
            assert request.url.params.get_list("prop_types") == ["player_pts"]
            return httpx.Response(200, json={"sport": "nba", "event_id": "first", "props": []})
        raise AssertionError("Unexpected Oddize request")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        provider = OddizeRest({"enabled": True, "sports": ["nba"], "include_props": True,
            "prop_types": {"nba": ["player_pts"]}, "auto_prop_event_limit": 1,
            "session_request_budget": 3}, client=client, credential="offline-secret")
        stream = provider.stream_updates()
        frames = [await anext(stream) for _ in range(6)]
        await stream.aclose()
        await provider.disconnect()
    assert [frame.kind for frame in frames] == ["account_capabilities", "sport_discovery",
        "bookmaker_discovery", "market_discovery", "snapshot", "props_snapshot"]
    assert frames[-1].metadata["event_ref"] == {"event_id": "first", "sport": "nba",
        "team1": "AWAY", "team2": "HOME", "start_date": future}
    assert paths.count("/api/v1/events/nba/first/props") == 1
    assert provider.quota.billable_requests == 3


async def test_exact_header_auth_and_repeated_query_filters():
    seen = []

    def handle(request):
        seen.append(request)
        assert request.headers["X-API-Key"] == "offline-secret"
        assert "offline-secret" not in str(request.url)
        assert request.url.params.get_list("books") == ["pin", "circa"]
        assert request.url.params.get_list("offer_types") == ["ml", "s"]
        return httpx.Response(200, json={"sport": "nba", "events": []}, headers={
            "X-Credits-Cost": "1", "X-Credits-Remaining": "1499", "X-RateLimit-Limit": "20",
            "X-RateLimit-Remaining": "19", "X-Cache": "HIT"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        provider = OddizeRest({"enabled": True, "bookmakers": ["pin", "circa"], "markets": ["ml", "s"]},
                              client=client, credential="offline-secret")
        event = await provider.fetch_snapshot("nba")
        assert seen[0].url.path == "/api/v1/odds/latest"
        assert event.synthetic is False and event.published_at is None
        assert event.metadata["delta"] is False
        assert event.snapshot_complete is True
        assert provider.quota.provider_remaining == 1499
        assert provider.quota.billable_requests == 1  # cache HIT is still metered
        assert provider.quota.max_per_minute == 20
        assert provider.quota.minute_remaining == 19


async def test_metadata_and_usage_are_free_and_account_is_allowlisted():
    paths = []

    def handle(request):
        paths.append(request.url.path)
        body = {"plan_id": "api_developer", "credits_remaining": 1450, "rate_limit_rpm": 15,
                "period_end": (NOW + timedelta(days=20)).isoformat(),
                "key": {"name": "Private account label", "prefix": "privateprefix"},
                "api_key": "offline-secret"}
        return httpx.Response(200, json=body, headers={"X-Credits-Cost": "0"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        provider = OddizeRest({"enabled": True}, client=client, credential="offline-secret")
        account = await provider.fetch_account()
        await provider.discover_sports()
        await provider.discover_bookmakers()
        await provider.discover_markets("nba")
        await provider.discover_teams("nba")
        assert paths == ["/api/v1/account/usage", "/api/v1/sports", "/api/v1/books",
                         "/api/v1/offer-types", "/api/v1/sports/nba/teams"]
        assert provider.quota.billable_requests == 0
        assert provider.quota.calls == 5
        assert provider.quota.provider_remaining == 1450
        assert provider.quota.provider_reset_at == NOW + timedelta(days=20)
        assert provider.quota.max_per_minute == 15
        assert "key" not in account.payload
        assert "offline-secret" not in repr(account)
        health = await provider.health_check()
        assert "privateprefix" not in repr(health)
        assert "Private account label" not in repr(health)
        assert health["streaming"] is False
        assert health["polling"] is True


async def test_http_402_stops_without_repeated_paid_requests():
    calls = 0

    def handle(request):
        nonlocal calls
        calls += 1
        return httpx.Response(402, json={"message": "No credits", "api_key": "offline-secret"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        provider = OddizeRest({"enabled": True}, client=client, credential="offline-secret")
        for _ in range(2):
            with pytest.raises(QuotaExceeded, match="exhausted"):
                await provider.fetch_snapshot("nba")
        assert calls == 1
        assert (await provider.health_check())["state"] == "quota_exhausted"


async def test_rate_limit_retry_after_prevents_second_request():
    calls = 0

    def handle(request):
        nonlocal calls
        calls += 1
        return httpx.Response(429, headers={"Retry-After": "90"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        provider = OddizeRest({"enabled": True}, client=client, credential="offline-secret")
        for _ in range(2):
            with pytest.raises(QuotaExceeded):
                await provider.fetch_snapshot("nba")
        assert calls == 1
        assert provider.quota.stats()["cooldown_seconds"] > 85


async def test_zero_remaining_minute_header_blocks_next_call():
    calls = 0

    def handle(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"events": []}, headers={"X-RateLimit-Remaining": "0"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        provider = OddizeRest({"enabled": True}, client=client, credential="offline-secret")
        await provider.fetch_snapshot("nba")
        with pytest.raises(QuotaExceeded):
            await provider.fetch_snapshot("nba")
        assert calls == 1


async def test_auth_refusal_has_no_retry_or_secret_error():
    calls = 0

    def handle(request):
        nonlocal calls
        calls += 1
        return httpx.Response(401, json={"message": "offline-secret"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        provider = OddizeRest({"enabled": True}, client=client, credential="offline-secret")
        for _ in range(2):
            with pytest.raises(ProviderDisabled) as error:
                await provider.fetch_snapshot("nba")
            assert "offline-secret" not in str(error.value)
        assert calls == 1


async def test_event_id_literal_slashes_and_explicit_prop_types():
    seen = []

    def handle(request):
        seen.append(request)
        return httpx.Response(200, json={"event_id": "2026/10/20/event", "sport": "nba", "props": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        provider = OddizeRest({"enabled": True}, client=client, credential="offline-secret")
        result = await provider.fetch_event_props("nba", "2026/10/20/event", prop_types=["player_pts", "player_ast"])
        assert seen[0].url.path == "/api/v1/events/nba/2026/10/20/event/props"
        assert seen[0].url.params.get_list("prop_types") == ["player_pts", "player_ast"]
        assert result.kind == "props_snapshot"
        assert provider.quota.billable_requests == 2


async def test_event_props_keep_only_requested_market_when_server_returns_unrelated_rows():
    wanted = props_row(subject="Example Player", side="Over")
    unrelated = {**wanted, "offer_type": "alt_s", "subject": None, "side": "HOME"}
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"event_id": "event-1", "sport": "nba",
                                                  "props": [wanted, unrelated]})
    )) as client:
        provider = OddizeRest({"enabled": True}, client=client, credential="offline-secret")
        result = await provider.fetch_event_props("nba", "event-1", prop_types=["player_pts"])
        assert result.metadata["out_of_scope_prop_rows"] == 1
        assert result.metadata["requested_prop_types"] == ["player_pts"]
        offers = extract_offers(result)
        assert len(offers) == 1
        assert offers[0].market_key == "player_pts"
        assert (await provider.health_check())["out_of_scope_prop_rows"] == 1


async def test_requested_oddize_prop_with_missing_player_remains_invalid():
    invalid = props_row(subject=None)
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"event_id": "event-1", "sport": "nba",
                                                  "props": [invalid]})
    )) as client:
        provider = OddizeRest({"enabled": True}, client=client, credential="offline-secret")
        result = await provider.fetch_event_props("nba", "event-1", prop_types=["player_pts"])
        with pytest.raises(ValueError, match="subject identity missing"):
            extract_offers(result)


async def test_delta_is_not_a_complete_baseline():
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"sport": "nba", "events": []})
    )) as client:
        provider = OddizeRest({"enabled": True}, client=client, credential="offline-secret")
        result = await provider.fetch_snapshot("nba", updated_since=int(NOW.timestamp()))
        assert result.snapshot_complete is False
        assert result.metadata["delta"] is True


@pytest.mark.parametrize("event_id", ["../x", "x/../y", "https://other.invalid", "event?api_key=x", "x//y", "/event", "event#fragment"])
async def test_event_path_cannot_escape_provider(event_id):
    provider = OddizeRest({"enabled": True}, credential="offline-secret")
    with pytest.raises(ValueError):
        await provider.fetch_event("nba", event_id)
    assert provider.quota.calls == 0


def test_conservative_budget_scales_with_sports_and_props():
    one = OddizeRest({"sports": ["nba"]}, credential="")
    many = OddizeRest({"sports": ["nba", "nfl", "nhl", "mlb", "ncaabm", "ncaaf"]}, credential="")
    props = OddizeRest({"sports": ["nba"], "include_props": True, "event_ids": {"nba": ["a", "b"]},
                        "prop_types": {"nba": ["player_pts"]}}, credential="")
    assert one.effective_poll_interval() == 3600
    assert many.effective_poll_interval() >= 31 * 86400 * 6 / 1200
    assert props.effective_poll_interval() >= 31 * 86400 * 5 / 1200


@pytest.mark.parametrize("plan,expected_delta", [("api_developer", False), ("api_starter", True)])
async def test_delta_polling_does_not_assume_free_tier_entitlement(monkeypatch, plan, expected_delta):
    snapshots = []

    async def no_sleep(_):
        return

    monkeypatch.setattr(asyncio, "sleep", no_sleep)

    def handle(request):
        if request.url.path.endswith("/account/usage"):
            return httpx.Response(200, json={"plan_id": plan, "credits_remaining": 100})
        if request.url.path.endswith("/odds/latest"):
            snapshots.append(request)
            return httpx.Response(200, json=raw().payload)
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        provider = OddizeRest({"enabled": True, "sports": ["nba"], "use_delta_polling": True},
                              client=client, credential="offline-secret")
        stream = provider.stream_updates()
        for _ in range(6):
            await anext(stream)
        await stream.aclose()
        assert "updated_since" not in snapshots[0].url.params
        assert ("updated_since" in snapshots[1].url.params) is expected_delta


def test_main_rows_preserve_observation_time_and_away_home_metadata():
    offers = extract_offers(raw())
    assert len(offers) == 1
    offer = offers[0]
    assert set(offer.prices) == {"AWAY", "HOME"}
    assert offer.bookmaker == "pin"
    assert offer.prices["HOME"] == 1 + Decimal(100) / 110
    assert offer.observed_at == NOW
    assert offer.provider_time is None
    assert offer.context["participants"] == ["HOME", "AWAY"]
    assert offer.context["settlement_rules_provided"] is False


def test_grouped_market_uses_oldest_side_clock_and_no_arrival_fallback():
    older = NOW - timedelta(seconds=60)
    offer = extract_offers(raw([price("AWAY"), price("HOME", ts=int(older.timestamp()), timestamp=older.isoformat())]))[0]
    assert offer.observed_at == older
    for updates in [{"ts": None, "timestamp": None}, {"ts": int(NOW.timestamp()), "timestamp": older.isoformat()}]:
        offer = extract_offers(raw([price("AWAY"), price("HOME", **updates)]))[0]
        assert offer.observed_at is None


def test_spread_opposing_points_are_preserved_and_different_rungs_separate():
    offers = extract_offers(raw([
        price("AWAY", offer_type="s", line=-3.5), price("HOME", offer_type="s", line=3.5),
        price("AWAY", offer_type="s", line=-4.5), price("HOME", offer_type="s", line=4.5)]))
    assert {offer.line for offer in offers} == {"3.5", "4.5"}
    assert offers[0].outcome_points == {"AWAY": "-3.5", "HOME": "3.5"}


def test_props_never_join_different_players_thresholds_books_or_subject_types():
    rows = [props_row(), props_row(side="Under"), props_row(line=25.5), props_row("Other Player"),
            props_row(book="circa"), props_row("HOME", subject_type="team")]
    offers = extract_offers(raw_props(rows))
    assert len(offers) == 5
    complete = next(offer for offer in offers if set(offer.prices) == {"Over", "Under"})
    assert complete.player_key == "Example Player" and complete.line == "24.5"
    assert all(len(offer.prices) == 1 for offer in offers if offer is not complete)
    assert next(offer for offer in offers if offer.context["subject_type"] == "team").player_key is None


@pytest.mark.parametrize("mutate", [
    lambda rows: rows.append(deepcopy(rows[0])),
    lambda rows: rows[0].update(american_odds="0"),
    lambda rows: rows[0].update(american_odds="NaN"),
    lambda rows: rows[0].update(american_odds="20"),
    lambda rows: rows[0].pop("book"),
])
def test_invalid_odds_or_duplicate_outcomes_are_quarantined(mutate):
    rows = [price("AWAY"), price("HOME")]
    mutate(rows)
    mapper = VerifiedProviderMapper()
    assert mapper(raw(rows)) == []
    assert mapper.rejected_count == 1
    assert next(iter(mapper.quarantine.values()))["reason"] == "Invalid provider payload or price"


def test_unknown_mapping_blocks_quotes_then_explicit_fixture_mapping_normalizes():
    event = raw_props([props_row(), props_row(side="Under")])
    mapper = VerifiedProviderMapper()
    assert mapper(event) == []
    assert mapper.rejected_count == 1
    offer = extract_offers(event)[0]
    market = Market(id="canonical-points", event_id="canonical-event", event_name="Away vs Home",
                    sport="basketball", league="NBA", participants=("AWAY", "HOME"),
                    start_time=NOW + timedelta(hours=1), market_type="player_prop", player="Example Player",
                    player_id="verified-player", statistic="points", line=Decimal("24.5"),
                    outcomes=("over", "under"), settlement=Settlement(scope="player", participation="must_play"))
    mapper.register(VerifiedOfferMapping(signature=offer.signature, market=market,
                    outcome_map={"Over": "over", "Under": "under"}, bookmaker="pinnacle", verified=True,
                    evidence="Offline fixture with independently verified participant and settlement rules"))
    normalized = mapper(event)
    assert len(normalized) == 1
    assert normalized[0][1].bookmaker == "pinnacle"
    assert normalized[0][1].source_time == NOW
    changed = raw_props([props_row(line=25.5), props_row(side="Under", line=25.5)])
    assert not any(quote.status == "active" for _, quote in mapper(changed))


def test_plugin_registration_and_unsupported_transports_are_honest():
    assert PROVIDER_FACTORIES["oddize_rest"] is OddizeRest
    manifest = OddizeRest.manifest
    assert manifest["transports"] == ["rest"]
    assert manifest["streaming"] is False
    assert manifest["authentication"]["credential_env"] == ["ODDIZE_API_KEY"]
    assert manifest["live_tested"] is False
    assert OddizeConfig().enabled is False
    with pytest.raises(ValidationError):
        OddizeConfig(markets=["h2h"])
