"""OFFLINE protocol tests. None of these prove live exchange connectivity."""
import base64
import json
import time
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa

from evplatform.venues.common import (
    CredentialsMissing, NativeTradingGate, PreflightRequired, SequenceGap,
    SequenceTracker, UnknownOrderOutcome, VenueError, VenueHTTPError,
)
from evplatform.venues.kalshi import KalshiBook, KalshiClient, KalshiSettings, sign_kalshi
from evplatform.venues.novig import NovigBook, NovigClient, NovigSettings, novig_canonical, sign_novig
from evplatform.venues.prophetx import ProphetXClient, ProphetXSettings, decode_talaria, executable_level, order_progress


VECTORS = json.loads((Path(__file__).parent / "fixtures/novig_signing_vectors.json").read_text(encoding="utf-8-sig"))["vectors"]


@pytest.mark.parametrize("vector", VECTORS, ids=lambda vector: vector["id"])
def test_novig_official_canonical_vectors(vector):
    value = vector["input"]
    actual = novig_canonical(str(value["timestamp"]), value["method"], value["path"], value["query"], value["body"].encode())
    assert actual.decode() == vector["string_to_sign"]


@pytest.mark.parametrize("algorithm", ["ed25519", "p256"])
def test_novig_signature_verifies(algorithm):
    key = ed25519.Ed25519PrivateKey.generate() if algorithm == "ed25519" else ec.generate_private_key(ec.SECP256R1())
    canonical = novig_canonical("1755000000000", "POST", "/v3/orders", body=b'{"qty":10}')
    signature = base64.b64decode(sign_novig(key, canonical))
    if algorithm == "ed25519":
        key.public_key().verify(signature, canonical)
    else:
        key.public_key().verify(signature, canonical, ec.ECDSA(hashes.SHA256()))


@pytest.mark.parametrize("algorithm", ["rsa", "ed25519"])
def test_kalshi_signature_query_excluded(algorithm):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048) if algorithm == "rsa" else ed25519.Ed25519PrivateKey.generate()
    signature = base64.b64decode(sign_kalshi(key, "123", "get", "/trade-api/v2/portfolio/orders?limit=3"))
    message = b"123GET/trade-api/v2/portfolio/orders"
    if algorithm == "rsa":
        key.public_key().verify(signature, message, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
    else:
        key.public_key().verify(signature, message)


def test_sequence_gap_requires_new_snapshot():
    tracker = SequenceTracker()
    tracker.snapshot("A", 10)
    assert tracker.delta("A", 11)
    assert not tracker.delta("A", 11)
    with pytest.raises(SequenceGap):
        tracker.delta("A", 13)
    with pytest.raises(SequenceGap):
        tracker.delta("A", 12)
    tracker.snapshot("A", 15)
    assert tracker.delta("A", 16)


def kalshi_snapshot(ticker="A", seq=1):
    return {"type": "orderbook_snapshot", "sid": 4, "seq": seq,
            "msg": {"market_ticker": ticker, "yes_dollars_fp": [["0.48", "5.00"]],
                    "no_dollars_fp": [["0.54", "9.00"]]}}


def test_kalshi_executable_asks_and_subscription_sequence():
    book = KalshiBook()
    book.apply(kalshi_snapshot("A"))
    book.apply(kalshi_snapshot("B", 2))
    assert book.asks("A", "yes") == [(Decimal(".54"), Decimal("9"))]
    book.apply({"type": "orderbook_delta", "sid": 4, "seq": 3,
                "msg": {"market_ticker": "A", "side": "no", "price_dollars": ".54", "delta_fp": "-3"}})
    assert book.asks("A", "yes") == [(Decimal(".54"), Decimal("6"))]
    with pytest.raises(SequenceGap):
        book.apply({"type": "orderbook_delta", "sid": 4, "seq": 5,
                    "msg": {"market_ticker": "B", "side": "yes", "price_dollars": ".48", "delta_fp": "1"}})
    assert book.asks("A", "yes") == []
    assert book.asks("B", "yes") == []


def test_kalshi_no_buy_maps_to_yes_ask():
    body = KalshiClient.order_body("A", "no", Decimal(".42"), Decimal("3"), "unique")
    assert body["side"] == "ask"
    assert body["price"] == "0.5800"
    assert body["count"] == "3.00"
    assert body["cancel_order_on_pause"] is True


@pytest.mark.parametrize("price", [".667", "0", "1", ".0001", "NaN"])
def test_novig_rejects_off_grid_price(price):
    with pytest.raises(ValueError):
        NovigClient.order_body("outcome", Decimal(price), 100, "unique")


def test_novig_contract_value_is_one_cent():
    body = NovigClient.order_body("3f2504e0-4f89-11d3-9a0c-0305e82c3301", Decimal(".665"), 110,
                                 "7c9e6679-7425-40de-944b-e07fc1f90ae7")
    assert Decimal(body["price"]) * body["qty"] * Decimal(".01") == Decimal(".73150")
    assert Decimal(body["qty"]) * Decimal(".01") == Decimal("1.10000")
    assert body["tif"] == "IOC"
    assert NovigClient.get_manifest()["capabilities"]["payout_usd"] == "0.01"
    assert NovigClient.get_manifest()["capabilities"]["client_id_deduplicated_by_exchange"] is False


def novig_snapshot():
    return {"snapshot": {"m": {"book": {"seq": 9, "orders": {"opposing": [
        {"order": "o1", "price": ".335", "qty": 100}]}}, "lifecycle": {"seq": 1, "status": "OPEN"}}},
        "orders": {"seq": 20, "open": []}}


def test_novig_atomic_partial_fill_remove_add_and_lifecycle():
    book = NovigBook()
    book.apply(novig_snapshot())
    book.apply({"delta": {"m": {"book": {"seq": 10, "deltas": [
        {"kind": "remove", "order": "o1", "reason": "fill"},
        {"kind": "add", "order": "o1", "outcome": "opposing", "price": ".335", "qty": 60},
    ]}}}})
    assert book.asks("m", "opposing") == [(Decimal(".665"), 60)]
    book.apply({"delta": {"m": {"lifecycle": {"seq": 2, "deltas": ["CLOSE"]}}}})
    assert book.asks("m", "opposing") == []


def test_novig_dropped_private_update_detected_by_heartbeat():
    book = NovigBook()
    book.apply(novig_snapshot())
    book.apply({"heartbeat": {"orders": 20, "ts": 1755000000000}})
    with pytest.raises(SequenceGap):
        book.apply({"heartbeat": {"orders": 22, "ts": 1755000000001}})


def test_novig_reconnect_resets_sequences():
    book = NovigBook()
    book.apply(novig_snapshot())
    book.reset()
    new = novig_snapshot()
    new["snapshot"]["m"]["book"]["seq"] = 1
    book.apply(new)
    assert book.asks("m", "opposing")


def test_prophetx_decodes_double_json_and_base64_payload():
    payload = {"info": {"id": "one", "total_filled_quantity": 4}}
    envelope = {"timestamp": 100, "change_type": "orders", "op": "u",
                "payload": base64.b64encode(json.dumps(payload).encode()).decode()}
    raw = json.dumps({"event": "orders", "channel": "private-one", "data": json.dumps(envelope)})
    decoded = decode_talaria(raw)
    assert decoded["data"] == payload
    assert decoded["envelope"]["timestamp"] == 100


def test_prophetx_depth_uses_value_never_legacy_quantity():
    level = executable_level({"strike_id": "S", "price": -132, "value": 1387.08, "quantity": 1536.62, "updated_at": 100})
    assert level["executable_stake_usd"] == Decimal("1387.08")
    assert level["source_observed_at"] == 100
    missing = executable_level({"strike_id": "S", "price": 120, "quantity": 100})
    assert missing["executable_stake_usd"] == 0


def test_prophetx_partial_killed_order_not_overstated_as_full_fill():
    partial = {"quantity": 4, "original_quantity": 10, "filled_quantity": 4, "open_quantity": 0,
               "status": "open", "matching_status": "fully_matched"}
    state = order_progress(partial)
    assert state["state"] == "partially_filled_canceled"
    assert state["terminal"]
    assert order_progress({"quantity": 10, "status": "open"})["state"] == "unknown"


def test_prophetx_ws_fill_is_not_cumulative_fill():
    state = order_progress({"quantity": 10, "filled_quantity": 2, "total_filled_quantity": 6,
                            "open_quantity": 4}, Decimal(10), websocket=True)
    assert state["filled"] == 6
    assert state["state"] == "partially_filled"


@pytest.mark.parametrize("client", [KalshiClient, NovigClient, ProphetXClient])
async def test_missing_credentials_fail_without_http(client):
    calls = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: calls.append(request))) as http:
        native = client(client=http)
        with pytest.raises(CredentialsMissing):
            await native.connect()
        assert not calls


@pytest.mark.parametrize("client,body", [
    (KalshiClient, {"client_order_id": "one"}),
    (NovigClient, {"clientId": "one"}),
    (ProphetXClient, {"external_id": "one"}),
])
async def test_native_trading_disabled_by_default(client, body):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: pytest.fail("Must not transmit"))) as http:
        with pytest.raises(PreflightRequired):
            await client(client=http).submit_order(body)


def test_production_gate_needs_current_eligibility_and_durable_intent():
    gate = NativeTradingGate(enabled=True, preflight_valid_until=time.time() + 10, account_authorized=True,
                            risk_approval_id="risk-one", durable_intent_id="one")
    gate.require("one", "sandbox")
    with pytest.raises(PreflightRequired):
        gate.require("one", "production")
    gate.geographic_eligibility_verified = True
    gate.require("one", "production")
    with pytest.raises(PreflightRequired):
        gate.require("different-id", "production")
    gate.preflight_valid_until = 0
    with pytest.raises(PreflightRequired):
        gate.require("one", "production")


def save_key(tmp_path, key):
    target = tmp_path / "test-private.pem"
    target.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                         serialization.NoEncryption()))
    return str(target)


async def test_novig_http_signs_exact_transmitted_body(tmp_path):
    key = ed25519.Ed25519PrivateKey.generate()
    observed = []
    def handler(request):
        observed.append(request)
        canonical = novig_canonical(request.headers["Novig-Timestamp"], request.method,
                                    request.url.raw_path.decode().split("?")[0],
                                    request.url.query.decode(), request.content)
        key.public_key().verify(base64.b64decode(request.headers["Novig-Signature"]), canonical)
        return httpx.Response(200, json={"orders": []})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        native = NovigClient(NovigSettings(api_key="test-id", private_key_path=save_key(tmp_path, key)), http)
        await native.reconcile_account(status="OPEN")
    assert str(observed[0].url).startswith("https://api.paper.novig.com/v3/orders")
    assert observed[0].headers["Content-Type"] == "application/json"


async def test_kalshi_mutation_timeout_is_unknown_and_never_retried(tmp_path):
    key = ed25519.Ed25519PrivateKey.generate()
    calls = []
    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("timeout after exchange may have accepted", request=request)
    gate = NativeTradingGate(enabled=True, preflight_valid_until=time.time() + 10, account_authorized=True,
                            risk_approval_id="risk", durable_intent_id="once")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        native = KalshiClient(KalshiSettings(api_key_id="test", private_key_path=save_key(tmp_path, key), gate=gate), http)
        body = native.order_body("S", "yes", Decimal(".4"), Decimal(1), "once")
        with pytest.raises(UnknownOrderOutcome):
            await native.submit_order(body)
    assert len(calls) == 1
    assert calls[0].url.path == "/trade-api/v2/portfolio/events/orders"


async def test_prophetx_caches_token_and_preserves_partner_prefix():
    paths = []
    def handler(request):
        paths.append(request.url.path)
        if request.url.path.endswith("/auth/login"):
            assert json.loads(request.content) == {"access_key": "key", "secret_key": "secret"}
            return httpx.Response(200, json={"data": {"access_token": "token", "refresh_token": "refresh",
                                                    "access_expire_time": time.time() + 1200}})
        assert request.headers["Authorization"] == "Bearer token"
        assert request.headers["X-CFTC-Terminology"] == "true"
        return httpx.Response(200, json={"data": []})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        native = ProphetXClient(ProphetXSettings(api_key="key", secret_key="secret", requests_per_second=100), http)
        await native.discover_events(tournament_id=1)
        await native.discover_markets(["2"])
    assert paths.count("/partner/auth/login") == 1
    assert paths[-1] == "/partner/v4/mm/get_multiple_markets"


async def test_prophetx_register_complete_scope_and_wait_for_signin():
    sent = []
    async def request(method, path, **kwargs):
        assert path == "/v4/mm/websocket"
        assert kwargs["body"]["subscriptions"] == [{"type": "event", "ids": ["1"]},
                                                    {"type": "event_subtype", "ids": ["1:player_total_points"]}]
        return {"data": {"success": True, "channel_count": 1, "channel_limit": 10,
                         "authenticated": {"auth": "signin", "user_data": "{}"},
                         "authorized_channel": [{"channel_name": "native-channel", "auth": "channel-auth",
                                                 "scope": {"event_id": 1, "sub_type": "player_total_points"},
                                                 "binding_events": [{"name": "v2.market_selections"}]}]}}
    async with httpx.AsyncClient() as http:
        native = ProphetXClient(client=http)
        native.request = request
        async def send(payload):
            sent.append(payload)
        native._send_ws = send
        await native.subscribe(["1"], {"1": ["player_total_points"]})
        await native._register("123.456")
        assert [x["event"] for x in sent] == ["pusher:signin"]
        await native._subscribe_authorized_channels()
        assert sent[-1]["data"]["auth"] == "channel-auth"
        frame = {"event": "v2.market_selections", "channel": "native-channel",
                 "data": {"info": {"id": 5, "sequence_number": 10000000}}}
        assert native.accept_message(frame)
        assert frame["scope"]["event_id"] == 1
        assert not native.accept_message(frame)
        frame["data"]["info"]["sequence_number"] += 4000
        assert native.accept_message(frame)  # sequence is not a contiguous counter


def test_prophetx_requires_secure_discovered_talaria_host():
    assert ProphetXClient.websocket_url({"ws_host": "talaria.example.test", "key": "abc"}) == \
           "wss://talaria.example.test/app/abc?protocol=7"
    with pytest.raises(VenueError):
        ProphetXClient.websocket_url({"key": "abc", "cluster": "old"})
    with pytest.raises(ValueError):
        ProphetXClient.websocket_url({"key": "abc", "ws_host": "ws://unsafe.test"})


async def test_cancel_race_preserves_transient_code_without_retry():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(409, json={"code": "order_is_matching", "message": "untrusted detail"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        native = ProphetXClient(ProphetXSettings(api_key="key", secret_key="secret"), http)
        native._access_token, native._access_expires = "token", time.time() + 120
        with pytest.raises(VenueHTTPError) as error:
            await native.cancel_order("native-id", external_id="external-id")
    assert error.value.status_code == 409
    assert error.value.code == "order_is_matching"
    assert "untrusted detail" not in str(error.value)
    assert len(calls) == 1


async def test_novig_websocket_write_failure_is_unknown():
    class ClosedSocket:
        async def send(self, payload):
            raise OSError("socket dropped after write")
    async with httpx.AsyncClient() as http:
        gate = NativeTradingGate(enabled=True, preflight_valid_until=time.time() + 60, account_authorized=True,
                                risk_approval_id="risk", durable_intent_id="unique")
        native = NovigClient(NovigSettings(gate=gate), http)
        native._ws = ClosedSocket()
        with pytest.raises(UnknownOrderOutcome):
            await native.submit_ws({"clientId": "unique"})
        assert native._nonce == 1
        assert not native._pending


async def test_kalshi_stream_reconnect_invalidates_then_resnapshots(monkeypatch):
    import evplatform.venues.kalshi as module
    sent = []
    sessions = [
        [kalshi_snapshot(), {"type": "orderbook_delta", "sid": 4, "seq": 3,
                             "msg": {"market_ticker": "A", "side": "no", "price_dollars": ".54", "delta_fp": "1"}}],
        [kalshi_snapshot(seq=1)],
    ]
    class Socket:
        def __init__(self, frames):
            self.frames = iter(frames)
        async def __aenter__(self):
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
            sent.append(json.loads(payload))
    def connect(url, **kwargs):
        assert url.startswith("wss://external-api-ws.demo.kalshi.co/")
        assert "KALSHI-ACCESS-SIGNATURE" in kwargs["additional_headers"]
        return Socket(sessions.pop(0))
    monkeypatch.setattr(module, "ws_connect", connect)
    async with httpx.AsyncClient() as http:
        native = KalshiClient(KalshiSettings(api_key_id="fake", private_key_path="unused"), http)
        native._key = ed25519.Ed25519PrivateKey.generate()
        async def backoff(_):
            pass
        native._backoff = backoff
        await native.subscribe(["A"])
        stream = native.stream_updates()
        assert (await anext(stream))["type"] == "orderbook_snapshot"
        assert native.book.asks("A", "yes")
        assert (await anext(stream))["type"] == "transport_invalidated"
        assert native.book.asks("A", "yes") == []
        assert (await anext(stream))["type"] == "orderbook_snapshot"
        assert native.book.asks("A", "yes")
        await stream.aclose()
    assert len(sessions) == 0
    assert len([x for x in sent if x["params"]["channels"] == ["orderbook_delta"]]) == 2


def native_order(venue="kalshi"):
    from evplatform.core.models import Order
    return Order(id="local-order", client_order_id="persistent-client", candidate_id="candidate",
                 market_id="canonical", event_id="event", player_id=None, correlation_id="event",
                 venue=venue, outcome="home", price=Decimal(".4"), quantity=3, reserved_cost=Decimal("1.3"),
                 synthetic=False, mode="sandbox")


def verified_mapping(payout="1", unit="contracts"):
    from evplatform.venues.bridge import VerifiedVenueMapping
    return VerifiedVenueMapping(
        canonical_market_id="canonical", canonical_outcome="home", native_instrument_id="TICKER",
        native_outcome="yes", settlement_fingerprint="reviewed-rules-hash",
        mapping_evidence="operator-reviewed-fixture", verified=True, fee_schedule_verified=True,
        native_payout_usd=Decimal(payout), native_quantity_unit=unit,
    )


async def test_native_bridge_requires_single_use_authority_and_returns_ack_only():
    from evplatform.core.risk import RiskAuthority
    from evplatform.venues.bridge import NativeExecutionVenue, NativePreflight
    authority, calls, order = RiskAuthority(), [], native_order()
    async with httpx.AsyncClient() as http:
        client = KalshiClient(client=http)
        async def submit(body, *, before_send):
            before_send()
            client.settings.gate.require(body["client_order_id"], "sandbox")
            calls.append(body)
            return {"order_id": "exchange-order", "fill_count": "3.00"}
        client.submit_order = submit
        adapter = NativeExecutionVenue(
            client, authority, mappings=[verified_mapping()],
            preflight=NativePreflight(account_authorized=True, valid_until=time.time() + 60,
                                      native_trading_enabled=True, evidence="sandbox-preflight"),
        )
        with pytest.raises(PermissionError):
            await adapter.submit(order, "forged")
        token = authority.issue(order.id)
        response = await adapter.submit(order, token)
        assert response["status"] == "acknowledged"
        assert "filled_quantity" not in response
        assert not client.settings.gate.enabled
        with pytest.raises(PermissionError):
            await adapter.submit(order, token)
        with pytest.raises(UnknownOrderOutcome):
            await adapter.submit(order, authority.issue(order.id))
    assert len(calls) == 1


async def test_native_bridge_blocks_cent_contracts_before_transmission():
    from evplatform.core.risk import RiskAuthority
    from evplatform.venues.bridge import NativeExecutionVenue
    order, authority = native_order("novig"), RiskAuthority()
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: pytest.fail("No native order allowed"))) as http:
        adapter = NativeExecutionVenue(NovigClient(client=http), authority,
                                       mappings=[verified_mapping(".01", "whole_cent_contracts")])
        with pytest.raises(PreflightRequired, match="payout/quantity"):
            await adapter.submit(order, authority.issue(order.id))


async def test_native_bridge_cancel_remains_pending_until_reconciliation():
    from evplatform.core.risk import RiskAuthority
    from evplatform.venues.bridge import NativeExecutionVenue
    async with httpx.AsyncClient() as http:
        client = KalshiClient(client=http)
        async def cancel(*args, **kwargs):
            return {"order_id": "exchange-order", "reduced_by": "2.00"}
        async def reconcile(order_id):
            return {"order": {"fill_count_fp": "1.00", "remaining_count_fp": "0.00",
                              "taker_fill_cost_dollars": ".40", "maker_fill_cost_dollars": "0",
                              "taker_fees_dollars": ".01", "maker_fees_dollars": "0", "status": "canceled"}}
        client.cancel_order, client.reconcile_order = cancel, reconcile
        adapter = NativeExecutionVenue(client, RiskAuthority(), mappings=[verified_mapping()])
        order = native_order().model_copy(update={"venue_order_id": "exchange-order"})
        assert (await adapter.cancel(order))["status"] == "cancel_pending"
        state = await adapter.reconcile(order)
        assert state["status"] == "cancelled"
        assert state["filled_quantity"] == 1
        assert Decimal(state["average_fill_price"]) == Decimal("0.40")
        assert state["requires_fill_reconciliation"]


async def test_native_bridge_fractional_fill_stays_unknown():
    from evplatform.core.risk import RiskAuthority
    from evplatform.venues.bridge import NativeExecutionVenue
    async with httpx.AsyncClient() as http:
        client = KalshiClient(client=http)
        async def reconcile(order_id):
            return {"order": {"fill_count_fp": ".50", "remaining_count_fp": "2.50"}}
        client.reconcile_order = reconcile
        adapter = NativeExecutionVenue(client, RiskAuthority(), mappings=[verified_mapping()])
        order = native_order().model_copy(update={"venue_order_id": "exchange-order"})
        assert (await adapter.reconcile(order))["status"] == "unknown"
