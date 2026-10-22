"""Offline API/restart checks: every external transport below is a local fake."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict

from evplatform.api import create_app
from evplatform.core.config import Settings
from evplatform.core.plugins import ExecutionVenue, InputPlugin, Manifest, Plugin, PluginRegistry, StrategyPlugin
from evplatform.core.storage import Storage
from evplatform.plugins import external


HEADERS = {"X-EV-Control": "local"}


class FeedConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sport: str = "default"


class OfflineFeed(InputPlugin):
    config_model = FeedConfig

    def __init__(self, name, starts, dependencies=()):
        self.manifest = Manifest(name=name, kind="input", description="Offline feed",
                                 dependencies=list(dependencies))
        self.starts = starts

    async def start(self, context):
        await super().start(context)
        self.starts.append((self.manifest.name, dict(context.config)))


class OfflineStrategy(StrategyPlugin):
    manifest = Manifest(name="offline_strategy", kind="strategy", description="Offline strategy")

    async def evaluate(self, event, view):
        return []


class OfflineExecution(ExecutionVenue):
    manifest = Manifest(name="offline_execution", kind="execution", description="Offline execution")

    async def submit(self, order, authorization):
        raise AssertionError("No order may be sent in startup preference tests")

    async def cancel(self, order):
        raise AssertionError("No order may be cancelled in startup preference tests")

    async def reconcile(self, order):
        raise AssertionError("No native account is connected")


class MetadataOnlyObserver(Plugin):
    manifest = Manifest(name="untrusted_observer", kind="observer", description="Metadata is not authorization",
                        capabilities={"monitor_only": True, "auto_start_safe": True})


@pytest.fixture
def offline_plugins(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OFFLINE_MONITOR_SECRET", "offline-secret-never-persist")
    monkeypatch.setattr(PluginRegistry, "discover", lambda self: None)
    starts = []
    inputs = {"offline_feed": (), "other_feed": ()}

    class OfflineMonitorClient:
        def __init__(self, settings):
            self.settings = settings

        @staticmethod
        def get_manifest():
            return {"id": "offline_monitor", "name": "Offline native monitor", "version": "1.0.0",
                    "type": "execution", "capabilities": {"monitor_only": True}}

        async def connect(self):
            starts.append(("offline_monitor", {"environment": self.settings.environment}))

        async def configure_subscriptions(self, config):
            pass

        async def disconnect(self):
            pass

        async def stream_updates(self):
            await asyncio.Event().wait()
            if False:
                yield {}

        def health_check(self):
            return {"state": "connected", "authenticated": True}

    def register(registry, engine):
        for name, dependencies in inputs.items():
            registry.register(OfflineFeed(name, starts, dependencies))
        registry.register(external.NativeMonitor(
            OfflineMonitorClient, SimpleNamespace, {"api_key": "OFFLINE_MONITOR_SECRET"}, engine))
        registry.register(OfflineStrategy())
        registry.register(OfflineExecution())
        registry.register(MetadataOnlyObserver())

    monkeypatch.setattr(external, "register_external", register)
    return starts, inputs


def app_for(path, **overrides):
    return create_app(Settings(_env_file=None, database_path=str(path), mode="production",
                               auto_trade=False, live_enabled=False, **overrides))


def toggle(client, name, enabled):
    return client.post(f"/api/plugins/{name}/toggle", headers=HEADERS, json={"enabled": enabled})


def saved_preferences(client):
    return client.portal.call(client.app.state.engine.storage.load_config).get("plugin_enabled", {})


async def seed_preferences(path, value):
    storage = Storage(str(path))
    await storage.open()
    try:
        await storage.save_config("plugin_enabled", value)
    finally:
        await storage.close()


def test_enabled_feeds_and_real_monitor_wrapper_restore_after_api_restart(tmp_path, offline_plugins):
    starts, _ = offline_plugins
    path = tmp_path / "restart.db"
    with TestClient(app_for(path)) as first:
        assert not starts
        assert first.post("/api/plugins/offline_feed/config", headers=HEADERS,
                          json={"config": {"sport": "reviewed-scope"}}).status_code == 200
        assert toggle(first, "offline_feed", True).status_code == 200
        assert toggle(first, "offline_monitor", True).status_code == 200
        assert saved_preferences(first) == {"offline_feed": True, "offline_monitor": True}
        assert "offline-secret" not in json.dumps(saved_preferences(first))
    starts.clear()
    with TestClient(app_for(path)) as second:
        assert starts == [("offline_feed", {"sport": "reviewed-scope"}),
                          ("offline_monitor", {"environment": "sandbox"})]
        state = second.get("/api/state").json()
        assert not state["auto_trade"] and not state["native_orders"]
        assert second.get("/api/readiness").json()["live_trading_ready"] is False
        assert second.app.state.engine.registry.records["offline_monitor"].enabled
        assert saved_preferences(second) == {"offline_feed": True, "offline_monitor": True}


def test_explicit_disable_survives_restart_and_other_preference_is_preserved(tmp_path, offline_plugins):
    starts, _ = offline_plugins
    path = tmp_path / "disabled.db"
    with TestClient(app_for(path)) as first:
        assert toggle(first, "offline_feed", True).status_code == 200
        assert toggle(first, "other_feed", True).status_code == 200
        assert toggle(first, "offline_feed", False).status_code == 200
        assert saved_preferences(first) == {"offline_feed": False, "other_feed": True}
    starts.clear()
    with TestClient(app_for(path)) as second:
        assert starts == [("other_feed", {"sport": "default"})]
        assert not second.app.state.engine.registry.records["offline_feed"].enabled


def test_strategy_execution_and_metadata_claims_are_never_persisted(tmp_path, offline_plugins):
    path = tmp_path / "restricted.db"
    with TestClient(app_for(path)) as first:
        for name in ("offline_strategy", "offline_execution", "untrusted_observer"):
            assert toggle(first, name, True).status_code == 200
            assert first.app.state.engine.registry.records[name].enabled
        assert saved_preferences(first) == {}
    with TestClient(app_for(path)) as second:
        for name in ("offline_strategy", "offline_execution", "untrusted_observer"):
            assert not second.app.state.engine.registry.records[name].enabled
        assert not second.app.state.engine.risk.auto_trade


@pytest.mark.parametrize("malformed", [None, [], "true", True, 1, {"offline_feed": "true"},
                                       {"offline_feed": 1}, {"offline_feed": []}, {"offline_feed": {}}])
async def test_malformed_saved_preferences_never_activate_a_source(tmp_path, offline_plugins, malformed):
    starts, _ = offline_plugins
    path = tmp_path / "malformed.db"
    await seed_preferences(path, malformed)
    with TestClient(app_for(path)) as client:
        assert not starts
        assert not client.app.state.engine.registry.records["offline_feed"].enabled


async def test_stored_unknown_and_execution_flags_cannot_change_core_mode_defaults(tmp_path, offline_plugins):
    path = tmp_path / "forbidden.db"
    await seed_preferences(path, {"missing_plugin": True, "offline_strategy": True,
        "offline_execution": True, "untrusted_observer": True, "synthetic_schedule": True,
        "positive_ev": False, "native_positive_ev": True, "paper_execution": False, "auto_trade": True})
    with TestClient(app_for(path)) as client:
        engine = client.app.state.engine
        assert engine.registry.records["positive_ev"].enabled
        assert engine.registry.records["paper_execution"].enabled
        for name in ("offline_strategy", "offline_execution", "untrusted_observer",
                     "synthetic_schedule", "native_positive_ev"):
            assert not engine.registry.records[name].enabled
        assert not engine.risk.auto_trade and not engine.settings.live_enabled
        assert engine._plugin_enable_preferences == {}
        assert toggle(client, "offline_feed", True).status_code == 200
        assert saved_preferences(client) == {"offline_feed": True}


def test_explicit_native_workspace_starts_signal_strategy_without_arming(tmp_path, offline_plugins):
    settings = Settings(_env_file=None, database_path=str(tmp_path / "native-signals.db"),
                        data_dir=str(tmp_path), mode="production", live_enabled=True,
                        auto_trade=True, sample_feed_enabled=False)
    with TestClient(create_app(settings)) as client:
        engine = client.app.state.engine
        assert engine.registry.records["native_positive_ev"].enabled
        assert not engine.risk.auto_trade
        engine.risk.halt("Offline emergency stop")
        assert engine.registry.records["native_positive_ev"].enabled
        assert engine.risk.kill_switch and not engine.risk.auto_trade
        assert not engine.risk.native_orders


@pytest.mark.parametrize("dependencies", [("missing",), ("offline_feed",)])
async def test_restored_flags_do_not_revive_missing_or_cyclic_dependencies(tmp_path, offline_plugins, dependencies):
    starts, inputs = offline_plugins
    inputs["offline_feed"] = dependencies
    path = tmp_path / "dependency.db"
    await seed_preferences(path, {"offline_feed": True, "other_feed": True})
    with TestClient(app_for(path)) as client:
        record = client.app.state.engine.registry.records["offline_feed"]
        assert not record.enabled and record.health["status"] == "blocked"
        assert starts == [("other_feed", {"sport": "default"})]


async def test_restored_dependency_starts_first_and_disabled_dependency_blocks_child(tmp_path, offline_plugins):
    starts, inputs = offline_plugins
    inputs["offline_feed"] = ("other_feed",)
    path = tmp_path / "ordered.db"
    await seed_preferences(path, {"offline_feed": True, "other_feed": True})
    with TestClient(app_for(path)):
        assert [name for name, _ in starts] == ["other_feed", "offline_feed"]
    starts.clear()
    await seed_preferences(path, {"offline_feed": True, "other_feed": False})
    with TestClient(app_for(path)) as client:
        assert not starts
        assert client.app.state.engine.registry.records["offline_feed"].health["status"] == "blocked"


def test_failed_preference_write_does_not_start_or_publish_enabled(tmp_path, offline_plugins, monkeypatch):
    starts, _ = offline_plugins
    with TestClient(app_for(tmp_path / "failed-write.db")) as client:
        storage = client.app.state.engine.storage
        original = storage.save_config

        async def fail_preference(key, value):
            if key == "plugin_enabled":
                raise RuntimeError("offline-secret-never-expose")
            await original(key, value)

        monkeypatch.setattr(storage, "save_config", fail_preference)
        response = toggle(client, "offline_feed", True)
        assert response.status_code == 503
        assert "offline-secret" not in response.text
        assert not starts
        assert not client.app.state.engine.registry.records["offline_feed"].enabled
        assert saved_preferences(client) == {}


def test_unknown_or_unauthorized_toggle_does_not_persist(tmp_path, offline_plugins):
    with TestClient(app_for(tmp_path / "invalid-toggle.db")) as client:
        assert toggle(client, "missing", True).status_code == 404
        assert client.post("/api/plugins/offline_feed/toggle", json={"enabled": True}).status_code == 403
        assert saved_preferences(client) == {}
