"""Read-only Kalshi setup tests with offline HTTP/WebSocket transports."""
import json
from datetime import datetime, timezone

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519
from websockets.datastructures import Headers
from websockets.exceptions import InvalidStatus
from websockets.http11 import Response

from evplatform.core.models import utcnow
from evplatform.venues.account_probe import probe_native_account
from evplatform.venues.common import VenueHTTPError
from evplatform.venues.kalshi import KalshiClient, KalshiSettings
from evplatform.venues.reconciliation import ReconciliationError


def signed_client(http):
    client = KalshiClient(KalshiSettings(api_key_id="OFFLINE-key-id", private_key_path="unused",
                                       requests_per_second=100), http)
    client._key = ed25519.Ed25519PrivateKey.generate()  # Ephemeral offline test key.
    return client


async def test_public_reads_cannot_claim_authentication_and_balance_does_not_claim_streaming():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"balance_dollars": "10", "markets": []})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = signed_client(http)
        assert not client.health_check()["authenticated"]
        await client.discover_markets()
        assert not client.health_check()["authenticated"]
        await client.connect()
        assert client.health_check()["authenticated"]
        assert not client.health_check()["streaming"]
        assert not client.settings.gate.enabled
        await client.disconnect()
        assert not client.health_check()["authenticated"]
        assert not client.health_check()["streaming"]
    assert all(request.method == "GET" for request in calls)
    assert "KALSHI-ACCESS-SIGNATURE" not in calls[0].headers
    assert "KALSHI-ACCESS-SIGNATURE" in calls[1].headers


@pytest.mark.parametrize("status,state", [(401, "unauthorized"), (403, "access_denied"), (451, "geolocation_required")])
async def test_authenticated_permission_failure_clears_health_without_echoing_server_details(status, state):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(status,
            json={"error": {"message": "SECRET MUST NOT APPEAR"}}))) as http:
        client = signed_client(http)
        client.authenticated = True
        with pytest.raises(VenueHTTPError):
            await client.connect()
        health = client.health_check()
        assert health["state"] == state and not health["authenticated"] and not health["streaming"]
        assert "SECRET" not in str(health)
        assert not client.settings.gate.enabled


async def test_websocket_health_waits_for_native_subscription_and_invalidates_on_error(monkeypatch):
    import evplatform.venues.kalshi as module
    client = None
    class Socket:
        def __init__(self): self.frames = iter([
            {"type": "subscribed", "msg": {"channel": "fill", "sid": 1}},
            {"type": "error", "msg": {"message": "SECRET MUST NOT APPEAR"}},
        ])
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None
        def __aiter__(self): return self
        async def __anext__(self):
            frame = next(self.frames)
            if frame["type"] == "subscribed":
                assert client.health_check()["authenticated"]
                assert not client.health_check()["streaming"]
            return json.dumps(frame)
        async def send(self, payload):
            assert json.loads(payload)["cmd"] == "subscribe"
    monkeypatch.setattr(module, "ws_connect", lambda *args, **kwargs: Socket())
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: pytest.fail("No HTTP expected"))) as http:
        client = signed_client(http)
        stream = client.stream_updates()
        assert (await anext(stream))["type"] == "subscribed"
        assert client.health_check()["streaming"]
        assert (await anext(stream))["type"] == "transport_invalidated"
        health = client.health_check()
        assert health["state"] == "subscription_rejected" and not health["streaming"]
        assert "SECRET" not in str(health)
        with pytest.raises(StopAsyncIteration): await anext(stream)
        assert client._ws is None and not client.settings.gate.enabled


@pytest.mark.parametrize("status,state", [(401, "unauthorized"), (403, "access_denied"), (451, "geolocation_required")])
async def test_websocket_permission_rejection_does_not_retry_or_claim_streaming(monkeypatch, status, state):
    import evplatform.venues.kalshi as module
    class Rejected:
        async def __aenter__(self):
            raise InvalidStatus(Response(status, "OFFLINE rejection", Headers()))
        async def __aexit__(self, *args): return None
    monkeypatch.setattr(module, "ws_connect", lambda *args, **kwargs: Rejected())
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: pytest.fail("No HTTP expected"))) as http:
        client = signed_client(http)
        client.authenticated = True
        stream = client.stream_updates()
        assert (await anext(stream))["reason"] == state
        assert not client.health_check()["authenticated"] and not client.health_check()["streaming"]
        with pytest.raises(StopAsyncIteration): await anext(stream)
        assert client.reconnect_count == 0


async def account_metadata(*, scopes=None, expiry=None, omit_scopes=False, omit_expiry=False):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: pytest.fail("No network expected"))) as http:
        client = signed_client(http)
        key = {"api_key_id": client.settings.api_key_id}
        if not omit_scopes: key["scopes"] = scopes
        metadata = {"api_keys": [key]}
        if not omit_expiry: metadata["api_key_region_expiration_ts"] = expiry
        async def request(method, path, **kwargs):
            assert method == "GET"
            return {"/api_keys": metadata, "/portfolio/balance": {"balance_dollars": "0"},
                    "/portfolio/orders": {"orders": [], "cursor": ""},
                    "/portfolio/positions": {"market_positions": [], "cursor": ""}}[path]
        client.request = request
        result = await probe_native_account(client)
        assert not client.settings.gate.enabled
        return result


@pytest.mark.parametrize("scopes,allowed", [([], False), (["read"], False), (["write"], False),
    (["read", "write::transfer"], False), (["read", "write::trade"], True),
    (["read", "write"], True), (["write::trade"], True), (["future_scope"], False)])
async def test_only_explicit_authenticated_trading_scopes_are_recognized(scopes, allowed):
    result = await account_metadata(scopes=scopes)
    assert result.api_key_scopes == tuple(sorted(scopes))
    assert result.trading_permission_verified is allowed
    assert result.geographic_attestation_expires_at is None
    assert result.identity_verified and result.reconciled


async def test_absent_scopes_and_location_never_invent_permission():
    result = await account_metadata(omit_scopes=True, omit_expiry=True)
    assert result.api_key_scopes == () and not result.trading_permission_verified
    assert result.geographic_attestation_expires_at is None


@pytest.mark.parametrize("seconds", [0, 1, 2_000_000_000])
async def test_location_attestation_preserves_exact_source_expiry_even_when_expired(seconds):
    result = await account_metadata(scopes=["read"], expiry=seconds)
    assert result.geographic_attestation_expires_at == datetime.fromtimestamp(seconds, timezone.utc)
    if seconds < utcnow().timestamp():
        assert result.geographic_attestation_expires_at < result.observed_at
    assert not result.trading_permission_verified


@pytest.mark.parametrize("scopes", [None, "read", [True], [""], [1], ["read", None]])
async def test_malformed_scope_metadata_is_rejected(scopes):
    with pytest.raises(ReconciliationError, match="scopes"):
        await account_metadata(scopes=scopes)


@pytest.mark.parametrize("expiry", [True, "2000000000", 2_000_000_000.5, -1, 10**40])
async def test_malformed_location_metadata_is_rejected(expiry):
    with pytest.raises(ReconciliationError, match="expiry"):
        await account_metadata(scopes=["read"], expiry=expiry)
