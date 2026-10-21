import asyncio
import base64
import json
from collections import Counter
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError
from websockets.exceptions import InvalidMessage

from evplatform.core.plugins import PluginRegistry
from evplatform.plugins.external import NativeMonitor, register_external
from evplatform.venues.common import CredentialsMissing, VenueError, VenueHTTPError
from evplatform.venues.polymarket_us import PolymarketUSClient, PolymarketUSSettings, PolymarketUSMonitorConfig


KEY_ID = "00000000-0000-4000-8000-000000000001"
SECRET = base64.b64encode(bytes(range(32))).decode()


def settings(**kwargs):
    return PolymarketUSSettings(api_key_id=KEY_ID, secret_key=SECRET, requests_per_second=10, **kwargs)


def client_for(handler):
    return PolymarketUSClient(settings(), httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def prepare(client, channel="markets"):
    client.market_slugs = ["sample-nba-game"]
    client._ready = {channel: False}
    requests = client.subscription_requests(channel)
    return requests[0]["subscribe"]


def market_frame(subscription, *, snake=False):
    if snake:
        return {"request_id": subscription["requestId"], "subscription_type": 1,
                "market_data": {"market_slug": "sample-nba-game", "bids": [], "offers": [],
                                "state": "MARKET_STATE_OPEN", "transact_time": "2026-10-20T12:00:00Z"}}
    return {"requestId": subscription["requestId"], "subscriptionType": "SUBSCRIPTION_TYPE_MARKET_DATA",
            "marketData": {"marketSlug": "sample-nba-game", "bids": [{"px": {"value": "0.42", "currency": "USD"}, "qty": "1.25"}],
                           "offers": [], "state": "MARKET_STATE_OPEN", "transactTime": "2026-10-20T12:00:00Z"}}


def test_personal_us_signatures_are_ed25519_and_settings_redact_secrets():
    client = PolymarketUSClient(settings())
    headers = client.headers("GET", "/v1/account/balances", "1735689600000")
    Ed25519PrivateKey.from_private_bytes(bytes(range(32))).public_key().verify(
        base64.b64decode(headers["X-PM-Signature"]), b"1735689600000GET/v1/account/balances")
    assert headers["X-PM-Access-Key"] == KEY_ID
    assert SECRET not in repr(client.settings)
    assert "secret_key" not in client.settings.model_dump()
    assert "api_key_id" not in client.settings.model_dump()


@pytest.mark.parametrize("method,path", [("POST", "/v1/orders"), ("GET", "https://example.com"),
                                         ("DELETE", "/v1/account/balances"), ("GET", "/v1/orders")])
def test_signing_surface_cannot_authorize_writes_or_unknown_routes(method, path):
    client = PolymarketUSClient(settings())
    with pytest.raises(VenueError):
        client.headers(method, path)
    assert not hasattr(client, "place_order")
    assert not hasattr(client, "cancel_order")


@pytest.mark.parametrize("secret", ["not-base64", base64.b64encode(b"short").decode(), ""])
def test_bad_credentials_fail_without_exposing_input(secret):
    client = PolymarketUSClient(PolymarketUSSettings(api_key_id=KEY_ID, secret_key=secret))
    with pytest.raises(CredentialsMissing) as caught:
        client.headers("GET", "/v1/account/balances")
    if secret:
        assert secret not in str(caught.value)
    assert client.authenticated is False


def test_no_invented_personal_sandbox():
    with pytest.raises(ValidationError):
        PolymarketUSSettings(environment="sandbox")
    with pytest.raises(ValidationError):
        PolymarketUSMonitorConfig(environment="sandbox")


async def test_account_check_is_signed_get_and_private_data_not_in_health():
    def handle(request):
        assert request.method == "GET"
        assert str(request.url) == "https://api.polymarket.us/v1/account/balances"
        assert request.headers["X-PM-Access-Key"] == KEY_ID
        return httpx.Response(200, content=b'{"balances":[{"currency":"USD","currentBalance":1.2345,"buyingPower":1.2}]}')

    client = client_for(handle)
    assert await client.connect()
    assert client.account_snapshot["balances"][0]["currentBalance"] == Decimal("1.2345")
    health = client.health_check()
    assert health["authenticated"] and not health["streaming"]
    assert health["read_only"] and not health["reconciled"]
    assert "balances" not in health and KEY_ID not in json.dumps(health)
    await client.disconnect()


@pytest.mark.parametrize("status,state", [(401, "unauthorized"), (403, "access_denied")])
async def test_denial_is_sanitized_and_does_not_authenticate(status, state):
    client = client_for(lambda request: httpx.Response(status, json={"message": SECRET}))
    with pytest.raises(VenueHTTPError):
        await client.connect()
    assert client.health_check()["state"] == state
    assert not client.authenticated
    assert SECRET not in json.dumps(client.health_check())
    await client.disconnect()


async def test_invalid_account_envelope_is_not_authentication_success():
    client = client_for(lambda request: httpx.Response(200, json={"message": "ok"}))
    with pytest.raises(VenueError):
        await client.connect()
    assert not client.authenticated
    await client.disconnect()


async def test_public_discovery_is_bounded_unsigned_and_no_auto_subscription():
    calls = []
    def handle(request):
        calls.append(request)
        assert request.url.host == "gateway.polymarket.us"
        assert "x-pm-access-key" not in request.headers
        offset = int(request.url.params["offset"])
        return httpx.Response(200, json={"markets": [{"slug": f"nba-{offset}"}, {"slug": f"nba-{offset+1}"}]})
    client = client_for(handle)
    result = await client.discover_markets(limit=2, max_pages=2)
    assert len(result["markets"]) == 4 and result["bounded"]
    assert len(calls) == 2 and client.market_slugs == []
    with pytest.raises(ValueError):
        await client.discover_markets(max_pages=6)
    await client.disconnect()


async def test_discovery_repeated_page_and_oversized_page_fail_closed():
    client = client_for(lambda request: httpx.Response(200, json={"markets": [{"slug": "same"}]}))
    with pytest.raises(VenueError, match="Repeated"):
        await client.discover_markets(limit=1, max_pages=2)
    await client.disconnect()


@pytest.mark.parametrize("slugs", [["../escape"], ["a?key=bad"], [f"market-{i}" for i in range(51)]])
def test_subscription_scope_validation(slugs):
    with pytest.raises(ValidationError):
        PolymarketUSMonitorConfig(market_slugs=slugs)


def test_subscriptions_follow_official_sdk_and_private_snapshot_is_explicit():
    client = PolymarketUSClient(settings())
    request = prepare(client)
    assert request["subscriptionType"] == "SUBSCRIPTION_TYPE_MARKET_DATA"
    assert request["marketSlugs"] == ["sample-nba-game"]
    private = client.subscription_requests("private")
    assert {row["subscribe"]["subscriptionType"] for row in private} == {
        "SUBSCRIPTION_TYPE_ORDER", "SUBSCRIPTION_TYPE_ORDER_SNAPSHOT", "SUBSCRIPTION_TYPE_POSITION", "SUBSCRIPTION_TYPE_ACCOUNT_BALANCE"}
    assert all("marketSlugs" not in row["subscribe"] for row in private)


@pytest.mark.parametrize("snake", [False, True])
def test_actual_market_data_required_and_heartbeat_does_not_refresh_book(snake):
    client = PolymarketUSClient(settings())
    request = prepare(client)
    assert client.observe_frame("markets", {"heartbeat": {}}) == "heartbeat"
    assert not client.health_check()["streaming"]
    frame = market_frame(request, snake=snake)
    assert client.observe_frame("markets", frame) == "data"
    received = dict(client.book_received_at)
    client.observe_frame("markets", {"heartbeat": {}})
    assert received == client.book_received_at
    assert client.health_check()["market_messages"] == 1
    assert client.health_check()["streaming"]


def test_unknown_subscription_does_not_establish_delivery_or_readiness():
    client = PolymarketUSClient(settings())
    request = prepare(client)
    frame = market_frame(request)
    frame["requestId"] = "old-connection-id"
    assert client.observe_frame("markets", frame) == "unknown"
    assert not client.books and not client.health_check()["streaming"]


@pytest.mark.parametrize("mutation", ["wrong_slug", "wrong_type", "alias_conflict", "bad_currency", "negative_quantity"])
def test_malformed_or_conflicting_market_evidence_fails_closed(mutation):
    client = PolymarketUSClient(settings())
    request = prepare(client)
    frame = market_frame(request)
    if mutation == "wrong_slug":
        frame["marketData"]["marketSlug"] = "other-game"
    elif mutation == "wrong_type":
        frame["subscriptionType"] = "SUBSCRIPTION_TYPE_TRADE"
    elif mutation == "alias_conflict":
        frame["request_id"] = "conflict"
    elif mutation == "bad_currency":
        frame["marketData"]["bids"][0]["px"]["currency"] = "EUR"
    else:
        frame["marketData"]["bids"][0]["qty"] = "-1"
    with pytest.raises(VenueError):
        client.observe_frame("markets", frame)
    assert not client.books


class FakeSocket:
    def __init__(self):
        self.sent = []
        self.count = 0
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        return None
    async def send(self, raw):
        self.sent.append(json.loads(raw))
    async def recv(self):
        self.count += 1
        if self.count == 1:
            return json.dumps(market_frame(self.sent[0]["subscribe"]))
        return '{"error":"subscription denied"}'
    async def close(self):
        pass


@pytest.mark.parametrize("channel", ["private", "markets"])
async def test_quiet_channel_uses_protocol_keepalive_and_cancels_promptly(monkeypatch, channel):
    quiet_started = asyncio.Event()
    receive_cancelled = asyncio.Event()
    exited = asyncio.Event()
    wait_for_calls = []
    original_wait_for = asyncio.wait_for

    async def accelerated_timeout(awaitable, timeout):
        # Make any accidental message deadline expire during the short test.
        # A quiet healthy connection has no application recv timeout at all.
        wait_for_calls.append(timeout)
        return await original_wait_for(awaitable, timeout=0.001)

    class QuietSocket(FakeSocket):
        async def __aexit__(self, *args):
            exited.set()

        async def recv(self):
            self.count += 1
            if self.count == 1:
                if channel == "markets":
                    return json.dumps(market_frame(self.sent[0]["subscribe"]))
                balance = next(row["subscribe"] for row in self.sent
                               if row["subscribe"]["subscriptionType"] == "SUBSCRIPTION_TYPE_ACCOUNT_BALANCE")
                return json.dumps({"requestId": balance["requestId"], "subscriptionType": balance["subscriptionType"],
                                   "accountBalancesSnapshot": {"balances": []}})
            quiet_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                receive_cancelled.set()
                raise

    socket = QuietSocket()
    def connect(*args, **kwargs):
        assert kwargs["ping_interval"] == 20 and kwargs["ping_timeout"] == 20
        return socket
    monkeypatch.setattr("evplatform.venues.polymarket_us.ws_connect", connect)
    monkeypatch.setattr("evplatform.venues.polymarket_us.asyncio.wait_for", accelerated_timeout)
    client = PolymarketUSClient(settings())
    client.authenticated = True
    await client.configure_subscriptions({"market_slugs": ["sample-nba-game"] if channel == "markets" else [],
                                          "private_updates": channel == "private"})
    stream = client.stream_updates()
    async with asyncio.timeout(1):
        assert (await anext(stream))["kind"] == "data"
        received_at = dict(client.book_received_at)
        waiting = asyncio.create_task(anext(stream))
        await quiet_started.wait()
        await asyncio.sleep(0.02)
        assert not waiting.done()
        assert wait_for_calls == []
        assert client.error_count == client.reconnect_count == 0
        assert client.health_check()["streaming"]
        assert client.book_received_at == received_at
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        await stream.aclose()
    assert receive_cancelled.is_set() and exited.is_set()
    assert not client._sockets and not client.health_check()["streaming"]
    await client.disconnect()


async def test_stream_invalidation_clears_books_and_does_not_retry_subscription_rejection(monkeypatch):
    socket = FakeSocket()
    monkeypatch.setattr("evplatform.venues.polymarket_us.ws_connect", lambda *args, **kwargs: socket)
    client = PolymarketUSClient(settings())
    client.authenticated = True
    await client.configure_subscriptions({"environment": "production", "market_slugs": ["sample-nba-game"], "private_updates": False})
    frames = [frame async for frame in client.stream_updates()]
    assert frames[0]["kind"] == "data" and frames[-1]["type"] == "transport_invalidated"
    assert client.health == "subscription_rejected" and client.reconnect_count == 0
    assert not client.books and not client.health_check()["streaming"]
    await client.disconnect()


async def test_reconnect_resigns_resubscribes_and_recovers_only_from_new_snapshot(monkeypatch):
    calls = []
    socket = FakeSocket()
    def connect(*args, **kwargs):
        calls.append(kwargs["additional_headers"])
        if len(calls) == 1:
            raise TimeoutError("simulated disconnect")
        return socket
    monkeypatch.setattr("evplatform.venues.polymarket_us.ws_connect", connect)
    client = PolymarketUSClient(settings())
    client.authenticated = True
    client._backoff = AsyncMock()
    await client.configure_subscriptions({"environment": "production", "market_slugs": ["sample-nba-game"], "private_updates": False})
    frames = [frame async for frame in client.stream_updates()]
    assert len(calls) == 2
    assert frames[0]["type"] == "transport_invalidated" and frames[1]["kind"] == "data"
    client._backoff.assert_awaited_once()
    assert len(socket.sent) == 1
    await client.disconnect()


async def test_invalid_handshake_reconnects_without_stranding_stream(monkeypatch):
    socket = FakeSocket()
    calls = 0
    def connect(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise InvalidMessage("untrusted handshake contents")
        return socket
    monkeypatch.setattr("evplatform.venues.polymarket_us.ws_connect", connect)
    client = PolymarketUSClient(settings())
    client.authenticated = True
    client._backoff = AsyncMock()
    await client.configure_subscriptions({"market_slugs": ["sample-nba-game"], "private_updates": False})
    async def collect():
        return [frame async for frame in client.stream_updates()]
    frames = await asyncio.wait_for(collect(), timeout=1)
    assert calls == 2 and frames[0]["type"] == "transport_invalidated" and frames[1]["kind"] == "data"
    assert "untrusted handshake contents" not in json.dumps(frames)
    await client.disconnect()


@pytest.mark.parametrize("failure", ["raise", "return"])
async def test_unexpected_child_termination_wakes_consumer_and_clears_readiness(monkeypatch, failure):
    client = PolymarketUSClient(settings())
    client.authenticated = True
    await client.configure_subscriptions({"market_slugs": ["sample-nba-game"], "private_updates": False})
    async def bad_pump(channel, queue):
        client._ready[channel] = True
        client.books["sample-nba-game"] = {"old": "snapshot"}
        if failure == "raise":
            raise RuntimeError(SECRET)
    monkeypatch.setattr(client, "_pump", bad_pump)
    async def collect():
        return [frame async for frame in client.stream_updates()]
    frames = await asyncio.wait_for(collect(), timeout=1)
    assert len(frames) == 1 and frames[0]["type"] == "transport_invalidated"
    assert client.health == "error" and not client.books and not client.health_check()["streaming"]
    assert SECRET not in json.dumps(frames) and SECRET not in json.dumps(client.health_check())
    await client.disconnect()


async def test_monitor_registration_disabled_and_private_frames_remain_backend_only(monkeypatch):
    monkeypatch.delenv("POLYMARKET_US_API_KEY_ID", raising=False)
    monkeypatch.delenv("POLYMARKET_US_SECRET_KEY", raising=False)
    engine = SimpleNamespace(metrics=SimpleNamespace(counters=Counter()),
                             native=SimpleNamespace(on_frame=AsyncMock()))
    registry = PluginRegistry()
    register_external(registry, engine)
    record = registry.records["polymarket_us"]
    assert not record.enabled and record.credential_status == "missing"
    assert record.plugin.manifest.kind == "observer"
    assert record.config["environment"] == "production"
    async def updates():
        yield {"type": "polymarket_us_monitor", "payload": {"private_account_data": "not for dashboard"}}
        yield {"type": "transport_invalidated"}
    record.plugin.client = SimpleNamespace(stream_updates=updates)
    await record.plugin._run()
    assert engine.native.on_frame.await_count == 3
    assert engine.native.on_frame.await_args_list[0].args[0] == "polymarket_us"
    assert record.plugin.manifest.capabilities["native_execution"] is False
    assert record.plugin.frames == 1
    assert "private_account_data" not in json.dumps(registry.public())
