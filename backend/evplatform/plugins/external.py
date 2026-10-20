"""Metadata and lifecycle bridge. Provider protocols never enter pricing or dashboard code."""
import asyncio
import os
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ..core.models import Envelope
from ..core.plugins import InputPlugin, Manifest, Plugin


def normalize_manifest(raw):
    authentication = raw.get("authentication", [])
    if isinstance(authentication, dict):
        authentication = authentication.get("credential_env", [])
    elif isinstance(authentication, str):
        authentication = [authentication]
    schema = raw.get("config_schema", {})
    if "type" not in schema:
        schema = {"type": "object", "properties": schema}
    return Manifest(
        name=raw.get("id", raw["name"]), version=raw["version"],
        kind=raw.get("plugin_type", raw.get("type", "input")),
        description=raw["name"], sports=raw.get("sports", []), markets=raw.get("markets", []),
        input_schemas=raw.get("input_schemas", []), output_schemas=raw.get("output_schemas", []),
        transports=raw.get("transports", []), authentication=authentication,
        capabilities={**raw.get("capabilities", {}), "streaming": raw.get("streaming", "websocket" in raw.get("transports", [])),
                      "polling": raw.get("polling", "rest" in raw.get("transports", [])),
                      "live_tested": False, "limitations": raw.get("limitations", [])},
        rate_limits=raw.get("rate_limits", {}), config_schema=schema, permissions=raw.get("permissions", []),
        documentation=raw.get("documentation", [raw["docs"]] if "docs" in raw else []),
    )


class ProviderBridge(InputPlugin):
    def __init__(self, factory, engine):
        self.factory, self.engine = factory, engine
        self.manifest = normalize_manifest(factory.manifest)
        self.config_model = factory.config_class
        self.client = None
        self.task = None
        self.mapper = None

    async def start(self, context):
        await self.stop()
        await super().start(context)
        from ..integrations.normalization import VerifiedProviderMapper
        self.mapper = await asyncio.to_thread(VerifiedProviderMapper.from_file, "data/verified_mappings.json")
        self.client = self.factory({**context.config, "enabled": True})
        if await self.client.connect():
            self.task = asyncio.create_task(self._run(), name=f"input:{self.manifest.name}")

    async def _invalidate(self):
        for values in self.engine.quotes.values():
            for quote in list(values.values()):
                if quote.provider == self.manifest.name:
                    await self.context.bus.publish(Envelope(
                        schema_name="odds.quote", source=self.manifest.name,
                        payload=quote.model_copy(update={"status": "withdrawn"})))

    async def _run(self):
        try:
            async for raw in self.client.stream_updates():
                if raw.kind in {"feed_invalidated", "disconnected", "reconnect"}:
                    await self._invalidate()
                for market, quote in self.mapper(raw):
                    await self.context.bus.publish(Envelope(schema_name="sports.market", source=self.manifest.name, payload=market))
                    await self.context.bus.publish(Envelope(schema_name="odds.quote", source=self.manifest.name, payload=quote))
                self.engine.metrics.counters[f"provider_messages:{self.manifest.name}"] += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self.engine.notice("error", f"Provider stopped: {type(exc).__name__}", self.manifest.name)
        finally:
            await self._invalidate()

    async def stop(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        if self.client:
            await self.client.disconnect()
            self.client = None

    async def connect(self):
        return await self.client.connect()

    async def disconnect(self):
        await self.stop()

    async def discover_sports(self):
        return await self.client.discover_sports()

    async def discover_events(self, *args, **kwargs):
        return await self.client.discover_events(*args, **kwargs)

    async def discover_markets(self, *args, **kwargs):
        return await self.client.discover_markets(*args, **kwargs)

    async def subscribe(self, markets):
        return await self.client.subscribe(markets)

    async def unsubscribe(self, markets):
        return await self.client.unsubscribe(markets)

    async def fetch_snapshot(self, *args, **kwargs):
        return await self.client.fetch_snapshot(*args, **kwargs)

    def stream_updates(self):
        return self.client.stream_updates()

    async def health_check(self):
        if not self.client:
            return {"status": "disabled", "message": "Enable after configuring credentials and sport subscriptions"}
        health = await self.client.health_check()
        status = health["state"]
        if status in {"missing_credentials", "unauthorized", "access_denied"}:
            status = "blocked"
        if self.task and self.task.done():
            status = "error"
        return {**health, "status": status, "message": health["reason"],
                "mapped_quotes": self.mapper.mapped_count if self.mapper else 0,
                "quarantined": len(self.mapper.quarantine) if self.mapper else 0}


class NativeMonitorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    environment: Literal["sandbox", "production"] = "sandbox"
    market_ids: list[str] = Field(default_factory=list)


class NativeMonitor(Plugin):
    config_model = NativeMonitorConfig

    def __init__(self, factory, settings_factory, credentials, engine):
        self.factory, self.settings_factory, self.credentials = factory, settings_factory, credentials
        self.engine = engine
        self.manifest = normalize_manifest(factory.get_manifest()).model_copy(update={
            "config_schema": NativeMonitorConfig.model_json_schema()})
        self.client, self.task = None, None
        self.frames = 0

    async def start(self, context):
        await self.stop()
        await super().start(context)
        if not all(os.getenv(env) for env in self.credentials.values()):
            return
        self.client = self.factory(self.settings_factory(
            environment=context.config.get("environment", "sandbox"),
            **{key: os.environ[env] for key, env in self.credentials.items()}))
        await self.client.connect()
        if context.config.get("market_ids"):
            await self.client.subscribe(context.config["market_ids"])
        self.task = asyncio.create_task(self._run(), name=f"native-monitor:{self.manifest.name}")

    async def _run(self):
        try:
            async for frame in self.client.stream_updates():
                # Account payloads stay backend-only. Mapping into tradable canonical books
                # is explicitly separate and requires verified settlement, units, and fees.
                if frame.get("type") == "transport_invalidated":
                    self.engine.metrics.counters[f"native_invalidations:{self.manifest.name}"] += 1
                    continue
                self.frames += 1
                self.engine.metrics.counters[f"native_messages:{self.manifest.name}"] += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self.engine.notice("error", f"Native monitor stopped: {type(exc).__name__}", self.manifest.name)

    async def stop(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        if self.client:
            await self.client.disconnect()
            self.client = None

    async def health_check(self):
        if not self.client:
            return {"status": "blocked", "message": "Native credentials missing; trading remains gated"}
        state = self.client.health_check()
        blocked = state["state"] in {"credentials_missing", "unauthorized", "access_denied", "geolocation_required", "subscription_rejected"}
        status = "blocked" if blocked else "error" if self.task and self.task.done() else state["state"]
        return {**state, "status": status,
                "message": state.get("reason") or "Native monitor; normalized venue mapping and live preflight required for orders",
                "frames": self.frames}


def register_external(registry, engine):
    from ..integrations import PROVIDER_FACTORIES
    from ..venues import KalshiClient, KalshiSettings, NovigClient, NovigSettings, ProphetXClient, ProphetXSettings
    for name, factory in PROVIDER_FACTORIES.items():
        registry.register(ProviderBridge(factory, engine))
        registry.records[name].credential_status = "configured_unverified" if os.getenv(factory.credential_env) else "missing"
    for factory, settings_factory, credentials in [
        (KalshiClient, KalshiSettings, {"api_key_id": "KALSHI_API_KEY_ID", "private_key_path": "KALSHI_PRIVATE_KEY_PATH"}),
        (NovigClient, NovigSettings, {"api_key": "NOVIG_API_KEY", "private_key_path": "NOVIG_PRIVATE_KEY_PATH"}),
        (ProphetXClient, ProphetXSettings, {"api_key": "PROPHETX_API_KEY", "secret_key": "PROPHETX_SECRET_KEY"}),
    ]:
        plugin = NativeMonitor(factory, settings_factory, credentials, engine)
        registry.register(plugin)
        registry.records[plugin.manifest.name].credential_status = "configured_unverified" if all(os.getenv(v) for v in credentials.values()) else "missing"

