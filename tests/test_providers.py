"""Offline protocol/contract tests. These do not prove live API connectivity."""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from pydantic import ValidationError
from websockets.exceptions import ConnectionClosed
from websockets.frames import Close

from evplatform.core.models import Market, Settlement
from evplatform.integrations import PROVIDER_FACTORIES
from evplatform.integrations.base import (
    Backoff, ProtocolError, ProviderDisabled, ProviderEnvelope, ProviderError,
    ProviderState, QuotaExceeded, QuotaGuard, SequenceTracker, parse_timestamp,
)
from evplatform.integrations.normalization import (
    VerifiedOfferMapping, VerifiedProviderMapper, decimal_price, extract_offers,
)
from evplatform.integrations.oddspapi import OddsPapiRest
from evplatform.integrations.owls import OwlsPinnacleRealtimeRest, OwlsRest
from evplatform.integrations.parlay import ParlayRest, ParlayWebSocket
from evplatform.integrations.templates import map_json_fields, parse_sse, replay_envelopes


NOW = datetime(2026, 10, 20, 18, tzinfo=UTC)


def sample_row(**updates):
    return {
        "bookmaker": "pinnacle", "event_id": "native-game-1",
        "home_team": "Team A", "away_team": "Team B",
        "market_key": "player_points", "player": "Example Player Jr.",
        "line": 24.5, "period": "full_game", "over_price": -110, "under_price": -110,
        "last_update": (NOW - timedelta(seconds=4)).isoformat(),
        **updates,
    }


def envelope(row=None, **updates):
    return ProviderEnvelope(
        "parlay_ws", "odds_update", {"type": "odds_update", "data": [row or sample_row()]},
        NOW, 1_000_000, published_at=NOW - timedelta(seconds=1), **updates,
    )


def market():
    return Market(
        id="points-24.5", event_id="game-1", event_name="Team A vs Team B",
        sport="basketball", league="example", participants=("Team A", "Team B"),
        start_time=NOW + timedelta(hours=1), market_type="player_prop",
        player="Example Player Jr.", player_id="player-1", statistic="points",
        line=Decimal("24.5"), outcomes=("over", "under"),
        settlement=Settlement(scope="player", participation="must_play"),
    )


def mapper_for(raw):
    offer = extract_offers(raw)[0]
    rule = VerifiedOfferMapping(
        signature=offer.signature, market=market(), outcome_map={"over": "over", "under": "under"},
        bookmaker="pinnacle", verified=True, evidence="Offline fixture explicitly verified for this test only",
    )
    return VerifiedProviderMapper([rule])


@pytest.mark.parametrize("factory", PROVIDER_FACTORIES.values())
async def test_missing_key_is_disabled_without_network(factory):
    called = False

    def handle(request):
        nonlocal called
        called = True
        raise AssertionError("Must not connect without credentials")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        provider = factory({"enabled": True}, client=client, credential="")
        assert await provider.connect() is False
        assert provider.state == ProviderState.MISSING_CREDENTIALS
        assert not called
        assert [event async for event in provider.stream_updates()] == []
        await provider.disconnect()


async def test_parlay_rest_documented_header_endpoint_and_cost():
    requests = []

    def handle(request):
        requests.append(request)
        assert request.headers["X-API-Key"] == "fixture-secret"
        assert "fixture-secret" not in str(request.url)
        return httpx.Response(200, json=[], headers={"x-requests-remaining": "97"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        provider = ParlayRest({"enabled": True, "bookmakers": ["pinnacle"]}, client=client, credential="fixture-secret")
        result = await provider.fetch_snapshot("basketball_nba")
        assert requests[0].url.path == "/v1/sports/basketball_nba/odds"
        assert requests[0].url.params["oddsFormat"] == "decimal"
        assert requests[0].url.params["include"] == "normalized,verification"
        assert requests[0].url.params["includeLinks"] == "true"
        assert provider.quota.billable_requests == 3
        assert provider.quota.provider_remaining == 97
        assert result.synthetic is False


async def test_parlay_props_requests_documented_native_status_link_evidence():
    def handle(request):
        assert request.url.path == "/v1/sports/basketball_nba/props"
        assert request.url.params["includeLinks"] == "true"
        assert request.headers["X-API-Key"] == "fixture-secret"
        return httpx.Response(200, json=[])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        provider = ParlayRest({"enabled": True, "bookmakers": ["fanduel"]},
                              client=client, credential="fixture-secret")
        await provider.fetch_props("basketball_nba")


async def test_parlay_coverage_uses_documented_free_diagnostics():
    paths = []

    def handle(request):
        paths.append(request.url.path)
        assert request.headers["X-API-Key"] == "fixture-secret"
        assert request.url.params["bookmakers"] == "pinnacle,fanduel"
        return httpx.Response(200, json={"sources": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        provider = ParlayRest({"enabled": True, "session_request_budget": 1},
                              client=client, credential="fixture-secret")
        await provider.discover_props_coverage("basketball_nba", bookmakers="pinnacle,fanduel")
        await provider.discover_odds_coverage("basketball_nba", bookmakers="pinnacle,fanduel")
        assert paths == ["/v1/sports/basketball_nba/props/coverage",
                         "/v1/sports/basketball_nba/odds/coverage"]
        assert provider.quota.billable_requests == 0


async def test_parlay_stale_board_header_is_preserved_for_normalization():
    def handle(request):
        return httpx.Response(200, json=[], headers={
            "X-Parlay-Stale": "props_scan_busy",
            "X-Parlay-Stale-Built-At": "2026-10-20T19:00:00Z",
            "X-Parlay-Stale-Age-Seconds": "9",
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        provider = ParlayRest({"enabled": True}, client=client, credential="fixture-secret")
        frame = await provider.fetch_props("basketball_nba")
        assert frame.metadata["headers"] == {
            "x-parlay-stale": "props_scan_busy",
            "x-parlay-stale-built-at": "2026-10-20T19:00:00Z",
            "x-parlay-stale-age-seconds": "9",
        }


async def test_owls_bearer_uses_publication_not_quote_time():
    def handle(request):
        assert request.headers["Authorization"] == "Bearer fixture-secret"
        assert request.url.path == "/api/v1/nba/odds"
        return httpx.Response(200, json={"success": True, "data": {}, "meta": {
            "timestamp": NOW.isoformat(), "availableBooks": ["circa", "pinnacle"],
        }}, headers={"X-RateLimit-Remaining-Minute": "8", "X-RateLimit-Remaining-Month": "500"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        provider = OwlsRest({"enabled": True}, client=client, credential="fixture-secret")
        raw = await provider.fetch_snapshot("nba")
        assert raw.published_at == NOW
        assert provider.quota.minute_remaining == 8
        assert provider.quota.provider_remaining == 500
        assert raw.metadata["provider_meta"]["availableBooks"] == ["circa", "pinnacle"]


async def test_auth_refusal_stops_retry_and_redacts():
    calls = 0

    def handle(request):
        nonlocal calls
        calls += 1
        return httpx.Response(403, json={"secret": "fixture-secret"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        provider = OwlsRest({"enabled": True}, client=client, credential="fixture-secret")
        for _ in range(2):
            with pytest.raises(ProviderDisabled) as error:
                await provider.fetch_snapshot("nba")
            assert "fixture-secret" not in str(error.value)
        assert calls == 1
        assert (await provider.health_check())["credential_configured"] is True
        assert "fixture-secret" not in repr(provider)


async def test_odds_papi_account_key_never_leaves_adapter(caplog):
    def handle(request):
        assert request.url.params["apiKey"] == "fixture-secret"
        return httpx.Response(200, json={
            "api_key": "fixture-secret", "current_subscription_id": "active",
            "subscriptions": [{"subscription_id": "active", "is_active": True,
                               "request_limit": 500, "request_count": 15, "sport_ids": [10],
                               "bookmakers": {"pinnacle": {"has_live_odds": False, "has_player_props": True}}}],
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        provider = OddsPapiRest({"enabled": True}, client=client, credential="fixture-secret")
        with caplog.at_level(logging.INFO, logger="httpx"):
            result = await provider.fetch_account()
        assert "fixture-secret" not in caplog.text
        assert "fixture-secret" not in repr(result)
        assert "api_key" not in result.payload
        assert provider.quota.provider_remaining == 485
        assert provider.supports("pinnacle", "props") is True
        assert provider.supports("pinnacle", "live") is False
        await provider.disconnect()


async def test_oddspapi_transport_error_does_not_leak_query_key():
    def handle(request):
        raise httpx.ConnectError(f"Failed {request.url}", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        provider = OddsPapiRest({"enabled": True}, client=client, credential="fixture-secret")
        with pytest.raises(ProviderError) as error:
            await provider.discover_sports()
        assert "fixture-secret" not in str(error.value)
        assert error.value.__suppress_context__ is True
        await provider.disconnect()


async def test_429_cooldown_blocks_another_http_request():
    calls = 0

    def handle(request):
        nonlocal calls
        calls += 1
        return httpx.Response(429, headers={"Retry-After": "60"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        provider = ParlayRest({"enabled": True}, client=client, credential="secret")
        for _ in range(2):
            with pytest.raises(QuotaExceeded):
                await provider.discover_sports()
        assert calls == 1


def test_local_quota_and_capped_backoff():
    clock = [100.0]
    quota = QuotaGuard(2, 3, clock=lambda: clock[0])
    quota.reserve()
    quota.reserve()
    with pytest.raises(QuotaExceeded):
        quota.reserve()
    clock[0] += 61
    quota.reserve()
    clock[0] += 61
    with pytest.raises(QuotaExceeded):
        quota.reserve()
    backoff = Backoff(seed=1)
    assert all(0 < backoff.next_delay() <= 30 for _ in range(100))
    backoff.reset()
    assert backoff.next_delay() <= 1


def test_sequence_gap_requires_new_snapshot():
    tracker = SequenceTracker()
    assert not tracker.delta(10)
    tracker.snapshot(10)
    assert tracker.delta(11)
    assert not tracker.delta(13)
    assert not tracker.delta(14)
    tracker.snapshot(14)
    assert tracker.delta(15)


async def test_parlay_tier_is_disabled_without_connect():
    provider = ParlayWebSocket({"enabled": True, "account_tier": "pro"}, credential="fixture-secret")
    assert await provider.connect() is False
    assert provider.state == ProviderState.ACCESS_DENIED
    await provider.disconnect()


def test_parlay_frame_snapshot_first_and_heartbeat_does_not_refresh_quote():
    provider = ParlayWebSocket()
    with pytest.raises(ProtocolError):
        provider.parse_frame(json.dumps({"type": "odds_update", "data": [sample_row()]}), has_snapshot=False)
    snap = provider.parse_frame(json.dumps({
        "type": "initial_state", "timestamp": NOW.timestamp(), "data": [sample_row()],
        "snapshot_complete": False, "truncated": False, "snapshot_scope": "recent_rows",
    }), has_snapshot=False)
    assert snap.snapshot_complete is False
    assert snap.payload["data"][0]["last_update"] == sample_row()["last_update"]
    heartbeat = provider.parse_frame(json.dumps({"type": "heartbeat", "timestamp": NOW.timestamp()}), has_snapshot=True)
    assert heartbeat.kind == "heartbeat"
    assert extract_offers(heartbeat) == []
    complete = provider.parse_frame(json.dumps({
        "type": "initial_state", "data": [], "snapshot_complete": True,
        "truncated": False, "snapshot_scope": "current_game_board",
    }), has_snapshot=False)
    assert complete.snapshot_complete


@pytest.mark.parametrize("code", [1008, 4001, 4003, 4004, 4005])
def test_parlay_terminal_close_never_retries(code):
    provider = ParlayWebSocket()
    assert provider._apply_close_code(code) is False
    assert provider._apply_close_code(1006) is True


async def test_websocket_uses_header_streams_snapshot_then_updates_and_invalidates():
    messages = [
        {"type": "connected", "tier": "business"},
        {"type": "initial_state", "data": [sample_row()]},
        {"type": "odds_update", "data": [sample_row(over_price=-105)]},
    ]
    args = []

    class Socket:
        async def recv(self):
            if messages:
                return json.dumps(messages.pop(0))
            raise ConnectionClosed(Close(4001, "Tier"), None)

        async def send(self, data):
            pass

        async def close(self):
            pass

    class Connection:
        async def __aenter__(self):
            return Socket()

        async def __aexit__(self, *ignored):
            return False

    def factory(url, **kwargs):
        args.append((url, kwargs))
        return Connection()

    provider = ParlayWebSocket(
        {"enabled": True, "sports": ["basketball_nba"]},
        credential="fixture-secret", ws_factory=factory,
    )
    received = [raw async for raw in provider.stream_updates()]
    assert [raw.kind for raw in received] == ["connected", "initial_state", "odds_update", "feed_invalidated"]
    assert len(args) == 1
    assert "fixture-secret" not in args[0][0]
    assert args[0][1]["additional_headers"]["X-API-Key"] == "fixture-secret"
    assert provider.reconnects == 0
    await provider.disconnect()


def test_verified_mapping_preserves_source_and_provider_clocks():
    raw = envelope()
    mapper = mapper_for(raw)
    matched = mapper(raw)
    assert len(matched) == 1
    _, quote = matched[0]
    assert quote.source_time == NOW - timedelta(seconds=4)
    assert quote.provider_time == NOW - timedelta(seconds=1)
    assert quote.received_at == NOW
    assert quote.fresh(3000, NOW) is False
    assert quote.synthetic is False


@pytest.mark.parametrize("changed", [
    {"line": 25.5}, {"player": "Different Player"}, {"period": "first_half"},
    {"market_key": "player_rebounds"}, {"event_id": "different-game"},
    {"marketStructure": "milestone"},
])
def test_unverified_player_line_period_and_settlement_context_quarantined(changed):
    mapper = mapper_for(envelope())
    assert mapper(envelope(sample_row(**changed))) == []
    assert mapper.quarantine


def test_missing_source_timestamp_is_not_replaced_with_arrival():
    raw = envelope(sample_row(last_update=None))
    quote = mapper_for(raw)(raw)[0][1]
    assert quote.source_time is None
    assert quote.fresh(30000, NOW) is False
    assert parse_timestamp("2026-10-20T12:00:00") is None


def test_disconnect_invalidates_previously_mapped_quotes():
    raw = envelope()
    mapper = mapper_for(raw)
    mapper(raw)
    invalidated = mapper(replace(raw, kind="feed_invalidated", payload={"reason": "gap"}))
    assert len(invalidated) == 1
    assert invalidated[0][1].status == "suspended"
    assert invalidated[0][1].source_time == NOW - timedelta(seconds=4)


def test_remove_without_prices_invalidates_existing_quote():
    raw = envelope()
    mapper = mapper_for(raw)
    mapper(raw)
    removal = {"event_id": "native-game-1", "bookmaker": "pinnacle", "market_key": "player_points", "action": "remove"}
    invalidated = mapper(envelope(removal))
    assert invalidated[0][1].status == "suspended"


def test_invalid_provider_price_invalidates_cached_quote():
    raw = envelope()
    mapper = mapper_for(raw)
    mapper(raw)
    result = mapper(envelope(sample_row(over_price="NaN")))
    assert result[0][1].status == "suspended"
    assert "invalid:parlay_ws" in mapper.quarantine


def test_mapping_requires_verification_and_full_outcome_coverage():
    offer = extract_offers(envelope())[0]
    values = dict(signature=offer.signature, market=market(), outcome_map={"over": "over"},
                  bookmaker="pinnacle", evidence="fixture context verified")
    with pytest.raises(ValidationError):
        VerifiedOfferMapping(**values, verified=True)
    values["outcome_map"]["under"] = "under"
    with pytest.raises(ValidationError):
        VerifiedOfferMapping(**values, verified=False)


def test_mapping_file_version_and_loading(tmp_path):
    raw = envelope()
    mapper = mapper_for(raw)
    path = tmp_path / "mappings.json"
    path.write_text(json.dumps({"version": 1, "mappings": [
        item.model_dump(mode="json") for item in mapper.mappings.values()
    ]}), encoding="utf-8")
    assert VerifiedProviderMapper.from_file(path)(raw)[0][1].market_id == "points-24.5"
    assert VerifiedProviderMapper.from_file(tmp_path / "missing.json").mappings == {}
    path.write_text('{"version": 99, "mappings": []}', encoding="utf-8")
    with pytest.raises(ValidationError):
        VerifiedProviderMapper.from_file(path)


def test_owls_per_book_liveness_never_dates_a_quote():
    event = {
        "id": "native-event", "home_team": "A", "away_team": "B",
        "bookmakers": [{"key": "circa", "last_update": NOW.isoformat(),
                       "markets": [{"key": "h2h", "outcomes": [{"name": "A", "price": -120},
                                                               {"name": "B", "price": 110}]}]}],
    }
    raw = replace(envelope(), provider_id="owls_rest", kind="snapshot",
                  payload={"data": {"circa": [event]}, "meta": {"timestamp": NOW.isoformat()}})
    assert extract_offers(raw)[0].observed_at is None


def test_owls_pinnacle_realtime_uses_main_market_confirmation_only():
    event = {"id": "event-1", "sport_key": "nba", "home_team": "Team A", "away_team": "Team B",
             "commence_time": (NOW + timedelta(hours=2)).isoformat(),
             "status": "scheduled", "phase": "scheduled", "isLive": False,
             "bookmakers": [{"key": "pinnacle", "markets": [
                 {"key": "h2h", "last_seen": (NOW - timedelta(seconds=5)).isoformat(),
                  "last_update": (NOW - timedelta(minutes=2)).isoformat(),
                  "outcomes": [{"name": "Team A", "price": -120},
                               {"name": "Team B", "price": 110}]},
                 {"key": "special_123_total_p0", "last_seen": NOW.isoformat(),
                  "outcomes": [{"name": "over", "price": -110}, {"name": "under", "price": -110}]},
             ]}]}
    payload = {"data": [event], "meta": {"source": "pinnacle", "sport": "nba",
        "available": True, "freshness": {"stale": False}, "feed": {"resubscribing": False}}}
    raw = replace(envelope(), provider_id="owls_pinnacle_realtime", kind="snapshot", payload=payload)
    offers = extract_offers(raw)
    assert len(offers) == 1  # Special/prop markets have different clock semantics.
    offer = offers[0]
    assert offer.status == "active"
    assert offer.observation_verified and offer.observation_complete and offer.market_status_verified
    assert offer.source_observed_at == NOW - timedelta(seconds=5)
    assert offer.source_price_changed_at == NOW - timedelta(minutes=2)
    assert offer.prices == {"Team A": Decimal("1.833333333333333333333333333"), "Team B": Decimal("2.1")}

    def first(changes=None, *, market_changes=None, meta_changes=None):
        market = {**event["bookmakers"][0]["markets"][0], **(market_changes or {})}
        changed_event = {**event, **(changes or {}), "bookmakers": [{"key": "pinnacle", "markets": [market]}]}
        changed = {**payload, "data": [changed_event], "meta": {**payload["meta"], **(meta_changes or {})}}
        return extract_offers(replace(raw, payload=changed))[0]

    for changes in ({"phase": "suspended"}, {"phase": "live"}, {"status": "final"},
                    {"isLive": 0},
                    {"commence_time": (NOW - timedelta(minutes=1)).isoformat()}):
        result = first(changes)
        assert result.status == "suspended" and not result.market_status_verified
    for market_changes in ({"suspended": True}, {"suspended": 0}, {"last_seen": None},
                           {"last_seen": (NOW + timedelta(seconds=1)).isoformat()},
                           {"last_update": NOW.isoformat()}):
        result = first(market_changes=market_changes)
        assert result.status == "suspended" and not result.observation_verified
    for meta_changes in ({"available": False}, {"freshness": {"stale": True}},
                         {"feed": {"resubscribing": True}}, {"feed": {"resubscribing": 0}}):
        result = first(meta_changes=meta_changes)
        assert result.status == "suspended" and not result.observation_verified
    assert extract_offers(replace(raw, payload={**payload, "meta": {**payload["meta"],
                                             "source": "ps3838"}})) == []


async def test_owls_pinnacle_realtime_rest_isolated_source_and_auth():
    requests = []
    source = "pinnacle"
    async def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"data": [], "meta": {
            "source": source, "sport": "nba", "available": True,
            "freshness": {"stale": False}, "timestamp": NOW.isoformat(),
        }})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = OwlsPinnacleRealtimeRest({"enabled": True, "sports": ["nba"]},
                                            client=client, credential="test-secret")
        result = await provider.fetch_snapshot("nba")
        assert result.provider_id == "owls_pinnacle_realtime"
        assert result.snapshot_complete is True
        assert result.published_at == NOW
        assert requests[0].url.path == "/api/v1/nba/realtime"
        assert requests[0].headers["Authorization"] == "Bearer test-secret"
        assert "owls_pinnacle_realtime" in PROVIDER_FACTORIES
        source = "ps3838"
        fallback = await provider.fetch_snapshot("nba")
        assert fallback.kind == "feed_invalidated"
        assert fallback.payload["sport"] == "nba"
        assert extract_offers(fallback) == []
        await provider.disconnect()


async def test_realtime_rest_poll_failure_invalidates_cached_reference():
    async def fail(_request):
        raise httpx.ConnectError("transport unavailable")
    async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as client:
        provider = OwlsPinnacleRealtimeRest({"enabled": True, "sports": ["nba"]},
                                            client=client, credential="test-secret")
        stream = provider.stream_updates()
        frame = await stream.__anext__()
        assert frame.kind == "feed_invalidated"
        assert frame.payload == {"reason": "provider_poll_failed"}
        assert extract_offers(frame) == []
        await stream.aclose()
        await provider.disconnect()


def test_owls_realtime_fallback_requires_new_market_confirmation_to_recover():
    def snapshot(seen, received):
        return ProviderEnvelope("owls_pinnacle_realtime", "snapshot", {
            "meta": {"source": "pinnacle", "sport": "nba", "available": True,
                     "freshness": {"stale": False}},
            "data": [{"id": "event-1", "sport_key": "nba", "home_team": "Team A",
                      "away_team": "Team B", "commence_time": (NOW + timedelta(hours=2)).isoformat(),
                      "phase": "scheduled", "status": "scheduled", "isLive": False,
                      "bookmakers": [{"key": "pinnacle", "markets": [{
                          "key": "h2h", "last_seen": seen.isoformat(),
                          "last_update": (NOW - timedelta(minutes=2)).isoformat(),
                          "outcomes": [{"name": "Team A", "price": -120},
                                       {"name": "Team B", "price": 110}],
                      }]}]}],
        }, received, 1_000_000, snapshot_complete=True, metadata={"sport": "nba"})

    first = snapshot(NOW - timedelta(seconds=5), NOW)
    offer = extract_offers(first)[0]
    canonical = market().model_copy(update={
        "id": "nba-h2h", "market_type": "moneyline", "player": None, "player_id": None,
        "statistic": None, "line": None, "outcomes": ("Team A", "Team B"),
        "settlement": Settlement(),
    })
    mapper = VerifiedProviderMapper([VerifiedOfferMapping(
        signature=offer.signature, market=canonical,
        outcome_map={"Team A": "Team A", "Team B": "Team B"},
        bookmaker="pinnacle", verified=True, evidence="Controlled exact market fixture",
    )])
    quotes = mapper(first)
    assert len(quotes) == 1 and quotes[0][1].observation_complete
    assert quotes[0][1].market_status_verified

    invalidated = mapper(ProviderEnvelope(
        "owls_pinnacle_realtime", "feed_invalidated", {"sport": "nba"},
        NOW + timedelta(seconds=1), 2_000_000, metadata={"sport": "nba"}))
    assert len(invalidated) == 1 and invalidated[0][1].status == "suspended"
    same = mapper(snapshot(NOW - timedelta(seconds=5), NOW + timedelta(seconds=2)))
    assert len(same) == 1 and same[0][1].observation_complete is False
    assert same[0][1].verification_reason == "new_complete_verified_observation_required"
    fresh = mapper(snapshot(NOW + timedelta(seconds=2), NOW + timedelta(seconds=3)))
    assert fresh[-1][1].status == "active"
    assert fresh[-1][1].observation_complete is True
    assert fresh[-1][1].market_status_verified is True


def test_oddspapi_price_and_bookmaker_clocks_are_distinct():
    outcome = {"price": 1.91, "active": True, "bookmakerChangedAt": None, "changedAt": NOW.isoformat()}
    raw = replace(envelope(), provider_id="oddspapi_rest", kind="snapshot", payload={
        "fixtureId": "fixture1", "statusId": 0, "bookmakerOdds": {
            "pinnacle": {"markets": {"10": {"marketActive": True, "outcomes": {
                "11": {"players": {"0": outcome}}, "12": {"players": {"0": outcome}},
            }}}},
        },
    })
    offer = extract_offers(raw)[0]
    assert offer.observed_at is None
    assert offer.provider_time == NOW
    assert offer.prices == {"11": Decimal("1.91"), "12": Decimal("1.91")}


def test_oddspapi_explicit_inactive_bookmaker_blocks_its_active_market():
    selection = {"price": 1.91, "active": True, "changedAt": NOW.isoformat()}
    raw = replace(envelope(), provider_id="oddspapi_rest", kind="snapshot", payload={
        "fixtureId": "fixture1", "statusId": 0, "bookmakerOdds": {
            "pinnacle": {"bookmakerIsActive": False, "suspended": False,
                         "markets": {"10": {"marketActive": True, "outcomes": {
                             "11": {"players": {"0": selection}},
                             "12": {"players": {"0": selection}},
                         }}}},
        },
    })
    offer = extract_offers(raw)[0]
    assert offer.status == "suspended"
    assert offer.market_status_verified is False


def test_oddspapi_recent_book_change_and_active_selection_verify_both_sides():
    selection = {"price": 1.91, "active": True,
                 "bookmakerChangedAt": NOW.isoformat(), "changedAt": NOW.isoformat()}
    fixture = {"fixtureId": "fixture1", "statusId": 0, "hasOdds": True,
               "bookmakerOdds": {"pinnacle": {
                   "bookmakerIsActive": True, "suspended": False,
                   "markets": {"10": {"marketActive": True, "outcomes": {
                       "11": {"players": {"0": selection}},
                       "12": {"players": {"0": selection}},
                   }}},
               }}}
    raw = replace(envelope(), provider_id="oddspapi_rest", kind="snapshot", payload=fixture)
    offer = extract_offers(raw)[0]
    assert offer.status == "active"
    assert offer.market_status_verified is True
    assert offer.observed_at == NOW
    assert offer.observation_verified is True
    assert offer.observation_complete is True
    assert offer.source_observed_at == NOW
    assert offer.source_price_changed_at == NOW
    assert offer.verification_reason == "bookmaker_price_change_with_active_market"

    # Missing, inactive, or suspended fields cannot be interpreted as a
    # verified current quote, even when price-change timestamps are recent.
    for field, value in (("hasOdds", None), ("hasOdds", False), ("statusId", None),
                         ("statusId", 2)):
        changed = {**fixture, field: value}
        result = extract_offers(replace(raw, payload=changed))[0]
        assert result.status == "suspended"
        assert result.market_status_verified is False
    for field, value in (("bookmakerIsActive", None), ("bookmakerIsActive", False),
                         ("suspended", None), ("suspended", True)):
        changed_book = {**fixture["bookmakerOdds"]["pinnacle"], field: value}
        changed = {**fixture, "bookmakerOdds": {"pinnacle": changed_book}}
        result = extract_offers(replace(raw, payload=changed))[0]
        assert result.status == "suspended"
        assert result.market_status_verified is False
    changed_market = {**fixture["bookmakerOdds"]["pinnacle"]["markets"]["10"], "marketActive": None}
    changed = {**fixture, "bookmakerOdds": {"pinnacle": {
        **fixture["bookmakerOdds"]["pinnacle"], "markets": {"10": changed_market},
    }}}
    result = extract_offers(replace(raw, payload=changed))[0]
    assert result.status == "suspended"
    assert result.market_status_verified is False


def test_oddspapi_book_change_is_not_replaced_by_rest_arrival_or_one_recent_side():
    def make_fixture(left, right):
        return {"fixtureId": "fixture1", "statusId": 0, "hasOdds": True,
                "bookmakerOdds": {"pinnacle": {"bookmakerIsActive": True,
                    "suspended": False, "markets": {"10": {"marketActive": True,
                        "outcomes": {"11": {"players": {"0": left}},
                                     "12": {"players": {"0": right}}}}}}}}

    recent = {"price": 1.91, "active": True,
              "bookmakerChangedAt": NOW.isoformat(), "changedAt": NOW.isoformat()}
    missing = {**recent, "bookmakerChangedAt": None}
    raw = replace(envelope(), provider_id="oddspapi_rest", kind="snapshot",
                  payload=make_fixture(recent, missing))
    offer = extract_offers(raw)[0]
    assert offer.market_status_verified is True
    assert offer.observation_verified is False
    assert offer.observation_complete is False
    assert offer.source_observed_at is None

    old = {**recent, "bookmakerChangedAt": (NOW - timedelta(minutes=5)).isoformat(),
           "changedAt": (NOW - timedelta(minutes=5)).isoformat()}
    offer = extract_offers(replace(raw, payload=make_fixture(recent, old)))[0]
    assert offer.source_observed_at == NOW - timedelta(minutes=5)
    assert offer.observation_verified is True
    assert (raw.received_at - offer.source_observed_at).total_seconds() == 300

    impossible = {**recent, "bookmakerChangedAt": (NOW + timedelta(seconds=1)).isoformat()}
    offer = extract_offers(replace(raw, payload=make_fixture(impossible, recent)))[0]
    assert offer.observation_verified is False
    assert offer.source_observed_at is None


async def test_sse_multiline_comments_and_truncated_event():
    async def lines():
        for line in [": heartbeat", "id: 12", "event: projection", 'data: {"x":',
                     "data: 1}", "", 'data: {"truncated":']:
            yield line
    parsed = [event async for event in parse_sse(lines())]
    assert parsed == [{"event": "projection", "id": "12", "data": '{"x":\n1}'}]
    assert json.loads(parsed[0]["data"]) == {"x": 1}


async def test_replay_preserves_clocks_and_is_repeatable():
    fixtures = [envelope(), envelope(sample_row(over_price=105))]
    one = [row async for row in replay_envelopes(fixtures)]
    two = [row async for row in replay_envelopes(fixtures)]
    assert one == two
    assert one[0].received_at == NOW


def test_strict_json_projection_and_odds_math():
    assert map_json_fields({"quote": {"observedAt": "source"}}, {"timestamp": "quote.observedAt"}) == {"timestamp": "source"}
    with pytest.raises(ProtocolError):
        map_json_fields({}, {"timestamp": "quote.observedAt"})
    assert decimal_price(-200, "american") == Decimal("1.5")
    assert decimal_price(150, "american") == Decimal("2.5")


def test_parlay_numeric_source_timestamp_is_milliseconds():
    raw = envelope(sample_row(last_update=int((NOW - timedelta(seconds=4)).timestamp() * 1000)))
    assert extract_offers(raw)[0].observed_at == NOW - timedelta(seconds=4)


def test_partial_moneyline_frames_wait_for_complete_verified_outcome_set():
    first = sample_row(market_key="h2h", player=None, line=None, over_price=None, under_price=None,
                       team="Team A", price=-120)
    second = {**first, "team": "Team B", "price": 110,
              "last_update": (NOW - timedelta(seconds=2)).isoformat()}
    mapper = VerifiedProviderMapper()
    assert mapper(envelope(first)) == []
    assert mapper(envelope(second)) == []
    identity = next(reversed(mapper.quarantine.values()))
    ml = market().model_copy(update={
        "id": "ml", "market_type": "moneyline", "player": None, "player_id": None,
        "statistic": None, "line": None, "outcomes": ("Team A", "Team B"),
    })
    mapper.register(VerifiedOfferMapping(
        signature=identity["signature"], market=ml, outcome_map={"Team A": "Team A", "Team B": "Team B"},
        bookmaker="pinnacle", verified=True, evidence="Explicit two-way settlement fixture verification",
    ))
    result = mapper(envelope(second))
    assert len(result) == 1
    assert result[0][1].source_time == NOW - timedelta(seconds=4)
    assert set(result[0][1].decimal_odds) == {"Team A", "Team B"}
    mapper(replace(envelope(), kind="feed_invalidated", payload={}))
    # Reconnect never joins a new side with an old cached side.
    assert mapper(envelope(second)) == []


def test_out_of_order_prices_do_not_replace_newer_quote():
    raw = envelope()
    mapper = mapper_for(raw)
    mapper(raw)
    older = envelope(sample_row(last_update=(NOW - timedelta(seconds=20)).isoformat(), over_price=200))
    assert mapper(older) == []


def test_incomplete_snapshot_suspends_previously_mapped_quote():
    raw = envelope()
    mapper = mapper_for(raw)
    mapper(raw)
    empty = replace(raw, kind="initial_state", payload={"type": "initial_state", "data": []})
    assert mapper(empty)[0][1].status == "suspended"


async def test_oddspapi_scheduled_fixture_that_became_live_is_blocked():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(
        200, json={"fixtureId": "test", "statusId": 1, "bookmakerOdds": {}},
    ))) as client:
        provider = OddsPapiRest({"enabled": True}, client=client, credential="fixture-secret")
        raw = await provider.fetch_fixture("test")
        assert raw.kind == "coverage_blocked"
        assert extract_offers(raw) == []
        await provider.disconnect()


def test_manually_wrong_canonical_threshold_is_rejected():
    raw = envelope()
    offer = extract_offers(raw)[0]
    bad_rule = VerifiedOfferMapping(
        signature=offer.signature, market=market().model_copy(update={"line": Decimal("25.5")}),
        outcome_map={"over": "over", "under": "under"}, bookmaker="pinnacle",
        verified=True, evidence="Incorrect fixture mapping must not evade line check",
    )
    mapper = VerifiedProviderMapper([bad_rule])
    assert mapper(raw) == []
    assert next(iter(mapper.quarantine.values()))["reason"] == "Source and canonical thresholds differ"


def test_oddspapi_props_need_entitlement_and_catalog_threshold():
    selection = {"price": 1.91, "active": True, "bookmakerChangedAt": NOW.isoformat(), "changedAt": NOW.isoformat()}
    payload = {"fixtureId": "fixture1", "statusId": 0, "bookmakerOdds": {
        "pinnacle": {"markets": {"10": {"marketActive": True, "outcomes": {
            "11": {"players": {"p1": selection}}, "12": {"players": {"p1": selection}},
        }}}},
    }}
    raw = replace(envelope(), provider_id="oddspapi_rest", kind="snapshot", payload=payload)
    assert extract_offers(raw) == []
    permitted = replace(raw, metadata={
        "allow_props": True, "props_books": ["pinnacle"],
        "market_catalog": {"10": {"handicap": 24.5, "period": "fulltime", "playerProp": True}},
    })
    offer = extract_offers(permitted)[0]
    assert offer.line == "24.5"
    assert offer.player_key == "p1"
    assert offer.period == "fulltime"
