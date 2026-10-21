from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from importlib.metadata import entry_points
import os
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .bus import EventBus, SchemaRegistry
from .models import Candidate, Order


class UnsupportedOperation(NotImplementedError):
    pass


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    version: str = "1.0.0"
    contract_version: int = 1
    kind: Literal["input", "strategy", "execution", "observer", "risk", "pricing"]
    description: str
    sports: list[str] = Field(default_factory=lambda: ["discoverable"])
    markets: list[str] = Field(default_factory=lambda: ["discoverable"])
    input_schemas: list[str] = Field(default_factory=list)
    output_schemas: list[str] = Field(default_factory=list)
    transports: list[str] = Field(default_factory=list)
    authentication: list[str] = Field(default_factory=list)
    capabilities: dict[str, Any] = Field(default_factory=dict)
    rate_limits: dict[str, Any] = Field(default_factory=dict)
    config_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}})
    health_check: bool = True
    permissions: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    documentation: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class PluginContext:
    """Strategy context deliberately excludes order routers, credentials, and risk mutation."""
    bus: EventBus
    schemas: SchemaRegistry
    config: dict
    services: dict = field(default_factory=dict)


class Plugin(ABC):
    manifest: Manifest
    config_model: type[BaseModel] | None = None

    async def start(self, context: PluginContext):
        self.context = context

    async def stop(self):
        pass

    async def health_check(self) -> dict:
        return {"status": "ready", "message": "Initialized"}


class InputPlugin(Plugin):
    async def connect(self):
        raise UnsupportedOperation("connect")

    async def disconnect(self):
        pass

    async def discover_sports(self):
        raise UnsupportedOperation("discover_sports")

    async def discover_events(self):
        raise UnsupportedOperation("discover_events")

    async def discover_markets(self):
        raise UnsupportedOperation("discover_markets")

    async def subscribe(self, markets):
        raise UnsupportedOperation("subscribe")

    async def unsubscribe(self, markets):
        raise UnsupportedOperation("unsubscribe")

    async def fetch_snapshot(self):
        raise UnsupportedOperation("fetch_snapshot")

    def stream_updates(self):
        raise UnsupportedOperation("stream_updates")

    def get_capabilities(self):
        return self.manifest.capabilities


class StrategyPlugin(Plugin):
    @abstractmethod
    async def evaluate(self, event, view) -> list[Candidate]: ...

    def revalidate(self, candidate, view) -> Candidate:
        """Implement synchronous, memory-only pre-transmission validation for new strategies."""
        raise UnsupportedOperation("Strategy must implement revalidate before execution can be enabled")


class ExecutionVenue(Plugin):
    @abstractmethod
    async def submit(self, order: Order, authorization): ...

    @abstractmethod
    async def cancel(self, order: Order): ...

    @abstractmethod
    async def reconcile(self, order: Order): ...


@dataclass
class PluginRecord:
    plugin: Plugin
    enabled: bool = False
    config: dict = field(default_factory=dict)
    health: dict = field(default_factory=lambda: {"status": "disabled", "message": "Not enabled"})
    credential_status: str = "not_required"


class PluginRegistry:
    def __init__(self):
        self.records: dict[str, PluginRecord] = {}
        self.config_migrations: dict[tuple[str, int], Any] = {}
        self.discovery_errors: list[dict] = []

    def register(self, plugin: Plugin, *, enabled=False):
        m = plugin.manifest
        if m.contract_version != 1:
            raise ValueError("Unsupported plugin contract version")
        if m.name in self.records:
            raise ValueError(f"Duplicate plugin name: {m.name}")
        config = plugin.config_model().model_dump(mode="json") if plugin.config_model else {}
        self.records[m.name] = PluginRecord(plugin, enabled, config)

    def discover(self):
        for point in entry_points(group="evplatform.plugins"):
            try:
                self.register(point.load()())
            except Exception as exc:
                self.discovery_errors.append({"plugin": point.name, "error": type(exc).__name__})

    def validate_dependencies(self):
        visiting, visited, ordered = set(), set(), []

        def visit(name):
            if name in visiting:
                raise ValueError("Plugin dependency cycle")
            if name in visited:
                return
            if name not in self.records:
                raise ValueError(f"Missing plugin dependency: {name}")
            visiting.add(name)
            for dependency in self.records[name].plugin.manifest.dependencies:
                visit(dependency)
            visiting.remove(name)
            visited.add(name)
            ordered.append(name)
        for name in self.records:
            visit(name)
        return ordered

    def compatible_startup_order(self):
        """Keep a broken new dependency tree from taking working plugins down."""
        ordered = []
        for root in self.records:
            visiting, local = set(), []

            def visit(name):
                if name in visiting:
                    raise ValueError("Dependency cycle")
                if name not in self.records:
                    raise ValueError("Missing dependency")
                if name in local:
                    return
                visiting.add(name)
                for dependency in self.records[name].plugin.manifest.dependencies:
                    visit(dependency)
                visiting.remove(name)
                local.append(name)

            try:
                visit(root)
            except ValueError:
                self.records[root].enabled = False
                self.records[root].health = {"status": "blocked", "message": "Missing or cyclic plugin dependency"}
                self.discovery_errors.append({"plugin": root, "error": "IncompatibleDependencies"})
            else:
                ordered.extend(name for name in local if name not in ordered)
        return ordered

    def configure(self, name, config, version=1):
        record = self.records[name]
        while (name, version) in self.config_migrations:
            config = self.config_migrations[name, version](config)
            version += 1
        if not record.plugin.config_model:
            if config:
                raise ValueError("Plugin does not accept configurable options")
        else:
            config = record.plugin.config_model.model_validate(config).model_dump(mode="json")
        record.config = config
        return config

    async def start(self, name, context):
        record = self.records[name]
        unavailable = [d for d in record.plugin.manifest.dependencies
                       if d not in self.records or not self.records[d].enabled]
        if unavailable:
            record.enabled = False
            record.health = {"status": "blocked", "message": "Enable required plugins: " + ", ".join(unavailable)}
            return
        try:
            await record.plugin.start(context)
            record.health = await record.plugin.health_check()
            record.enabled = record.health["status"] not in {"disabled", "error", "blocked"}
        except Exception as exc:
            try:
                await record.plugin.stop()
            except Exception:
                pass
            record.enabled = False
            # Exception strings from network clients can contain auth query parameters.
            record.health = {"status": "error", "message": f"Plugin failed: {type(exc).__name__}"}

    async def stop(self, name):
        record = self.records[name]
        for other, dependent in self.records.items():
            if dependent.enabled and name in dependent.plugin.manifest.dependencies:
                await self.stop(other)
        try:
            await record.plugin.stop()
        finally:
            record.enabled = False
            record.health = {"status": "disabled", "message": "Disabled by operator"}

    def public(self):
        def credential_status(record):
            if record.health.get("authenticated") is True:
                return "authenticated"
            if record.health.get("state") == "unauthorized":
                return "rejected"
            names_for_config = getattr(record.plugin, "credential_names", None)
            if callable(names_for_config):
                try:
                    selected = names_for_config(record.config)
                except ValueError:
                    return "missing"
                return "configured_unverified" if all(os.getenv(name) for name in selected.values()) else "missing"
            return record.credential_status

        return [{
            "manifest": r.plugin.manifest.model_dump(mode="json"),
            "enabled": r.enabled, "health": r.health,
            "credential_status": credential_status(r),
            "config": r.config,
        } for r in self.records.values()]

