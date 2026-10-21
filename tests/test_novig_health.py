"""Offline Novig connection health checks; no credentials or network access."""
import json
from decimal import Decimal

import httpx
import pytest
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosedError, InvalidHandshake, InvalidStatus
from websockets.frames import Close
from websockets.http11 import Response

import evplatform.venues.novig as novig_module
from evplatform.venues.novig import NovigBook, NovigClient


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
    assert health["last_error_type"] == "InvalidStatus"
    assert health["received_close_code"] is None
    assert health["sent_close_code"] is None
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


def test_private_only_snapshot_and_independent_channel_deltas():
    book = NovigBook()
    book.apply({"nonce": 1, "subscribed": {"markets": {}, "events": {}, "private": ["orders", "positions"]},
                "snapshot": {},
                "orders": {"seq": 907, "open": [{"orderId": "order-one", "qty": 110}]},
                "positions": {"seq": 12, "positions": [{"marketId": "market-one", "qty": 110, "cost": "0.73150"}]}})
    assert book.sequences.values == {"PRIVATE:orders": 907, "PRIVATE:positions": 12}
    assert book.books == {}
    book.apply({"delta": {}, "orders": {"seq": 908, "deltas": [
        {"kind": "fill", "orderId": "order-one", "qty": 40, "remaining": 70}]}})
    assert book.sequences.values == {"PRIVATE:orders": 908, "PRIVATE:positions": 12}
    book.apply({"delta": {}, "positions": {"seq": 13, "deltas": [
        {"marketId": "market-one", "qty": 0, "cost": "0.00000"}]}})
    book.apply({"heartbeat": {"orders": 908, "positions": 13, "ts": 1757894415123}})
    assert book.sequences.values == {"PRIVATE:orders": 908, "PRIVATE:positions": 13}
    assert not book.sequences.invalid


@pytest.mark.parametrize("channel", ["orders", "positions"])
async def test_private_heartbeat_gap_invalidates_and_reconnects_from_fresh_snapshot(native, monkeypatch, channel):
    sessions = [Socket([SUBSCRIBED, {"heartbeat": {channel: 2, "ts": 1757894415123}}]),
                Socket([SUBSCRIBED, {"heartbeat": {"orders": 0, "positions": 0, "ts": 1757894430123}}])]
    backoffs = []

    async def backoff(failures):
        backoffs.append(failures)

    monkeypatch.setattr(novig_module, "ws_connect", lambda *args, **kwargs: sessions.pop(0))
    monkeypatch.setattr(native, "_backoff", backoff)
    updates = native.stream_updates()
    try:
        await anext(updates)
        assert native.health_check()["streaming"] is True
        invalidation = await anext(updates)
        assert invalidation["reconcile_private"] is True
        assert native.health_check()["streaming"] is False
        assert native.book.sequences.values == {}
        assert await anext(updates) == SUBSCRIBED
        assert native.book.sequences.values == {"PRIVATE:orders": 0, "PRIVATE:positions": 0}
        heartbeat = await anext(updates)
        assert "heartbeat" in heartbeat
        assert native.health_check()["streaming"] is True
        assert native.health_check()["errors"] == 1
        assert backoffs == [0]
    finally:
        await updates.aclose()


@pytest.mark.parametrize("received,sent", [(1008, 1008), (None, 1011), (None, None)])
async def test_close_diagnostics_are_sanitized_and_survive_recovery(native, monkeypatch, received, sent):
    failure = ConnectionClosedError(Close(received, PRIVATE_DETAIL) if received is not None else None,
                                    Close(sent, PRIVATE_DETAIL) if sent is not None else None,
                                    True if received is not None and sent is not None else None)
    sessions = [Socket(failure=failure), Socket([SUBSCRIBED])]

    async def backoff(failures):
        return None

    monkeypatch.setattr(novig_module, "ws_connect", lambda *args, **kwargs: sessions.pop(0))
    monkeypatch.setattr(native, "_backoff", backoff)
    updates = native.stream_updates()
    try:
        invalidation = await anext(updates)
        health = native.health_check()
        assert health["last_error_type"] == "ConnectionClosedError"
        assert health["received_close_code"] == received
        assert health["sent_close_code"] == sent
        assert PRIVATE_DETAIL not in json.dumps([health, invalidation])
        assert await anext(updates) == SUBSCRIBED
        health = native.health_check()
        assert health["streaming"] is True
        assert health["last_error_type"] == "ConnectionClosedError"
        assert health["received_close_code"] == received
        assert health["sent_close_code"] == sent
        assert PRIVATE_DETAIL not in json.dumps(health)
    finally:
        await updates.aclose()


def book_snapshot(identifier):
    return {"snapshot": {"market-one": {"book": {"seq": 1, "orders": {
        "opposing": [{**identifier, "price": "0.315", "qty": 8}]}},
        "lifecycle": {"seq": 1, "status": "OPEN"}}}}


@pytest.mark.parametrize("identifier", [{"order": "order-one"}, {"orderId": "order-one"},
                                         {"order": "order-one", "orderId": "order-one"}])
def test_book_snapshot_accepts_documented_and_observed_identifier_aliases(identifier):
    book = NovigBook()
    book.apply(book_snapshot(identifier))
    assert book.asks("market-one", "opposing") == [(Decimal("0.685"), 8)]
    assert set(book.books["market-one"]) == {"order-one"}


@pytest.mark.parametrize("identifier", [
    {"order": "order-one", "orderId": "different-order"}, {},
    {"orderId": None}, {"orderId": 123}, {"orderId": True}, {"orderId": ""},
    {"order": "order-one", "orderId": None},
])
def test_book_snapshot_rejects_conflicting_or_untyped_identifiers(identifier):
    book = NovigBook()
    book.apply(book_snapshot({"order": "order-one"}))
    with pytest.raises(ValueError):
        book.apply(book_snapshot(identifier))
    assert "market-one:book" in book.sequences.invalid
    assert book.asks("market-one", "opposing") == []


@pytest.mark.parametrize("order_key,outcome_key", [("order", "outcome"), ("orderId", "outcomeId"),
                                                    ("order", "outcomeId"), ("orderId", "outcome")])
def test_book_delta_accepts_documented_and_observed_identifier_aliases(order_key, outcome_key):
    book = NovigBook()
    book.apply(book_snapshot({"orderId": "order-one"}))
    book.apply({"delta": {"market-one": {"book": {"seq": 2, "deltas": [
        {"kind": "remove", order_key: "order-one", "reason": "fill"},
        {"kind": "add", order_key: "order-one", outcome_key: "opposing", "price": "0.315", "qty": 3},
    ]}}}})
    assert book.asks("market-one", "opposing") == [(Decimal("0.685"), 3)]


@pytest.mark.parametrize("delta", [
    {"kind": "remove", "order": "order-one", "orderId": "different-order"},
    {"kind": "add", "orderId": "order-two", "outcome": "opposing", "outcomeId": "different-outcome", "price": "0.315", "qty": 3},
    {"kind": "add", "orderId": "order-two", "outcomeId": None, "price": "0.315", "qty": 3},
])
def test_book_delta_rejects_conflicting_aliases_without_publishing_partial_batch(delta):
    book = NovigBook()
    book.apply(book_snapshot({"orderId": "order-one"}))
    with pytest.raises(ValueError):
        book.apply({"delta": {"market-one": {"book": {"seq": 2, "deltas": [
            {"kind": "add", "orderId": "valid-first-row", "outcomeId": "opposing", "price": "0.315", "qty": 3},
            delta,
        ]}}}})
    assert set(book.books["market-one"]) == {"order-one"}
    assert "market-one:book" in book.sequences.invalid
    assert book.asks("market-one", "opposing") == []
