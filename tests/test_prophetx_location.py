"""The owner-device location check is read-only and bound to one order."""
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from evplatform.core.native_runtime import NativeRuntime
from evplatform.core.models import utcnow
from evplatform.core.risk import RiskRejected
from evplatform.venues.common import NativeTradingGate, VenueError
from evplatform.venues.prophetx import ProphetXClient, ProphetXSettings


@pytest.mark.parametrize("eligible", [True, False])
async def test_current_egress_location_requires_official_success(eligible):
    calls = []

    def respond(request):
        calls.append(request.url.host)
        if request.url.host == "api.ipify.org":
            return httpx.Response(200, json={"ip": "8.8.8.8"})
        assert request.url.host == "www.prophetx.co"
        assert request.url.path == "/ip-geolocation/api/v1/check-ip"
        assert json.loads(request.content) == {"ip_address": "8.8.8.8"}
        return httpx.Response(200, json={"success": eligible})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = ProphetXClient(ProphetXSettings(environment="production",
            api_key="fixture-key", secret_key="fixture-secret"), http)
        assert await client.check_current_location("one-order") is eligible
        assert client.location_valid_for("one-order") is eligible
        assert not client.location_valid_for("another-order")
    assert calls == ["api.ipify.org", "www.prophetx.co"]


async def test_production_order_cannot_reuse_location_after_first_dispatch():
    requests = []

    def respond(request):
        requests.append((request.method, request.url.path))
        if request.url.host == "api.ipify.org":
            return httpx.Response(200, json={"ip": "8.8.8.8"})
        if request.url.path.endswith("/check-ip"):
            return httpx.Response(200, json={"success": True})
        if request.url.path.endswith("/submit_order"):
            return httpx.Response(200, json={"data": {"order_id": "test"}})
        pytest.fail("Unexpected account or placement request")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = ProphetXClient(ProphetXSettings(environment="production",
            api_key="fixture-key", secret_key="fixture-secret"), http)
        client._access_token, client._access_expires = "fixture-token", time.time() + 120
        client.price_ladder = {-110}
        client.settings.gate = NativeTradingGate(enabled=True, preflight_valid_until=time.time()+30,
            account_authorized=True, geographic_eligibility_verified=True,
            risk_approval_id="review", durable_intent_id="one-order")
        body = client.order_body("strike", -110, __import__("decimal").Decimal("1"), "one-order")
        with pytest.raises(VenueError, match="fresh ProphetX location"):
            await client.submit_order(body)
        assert requests == []
        assert await client.check_current_location("one-order")
        await client.submit_order(body)
        assert not client.location_valid_for("one-order")
        with pytest.raises(VenueError, match="fresh ProphetX location"):
            await client.submit_order(body)
    assert requests.count(("POST", "/partner/v4/mm/submit_order")) == 1


async def test_runtime_location_proof_is_order_bound_and_expires():
    client = SimpleNamespace(check_current_location=AsyncMock(return_value=True),
                             location_valid_for=lambda value: value == "client-1")
    runtime = NativeRuntime.__new__(NativeRuntime)
    runtime._prophetx_location = None
    runtime.manager = SimpleNamespace(adapters={"prophetx": SimpleNamespace(client=client)})
    order = SimpleNamespace(id="order-1", venue="prophetx", mode="production",
                            intent=SimpleNamespace(client_order_id="client-1"))
    await runtime.verify_prophetx_location(order)
    client.check_current_location.assert_awaited_once_with("client-1")
    assert runtime._prophetx_location_valid(order, client)
    assert not runtime._prophetx_location_valid(SimpleNamespace(id="other"), client)
    runtime._prophetx_location = (order.id, client, time.monotonic() - 1)
    assert not runtime._prophetx_location_valid(order, client)
    client.check_current_location.return_value = False
    with pytest.raises(RiskRejected, match="current location check failed"):
        await runtime.verify_prophetx_location(order)
    assert runtime._prophetx_location is None


@pytest.mark.parametrize("eligible", [True, False])
async def test_automatic_prophetx_checks_location_before_oms(eligible):
    client = SimpleNamespace(check_current_location=AsyncMock(return_value=eligible),
                             location_valid_for=lambda value: value == "client-1")
    order = SimpleNamespace(id="order-1", venue="prophetx", mode="production",
                            intent=SimpleNamespace(client_order_id="client-1"))
    candidate = SimpleNamespace(market_id="m", venue="prophetx", outcome="yes",
                                book_revision=4, calculated_at=utcnow(), qualified=True)
    runtime = NativeRuntime.__new__(NativeRuntime)
    runtime.engine = SimpleNamespace(markets={"m": SimpleNamespace(id="m")},
        risk=SimpleNamespace(config=SimpleNamespace(max_candidate_age_ms=10000), native_orders={}))
    runtime.books = {("m", "prophetx", "yes"): SimpleNamespace(revision=4)}
    runtime.manager = SimpleNamespace(adapters={"prophetx": SimpleNamespace(client=client)},
                                      execute=AsyncMock(return_value="saved-order"))
    runtime.build_order = lambda value, execution_origin: order
    runtime._prophetx_location = None
    runtime.dirty_venues = set()
    runtime.invalidate_account = lambda venue: None
    if eligible:
        assert await runtime.execute(candidate) == "saved-order"
        runtime.manager.execute.assert_awaited_once_with(order)
    else:
        with pytest.raises(RiskRejected, match="current location check failed"):
            await runtime.execute(candidate)
        runtime.manager.execute.assert_not_awaited()
    client.check_current_location.assert_awaited_once_with("client-1")
    assert runtime._prophetx_location is None
