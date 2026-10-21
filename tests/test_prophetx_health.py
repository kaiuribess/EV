"""Offline ProphetX lifecycle checks; no live API access or account credentials."""
import asyncio
import json
import time
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from evplatform.core.plugins import PluginRegistry
from evplatform.plugins.external import (
    NativeMonitor, ProphetXMonitorConfig, migrate_prophetx_monitor_config, register_external,
)
from evplatform.venues.common import VenueError
from evplatform.venues.prophetx import ProphetXClient, ProphetXSettings


def settings():
    return ProphetXSettings(api_key="offline-key", secret_key="offline-secret", requests_per_second=100)


def session(token="offline-token", refresh="offline-refresh"):
    return {"data": {"access_token": token, "refresh_token": refresh,
                     "access_expire_time": time.time() + 600}}


def registration():
    return {"data": {"success": True, "channel_count": 2, "channel_limit": 10,
                     "authenticated": {"auth": "signin-auth", "user_data": "{}"},
                     "authorized_channel": [
                         {"channel_name": name, "auth": f"{name}-auth", "scope": {"event_id": event},
                          "binding_events": [{"name": "v2.market_selections"}]}
                         for name, event in [("channel-a", 1), ("channel-b", 2)]]}}


class Socket:
    def __init__(self, frames):
        self.frames = iter(frames)
        self.sent = []
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return json.dumps(next(self.frames))
        except StopIteration:
            raise StopAsyncIteration

    async def send(self, payload):
        self.sent.append(json.loads(payload))

    async def close(self):
        self.closed = True


async def test_auth_health_and_refresh_reuse_the_existing_session():
    calls = []

    def handler(request):
        calls.append((request.url.path, json.loads(request.content)))
        if request.url.path.endswith("/auth/refresh"):
            assert json.loads(request.content) == {"refresh_token": "offline-refresh"}
            return httpx.Response(200, json=session("renewed-token", "renewed-refresh"))
        return httpx.Response(200, json=session())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        native = ProphetXClient(settings(), http)
        assert not native.health_check()["authenticated"]
        await native.connect()
        assert native.health_check()["authenticated"]
        assert native.health_check()["state"] == "authenticated"
        assert not native.health_check()["streaming"]
        await native.connect()
        native._access_expires = time.time() + 20
        await native.connect()
        assert native._access_token == "renewed-token"
        assert native.health_check()["authenticated"]
        assert not native.health_check()["trading_enabled"]
    assert [path for path, _ in calls] == ["/partner/auth/login", "/partner/auth/refresh"]


@pytest.mark.parametrize("status,code,state", [
    (401, None, "unauthorized"), (403, None, "access_denied"),
    (451, None, "geolocation_required"), (400, "session_num_exceed", "session_limit"),
    (500, None, "auth_outcome_unknown"), (429, None, "auth_outcome_unknown"),
])
async def test_denied_or_indeterminate_login_is_not_retried(status, code, state):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={"code": code, "message": "untrusted server detail"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        native = ProphetXClient(settings(), http)
        for _ in range(2):
            with pytest.raises(VenueError):
                await native.connect()
        health = native.health_check()
        assert health["state"] == state
        assert not health["authenticated"]
        assert not health["streaming"]
        assert "untrusted server detail" not in health["reason"]
    assert len(calls) == 1


@pytest.mark.parametrize("outcome", ["timeout", "malformed"])
async def test_unknown_login_outcome_does_not_create_a_second_session(outcome):
    calls = []

    def handler(request):
        calls.append(request)
        if outcome == "timeout":
            raise httpx.ReadTimeout("possibly accepted", request=request)
        return httpx.Response(200, json={"data": {"access_token": "incomplete"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        native = ProphetXClient(settings(), http)
        for _ in range(2):
            with pytest.raises(VenueError):
                await native.connect()
        assert native.health == "auth_outcome_unknown"
    assert len(calls) == 1


async def test_queued_auth_call_observes_the_first_call_unknown_outcome():
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def handler(request):
        calls.append(request)
        entered.set()
        await release.wait()
        raise httpx.ReadTimeout("possibly accepted", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        native = ProphetXClient(settings(), http)
        first = asyncio.create_task(native.connect())
        await entered.wait()
        second = asyncio.create_task(native.connect())
        await asyncio.sleep(0)
        release.set()
        outcomes = await asyncio.gather(first, second, return_exceptions=True)
        assert all(isinstance(outcome, VenueError) for outcome in outcomes)
    assert len(calls) == 1


async def test_refresh_rejection_never_falls_back_to_login():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(401, json={"code": "invalid_token"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        native = ProphetXClient(settings(), http)
        native._access_token, native._refresh_token = "expired-token", "existing-refresh"
        native._access_expires, native.authenticated = time.time() - 1, True
        for _ in range(2):
            with pytest.raises(VenueError):
                await native.connect()
        assert not native.health_check()["authenticated"]
    assert calls == ["/partner/auth/refresh"]


async def test_stream_waits_for_signin_and_each_channel_acknowledgement(monkeypatch):
    import evplatform.venues.prophetx as module

    def market(channel, identifier):
        return {"event": "v2.market_selections", "channel": channel,
                "data": {"info": {"id": identifier}}}

    socket = Socket([
        {"event": "pusher:connection_established", "data": {"socket_id": "1.2"}},
        market("channel-a", "before-signin"),
        {"event": "pusher_internal:subscription_succeeded", "channel": "channel-b", "data": "{}"},
        {"event": "pusher:signin_success", "data": {}},
        market("channel-a", "before-subscription"),
        {"event": "pusher_internal:subscription_succeeded", "channel": "channel-a", "data": "{}"},
        market("channel-a", "first-confirmed"),
        {"event": "pusher_internal:subscription_succeeded", "channel": "channel-b", "data": "{}"},
        market("channel-b", "all-confirmed"),
    ])
    monkeypatch.setattr(module, "ws_connect", lambda *args, **kwargs: socket)

    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: pytest.fail("Offline test"))) as http:
        native = ProphetXClient(settings(), http)

        async def request(method, path, **kwargs):
            if path == "/websocket/connection-config":
                return {"key": "app", "ws_host": "talaria.example.test"}
            assert path == "/v4/mm/websocket"
            return registration()

        native.request = request
        stream = native.stream_updates()
        try:
            assert (await anext(stream))["type"] == "snapshot_required"
            assert native.health == "authenticating_stream"
            assert not native.health_check()["streaming"]
            first = await anext(stream)
            assert first["data"]["info"]["id"] == "first-confirmed"
            assert native.health == "subscribing"
            assert not native.health_check()["streaming"]
            final = await anext(stream)
            assert final["data"]["info"]["id"] == "all-confirmed"
            assert native.health_check()["streaming"]
            assert native.health_check()["subscriptions_confirmed"] == 2
            assert native.health_check()["market_frames"] == 2
            assert [frame["event"] for frame in socket.sent] == [
                "pusher:signin", "pusher:subscribe", "pusher:subscribe"]
        finally:
            await stream.aclose()
        assert not native.health_check()["streaming"]


async def test_http_subscription_quota_rejection_stops_reconnecting(monkeypatch):
    import evplatform.venues.prophetx as module

    calls = []
    socket = Socket([{"event": "pusher:connection_established", "data": {"socket_id": "1.2"}}])
    monkeypatch.setattr(module, "ws_connect", lambda *args, **kwargs: socket)

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/auth/login"):
            return httpx.Response(200, json=session())
        if request.url.path.endswith("connection-config"):
            return httpx.Response(200, json={"key": "app", "ws_host": "talaria.example.test"})
        return httpx.Response(400, json={"code": "exceed_subscription_count"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        native = ProphetXClient(settings(), http)
        frames = [frame async for frame in native.stream_updates()]
        assert len(frames) == 1
        assert frames[0]["type"] == "transport_invalidated"
        assert native.health == "subscription_rejected"
        assert native.reconnect_count == 0
    assert calls.count("/partner/auth/login") == 1
    assert calls.count("/partner/v4/mm/websocket") == 1


async def test_unsubscribe_preserves_health_of_remaining_confirmed_channels():
    async with httpx.AsyncClient() as http:
        native = ProphetXClient(settings(), http)
        native._ws = Socket([])
        native.channels = {"channel-a": {}, "channel-b": {}}
        native._confirmed_channels = set(native.channels)
        native._signin_confirmed, native.health = True, "streaming"
        await native.unsubscribe(["channel-a"])
        assert native.health_check()["streaming"]
        assert native._confirmed_channels == {"channel-b"}


async def test_quiet_socket_refreshes_the_same_session(monkeypatch):
    import evplatform.venues.prophetx as module

    paths = []
    socket = Socket([])

    async def immediate_sleep(delay):
        pass

    def handler(request):
        paths.append(request.url.path)
        return httpx.Response(200, json=session("renewed"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        native = ProphetXClient(settings(), http)
        native._ws, native._refresh_token = socket, "existing-refresh"
        native._access_expires = time.time() + 10
        original_connect = native.connect

        async def refresh_then_stop():
            await original_connect()
            native._closed = True

        native.connect = refresh_then_stop
        monkeypatch.setattr(module.asyncio, "sleep", immediate_sleep)
        await native._refresh_while_connected()
    assert paths == ["/partner/auth/refresh"]


async def test_quiet_socket_reconnects_to_rediscover_config(monkeypatch):
    import evplatform.venues.prophetx as module

    clock = iter([0, 25 * 60])
    monkeypatch.setattr(module, "time", SimpleNamespace(time=time.time, monotonic=lambda: next(clock)))

    async def immediate_sleep(delay):
        pass

    monkeypatch.setattr(module.asyncio, "sleep", immediate_sleep)
    async with httpx.AsyncClient() as http:
        native = ProphetXClient(settings(), http)
        native._ws = Socket([])
        native._access_expires = time.time() + 600
        await native._refresh_while_connected()
        assert native._ws.closed


async def test_monitor_passes_configured_events_and_prop_subtypes(monkeypatch):
    configured = []

    class Client:
        get_manifest = staticmethod(ProphetXClient.get_manifest)

        def __init__(self, native_settings):
            pass

        async def connect(self):
            pass

        async def configure_subscriptions(self, config):
            configured.append(config)

        async def disconnect(self):
            pass

        async def stream_updates(self):
            if False:
                yield {}

    config = ProphetXMonitorConfig(event_ids=["1", "2", "1"], prop_subtypes={"2": ["player_total_points"]}).model_dump()
    monitor = NativeMonitor(Client, ProphetXSettings, {}, None, config_model=ProphetXMonitorConfig)
    await monitor.start(SimpleNamespace(config=config))
    try:
        assert configured == [config]
        assert configured[0]["event_ids"] == ["1", "2"]
    finally:
        await monitor.stop()

    async with httpx.AsyncClient() as http:
        native = ProphetXClient(settings(), http)
        await native.configure_subscriptions(config)
        assert native.subscriptions == [{"type": "event", "ids": ["1", "2"]},
                                        {"type": "event_subtype", "ids": ["2:player_total_points"]}]


@pytest.mark.parametrize("config", [{"event_ids": ["abc"]}, {"event_ids": ["١"]},
                                    {"prop_subtypes": {"1": [" "]}}, {"prop_subtypes": {"abc": ["moneyline"]}}])
def test_event_and_prop_configuration_rejects_invalid_scope(config):
    with pytest.raises(ValidationError):
        ProphetXMonitorConfig.model_validate(config)


async def test_legacy_configuration_migrates_without_crashing_or_guessing_scope(monkeypatch):
    import evplatform.plugins.external as module

    # Registration must not consult real credentials in this offline test.
    monkeypatch.setattr(module.os, "getenv", lambda name: None)
    registry = PluginRegistry()
    register_external(registry, None)
    clean = registry.configure("prophetx", {"environment": "sandbox", "market_ids": []})
    assert clean["event_ids"] == []
    assert "market_ids" not in clean
    preserved = registry.configure("prophetx", {"market_ids": ["legacy-strike-id"]})
    assert preserved["legacy_market_ids"] == ["legacy-strike-id"]
    monitor = registry.records["prophetx"].plugin
    await monitor.start(SimpleNamespace(config=preserved))
    health = await monitor.health_check()
    assert health["status"] == "blocked"
    assert health["state"] == "configuration_required"
    assert "event_ids" in health["message"]
    assert monitor.client is None
    assert migrate_prophetx_monitor_config({"event_ids": ["1"]}) == {"event_ids": ["1"]}
