"""Offline Novig connection health checks; no credentials or network access."""
import json

import httpx
import pytest
from websockets.datastructures import Headers
from websockets.exceptions import InvalidHandshake, InvalidStatus
from websockets.http11 import Response

import evplatform.venues.novig as novig_module
from evplatform.venues.novig import NovigClient


PRIVATE_DETAIL = "private-server-detail-must-not-appear"
SUBSCRIBED = {"nonce": 1, "subscribed": {"private": ["orders", "positions"]},
              "snapshot": {}, "orders": {"seq": 0, "open": []},
              "positions": {"seq": 0, "positions": []}}


class Socket:
    def __init__(self, frames=(), failure=None):
        self.frames = iter(frames)
        self.failure = failure
        self.sent = []

    async def __aenter__(self):
        if self.failure:
            raise self.failure
        return self

    async def __aexit__(self, *args):
        return None

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
        return None


def handshake_failure(status):
    return InvalidStatus(Response(status, PRIVATE_DETAIL, Headers({"X-Private": PRIVATE_DETAIL}),
                                  body=PRIVATE_DETAIL.encode()))


@pytest.fixture
async def native(monkeypatch):
    def echo(request):
        assert request.method == "POST"
        assert request.url.path == "/v3/echo"
        return httpx.Response(200, json=json.loads(request.content))

    async with httpx.AsyncClient(transport=httpx.MockTransport(echo)) as http:
        client = NovigClient(client=http)
        monkeypatch.setattr(client, "_load_key", lambda: None)
        monkeypatch.setattr(client, "headers", lambda *args, **kwargs: {})
        yield client
        await client.disconnect()


async def test_echo_authenticates_without_claiming_streaming(native):
    assert native.health_check()["authenticated"] is False
    assert await native.connect() == {"probe": "evplatform"}
    health = native.health_check()
    assert health["state"] == "authenticated"
    assert health["authenticated"] is True
    assert health["streaming"] is False
    assert not native.settings.gate.enabled


@pytest.mark.parametrize("status,state,authenticated", [
    (401, "unauthorized", False),
    (403, "access_denied", True),
    (451, "geolocation_required", True),
])
async def test_terminal_upgrade_denial_preserves_sanitized_health(
    native, monkeypatch, status, state, authenticated
):
    attempts = []
    def connect(*args, **kwargs):
        attempts.append(1)
        return Socket(failure=handshake_failure(status))

    async def no_retry(*args):
        pytest.fail("A denied handshake must not retry")

    monkeypatch.setattr(novig_module, "ws_connect", connect)
    monkeypatch.setattr(native, "_backoff", no_retry)
    await native.connect()
    frames = [frame async for frame in native.stream_updates()]
    health = native.health_check()
    assert len(attempts) == len(frames) == 1
    assert frames[0]["type"] == "transport_invalidated"
    assert health["state"] == state
    assert health["authenticated"] is authenticated
    assert health["streaming"] is False
    assert health["errors"] == 1
    assert str(status) in health["reason"]
    assert PRIVATE_DETAIL not in json.dumps([health, frames])
    assert native._ws is None
    if status == 403:
        assert "trading or trading::read" in health["reason"]
        assert "management" not in health["reason"]


@pytest.mark.parametrize("failure", [handshake_failure(429), handshake_failure(503),
                                     InvalidHandshake(PRIVATE_DETAIL), TimeoutError(PRIVATE_DETAIL)])
async def test_transient_upgrade_failure_reconnects_and_resubscribes(native, monkeypatch, failure):
    sessions = [Socket(failure=failure), Socket([SUBSCRIBED])]
    attempts, backoffs = [], []

    def connect(*args, **kwargs):
        attempts.append(1)
        return sessions.pop(0)

    async def backoff(failures):
        backoffs.append(failures)

    monkeypatch.setattr(novig_module, "ws_connect", connect)
    monkeypatch.setattr(native, "_backoff", backoff)
    await native.connect()
    updates = native.stream_updates()
    try:
        invalidation = await anext(updates)
        assert invalidation["all_books_invalid"] is True
        health = native.health_check()
        assert health["state"] == "reconnecting"
        assert health["streaming"] is False
        assert PRIVATE_DETAIL not in json.dumps([health, invalidation])
        assert await anext(updates) == SUBSCRIBED
        assert native.health_check()["streaming"] is True
        assert len(attempts) == 2
        assert backoffs == [0]
    finally:
        await updates.aclose()


async def test_streaming_waits_for_subscription_acknowledgement(native, monkeypatch):
    heartbeat = {"heartbeat": {"ts": 1755000000000}}
    socket = Socket([heartbeat, SUBSCRIBED])
    monkeypatch.setattr(novig_module, "ws_connect", lambda *args, **kwargs: socket)
    updates = native.stream_updates()
    try:
        assert await anext(updates) == heartbeat
        assert native.health_check()["state"] == "subscribing"
        assert native.health_check()["streaming"] is False
        assert native.health_check()["authenticated"] is True
        assert socket.sent == [{"nonce": 1, "subscribe": {"private": ["orders", "positions"]}}]
        assert await anext(updates) == SUBSCRIBED
        assert native.health_check()["state"] == "streaming"
        assert native.health_check()["streaming"] is True
    finally:
        await updates.aclose()
    assert native.health_check()["state"] == "disconnected"
    assert native.health_check()["streaming"] is False


@pytest.mark.parametrize("error_frame", [
    {"nonce": 1, "error": {"code": "SCOPE_INSUFFICIENT", "message": PRIVATE_DETAIL}},
    {"nonce": 1, "code": "EMPTY_SELECTION", "message": PRIVATE_DETAIL},
    {"nonce": 1, "error": PRIVATE_DETAIL},
])
async def test_subscription_rejection_is_terminal_and_sanitized(native, monkeypatch, error_frame):
    monkeypatch.setattr(novig_module, "ws_connect", lambda *args, **kwargs: Socket([error_frame]))
    frames = [frame async for frame in native.stream_updates()]
    health = native.health_check()
    assert len(frames) == 1
    assert frames[0]["reason"] == "subscription_rejected"
    assert health["state"] == "subscription_rejected"
    assert health["authenticated"] is True
    assert health["streaming"] is False
    assert health["reconnects"] == 0
    assert PRIVATE_DETAIL not in json.dumps([health, frames])


@pytest.mark.parametrize("first_frame", [
    {"error": {"code": "RATE_LIMIT_EXCEEDED", "message": PRIVATE_DETAIL}},
    {"nonce": 1, "subscribed": {"private": ["orders"]}},
])
async def test_transient_or_invalid_subscription_acknowledgement_reconnects(native, monkeypatch, first_frame):
    sessions = [Socket([first_frame]), Socket([SUBSCRIBED])]
    backoffs = []

    async def backoff(failures):
        backoffs.append(failures)

    monkeypatch.setattr(novig_module, "ws_connect", lambda *args, **kwargs: sessions.pop(0))
    monkeypatch.setattr(native, "_backoff", backoff)
    updates = native.stream_updates()
    try:
        assert (await anext(updates))["type"] == "transport_invalidated"
        assert native.health_check()["streaming"] is False
        assert await anext(updates) == SUBSCRIBED
        assert native.health_check()["streaming"] is True
        assert backoffs == [0]
    finally:
        await updates.aclose()
