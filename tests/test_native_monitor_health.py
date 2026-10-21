from collections import Counter
from types import SimpleNamespace

import pytest

from evplatform.core.plugins import PluginRegistry
from evplatform.plugins.external import NativeMonitor, ProviderBridge
from evplatform.integrations.parlay import ParlayWebSocket
from evplatform.venues.novig import NovigClient, NovigSettings


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
