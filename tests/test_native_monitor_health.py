import asyncio
from collections import Counter
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from evplatform.core.plugins import PluginRegistry
from evplatform.plugins.external import NativeMonitor, ProviderBridge
from evplatform.integrations.parlay import ParlayWebSocket
from evplatform.venues.novig import NovigClient, NovigSettings
from evplatform.venues.polymarket_us import PolymarketUSClient, PolymarketUSMonitorConfig, PolymarketUSSettings


@pytest.mark.asyncio
async def test_polymarket_private_denial_keeps_public_research_enabled_without_auth(monkeypatch):
    monkeypatch.setenv("TEST_POLY_ID", "not-a-real-key")
    monkeypatch.setenv("TEST_POLY_SECRET", "not-a-real-secret")
    monitor = NativeMonitor(PolymarketUSClient, PolymarketUSSettings,
        {"api_key_id": "TEST_POLY_ID", "secret_key": "TEST_POLY_SECRET"}, None,
        config_model=PolymarketUSMonitorConfig)
    http = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(401, json={"message": "denied"})))
    monitor.factory = lambda settings: PolymarketUSClient(settings, http)
    monitor._refresh_polymarket_discovery = AsyncMock()
    registry = PluginRegistry()
    registry.register(monitor)
    context = SimpleNamespace(config={"environment": "production", "market_slugs": [],
        "private_updates": True, "discovery_leagues": ["nba"]})
    try:
        await registry.start("polymarket_us", context)
        record = registry.public()[0]
        assert record["enabled"] is True
        assert record["credential_status"] == "rejected"
        assert record["health"]["state"] == "public_research"
        assert record["health"]["private_auth_failure"] == "unauthorized"
        assert record["health"]["authenticated"] is False
        assert record["health"]["streaming"] is False
        assert monitor.task is None
        assert monitor.discovery_task is not None
    finally:
        await registry.stop("polymarket_us")
        await http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["missing_credentials", "unauthorized", "access_denied", "quota_exhausted"])
async def test_provider_permission_or_quota_stop_preserves_blocked_health(state):
    async def health_check():
        return {"state": state, "reason": "Provider action required", "streaming": False}

    provider = ProviderBridge(ParlayWebSocket, None)
    provider.client = SimpleNamespace(health_check=health_check)
    provider.task = SimpleNamespace(done=lambda: True)
    health = await provider.health_check()
    assert health["status"] == "blocked"
    assert health["state"] == state
    assert health["message"] == "Provider action required"
    assert health["streaming"] is False


@pytest.mark.asyncio
async def test_unexpected_provider_stop_still_reports_error():
    async def health_check():
        return {"state": "streaming", "reason": ""}

    provider = ProviderBridge(ParlayWebSocket, None)
    provider.client = SimpleNamespace(health_check=health_check)
    provider.task = SimpleNamespace(done=lambda: True)
    assert (await provider.health_check())["status"] == "error"


@pytest.mark.asyncio
async def test_native_permission_denial_preserves_actionable_health():
    monitor = NativeMonitor(NovigClient, NovigSettings, {}, None)
    monitor.client = SimpleNamespace(health_check=lambda: {
        "state": "access_denied", "authenticated": True, "streaming": False,
        "reason": "WebSocket requires a trading or trading::read key",
    })
    monitor.task = SimpleNamespace(done=lambda: True)
    health = await monitor.health_check()
    assert health["status"] == "blocked"
    assert "trading::read" in health["message"]
    assert health["authenticated"] is True
    assert health["streaming"] is False

    registry = PluginRegistry()
    registry.register(monitor)
    record = registry.records["novig"]
    record.credential_status = "configured_unverified"
    record.health = health
    assert registry.public()[0]["credential_status"] == "authenticated"
    record.health = {"state": "unauthorized", "authenticated": False}
    assert registry.public()[0]["credential_status"] == "rejected"


@pytest.mark.asyncio
async def test_unexpected_native_monitor_stop_still_reports_error():
    monitor = NativeMonitor(NovigClient, NovigSettings, {}, None)
    monitor.client = SimpleNamespace(health_check=lambda: {"state": "streaming"})
    monitor.task = SimpleNamespace(done=lambda: True)
    assert (await monitor.health_check())["status"] == "error"


@pytest.mark.asyncio
async def test_local_invalidation_is_not_counted_as_received_exchange_frame():
    async def updates():
        yield {"type": "transport_invalidated", "reason": "access_denied"}

    from unittest.mock import AsyncMock
    engine = SimpleNamespace(metrics=SimpleNamespace(counters=Counter()),
                             native=SimpleNamespace(on_frame=AsyncMock()))
    monitor = NativeMonitor(NovigClient, NovigSettings, {}, engine)
    monitor.client = SimpleNamespace(stream_updates=updates)
    await monitor._run()
    assert monitor.frames == 0
    assert engine.metrics.counters["native_messages:novig"] == 0
    assert engine.metrics.counters["native_invalidations:novig"] == 1


@pytest.mark.asyncio
async def test_prophetx_discovery_fields_do_not_leak_into_other_native_configs(monkeypatch):
    monkeypatch.setenv("TEST_POLY_ID", "fixture")
    monkeypatch.setenv("TEST_POLY_SECRET", "fixture")

    class Client:
        def __init__(self): self.config = None

        async def connect(self): return {}

        async def configure_subscriptions(self, config):
            self.config = PolymarketUSMonitorConfig.model_validate(config)

        async def disconnect(self): pass

        async def stream_updates(self):
            await asyncio.sleep(3600)
            yield {}

    engine = SimpleNamespace(native=SimpleNamespace(on_frame=AsyncMock()),
        metrics=SimpleNamespace(counters=Counter()), notice=AsyncMock())
    monitor = NativeMonitor(PolymarketUSClient, PolymarketUSSettings,
        {"api_key_id": "TEST_POLY_ID", "secret_key": "TEST_POLY_SECRET"}, engine,
        config_model=PolymarketUSMonitorConfig)
    fake = Client()
    monitor.factory = lambda _: fake
    try:
        await monitor.start(SimpleNamespace(config={"environment": "production",
            "market_slugs": [], "private_updates": True}))
        assert fake.config is not None and not hasattr(fake.config, "event_ids")
    finally:
        await monitor.stop()
