import asyncio
from types import SimpleNamespace

import pytest

from evplatform.core.config import Settings
from evplatform.engine import Engine


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
