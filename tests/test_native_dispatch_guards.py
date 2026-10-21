"""Offline dispatch races: no credentials, venue connection or real orders."""
import asyncio
import json
import time

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from evplatform.core.risk import RiskAuthority
from evplatform.venues.bridge import NativeExecutionVenue, NativePreflight, native_execution_factories
from evplatform.venues.common import NativeTradingGate, PreflightRequired, VenueError
from evplatform.venues.kalshi import KalshiClient, KalshiSettings
from evplatform.venues.novig import NovigClient, NovigSettings
from evplatform.venues.prophetx import ProphetXClient, ProphetXSettings
from test_native_reconciliation import CLIENT, intent, mapping


def configured_client(venue, http):
    if venue == "kalshi":
        client = KalshiClient(KalshiSettings(api_key_id="offline", private_key_path="never-read"), client=http)
        client._key = Ed25519PrivateKey.generate()
    elif venue == "novig":
        client = NovigClient(NovigSettings(api_key="offline", private_key_path="never-read"), client=http)
        client._key = Ed25519PrivateKey.generate()
    else:
        client = ProphetXClient(ProphetXSettings(api_key="offline", secret_key="offline"), client=http)
        client._access_token, client._refresh_token = "offline-access", "offline-refresh"
        client._access_expires = time.time() + 3600
        client.set_verified_price_ladder([120])
    return client


def acknowledgement(venue):
    if venue == "kalshi":
        return {"order_id": "native"}
    if venue == "novig":
        return {"orderId": "native", "clientId": CLIENT}
    return {"data": {"success": True, "order": {"order_id": "native", "external_id": CLIENT}}}


def configured_adapter(client, order):
    adapter = NativeExecutionVenue(client, RiskAuthority(), mappings=[mapping(order)], preflight=NativePreflight(
        account_authorized=True, native_trading_enabled=True, evidence="offline regression",
        valid_until=time.time()+60))
    adapter.dispatch_guard = lambda intent: None
    return adapter


@pytest.mark.parametrize("venue", ["kalshi", "novig", "prophetx"])
@pytest.mark.parametrize("change", ["preflight", "gate", "credentials", "client", "mapping", "kill", "guard", "signer"])
async def test_no_native_placement_after_binding_changes_while_transport_is_queued(venue, change):
    sent = []
    def handle(request):
        sent.append(request)
        return httpx.Response(200, json=acknowledgement(venue))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        order = intent(venue)
        client = configured_client(venue, http)
        adapter = configured_adapter(client, order)
        killed = False
        def risk_guard(order):
            if killed:
                raise PreflightRequired("Risk engine stopped while placement was queued")
        adapter.dispatch_guard = risk_guard
        settings = client.settings
        await client._request_lock.acquire()
        task = asyncio.create_task(adapter.submit_intent(order, adapter.authority.issue(order.id)))
        try:
            await asyncio.sleep(0)
            assert not task.done()
            if change == "preflight":
                adapter.preflight = adapter.preflight.model_copy(update={"valid_until": 0})
            elif change == "gate":
                settings.gate = NativeTradingGate()
            elif change == "credentials":
                if venue == "kalshi":
                    settings.api_key_id = "different-offline-account"
                else:
                    settings.api_key = "different-offline-account"
            elif change == "client":
                adapter.client = configured_client(venue, http)
            elif change == "mapping":
                adapter.mappings.clear()
            elif change == "kill":
                killed = True
            elif change == "guard":
                adapter.dispatch_guard = None
            elif change == "signer":
                if venue == "prophetx":
                    client._access_token = "different-offline-session"
                else:
                    client._key = Ed25519PrivateKey.generate()
        finally:
            client._request_lock.release()
        with pytest.raises(PreflightRequired):
            await task
        assert sent == []
        assert not settings.gate.enabled
        assert CLIENT in adapter._transmitted  # No automatic retry under the old ID.


@pytest.mark.parametrize("venue", ["kalshi", "novig", "prophetx"])
async def test_preflight_expiring_during_real_rate_pacing_cannot_send(venue):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: pytest.fail("Expired order reached transport"))) as http:
        order = intent(venue)
        client = configured_client(venue, http)
        adapter = configured_adapter(client, order)
        adapter.preflight = adapter.preflight.model_copy(update={"valid_until": time.time()+.02})
        client._next_request = time.monotonic()+.06
        with pytest.raises(PreflightRequired):
            await adapter.submit_intent(order, adapter.authority.issue(order.id))


@pytest.mark.parametrize("venue", ["kalshi", "novig", "prophetx"])
async def test_valid_native_dispatch_rechecks_risk_and_consumes_only_one_permit(venue):
    sent, checks = [], []
    def handle(request):
        sent.append(request)
        return httpx.Response(200, json=acknowledgement(venue))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        order = intent(venue)
        client = configured_client(venue, http)
        adapter = configured_adapter(client, order)
        adapter.dispatch_guard = lambda checked: checks.append((checked, time.monotonic()))
        ready = time.monotonic()+.02
        client._next_request = ready
        token = adapter.authority.issue(order.id)
        report = await adapter.submit_intent(order, token)
        assert report.status == "acknowledged" and not report.financial_complete
        assert len(sent) == 1 and checks[-1][0] == order and checks[-1][1] >= ready
        assert not adapter.authority.consume(token, order.id)
        assert not client.settings.gate.enabled


@pytest.mark.parametrize("venue", ["kalshi", "novig"])
async def test_signed_http_placement_refreshes_timestamp_after_pacing(venue):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=acknowledgement(venue)))) as http:
        order = intent(venue)
        client = configured_client(venue, http)
        adapter = configured_adapter(client, order)
        signed_at = []
        original = client.headers
        def headers(*args, **kwargs):
            signed_at.append(time.monotonic())
            return original(*args, **kwargs)
        client.headers = headers
        ready = time.monotonic()+.03
        client._next_request = ready
        await adapter.submit_intent(order, adapter.authority.issue(order.id))
        assert len(signed_at) == 2 and signed_at[0] < ready <= signed_at[1]


@pytest.mark.parametrize("venue", ["kalshi", "novig", "prophetx"])
async def test_cancellation_stays_available_with_kill_and_closed_placement_gate(venue):
    sent = []
    def handle(request):
        sent.append(request)
        return httpx.Response(200, json={})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = configured_client(venue, http)
        adapter = configured_adapter(client, intent(venue))
        adapter.preflight = NativePreflight()
        adapter.dispatch_guard = lambda order: pytest.fail("Cancellation invoked placement guard")
        result = await adapter.cancel_native("native", intent(venue))
        assert result.status in {"cancel_pending", "unknown"}
        assert len(sent) == 1 and not client.settings.gate.enabled


@pytest.mark.parametrize("change", ["token_expiry", "ladder"])
async def test_prophetx_session_and_price_ladder_checked_again_after_queue(change):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: pytest.fail("Invalid order sent"))) as http:
        client = configured_client("prophetx", http)
        order = intent("prophetx")
        adapter = configured_adapter(client, order)
        await client._request_lock.acquire()
        task = asyncio.create_task(adapter.submit_intent(order, adapter.authority.issue(order.id)))
        try:
            await asyncio.sleep(0)
            assert not task.done()
            if change == "token_expiry":
                client._access_expires = 0
            else:
                client.price_ladder.clear()
        finally:
            client._request_lock.release()
        with pytest.raises((PreflightRequired, VenueError)):
            await task


class Socket:
    def __init__(self, client):
        self.client, self.sent = client, []

    async def send(self, text):
        frame = json.loads(text)
        self.sent.append(frame)
        self.client._pending[frame["nonce"]].set_result({"success": True})


@pytest.mark.parametrize("change", ["gate", "socket", "credentials", "kill"])
async def test_novig_ws_placement_is_rechecked_after_write_lock(change):
    async with httpx.AsyncClient() as http:
        client = configured_client("novig", http)
        socket = client._ws = Socket(client)
        client.settings.gate = NativeTradingGate(enabled=True, account_authorized=True,
            preflight_valid_until=time.time()+60, risk_approval_id="offline-permit", durable_intent_id=CLIENT)
        killed = False
        def guard():
            if killed:
                raise PreflightRequired("Risk stopped while queued")
        await client._write_lock.acquire()
        task = asyncio.create_task(client.submit_ws({"clientId": CLIENT}, before_send=guard))
        try:
            await asyncio.sleep(0)
            assert not task.done()
            if change == "gate":
                client.settings.gate.enabled = False
            elif change == "socket":
                client._ws = Socket(client)
            elif change == "credentials":
                client.settings.api_key = "changed-offline-account"
            else:
                killed = True
        finally:
            client._write_lock.release()
        with pytest.raises(PreflightRequired):
            await task
        assert not socket.sent and not client._ws.sent and not client._pending


async def test_novig_ws_cancellation_is_available_after_placement_gate_closes():
    async with httpx.AsyncClient() as http:
        client = configured_client("novig", http)
        client._ws = Socket(client)
        assert not client.settings.gate.enabled
        await client.cancel_ws("native")
        assert client._ws.sent[0]["cancel"] == {"orderIds": ["native"]}


def test_execution_factory_registration_is_lazy_and_does_not_connect():
    factories = native_execution_factories(RiskAuthority())
    assert set(factories) == {"kalshi", "novig", "prophetx", "polymarket_us"}
