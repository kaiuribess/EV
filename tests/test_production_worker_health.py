import asyncio
from types import SimpleNamespace

import pytest

from evplatform.core.config import Settings
from evplatform.core.config import RiskConfig
from evplatform.core.models import utcnow
from evplatform.core.risk import RiskEngine
from evplatform.engine import Engine
from evplatform.plugins.external import register_external
from datetime import timedelta


@pytest.mark.asyncio
async def test_stopped_core_worker_is_visible_without_stopping_research(tmp_path):
    engine = Engine(Settings(mode="production", database_path=str(tmp_path / "worker.db")))
    active = asyncio.create_task(asyncio.sleep(60), name="active-worker")
    stopped = asyncio.create_task(asyncio.sleep(0), name="stopped-worker")
    await stopped
    try:
        engine.tasks = [active, stopped]
        assert engine.stopped_workers() == ["stopped-worker"]
        engine.stopping = True
        assert engine.stopped_workers() == []
    finally:
        active.cancel()
        await asyncio.gather(active, return_exceptions=True)


@pytest.mark.asyncio
async def test_only_intended_stopped_plugin_workers_are_restartable(tmp_path):
    engine = Engine(Settings(mode="production", database_path=str(tmp_path / "plugin.db")))
    stopped = asyncio.create_task(asyncio.sleep(0))
    active = asyncio.create_task(asyncio.sleep(60))
    await stopped
    try:
        engine.registry.records["stopped_feed"] = SimpleNamespace(
            enabled=True, plugin=SimpleNamespace(task=stopped))
        engine.registry.records["active_feed"] = SimpleNamespace(
            enabled=True, plugin=SimpleNamespace(task=active))
        engine.registry.records["disabled_feed"] = SimpleNamespace(
            enabled=False, plugin=SimpleNamespace(task=stopped))
        assert engine.stopped_plugin_workers() == ["stopped_feed"]
        engine._plugin_enable_preferences["disabled_feed"] = True
        assert engine.stopped_plugin_workers() == ["stopped_feed", "disabled_feed"]
    finally:
        active.cancel()
        await asyncio.gather(active, return_exceptions=True)


def test_expired_native_lease_disarms_but_does_not_stop_scanner():
    risk = RiskEngine(RiskConfig())
    risk.kill_switch = False
    risk.auto_trade = True
    risk.native_authorization_expires_at = utcnow() - timedelta(seconds=1)
    assert risk.expire_native_authorization()
    assert risk.kill_switch and not risk.auto_trade
    assert risk.native_authorization_expires_at is None


def test_failed_enabled_input_startup_is_recoverable_without_reviving_operator_disabled_input(tmp_path):
    engine = Engine(Settings(mode="production", database_path=str(tmp_path / "startup.db")))
    register_external(engine.registry, engine)
    failed = engine.registry.records["novig"]
    failed.enabled = False
    failed.health = {"status": "error", "message": "Plugin failed: FileNotFoundError"}
    engine._plugin_enable_preferences["novig"] = True
    assert "novig" in engine.stopped_plugin_workers()
    engine._plugin_enable_preferences["novig"] = False
    assert "novig" not in engine.stopped_plugin_workers()
