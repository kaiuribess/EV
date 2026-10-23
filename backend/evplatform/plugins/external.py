"""Metadata and lifecycle bridge. Provider protocols never enter pricing or dashboard code."""
import asyncio
import os
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..core.models import Envelope, ProviderFrame, utcnow
from ..core.paths import runtime_data_file
from ..core.plugins import InputPlugin, Manifest, Plugin
from ..venues.prophetx_financial import ProphetXAccountReview


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
        from ..integrations.normalization import TrustedSourceProjector, VerifiedProviderMapper
        self.mapper = await asyncio.to_thread(VerifiedProviderMapper.from_file,
            runtime_data_file("verified_mappings.json", self.engine.settings.data_dir))
        self.mapper.research_projector = TrustedSourceProjector(lambda: self.engine.settings.pricing)
        self.client = self.factory({**context.config, "enabled": True})
        if await self.client.connect():
            self.task = asyncio.create_task(self._run(), name=f"input:{self.manifest.name}")

    async def _invalidate(self):
        paper = self.engine.registry.records.get("parlay_paper_input")
        if self.manifest.name == "parlay_game_ws" and paper and paper.enabled and self.client:
            for sport in self.client.config.sports:
                now = utcnow()
                await self.context.bus.publish(Envelope(schema_name="provider.frame",
                    source=self.manifest.name, payload=ProviderFrame(
                        provider_id="parlay_game_ws", kind="feed_invalidated",
                        payload={"sport": sport}, received_at=now, metadata={"sport": sport})))
        oddize_paper = self.engine.registry.records.get("oddize_paper_input")
        if self.manifest.name == "oddize_rest" and oddize_paper and oddize_paper.enabled:
            await self.context.bus.publish(Envelope(schema_name="provider.frame",
                source=self.manifest.name, payload=ProviderFrame(
                    provider_id="oddize_rest", kind="feed_invalidated", payload={},
                    received_at=utcnow())))
        for values in self.engine.quotes.values():
            for quote in list(values.values()):
                if quote.provider == self.manifest.name:
                    await self.context.bus.publish(Envelope(
                        schema_name="odds.quote", source=self.manifest.name,
                        payload=quote.model_copy(update={"status": "withdrawn"})))

    async def _run(self):
        try:
            async for raw in self.client.stream_updates():
                # Share the already-authenticated complete game board with
                # paper-only consumers; opening duplicate provider sockets can
                # hit account connection limits and split snapshot state.
                paper = self.engine.registry.records.get("parlay_paper_input")
                if (self.manifest.name == "parlay_game_ws" and paper and paper.enabled):
                    await self.context.bus.publish(Envelope(schema_name="provider.frame",
                        source=self.manifest.name, payload=ProviderFrame(
                            provider_id=raw.provider_id, kind=raw.kind, payload=raw.payload,
                            received_at=raw.received_at, published_at=raw.published_at,
                            snapshot_complete=raw.snapshot_complete, metadata=raw.metadata)))
                oddize_paper = self.engine.registry.records.get("oddize_paper_input")
                if (self.manifest.name == "oddize_rest" and oddize_paper and oddize_paper.enabled
                        and raw.kind in {"snapshot", "feed_invalidated"}):
                    await self.context.bus.publish(Envelope(schema_name="provider.frame",
                        source=self.manifest.name, payload=ProviderFrame(
                            provider_id=raw.provider_id, kind=raw.kind, payload=raw.payload,
                            received_at=raw.received_at, published_at=raw.published_at,
                            snapshot_complete=raw.snapshot_complete, metadata=raw.metadata)))
                # A provider may invalidate only one sport while its other
                # sockets retain complete, usable baselines. The raw scoped
                # frame above reaches paper consumers and the mapper below
                # suspends only that sport's verified quotes. A bridge-wide
                # invalidation here would erase every healthy sport whenever
                # one WebSocket reconnects.
                if raw.kind in {"disconnected", "reconnect"} or (
                    raw.kind == "feed_invalidated" and not (
                        isinstance(raw.payload, dict) and raw.payload.get("sport")
                        or raw.metadata.get("sport")
                    )
                ):
                    await self._invalidate()
                mapped = self.mapper(raw)
                for index, (market, quote) in enumerate([*mapped, *getattr(self.mapper, "research_updates", ())]):
                    await self.context.bus.publish(Envelope(schema_name="sports.market", source=self.manifest.name, payload=market))
                    await self.context.bus.publish(Envelope(schema_name="odds.quote", source=self.manifest.name, payload=quote))
                    if index % 64 == 63:
                        # A complete source board can contain thousands of
                        # rows. Give active critical consumers a chance to
                        # drain between batches without dropping events or
                        # relaxing their overflow safety check.
                        await asyncio.sleep(0)
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
        if status in {"missing_credentials", "unauthorized", "access_denied", "quota_exhausted"}:
            status = "blocked"
        if status != "blocked" and self.task and self.task.done():
            status = "error"
        return {**health, "status": status, "message": health["reason"],
                "mapped_quotes": self.mapper.mapped_count if self.mapper else 0,
                "quarantined": len(self.mapper.quarantine) if self.mapper else 0}


class NativeMonitorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    environment: Literal["sandbox", "production"] = "sandbox"
    market_ids: list[str] = Field(default_factory=list)


class KalshiMonitorConfig(NativeMonitorConfig):
    credential_profile: Literal["legacy", "environment"] = Field(default="environment",
        description="Environment selects the separate Kalshi demo key for sandbox and the production key for production. Legacy preserves older configuration explicitly.")
    discovery_series: list[str] = Field(default_factory=list, max_length=5,
        description="Documented public Kalshi sports series tickers for bounded current-book discovery")
    discovery_interval_seconds: int = Field(default=300, ge=60, le=3600)
    max_discovered_markets: int = Field(default=8, ge=1, le=100)

    @field_validator("discovery_series")
    @classmethod
    def valid_series(cls, values):
        if len(set(values)) != len(values) or any(
            not value or len(value) > 40 or not value.isascii()
            or any(ch not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" for ch in value)
            for value in values
        ):
            raise ValueError("Use distinct uppercase Kalshi series tickers")
        return values

    @model_validator(mode="after")
    def bounded_discovery_scope(self):
        if self.discovery_series and len(self.market_ids) + self.max_discovered_markets > 100:
            raise ValueError("Kalshi explicit and discovered market caps must total at most 100")
        return self


class NovigMonitorConfig(NativeMonitorConfig):
    credential_profile: Literal["legacy", "environment"] = Field(default="legacy",
        description="Legacy preserves the existing pair. Environment selects separate paper/production trading credentials.")
    discovery_leagues: list[str] = Field(default_factory=list, max_length=8,
        description="Canonical Novig league codes for read-only open-market discovery; empty disables discovery")
    discovery_market_types: list[str] = Field(default_factory=list, max_length=24,
        description="Canonical Novig market-type codes, paired with discovery_leagues")
    discovery_interval_seconds: int = Field(default=300, ge=60, le=3600)
    discovery_page_size: int = Field(default=500, ge=1, le=5000)
    max_discovered_markets: int = Field(default=50, ge=1, le=100)

    @field_validator("discovery_leagues", "discovery_market_types")
    @classmethod
    def canonical_discovery_codes(cls, values):
        if len(set(values)) != len(values) or any(
            not value or len(value) > 24 or not value.isascii()
            or any(ch not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_" for ch in value)
            for value in values
        ):
            raise ValueError("Use distinct uppercase canonical Novig discovery codes")
        return values

    @model_validator(mode="after")
    def complete_discovery_scope(self):
        if bool(self.discovery_leagues) != bool(self.discovery_market_types):
            raise ValueError("Novig discovery needs both leagues and market types")
        if len(self.market_ids) + self.max_discovered_markets > 100 and self.discovery_leagues:
            raise ValueError("Novig explicit and discovered market caps must total at most 100")
        return self


class ProphetXMonitorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    environment: Literal["sandbox", "production"] = "sandbox"
    event_ids: list[str] = Field(default_factory=list, description="Native ProphetX event IDs; empty monitors private/system channels only")
    prop_subtypes: dict[str, list[str]] = Field(default_factory=dict, description="Event ID to discovered prop sub_type values")
    discovery_tournament_ids: list[int] = Field(default_factory=list, max_length=4,
        description="Authenticated tournament catalogs to refresh for game-market subscriptions")
    discovery_interval_seconds: int = Field(default=300, ge=60, le=3600)
    max_discovered_events: int = Field(default=20, ge=1, le=50)
    max_discovered_prop_subscriptions: int = Field(default=3, ge=0, le=10,
        description="Read-only player prop subtype channels selected from the current V4 catalog")
    account_review: ProphetXAccountReview | None = Field(default=None,
        description="Non-secret account inception/funded-cash evidence; does not authorize orders")
    legacy_market_ids: list[str] = Field(default_factory=list, deprecated=True,
                                       description="Unverified IDs migrated from older settings; replace with event_ids and clear this list")

    @field_validator("event_ids")
    @classmethod
    def numeric_events(cls, values):
        if any(not value.isascii() or not value.isdigit() for value in values):
            raise ValueError("ProphetX event IDs must be numeric strings")
        return list(dict.fromkeys(values))

    @field_validator("prop_subtypes")
    @classmethod
    def valid_props(cls, values):
        if any(not key.isascii() or not key.isdigit() or any(not subtype.strip() for subtype in subtypes)
               for key, subtypes in values.items()):
            raise ValueError("Prop subscriptions require numeric event IDs and nonempty discovered subtypes")
        return values

    @field_validator("discovery_tournament_ids")
    @classmethod
    def valid_discovery_tournaments(cls, values):
        if any(type(value) is not int or value <= 0 or value > 1_000_000_000 for value in values) or len(set(values)) != len(values):
            raise ValueError("Use distinct positive tournament IDs from native discovery")
        return values


def migrate_prophetx_monitor_config(config):
    migrated = dict(config)
    legacy = migrated.pop("market_ids", [])
    if legacy:
        # The old generic field did not establish that these were event IDs.
        # Retain the operator's values, but require explicit scope confirmation.
        migrated["legacy_market_ids"] = legacy
    return migrated


class NativeMonitor(Plugin):
    config_model = NativeMonitorConfig

    def __init__(self, factory, settings_factory, credentials, engine, *, config_model=None,
                 credential_environments=None):
        self.factory, self.settings_factory, self.credentials = factory, settings_factory, credentials
        self.credential_environments = credential_environments or {}
        self.engine = engine
        self.config_model = config_model or NativeMonitorConfig
        self.manifest = normalize_manifest(factory.get_manifest()).model_copy(update={
            "config_schema": self.config_model.model_json_schema()})
        self.client, self.task = None, None
        self.management_client = None
        self.discovery_task = None
        self.frames = 0
        self.config_error = ""
        self.discovery_status = "disabled"
        self.discovered_event_ids: list[str] = []
        self.discovered_market_ids: list[str] = []
        self._subscribed_event_ids: list[str] = []
        self._subscribed_prop_subtypes: dict[str, list[str]] = {}
        self.discovered_prop_subtypes: dict[str, list[str]] = {}
        self._subscribed_market_ids: list[str] = []
        # Catalog evidence is read-only. It is never fed to NativeBookMapper or
        # used as an executable mapping without a separate exact review.
        self.research_catalog_rows: list[dict] = []
        self.research_catalog_at = None
        self.research_matches: dict[str, list] = {}
        self.research_events: dict[str, dict] = {}
        self.research_fee_metadata: dict[str, dict] = {}
        self._last_research_source_ids = frozenset()

    def credential_names(self, config):
        default_profile = getattr(self.config_model(), "credential_profile", "legacy")
        if config.get("credential_profile", default_profile) == "legacy":
            return self.credentials
        names = self.credential_environments.get(config.get("environment", "sandbox"))
        if not names:
            raise ValueError("No credential profile declared for this environment")
        return names

    async def start(self, context):
        await self.stop()
        await super().start(context)
        self.config_error = ""
        self.discovery_status = "disabled"
        self.discovered_event_ids = []
        self.discovered_market_ids = []
        self._subscribed_event_ids = []
        self._subscribed_prop_subtypes = {}
        self.discovered_prop_subtypes = {}
        self._subscribed_market_ids = []
        self.research_catalog_rows = []
        self.research_catalog_at = None
        self.research_matches = {}
        self.research_events = {}
        self.research_fee_metadata = {}
        self._last_research_source_ids = frozenset()
        if context.config.get("legacy_market_ids"):
            self.config_error = "Confirm the migrated ProphetX IDs in event_ids or prop_subtypes, then clear legacy_market_ids"
            return
        credentials = self.credential_names(context.config)
        if not all(os.getenv(env) for env in credentials.values()):
            self.config_error = "Complete the selected backend credential pair; credentials from another profile are not substituted"
            return
        self.client = self.factory(self.settings_factory(
            environment=context.config.get("environment", "sandbox"),
            **{key: os.environ[env] for key, env in credentials.items()}))
        if self.manifest.name == "novig":
            # Management metadata is a read-only permission witness. It is
            # never used as the execution signer or substituted for a missing
            # trading key, and Paper/Production credentials never cross hosts.
            from ..venues.novig import NovigClient, NovigSettings

            environment = context.config.get("environment", "sandbox")
            prefix = "NOVIG_PRODUCTION_MANAGEMENT_" if environment == "production" else "NOVIG_MANAGEMENT_"
            manager_id = os.getenv(prefix + "API_KEY", "")
            manager_path = os.getenv(prefix + "PRIVATE_KEY_PATH", "")
            if manager_id and manager_path:
                self.management_client = NovigClient(NovigSettings(environment=environment,
                    api_key=manager_id, private_key_path=manager_path))
        if self.manifest.name == "prophetx":
            review = context.config.get("account_review")
            self.client.account_review = ProphetXAccountReview.model_validate(review) if review else None
        try:
            await self.client.connect()
        except Exception:
            if self.client.health_check()["state"] in {
                "credentials_missing", "unauthorized", "access_denied", "geolocation_required",
                "session_limit", "auth_outcome_unknown", "auth_refresh_failed",
            }:
                return
            raise
        if self.manifest.name == "prophetx" and context.config.get("discovery_tournament_ids"):
            await self._refresh_discovery()
        if self.manifest.name == "novig" and context.config.get("discovery_leagues"):
            await self._refresh_novig_discovery()
        if self.manifest.name == "kalshi" and context.config.get("discovery_series"):
            await self._refresh_kalshi_discovery()
        if self.manifest.name == "polymarket_us" and context.config.get("discovery_leagues"):
            await self._refresh_polymarket_discovery()
        configure = getattr(self.client, "configure_subscriptions", None)
        if configure is not None:
            if self.manifest.name == "prophetx":
                selected = list(dict.fromkeys([*context.config.get("event_ids", []), *self.discovered_event_ids]))
                props = NativeMonitor._prophetx_prop_scope(self)
                await configure({**context.config, "event_ids": selected, "prop_subtypes": props})
                self._subscribed_event_ids = selected
                self._subscribed_prop_subtypes = props
            elif self.manifest.name == "novig":
                selected = list(dict.fromkeys([*context.config.get("market_ids", []),
                                               *self.discovered_market_ids]))
                await configure({**context.config, "market_ids": selected})
                self._subscribed_market_ids = selected
            elif self.manifest.name == "polymarket_us":
                selected = list(dict.fromkeys([*context.config.get("market_slugs", []),
                                               *self.discovered_market_ids]))
                await configure({**context.config, "market_slugs": selected,
                                 "discovery_leagues": []})
                self._subscribed_market_ids = selected
            else:
                await configure(context.config)
        elif self.manifest.name == "kalshi":
            selected = list(dict.fromkeys([*context.config.get("market_ids", []),
                                           *self.discovered_market_ids]))
            if selected:
                await self.client.subscribe(selected)
            self._subscribed_market_ids = selected
        elif context.config.get("market_ids"):
            await self.client.subscribe(context.config["market_ids"])
        self.task = asyncio.create_task(self._run(), name=f"native-monitor:{self.manifest.name}")
        if self.manifest.name == "prophetx" and context.config.get("discovery_tournament_ids"):
            self.discovery_task = asyncio.create_task(self._discover_periodically(), name="prophetx-event-discovery")
        if self.manifest.name == "novig" and context.config.get("discovery_leagues"):
            self.discovery_task = asyncio.create_task(self._discover_novig_periodically(), name="novig-market-discovery")
        if self.manifest.name == "kalshi" and context.config.get("discovery_series"):
            self.discovery_task = asyncio.create_task(self._discover_kalshi_periodically(), name="kalshi-market-discovery")
        if self.manifest.name == "polymarket_us" and context.config.get("discovery_leagues"):
            self.discovery_task = asyncio.create_task(self._discover_polymarket_periodically(),
                                                      name="polymarket-us-market-discovery")

    async def _refresh_novig_discovery(self):
        from ..integrations.novig_discovery import balanced_open_market_ids

        config = self.context.config
        try:
            # Market rows omit league. Query each requested league separately
            # so a dense schedule in one sport cannot hide every other sport.
            rows = []
            for league in config["discovery_leagues"]:
                params = {"league": league, "marketType": ",".join(config["discovery_market_types"]),
                          "limit": config.get("discovery_page_size", 500)}
                league_items = []
                remaining_cursor = None
                for _ in range(3):
                    page = await self.client.discover_markets(**params)
                    if not isinstance(page, dict) or not isinstance(page.get("items"), list):
                        raise ValueError("Novig catalog page is incomplete")
                    league_items.extend(page["items"])
                    remaining_cursor = page.get("next")
                    if not remaining_cursor:
                        break
                    params["after"] = remaining_cursor
                rows.append({"items": league_items, "next": remaining_cursor})
            selected, truncated = balanced_open_market_ids(rows, now=utcnow(),
                max_markets=config.get("max_discovered_markets", 50))
            self.research_catalog_rows = [market for page in rows for market in page["items"]]
            self.research_catalog_at = utcnow()
            priority = await self._refresh_research_matches()
            selected = list(dict.fromkeys([*priority, *selected]))[:config.get("max_discovered_markets", 50)]
        except asyncio.CancelledError:
            raise
        except Exception:
            self.discovery_status = "read_failed"
            # An uncertain catalog must not erase the last known subscription.
            return
        self.discovered_market_ids = selected
        self.discovery_status = "truncated" if truncated else "current" if selected else "empty"

    async def _refresh_kalshi_discovery(self):
        from ..integrations.kalshi_discovery import balanced_open_markets

        config = self.context.config
        try:
            payloads = [await self.client.discover_markets(
                series_ticker=series, status="open", limit=1000, mve_filter="exclude")
                for series in config["discovery_series"]]
            selected, truncated = balanced_open_markets(payloads,
                series=config["discovery_series"], now=utcnow(),
                max_markets=config.get("max_discovered_markets", 8))
            self.research_catalog_rows = [market for page in payloads for market in page["markets"]]
            self.research_catalog_at = utcnow()
            priority = await self._refresh_research_matches()
            selected = list(dict.fromkeys([*priority, *selected]))[:config.get("max_discovered_markets", 8)]
        except asyncio.CancelledError:
            raise
        except Exception:
            self.discovery_status = "read_failed"
            return
        self.discovered_market_ids = selected
        self.discovery_status = "truncated" if truncated else "current" if selected else "empty"

    async def _refresh_research_matches(self):
        """Use already-fetched catalogs; only exact candidate metadata adds reads."""
        from ..core.native_research import (NOVIG_PROP_TYPES, kalshi_moneyline_matches, kalshi_mlb_matches,
            novig_moneyline_matches, novig_prop_matches)
        if self.manifest.name not in {"kalshi", "novig"} or self.research_catalog_at is None:
            return []
        markets = [self.engine.markets[market_id] for market_id in self.engine.reference_estimates
                   if market_id in self.engine.markets]
        source_ids = frozenset(market.id for market in markets)
        if self.manifest.name == "kalshi":
            # A short-lived fair set should not spend its overlap window
            # waiting for the exact native threshold's first book snapshot.
            # Structural prewarming grants no source eligibility or mapping.
            prewarm = sorted((market for market in self.engine.markets.values()
                if market.id not in source_ids and market.extensions.get("research_only")
                and market.league == "MLB" and market.market_type in {"moneyline", "total"}
                and market.status == "scheduled" and market.start_time > self.research_catalog_at),
                key=lambda market: (market.start_time, market.id))
            markets.extend(prewarm[:self.context.config.get("max_discovered_markets", 8)])
        if self.manifest.name == "novig":
            # Market list rows expose codes and start but not the two full team
            # names. Fetch only a bounded set of plausible events and require
            # the authoritative event description before matching.
            candidate_ids = []
            for row in self.research_catalog_rows:
                if row.get("status") != "OPEN":
                    continue
                for market in markets:
                    if market.market_type not in {"moneyline", "player_prop"} or market.league not in self.context.config.get("discovery_leagues", []):
                        continue
                    expected_type = ("MONEY" if market.market_type == "moneyline"
                                     else NOVIG_PROP_TYPES.get(market.statistic))
                    if row.get("marketType") != expected_type:
                        continue
                    start = row.get("startsTs")
                    if type(start) is int and abs(start - int(market.start_time.timestamp()*1000)) <= 120000:
                        candidate_ids.append(row.get("eventId"))
                        break
            for event_id in list(dict.fromkeys(candidate_ids))[:32]:
                if not isinstance(event_id, str) or event_id in self.research_events:
                    continue
                try:
                    response = await self.client.request("GET", f"/v3/catalog/events/{event_id}")
                    if isinstance(response, dict) and response.get("eventId") == event_id:
                        self.research_events[event_id] = response
                except Exception:
                    continue
        result, priority = {}, []
        for market in markets:
            matches = ((kalshi_moneyline_matches(market, self.research_catalog_rows,
                        observed_at=self.research_catalog_at) if market.league == "NBA" else
                        kalshi_mlb_matches(market, self.research_catalog_rows,
                        observed_at=self.research_catalog_at)) if self.manifest.name == "kalshi" else
                       (novig_moneyline_matches(market, self.research_catalog_rows,
                        self.research_events, observed_at=self.research_catalog_at)
                        if market.market_type == "moneyline" else
                        novig_prop_matches(market, self.research_catalog_rows,
                            self.research_events, observed_at=self.research_catalog_at)))
            if matches:
                result[market.id] = matches
                priority.extend(item.native_market_id for item in matches)
        self.research_matches = result
        self._last_research_source_ids = source_ids
        if self.manifest.name == "kalshi":
            events = list(dict.fromkeys(item.native_event_id for market_id, matches in result.items()
                if market_id in source_ids for item in matches))[:8]
            for event_id in events:
                if event_id in self.research_fee_metadata:
                    continue
                series = event_id.split("-", 1)[0]
                try:
                    self.research_fee_metadata[event_id] = {
                        "series": await self.client.request("GET", f"/series/{series}", authenticated=False),
                        "event": await self.client.request("GET", f"/events/{event_id}", authenticated=False),
                        "series_changes": await self.client.request("GET", "/series/fee_changes",
                            params={"series_ticker": series}, authenticated=False),
                        "event_changes": await self.client.request("GET", "/events/fee_changes",
                            params={"event_ticker": event_id}, authenticated=False),
                        "observed_at": utcnow(),
                    }
                except Exception:
                    self.research_fee_metadata.pop(event_id, None)
        return list(dict.fromkeys(priority))

    def _research_match_refresh_due(self, elapsed: int) -> bool:
        engine = getattr(self, "engine", None)
        return (elapsed % 30 == 0 or engine is not None and
                frozenset(engine.reference_estimates) != self._last_research_source_ids)

    async def _discover_kalshi_periodically(self):
        try:
            elapsed = 0
            interval = self.context.config.get("discovery_interval_seconds", 300)
            while True:
                # Matching uses the cached catalog. A fresh source market need
                # not wait a full 30 seconds before a native subscription is
                # considered; this does not extend source or book freshness.
                step = min(5, interval - elapsed)
                await asyncio.sleep(step)
                elapsed += step
                if elapsed >= interval:
                    await self._refresh_kalshi_discovery()
                    elapsed = 0
                elif NativeMonitor._research_match_refresh_due(self, elapsed):
                    await self._refresh_research_matches()
                if self.discovery_status == "read_failed":
                    continue
                research = [item.native_market_id for matches in self.research_matches.values() for item in matches]
                selected = list(dict.fromkeys([*self.context.config.get("market_ids", []),
                    *research, *self.discovered_market_ids]))[:len(self.context.config.get("market_ids", []))
                        + self.context.config.get("max_discovered_markets", 8)]
                if selected == self._subscribed_market_ids:
                    continue
                try:
                    await self.client.replace_monitor_markets(selected)
                except Exception:
                    self.discovery_status = "subscription_failed"
                    continue
                self._subscribed_market_ids = selected
        except asyncio.CancelledError:
            raise

    async def _discover_novig_periodically(self):
        try:
            elapsed = 0
            interval = self.context.config.get("discovery_interval_seconds", 300)
            while True:
                step = min(5, interval - elapsed)
                await asyncio.sleep(step)
                elapsed += step
                if elapsed >= interval:
                    await self._refresh_novig_discovery()
                    elapsed = 0
                elif NativeMonitor._research_match_refresh_due(self, elapsed):
                    await self._refresh_research_matches()
                if self.discovery_status == "read_failed":
                    continue
                research = [item.native_market_id for matches in self.research_matches.values() for item in matches]
                selected = list(dict.fromkeys([*self.context.config.get("market_ids", []),
                    *research, *self.discovered_market_ids]))[:len(self.context.config.get("market_ids", []))
                        + self.context.config.get("max_discovered_markets", 50)]
                if selected == self._subscribed_market_ids:
                    continue
                try:
                    # Closing the existing socket makes the normal stream loop
                    # invalidate books and private state, then resubscribe with
                    # a fresh authoritative snapshot. No order is transmitted.
                    await self.client.replace_monitor_markets(selected)
                except Exception:
                    self.discovery_status = "subscription_failed"
                    continue
                self._subscribed_market_ids = selected
        except asyncio.CancelledError:
            raise

    async def _refresh_polymarket_discovery(self):
        from ..integrations.polymarket_us_discovery import balanced_open_moneylines

        config = self.context.config
        try:
            pages = [await self.client.discover_league_events(league)
                     for league in config["discovery_leagues"]]
            selected, truncated = balanced_open_moneylines(pages,
                leagues=config["discovery_leagues"], now=utcnow(),
                max_markets=config.get("max_discovered_markets", 8))
        except asyncio.CancelledError:
            raise
        except Exception:
            self.discovery_status = "read_failed"
            return
        self.discovered_market_ids = selected
        self.discovery_status = "truncated" if truncated else "current" if selected else "empty"

    async def _discover_polymarket_periodically(self):
        from ..venues.polymarket_us import PolymarketUSMonitorConfig

        try:
            while True:
                await asyncio.sleep(self.context.config.get("discovery_interval_seconds", 300))
                await self._refresh_polymarket_discovery()
                if self.discovery_status == "read_failed":
                    continue
                selected = list(dict.fromkeys([*self.context.config.get("market_slugs", []),
                                               *self.discovered_market_ids]))
                if selected == self._subscribed_market_ids:
                    continue
                replacement = {**self.context.config, "market_slugs": selected,
                               "discovery_leagues": []}
                try:
                    PolymarketUSMonitorConfig.model_validate(replacement)
                    # The retail market stream has no documented unsubscribe.
                    # Restart both read-only sockets and require fresh baselines.
                    if self.task:
                        self.task.cancel()
                        await asyncio.gather(self.task, return_exceptions=True)
                    await self.client.configure_subscriptions(replacement)
                    self.task = asyncio.create_task(self._run(), name="native-monitor:polymarket_us")
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.discovery_status = "subscription_failed"
                    # A failed rotation must not leave monitoring silently off.
                    try:
                        await self.client.configure_subscriptions({**self.context.config,
                            "market_slugs": self._subscribed_market_ids, "discovery_leagues": []})
                        self.task = asyncio.create_task(self._run(), name="native-monitor:polymarket_us")
                    except Exception:
                        self.task = None
                    continue
                self._subscribed_market_ids = selected
        except asyncio.CancelledError:
            raise

    async def _refresh_discovery(self):
        from ..integrations.prophetx_discovery import open_event_ids
        try:
            payloads = [await self.client.discover_events(tournament_id=tournament_id)
                        for tournament_id in self.context.config.get("discovery_tournament_ids", [])]
            selected = open_event_ids(payloads, now=utcnow(),
                max_events=self.context.config.get("max_discovered_events", 20))
            market_payloads = [await self.client.discover_markets(selected[start:start+10])
                               for start in range(0, len(selected), 10)]
            props = {}
            remaining = self.context.config.get("max_discovered_prop_subscriptions", 3)
            for event_id in selected:
                for payload in market_payloads:
                    rows = payload.get("data", {}).get(event_id, [])
                    for row in rows:
                        if not isinstance(row, dict):
                            continue
                        subtype = row.get("sub_type")
                        selections = row.get("selections")
                        if (remaining and row.get("status") == "active" and isinstance(subtype, str)
                                and subtype.startswith("player_") and isinstance(selections, list)
                                and selections and any(isinstance(group, list) and group for group in selections)
                                and subtype not in props.get(event_id, [])):
                            props.setdefault(event_id, []).append(subtype)
                            remaining -= 1
                        if not remaining:
                            break
                    if not remaining:
                        break
                if not remaining:
                    break
            if hasattr(self.client, "public_market_data"):
                self.client.public_market_data.replace_catalog(market_payloads, selected, observed_at=utcnow())
        except asyncio.CancelledError:
            raise
        except Exception:
            self.discovery_status = "read_failed"
            # Retain the last validated subscription set until an explicit
            # future catalog proves a replacement; stream invalidation is
            # handled independently by the native client.
            return
        self.discovery_status = "current"
        self.discovered_event_ids = selected
        self.discovered_prop_subtypes = props

    def _prophetx_prop_scope(self) -> dict[str, list[str]]:
        props = {event_id: list(subtypes) for event_id, subtypes in self.context.config.get("prop_subtypes", {}).items()}
        for event_id, subtypes in getattr(self, "discovered_prop_subtypes", {}).items():
            props[event_id] = list(dict.fromkeys([*props.get(event_id, []), *subtypes]))
        return props

    async def _discover_periodically(self):
        try:
            while True:
                await asyncio.sleep(self.context.config.get("discovery_interval_seconds", 300))
                await self._refresh_discovery()
                if self.discovery_status != "current":
                    continue
                selected = list(dict.fromkeys([*self.context.config.get("event_ids", []),
                                               *self.discovered_event_ids]))
                props = NativeMonitor._prophetx_prop_scope(self)
                if selected == self._subscribed_event_ids and props == getattr(self, "_subscribed_prop_subtypes", {}):
                    continue
                try:
                    await self.client.subscribe(selected, props)
                except Exception:
                    self.discovery_status = "subscription_failed"
                    continue
                self._subscribed_event_ids = selected
                self._subscribed_prop_subtypes = props
        except asyncio.CancelledError:
            raise

    async def _run(self):
        try:
            async for frame in self.client.stream_updates():
                if (not self.manifest.capabilities.get("monitor_only")
                        or self.manifest.capabilities.get("native_event_forwarding")):
                    await self.engine.native.on_frame(self.manifest.name, self.client, frame)
                if (self.manifest.name == "prophetx" and frame.get("type") == "snapshot_required"
                        and self.client.public_market_data.catalog_at is None
                        and self.context.config.get("discovery_tournament_ids")):
                    # Reconnect clears old depth. Refresh exact REST identity and
                    # status once, then wait for new scoped v2 selection messages.
                    await self._refresh_discovery()
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
        finally:
            if (not self.manifest.capabilities.get("monitor_only")
                    or self.manifest.capabilities.get("native_event_forwarding")):
                await self.engine.native.on_frame(self.manifest.name, self.client, {"type": "transport_invalidated"})

    async def stop(self):
        if self.discovery_task:
            self.discovery_task.cancel()
            await asyncio.gather(self.discovery_task, return_exceptions=True)
            self.discovery_task = None
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        if self.client:
            await self.client.disconnect()
            self.client = None
        if self.management_client:
            await self.management_client.disconnect()
            self.management_client = None

    async def health_check(self):
        if self.config_error:
            return {"status": "blocked", "state": "configuration_required", "message": self.config_error,
                    "authenticated": False, "streaming": False}
        if not self.client:
            return {"status": "blocked", "message": "Native credentials missing; trading remains gated"}
        state = self.client.health_check()
        blocked = state["state"] in {"credentials_missing", "unauthorized", "access_denied", "geolocation_required", "subscription_rejected",
                                     "session_limit", "auth_outcome_unknown", "auth_refresh_failed"}
        status = "blocked" if blocked else "error" if self.task and self.task.done() else state["state"]
        return {**state, "status": status,
                "message": state.get("reason") or "Native monitor; normalized venue mapping and live preflight required for orders",
                "frames": self.frames,
                "discovery_status": self.discovery_status,
                "discovered_event_count": len(self.discovered_event_ids),
                "discovered_market_count": (len(self.client.public_market_data.catalog)
                    if self.manifest.name == "prophetx" else len(self.discovered_market_ids)),
                **({"discovered_prop_subscription_count": sum(map(len, self.discovered_prop_subtypes.values()))}
                   if self.manifest.name == "prophetx" else {}),
                **({"discovered_market_ids": list(self.discovered_market_ids),
                    "subscribed_market_ids": list(self._subscribed_market_ids),
                    "research_matched_market_ids": sorted({item.native_market_id
                        for matches in self.research_matches.values() for item in matches})}
                   if self.manifest.name == "kalshi" else {})}


def register_external(registry, engine):
    from ..integrations import PROVIDER_FACTORIES
    from ..venues import KalshiClient, KalshiSettings, NovigClient, NovigSettings, ProphetXClient, ProphetXSettings
    from ..venues.polymarket_us import PolymarketUSClient, PolymarketUSSettings, PolymarketUSMonitorConfig
    registry.config_migrations["prophetx", 1] = migrate_prophetx_monitor_config
    for name, factory in PROVIDER_FACTORIES.items():
        registry.register(ProviderBridge(factory, engine))
        registry.records[name].credential_status = "configured_unverified" if os.getenv(factory.credential_env) else "missing"
    for factory, settings_factory, credentials, config_model in [
        (KalshiClient, KalshiSettings, {"api_key_id": "KALSHI_API_KEY_ID", "private_key_path": "KALSHI_PRIVATE_KEY_PATH"}, KalshiMonitorConfig),
        (NovigClient, NovigSettings, {"api_key": "NOVIG_API_KEY", "private_key_path": "NOVIG_PRIVATE_KEY_PATH"}, NovigMonitorConfig),
        (ProphetXClient, ProphetXSettings, {"api_key": "PROPHETX_API_KEY", "secret_key": "PROPHETX_SECRET_KEY"}, ProphetXMonitorConfig),
        (PolymarketUSClient, PolymarketUSSettings, {"api_key_id": "POLYMARKET_US_API_KEY_ID", "secret_key": "POLYMARKET_US_SECRET_KEY"}, PolymarketUSMonitorConfig),
    ]:
        environments = ({
            "sandbox": {"api_key": "NOVIG_PAPER_TRADING_API_KEY", "private_key_path": "NOVIG_PAPER_TRADING_PRIVATE_KEY_PATH"},
            "production": {"api_key": "NOVIG_PRODUCTION_TRADING_API_KEY", "private_key_path": "NOVIG_PRODUCTION_TRADING_PRIVATE_KEY_PATH"},
        } if factory is NovigClient else {
            "sandbox": {"api_key_id": "KALSHI_SANDBOX_API_KEY_ID", "private_key_path": "KALSHI_SANDBOX_PRIVATE_KEY_PATH"},
            "production": {"api_key_id": "KALSHI_API_KEY_ID", "private_key_path": "KALSHI_PRIVATE_KEY_PATH"},
        } if factory is KalshiClient else {})
        plugin = NativeMonitor(factory, settings_factory, credentials, engine, config_model=config_model,
                               credential_environments=environments)
        registry.register(plugin)
        registry.records[plugin.manifest.name].credential_status = "configured_unverified" if all(os.getenv(v) for v in credentials.values()) else "missing"

