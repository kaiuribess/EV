"""Metadata and lifecycle bridge. Provider protocols never enter pricing or dashboard code."""
import asyncio
import os
import time
from dataclasses import replace
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
        self.focused_task = None
        self.mapper = None
        self._map_lock = asyncio.Lock()
        self._focused_status: dict = {"state": "disabled"}

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
            if self.manifest.name == "parlay_rest" and self.client.config.focused_novig_nfl_props:
                self.focused_task = asyncio.create_task(self._run_focused_novig_props(),
                                                        name="parlay-focused-novig-nfl-props")

    async def _publish_mapped(self, raw):
        async with self._map_lock:
            mapped = self.mapper(raw)
            for index, (market, quote) in enumerate([*mapped, *getattr(self.mapper, "research_updates", ())]):
                await self.context.bus.publish(Envelope(schema_name="sports.market", source=self.manifest.name, payload=market))
                await self.context.bus.publish(Envelope(schema_name="odds.quote", source=self.manifest.name, payload=quote))
                if index % 64 == 63:
                    await asyncio.sleep(0)

    async def _run_focused_novig_props(self):
        from ..integrations.parlay_focused_props import FocusedCreditWindow

        interval = self.client.config.focused_props_interval_seconds
        self._focused_budget = FocusedCreditWindow(
            self.client.config.focused_props_hourly_credit_budget, interval)
        while True:
            await self._focused_novig_props_session()
            await asyncio.sleep(interval)

    async def _focused_novig_props_session(self):
        from ..integrations.parlay_focused_props import exact_parlay_event_id, needs_focused_references
        from ..integrations.base import ProviderError, QuotaExceeded

        interval = self.client.config.focused_props_interval_seconds
        budget = self._focused_budget
        events, events_at, cursor = [], 0.0, 0
        await asyncio.sleep(35)  # let the existing catalog and source streams seed identities
        try:
            while True:
                monitor_record = self.engine.registry.records.get("novig")
                monitor = monitor_record.plugin if monitor_record and monitor_record.enabled else None
                targets = ([self.engine.markets[mid] for mid in monitor.focused_prop_source_ids
                            if mid in self.engine.markets]
                           if monitor is not None else [])
                if not targets:
                    self._focused_status = {"state": "no_exact_native_prop_lead"}
                else:
                    now = time.monotonic()
                    if now - events_at >= 300:
                        discovery = await self.client.discover_events("americanfootball_nfl")
                        events = discovery.payload if isinstance(discovery.payload, list) else []
                        events_at = now
                    self._focused_status = {"state": "no_current_native_depth", "native_leads": len(targets)}
                    for market in targets[cursor % len(targets):] + targets[:cursor % len(targets)]:
                        matches = monitor.focused_prop_matches.get(market.id, ())
                        if not any(monitor.research_book_current(item.native_market_id)
                                   for item in matches):
                            continue
                        # A current /events ID may differ from the cached broad
                        # prop event ID. Once its exact new market has two usable
                        # books, do not keep buying the old scope's same rows.
                        complete_elsewhere = any(
                            other.participants == market.participants
                            and other.start_time == market.start_time
                            and other.player == market.player
                            and other.statistic == market.statistic
                            and other.line == market.line
                            and not needs_focused_references(
                                self.engine.quotes.get(other.id, {}).values(), now=utcnow(),
                                max_indicator_age_ms=self.engine.settings.pricing.trusted_max_indicator_age_ms)
                            for other in self.engine.markets.values())
                        if complete_elsewhere:
                            self._focused_status = {"state": "already_complete_current_pair"}
                            continue
                        event_id = exact_parlay_event_id(market, events)
                        if event_id is None:
                            self._focused_status = {"state": "current_parlay_event_identity_unresolved"}
                            continue
                        event = next(row for row in events if isinstance(row, dict)
                                     and row.get("id") == event_id)
                        key = market.extensions.get("source_market_key")
                        if key not in {"player_receptions", "player_receiving_yards", "player_reception_yds"}:
                            continue
                        scope = (event_id, market.player, key, str(market.line))
                        available = budget.available(now, scope)
                        if available < 3:
                            self._focused_status = {"state": "focused_credit_or_dedup_limit"}
                            continue
                        books = [book for book in ("fanduel", "draftkings", "caesars", "bet365")
                                 if book in self.client.config.bookmakers]
                        if len(books) < 2:
                            self._focused_status = {"state": "fewer_than_two_configured_books"}
                            continue
                        before = self.client.quota.billable_requests
                        try:
                            frames, detail = await self.client.fetch_focused_props(
                                event_id=event_id, player=market.player, market=key,
                                line=str(market.line), bookmakers=books,
                                limit=self.client.config.focused_props_limit,
                                max_pages=min(2, available // 3))
                        finally:
                            budget.charge(now, scope, self.client.quota.billable_requests - before)
                        for frame in frames:
                            await self._publish_mapped(replace(frame, metadata={**frame.metadata,
                                "focused_event": {"id": event_id,
                                    "home_team": event["home_team"],
                                    "away_team": event["away_team"],
                                    "commence_time": event["commence_time"]}}))
                        self._focused_status = {"state": detail["reason"],
                            "native_leads": len(targets), "returned_rows": detail.get("returned_rows", 0),
                            "pages": detail.get("pages", 0),
                            "hourly_credits_reserved": sum(cost for _, cost in budget.charges)}
                        cursor += 1
                        break
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise
        except (ProviderError, QuotaExceeded, ValueError) as exc:
            self._focused_status = {"state": "provider_or_scope_error", "error_type": type(exc).__name__}
            await self.engine.notice("warning", f"Focused Parlay props stopped: {type(exc).__name__}",
                                     self.manifest.name)

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
        oddize_research = self.engine.registry.records.get("oddize_research_input")
        if (self.manifest.name == "oddize_rest" and
                ((oddize_paper and oddize_paper.enabled) or
                 (oddize_research and oddize_research.enabled))):
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
                oddize_research = self.engine.registry.records.get("oddize_research_input")
                if (self.manifest.name == "parlay_game_ws" and
                        ((paper and paper.enabled) or
                         (oddize_research and oddize_research.enabled and
                          raw.kind in {"initial_state", "feed_invalidated", "disconnected", "reconnect"}))):
                    await self.context.bus.publish(Envelope(schema_name="provider.frame",
                        source=self.manifest.name, payload=ProviderFrame(
                            provider_id=raw.provider_id, kind=raw.kind, payload=raw.payload,
                            received_at=raw.received_at, published_at=raw.published_at,
                            snapshot_complete=raw.snapshot_complete, metadata=raw.metadata)))
                oddize_paper = self.engine.registry.records.get("oddize_paper_input")
                oddize_research = self.engine.registry.records.get("oddize_research_input")
                if (self.manifest.name == "oddize_rest"
                        and ((oddize_paper and oddize_paper.enabled)
                             or (oddize_research and oddize_research.enabled))
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
                await self._publish_mapped(raw)
                self.engine.metrics.counters[f"provider_messages:{self.manifest.name}"] += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self.engine.notice("error", f"Provider stopped: {type(exc).__name__}", self.manifest.name)
        finally:
            await self._invalidate()

    async def stop(self):
        if self.focused_task:
            self.focused_task.cancel()
            await asyncio.gather(self.focused_task, return_exceptions=True)
            self.focused_task = None
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
                "focused_novig_nfl_props": dict(self._focused_status),
                "mapped_quotes": self.mapper.mapped_count if self.mapper else 0,
                "quarantined": len(self.mapper.quarantine) if self.mapper else 0}

    def health_snapshot(self):
        """Expose live focused-request usage without polling the paid provider."""
        health = dict(self.engine.registry.records[self.manifest.name].health)
        if self.client:
            health["quota"] = self.client.quota.stats()
            health["focused_novig_nfl_props"] = dict(self._focused_status)
            health["mapped_quotes"] = self.mapper.mapped_count if self.mapper else 0
            health["quarantined"] = len(self.mapper.quarantine) if self.mapper else 0
            if self.focused_task and self.focused_task.done():
                error = self.focused_task.exception() if not self.focused_task.cancelled() else None
                health["focused_novig_nfl_props"] = {
                    **health["focused_novig_nfl_props"], "state": "task_stopped",
                    "error_type": type(error).__name__ if error else None}
        return health


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
    discovery_leagues: list[Literal["NFL", "NBA", "MLB", "NHL"]] = Field(default_factory=list, max_length=4,
        description="Exact active tournament names to discover through the Trading/MM catalog")
    discovery_lookahead_hours: int = Field(default=30, ge=30, le=168,
        description="Upcoming event subscription horizon; does not relax quote freshness or identity matching")
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

    @field_validator("discovery_leagues")
    @classmethod
    def unique_discovery_leagues(cls, values):
        if len(set(values)) != len(values):
            raise ValueError("ProphetX discovery leagues must be unique")
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
        self.recovery_task = None
        self._discovery_lock = asyncio.Lock()
        self.frames = 0
        self.config_error = ""
        self.discovery_status = "disabled"
        self.discovery_error_type = ""
        self.discovered_event_ids: list[str] = []
        self.discovered_market_ids: list[str] = []
        self._subscribed_event_ids: list[str] = []
        self._subscribed_prop_subtypes: dict[str, list[str]] = {}
        self.discovered_prop_subtypes: dict[str, list[str]] = {}
        self.discovered_tournaments: dict[str, int] = {}
        self._subscribed_market_ids: list[str] = []
        # Catalog evidence is read-only. It is never fed to NativeBookMapper or
        # used as an executable mapping without a separate exact review.
        self.research_catalog_rows: list[dict] = []
        self.research_catalog_at = None
        self.research_matches: dict[str, list] = {}
        self.focused_prop_matches: dict[str, list] = {}
        self.focused_prop_source_ids: list[str] = []
        self.research_events: dict[str, dict] = {}
        self.research_fee_metadata: dict[str, dict] = {}
        self._last_research_source_ids = frozenset()
        self._market_data_updates = 0
        self._last_market_data_at = None
        self._last_valid_book_at = None
        self._book_at: dict[str, object] = {}
        self._reconnect_count = 0
        self._seen_initial_snapshot = False

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
        self.discovery_error_type = ""
        self.discovered_event_ids = []
        self.discovered_market_ids = []
        self._subscribed_event_ids = []
        self._subscribed_prop_subtypes = {}
        self.discovered_prop_subtypes = {}
        self.discovered_tournaments = {}
        self._subscribed_market_ids = []
        self.research_catalog_rows = []
        self.research_catalog_at = None
        self.research_matches = {}
        self.focused_prop_matches = {}
        self.focused_prop_source_ids = []
        self.research_events = {}
        self.research_fee_metadata = {}
        self._last_research_source_ids = frozenset()
        self._market_data_updates = 0
        self._last_market_data_at = None
        self._last_valid_book_at = None
        self._book_at = {}
        self._reconnect_count = 0
        self._seen_initial_snapshot = False
        if context.config.get("legacy_market_ids"):
            self.config_error = "Confirm the migrated ProphetX IDs in event_ids or prop_subtypes, then clear legacy_market_ids"
            return
        credentials = self.credential_names(context.config)
        has_credentials = all(os.getenv(env) for env in credentials.values())
        public_polymarket = (self.manifest.name == "polymarket_us"
                             and bool(context.config.get("discovery_leagues")))
        if not has_credentials and not public_polymarket:
            self.config_error = "Complete the selected backend credential pair; credentials from another profile are not substituted"
            return
        self.client = self.factory(self.settings_factory(
            environment=context.config.get("environment", "sandbox"),
            **{key: os.environ[env] for key, env in credentials.items() if has_credentials}))
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
        if has_credentials:
            try:
                await self.client.connect()
            except Exception:
                if self.client.health_check()["state"] in {
                    "credentials_missing", "unauthorized", "access_denied", "geolocation_required",
                    "session_limit", "auth_outcome_unknown", "auth_refresh_failed",
                }:
                    if not public_polymarket:
                        return
                else:
                    raise
        elif public_polymarket:
            self.client.health = "public_research"
        if self.manifest.name == "prophetx" and (context.config.get("discovery_tournament_ids")
                                                 or context.config.get("discovery_leagues")):
            await self._refresh_discovery()
        if self.manifest.name == "novig" and context.config.get("discovery_leagues"):
            await self._refresh_novig_discovery()
        if self.manifest.name == "kalshi" and context.config.get("discovery_series"):
            await self._refresh_kalshi_discovery()
        if self.manifest.name == "polymarket_us" and context.config.get("discovery_leagues"):
            await self._refresh_polymarket_discovery()
        if public_polymarket and not self.client.authenticated:
            # Public SDK catalog and REST books do not require a retail key.
            # No private or market WebSocket is opened without authentication.
            self.discovery_task = asyncio.create_task(self._discover_polymarket_periodically(),
                                                      name="polymarket-us-public-discovery")
            return
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
        if self.manifest.name == "prophetx" and (context.config.get("discovery_tournament_ids")
                                                 or context.config.get("discovery_leagues")):
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
            focused = [item.native_market_id for market_id in getattr(self, "focused_prop_source_ids", ())
                       for item in getattr(self, "focused_prop_matches", {}).get(market_id, ())][:2]
            selected = list(dict.fromkeys([*focused, *priority, *selected]))[:config.get("max_discovered_markets", 50)]
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
            novig_moneyline_matches, novig_prop_matches, prophetx_matches)
        if self.manifest.name not in {"kalshi", "novig", "polymarket_us", "prophetx"} or self.research_catalog_at is None:
            return []
        research_ids = set(self.engine.reference_estimates) | set(getattr(self.engine, "consensus_estimates", {}))
        # Subscription capacity is scarce. Rank already-priced, exact source
        # markets by independent sportsbook coverage before selecting native
        # books. This changes monitoring only, never quote or order eligibility.
        def source_rank(market_id):
            row = getattr(self.engine, "consensus_estimates", {}).get(market_id) or self.engine.reference_estimates.get(market_id, {})
            books = row.get("independent_book_count", len(row.get("references", ())))
            return (-int(books >= 3), -books, self.engine.markets[market_id].start_time, market_id)

        markets = [self.engine.markets[market_id] for market_id in sorted(
            (market_id for market_id in research_ids if market_id in self.engine.markets),
            key=source_rank)]
        source_ids = frozenset(market.id for market in markets)
        focused_source_ids = frozenset()
        focus_candidates = []
        if self.manifest.name == "novig":
            from ..integrations.parlay_focused_props import needs_focused_references

            # A truncated broad REST board can contain an exact prop identity
            # even when a provisional fair estimate exists. It still needs a
            # scoped request. This never promotes the estimate to live status.
            age_limit = getattr(getattr(getattr(self.engine, "settings", None), "pricing", None),
                                "trusted_max_indicator_age_ms", 30000)
            focus_candidates = [market for market in self.engine.markets.values()
                if market.extensions.get("research_only")
                and market.league == "NFL" and market.market_type == "player_prop"
                and market.statistic in {"receptions", "receiving_yards"}
                and market.status == "scheduled" and market.start_time > self.research_catalog_at
                and len({quote.bookmaker for quote in self.engine.quotes.get(market.id, {}).values()
                         if quote.provider.startswith("parlay") and quote.status not in {"suspended", "withdrawn"}}) >= 2
                and needs_focused_references(self.engine.quotes.get(market.id, {}).values(),
                    now=self.research_catalog_at, max_indicator_age_ms=age_limit)]
            focus_candidates.sort(key=lambda market: (
                -len({q.bookmaker for q in self.engine.quotes.get(market.id, {}).values()
                      if q.provider.startswith("parlay")}), market.start_time, market.id))
            focus_candidates = focus_candidates[:32]
            focused_source_ids = frozenset(market.id for market in focus_candidates)
            markets.extend(market for market in focus_candidates if market.id not in source_ids)
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
            for market in markets:
                for row in self.research_catalog_rows:
                    if row.get("status") != "OPEN":
                        continue
                    if market.market_type not in {"moneyline", "player_prop"} or market.league not in self.context.config.get("discovery_leagues", []):
                        continue
                    expected_type = ("MONEY" if market.market_type == "moneyline"
                                     else NOVIG_PROP_TYPES.get(market.statistic))
                    if row.get("marketType") != expected_type:
                        continue
                    start = row.get("startsTs")
                    if type(start) is int and abs(start - int(market.start_time.timestamp()*1000)) <= 120000:
                        candidate_ids.append(row.get("eventId"))
            for event_id in list(dict.fromkeys(candidate_ids))[:32]:
                if not isinstance(event_id, str) or event_id in self.research_events:
                    continue
                try:
                    response = await self.client.request("GET", f"/v3/catalog/events/{event_id}")
                    if isinstance(response, dict) and response.get("eventId") == event_id:
                        self.research_events[event_id] = response
                except Exception:
                    continue
        result, focused_result, priority = {}, {}, []
        for market in markets:
            if self.manifest.name == "prophetx":
                matches = prophetx_matches(market, self.client.public_market_data.catalog,
                    self.research_events, observed_at=self.research_catalog_at)
            elif self.manifest.name == "polymarket_us":
                from ..integrations.polymarket_us_research import moneyline_matches
                matches = moneyline_matches(market, self.research_catalog_rows,
                                            observed_at=self.research_catalog_at)
            else:
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
                if self.manifest.name != "novig" or market.id in source_ids:
                    result[market.id] = matches
                    priority.extend(item.native_market_id for item in matches)
                if market.id in focused_source_ids:
                    focused_result[market.id] = matches
        self.research_matches = result
        if self.manifest.name == "novig":
            self.focused_prop_matches = focused_result
            self.focused_prop_source_ids = [market.id for market in focus_candidates
                                            if market.id in focused_result][:2]
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
                frozenset(set(engine.reference_estimates) |
                          set(getattr(engine, "consensus_estimates", {}))) != self._last_research_source_ids)

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
                research = [item.native_market_id for matches in self.research_matches.values()
                            for item in matches]
                focused = [item.native_market_id for market_id in getattr(self, "focused_prop_source_ids", ())
                           for item in getattr(self, "focused_prop_matches", {}).get(market_id, ())][:2]
                selected = list(dict.fromkeys([*self.context.config.get("market_ids", []),
                    *research, *focused, *self.discovered_market_ids]))[:len(self.context.config.get("market_ids", []))
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
                focused = [item.native_market_id for market_id in getattr(self, "focused_prop_source_ids", ())
                           for item in getattr(self, "focused_prop_matches", {}).get(market_id, ())][:2]
                selected = list(dict.fromkeys([*self.context.config.get("market_ids", []),
                    *focused, *research, *self.discovered_market_ids]))[:len(self.context.config.get("market_ids", []))
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
        from ..integrations.polymarket_us_research import catalog_record

        config = self.context.config
        try:
            pages = [await self.client.discover_league_events(league)
                     for league in config["discovery_leagues"]]
            # Match against the bounded public catalog *before* using the
            # eight-slot stream limit. Previously only the eight nearest
            # baseline slugs entered research_catalog_rows, so an exact
            # sportsbook event outside those slots could never be prioritized.
            pool, pool_truncated = balanced_open_moneylines(pages,
                leagues=config["discovery_leagues"], now=utcnow(), max_markets=50)
            baseline, truncated = balanced_open_moneylines(pages,
                leagues=config["discovery_leagues"], now=utcnow(),
                max_markets=config.get("max_discovered_markets", 8))
            catalog = []
            for slug in pool:
                found = [(league, event, market)
                         for league, league_pages in zip(config["discovery_leagues"], pages)
                         for page in league_pages for event in page["events"]
                         for market in event.get("markets", []) if market.get("slug") == slug]
                if len(found) != 1:
                    raise ValueError("Ambiguous Polymarket US event/market catalog")
                league, event, listed = found[0]
                # The official league catalog already includes sides, rules,
                # status, fee and tick metadata. An extra slug lookup for every
                # row doubled the public request burst without adding scope.
                complete = all(listed.get(field) is not None for field in (
                    "marketSides", "description", "feeCoefficient", "orderPriceMinTickSize"))
                if complete:
                    current = listed
                elif slug in baseline:
                    current = await self.client.public_market_metadata(slug)
                    if (current.get("id") != listed.get("id")
                            or current.get("gameStartTime") != listed.get("gameStartTime")):
                        raise ValueError("Polymarket US SDK market identity changed during discovery")
                else:
                    continue  # No extra broad per-slug reads for incomplete rows.
                catalog.append(catalog_record(league, event, current, observed_at=utcnow()))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.discovery_status = "read_failed"
            self.discovery_error_type = type(exc).__name__
            return
        self.research_catalog_rows = catalog
        self.research_catalog_at = utcnow()
        self.discovery_error_type = ""
        # The authenticated full stream seeds all selected books. A bounded
        # public REST sample remains useful without credentials or as recovery,
        # but exhausting its separate quota must not discard the catalog or
        # prevent the full WebSocket subscription.
        for slug in baseline[:2]:
            try:
                await self.client.fetch_snapshot(slug)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.discovery_error_type = f"rest_snapshot_{type(exc).__name__}"
                break
        priority = await self._refresh_research_matches()
        self.discovered_market_ids = list(dict.fromkeys([*priority, *baseline]))[:config.get("max_discovered_markets", 8)]
        self.discovery_status = "truncated" if truncated or pool_truncated else "current" if baseline else "empty"

    async def _discover_polymarket_periodically(self):
        from ..venues.polymarket_us import PolymarketUSMonitorConfig

        try:
            elapsed = 0
            interval = self.context.config.get("discovery_interval_seconds", 300)
            while True:
                step = min(5, interval - elapsed)
                await asyncio.sleep(step)
                elapsed += step
                if elapsed >= interval:
                    await self._refresh_polymarket_discovery()
                    elapsed = 0
                elif NativeMonitor._research_match_refresh_due(self, elapsed):
                    await self._refresh_research_matches()
                if self.discovery_status == "read_failed":
                    continue
                if not self.client.authenticated:
                    continue  # public REST research stays available without keys
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

    async def _refresh_discovery(self, *, preserve_scope=False):
        lock = getattr(self, "_discovery_lock", None)
        if lock is None:  # Lightweight offline monitor fixtures.
            return await NativeMonitor._refresh_discovery_once(self, preserve_scope=preserve_scope)
        async with lock:
            return await NativeMonitor._refresh_discovery_once(self, preserve_scope=preserve_scope)

    async def _refresh_discovery_once(self, *, preserve_scope=False):
        from ..integrations.prophetx_discovery import active_tournament_ids, open_event_ids
        from ..core.native_research import _aware
        try:
            config = self.context.config
            engine = getattr(self, "engine", None)
            source_rows = getattr(engine, "consensus_estimates", {})
            known_markets = getattr(engine, "markets", {})
            source_markets = [(known_markets[market_id], row.get("independent_book_count", 0))
                for market_id, row in source_rows.items() if market_id in known_markets]
            pinned = list(config.get("discovery_tournament_ids", []))
            leagues = config.get("discovery_leagues", [])
            discovered, catalog_error = {}, ""
            if leagues:
                try:
                    payload = await self.client.discover_sports(active_events_only=True)
                    discovered = active_tournament_ids(payload, leagues)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if not pinned:
                        raise
                    catalog_error = type(exc).__name__
            league_scores = {league: max((int(count) for market, count in source_markets
                if market.league == league), default=0) for league in leagues}
            ranked_leagues = sorted(discovered, key=lambda league: (-league_scores[league],
                                                                      leagues.index(league)))
            tournaments = list(dict.fromkeys([*pinned, *(discovered[league] for league in ranked_leagues)]))[:4]
            labels_by_tournament = {native_id: league for league, native_id in discovered.items()}
            payloads = [await self.client.discover_events(tournament_id=tournament_id)
                        for tournament_id in tournaments]
            pool = open_event_ids(payloads, now=utcnow(), max_events=50,
                                  lookahead_hours=config.get("discovery_lookahead_hours", 30))
            all_events, ambiguous_ids = {}, set()
            for tournament_id, payload in zip(tournaments, payloads):
                for row in payload.get("data", {}).get("sport_events", []):
                    if not isinstance(row, dict) or str(row.get("event_id")) not in pool:
                        continue
                    event_id = str(row["event_id"])
                    if event_id in all_events and all_events[event_id]["tournament_id"] != tournament_id:
                        ambiguous_ids.add(event_id)
                    else:
                        all_events[event_id] = {**row, "tournament_id": tournament_id,
                                                "league": labels_by_tournament.get(tournament_id)}
            for event_id in ambiguous_ids:
                all_events.pop(event_id, None)
            pool = [event_id for event_id in pool if event_id in all_events]
            def event_rank(event_id):
                event = all_events.get(event_id, {})
                start = _aware(event.get("scheduled"))
                score = max((count for market, count in source_markets
                    if start is not None and event.get("name") == market.event_name
                    and abs((start-market.start_time).total_seconds()) <= 120
                    and (event.get("league") == market.league or
                         market.league == "NBA" and event.get("tournament_id") == 132)), default=0)
                return (-int(score >= 3), -score, pool.index(event_id))
            ranked = sorted(pool, key=event_rank)
            existing = getattr(self, "_subscribed_event_ids", [])
            if preserve_scope and existing:
                # Reconnect is for repairing the already registered channels,
                # not reranking a volatile fair-price set into another socket
                # close. The periodic scheduler owns subscription rotation.
                selected = [event_id for event_id in existing if event_id in all_events]
            else:
                # Preserve one open event from each explicitly pinned tournament
                # before using the remaining bounded slots for source-rich leads.
                retained = [min((event_id for event_id in pool
                                 if all_events.get(event_id, {}).get("tournament_id") == tournament_id),
                                key=event_rank, default=None) for tournament_id in pinned]
                selected = [event_id for event_id in retained if event_id is not None]
                selected.extend(event_id for event_id in ranked if event_id not in selected)
            selected = selected[:config.get("max_discovered_events", 20)]
            events = {}
            for event_id in selected:
                if event_id in all_events:
                    events[event_id] = all_events[event_id]
            market_payloads = [await self.client.discover_markets(selected[start:start+10])
                               for start in range(0, len(selected), 10)]
            props = {}
            prop_candidates = []
            for event_id in selected:
                for payload in market_payloads:
                    rows = payload.get("data", {}).get(event_id, [])
                    for row in rows:
                        if not isinstance(row, dict):
                            continue
                        subtype = row.get("sub_type")
                        if (row.get("status") == "active" and isinstance(subtype, str)
                                and subtype.startswith("player_")
                                and (event_id, subtype) not in {(item[-2], item[-1])
                                                                 for item in prop_candidates}):
                            # Some current V4 prop rows omit selections/line.
                            # A subtype is enough to monitor a research channel,
                            # but never enough for exact native market matching.
                            event = events[event_id]
                            start = _aware(event.get("scheduled"))
                            related = [(market, int(count)) for market, count in source_markets
                                if market.market_type == "player_prop" and start is not None
                                and event.get("name") == market.event_name
                                and (event.get("league") == market.league or
                                     market.league == "NBA" and event.get("tournament_id") == 132)
                                and abs((start-market.start_time).total_seconds()) <= 120]
                            exact = max((count for market, count in related
                                if row.get("player_id") == market.player_id
                                and row.get("statistic") == market.statistic
                                and row.get("period") in {"full", "full_game", "FULL"}
                                and str(row.get("line")) == str(market.line)), default=0)
                            family = subtype.removeprefix("player_total_") if subtype.startswith(
                                "player_total_") else None
                            family_score = max((count for market, count in related
                                if market.statistic == family), default=0)
                            prop_candidates.append((-int(exact >= 3), -exact,
                                -int(family_score >= 3), -family_score, event_id, subtype))
            for *_, event_id, subtype in sorted(prop_candidates)[:config.get(
                    "max_discovered_prop_subscriptions", 3)]:
                props.setdefault(event_id, []).append(subtype)
            if hasattr(self.client, "public_market_data"):
                observed_at = utcnow()
                self.client.public_market_data.replace_catalog(market_payloads, selected, observed_at=observed_at)
                self.research_catalog_at = observed_at
                self.research_events = events
                if hasattr(self, "engine"):
                    await NativeMonitor._refresh_research_matches(self)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.discovery_status = "read_failed"
            self.discovery_error_type = (f"{type(exc).__name__}:{exc.status_code}"
                if isinstance(getattr(exc, "status_code", None), int) else type(exc).__name__)
            # Retain the last validated subscription set until an explicit
            # future catalog proves a replacement; stream invalidation is
            # handled independently by the native client.
            return
        self.discovery_status = "current"
        self.discovery_error_type = catalog_error
        self.discovered_tournaments = {league: native_id for league, native_id in discovered.items()
                                       if native_id in tournaments}
        self.discovered_event_ids = selected
        self.discovered_prop_subtypes = props

    def _prophetx_prop_scope(self) -> dict[str, list[str]]:
        props = {event_id: list(subtypes) for event_id, subtypes in self.context.config.get("prop_subtypes", {}).items()}
        for event_id, subtypes in getattr(self, "discovered_prop_subtypes", {}).items():
            props[event_id] = list(dict.fromkeys([*props.get(event_id, []), *subtypes]))
        return props

    async def _recover_prophetx_catalog(self):
        try:
            await self._refresh_discovery(preserve_scope=True)
            if self.discovery_status != "current":
                return
            selected = list(dict.fromkeys([*self.context.config.get("event_ids", []),
                                           *self.discovered_event_ids]))
            props = NativeMonitor._prophetx_prop_scope(self)
            if selected != self._subscribed_event_ids or props != self._subscribed_prop_subtypes:
                # A changed declarative scope needs a fresh registration. A
                # matching scope keeps the socket alive while REST recovers.
                await self.client.subscribe(selected, props)
                self._subscribed_event_ids = selected
                self._subscribed_prop_subtypes = props
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.discovery_status = "subscription_failed"
            self.discovery_error_type = type(exc).__name__

    async def _discover_periodically(self):
        try:
            elapsed = 0
            interval = self.context.config.get("discovery_interval_seconds", 300)
            while True:
                step = min(5, interval - elapsed)
                await asyncio.sleep(step)
                elapsed += step
                if elapsed < interval:
                    if NativeMonitor._research_match_refresh_due(self, elapsed):
                        await self._refresh_research_matches()
                    continue
                await self._refresh_discovery()
                elapsed = 0
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
                        and (self.context.config.get("discovery_tournament_ids")
                             or self.context.config.get("discovery_leagues"))):
                    # Reconnect clears old depth. A multi-league REST refresh
                    # must not block Talaria sign-in and channel confirmation.
                    # catalog_at=None keeps all depth non-executable meanwhile.
                    task = getattr(self, "recovery_task", None)
                    if task is None or task.done():
                        self.recovery_task = asyncio.create_task(
                            NativeMonitor._recover_prophetx_catalog(self),
                            name="prophetx-catalog-recovery")
                # Account payloads stay backend-only. Mapping into tradable canonical books
                # is explicitly separate and requires verified settlement, units, and fees.
                if frame.get("type") == "transport_invalidated":
                    self._reconnect_count += 1
                    self._book_at.clear()
                    self.engine.metrics.counters[f"native_invalidations:{self.manifest.name}"] += 1
                    continue
                if self.manifest.name == "prophetx" and frame.get("type") == "snapshot_required":
                    if getattr(self, "_seen_initial_snapshot", False):
                        self._reconnect_count += 1
                    self._seen_initial_snapshot = True
                if hasattr(self, "_book_at"):
                    NativeMonitor._observe_market_frame(self, frame)
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
        if self.recovery_task:
            self.recovery_task.cancel()
            await asyncio.gather(self.recovery_task, return_exceptions=True)
            self.recovery_task = None
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

    def _observe_market_frame(self, frame: dict):
        """Count real book payloads, never protocol heartbeats or private data."""
        subjects = []
        if self.manifest.name == "kalshi" and frame.get("type") in {"orderbook_snapshot", "orderbook_delta"}:
            subjects = [frame.get("msg", {}).get("market_ticker")]
        elif self.manifest.name == "novig":
            batch = frame.get("snapshot", frame.get("delta", {}))
            if isinstance(batch, dict):
                subjects = [key for key, channels in batch.items()
                    if isinstance(channels, dict) and "book" in channels]
        elif self.manifest.name == "prophetx" and frame.get("event") == "v2.market_selections":
            data = frame.get("data", {})
            subjects = [f"{data.get('sport_event_id')}:{data.get('market_id')}"]
        elif (self.manifest.name == "polymarket_us" and frame.get("type") == "polymarket_us_monitor"
                and frame.get("channel") == "markets" and frame.get("kind") == "data"):
            payload = frame.get("payload", {})
            data = payload.get("marketData", payload.get("market_data", {}))
            subjects = [data.get("marketSlug", data.get("market_slug"))] if isinstance(data, dict) else []
        if subjects:
            observed = utcnow()
            self._market_data_updates += 1
            self._last_market_data_at = observed
            for subject in subjects:
                if isinstance(subject, str) and subject:
                    self._book_at[subject] = observed
            if any(subject in self.health_snapshot().get("valid_book_ids", ()) for subject in subjects):
                self._last_valid_book_at = observed

    def research_book_current(self, market_id: str, *, now=None) -> bool:
        """A market-specific book message must still be recent and connected."""
        stamp = self._book_at.get(market_id)
        if stamp is None or self.client is None or self.client.health_check().get("streaming") is not True:
            return False
        age = ((now or utcnow())-stamp).total_seconds()
        return 0 <= age <= 30

    def health_snapshot(self):
        if self.config_error:
            return {"status": "blocked", "state": "configuration_required", "message": self.config_error,
                    "authenticated": False, "streaming": False}
        if not self.client:
            return {"status": "blocked", "message": "Native credentials missing; trading remains gated"}
        state = self.client.health_check()
        blocked = state["state"] in {"credentials_missing", "unauthorized", "access_denied", "geolocation_required", "subscription_rejected",
                                     "session_limit", "auth_outcome_unknown", "auth_refresh_failed"}
        status = "blocked" if blocked else "error" if self.task and self.task.done() else state["state"]
        now = utcnow()
        connected = state.get("streaming") is True
        ages = []
        if self.manifest.name == "prophetx":
            view = self.client.public_market_data.snapshot(now=now, connected=connected, limit=10000)
            current = {f"{row['event_id']}:{row['market_id']}" for row in view["levels"]
                if row["stream_current"] and row["executable_stake_usd"] != "0"}
            ages = [row["local_receipt_age_seconds"] for row in view["levels"] if row["stream_current"]]
        elif self.manifest.name == "polymarket_us" and hasattr(self.client, "rest_books"):
            from ..integrations.polymarket_us_research import current_book
            current = set()
            for row in self.research_catalog_rows:
                if not isinstance(row, dict) or not isinstance(row.get("slug"), str):
                    continue
                book = current_book(row, self.client, now=now,
                    stream_connected=state.get("market_streaming") is True)
                if book.get("current") and (book["bids"] or book["offers"]):
                    current.add(row["slug"])
                    ages.append(book["source_age_seconds"])
        elif self.manifest.name in {"kalshi", "novig"} and hasattr(self.client, "book"):
            current = set()
            book = self.client.book
            for market_id, stamp in getattr(self, "_book_at", {}).items():
                age = (now-stamp).total_seconds()
                if not connected or not 0 <= age <= 30 or market_id not in book.books:
                    continue
                if self.manifest.name == "kalshi":
                    valid = market_id not in book.invalid and any(book.books[market_id].values())
                else:
                    valid = (book.statuses.get(market_id) == "OPEN"
                        and f"{market_id}:book" not in book.sequences.invalid
                        and bool(book.books[market_id]))
                if valid:
                    current.add(market_id)
                    ages.append(age)
        else:
            current = set()
        subscription_count = (len(self._subscribed_event_ids) + sum(map(len, self._subscribed_prop_subtypes.values()))
            if self.manifest.name == "prophetx" else len(getattr(self, "_subscribed_market_ids", []))
            if self.manifest.name in {"kalshi", "novig", "polymarket_us"} else 0)
        last_error = (state.get("last_error") or state.get("error") or state.get("last_auth_failure")
            or getattr(self, "discovery_error_type", "") or None)
        if last_error is None and status in {"blocked", "error"}:
            last_error = state.get("reason") or None
        return {**state, "status": status,
                "message": state.get("reason") or "Native monitor; normalized venue mapping and live preflight required for orders",
                "frames": self.frames,
                 "market_data_updates": getattr(self, "_market_data_updates", 0),
                 "last_market_data_at": self._last_market_data_at.isoformat() if getattr(self, "_last_market_data_at", None) else None,
                 "last_valid_full_book_at": self._last_valid_book_at.isoformat() if getattr(self, "_last_valid_book_at", None) else None,
                 "valid_book_count": len(current), "valid_book_ids": sorted(current),
                 "subscription_count": subscription_count,
                 "reconnect_count": getattr(self, "_reconnect_count", 0),
                 "oldest_valid_book_age_seconds": max(ages) if ages else None,
                 "data_stale": not bool(current),
                 "last_reported_error": last_error,
                "discovery_status": self.discovery_status,
                **({"discovery_error_type": self.discovery_error_type}
                   if self.manifest.name in {"prophetx", "polymarket_us"} else {}),
                "discovered_event_count": len(self.discovered_event_ids),
                **({"discovered_tournaments": getattr(self, "discovered_tournaments", {})}
                   if self.manifest.name == "prophetx" else {}),
                "discovered_market_count": (len(self.client.public_market_data.catalog)
                    if self.manifest.name == "prophetx" else len(self.discovered_market_ids)),
                **({"focused_nfl_prop_leads": len(self.focused_prop_source_ids),
                    "focused_nfl_prop_subscribed_books": len({item.native_market_id
                        for market_id in self.focused_prop_source_ids
                        for item in self.focused_prop_matches.get(market_id, ())
                        if item.native_market_id in self._subscribed_market_ids})}
                   if self.manifest.name == "novig" else {}),
                **({"discovered_prop_subscription_count": sum(map(len, self.discovered_prop_subtypes.values()))}
                   if self.manifest.name == "prophetx" else {}),
                **({"discovered_market_ids": list(self.discovered_market_ids),
                    "subscribed_market_ids": list(self._subscribed_market_ids),
                    "research_matched_market_ids": sorted({item.native_market_id
                        for matches in self.research_matches.values() for item in matches})}
                   if self.manifest.name == "kalshi" else {})}

    async def health_check(self):
        return NativeMonitor.health_snapshot(self)


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

