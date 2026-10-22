"""OddsPapi v4 REST client with account-specific live/prop capability checks."""
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import Field

from .base import (
    Backoff, ProviderConfig, ProviderDisabled, ProviderEnvelope, ProviderError, ProviderState,
    RestProvider, UnsupportedOperation, provider_manifest,
)


class _QueryCredentialFilter(logging.Filter):
    """HTTPX logs request URLs; v4 mandates a query credential."""
    def __init__(self, credential: str):
        super().__init__()
        self.credential = credential

    def filter(self, record: logging.LogRecord) -> bool:
        if self.credential:
            record.msg = record.getMessage().replace(self.credential, "[REDACTED]")
            record.msg = re.sub(r"(?i)(apiKey=)[^&\s\"']+", r"\1[REDACTED]", record.msg)
            record.args = ()
        return True


class OddsPapiConfig(ProviderConfig):
    fixture_ids: list[str] = Field(default_factory=list)
    discovery_interval_seconds: float = Field(default=300, ge=60)
    discovery_window_hours: int = Field(default=24, ge=1, le=48)
    # There is no safe universal live permission; account and book must allow it.
    include_live: bool = False
    include_props: bool = False
    endpoint_cooldown_seconds: float = Field(default=2, ge=2, le=60)


class OddsPapiRest(RestProvider):
    provider_id = "oddspapi_rest"
    base_url = "https://api.oddspapi.io"
    credential_env = "ODDSPAPI_API_KEY"
    config_class = OddsPapiConfig

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.bookmaker_capabilities: dict[str, Any] = {}
        self.account_bookmakers: dict[str, Any] = {}
        self.account_sports: list[int] = []
        self.market_catalog: dict[str, dict] = {}
        self._fixture_cache: dict[str, list[dict]] = {}
        self._discovered_at: dict[str, float] = {}
        self._last_endpoint: dict[str, float] = {}
        self._endpoint_locks: dict[str, asyncio.Lock] = {}
        self._account_checked = False
        self._log_filter = _QueryCredentialFilter(self._credential)
        logging.getLogger("httpx").addFilter(self._log_filter)

    def auth(self) -> tuple[dict[str, str], dict[str, str]]:
        return {}, {"apiKey": self._credential}

    async def request(self, path: str, **kwargs: Any) -> ProviderEnvelope:
        # Per-endpoint cooldown applies even when callers invoke discovery directly.
        lock = self._endpoint_locks.setdefault(path, asyncio.Lock())
        async with lock:
            delay = self._last_endpoint.get(path, 0) + self.config.endpoint_cooldown_seconds - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self._last_endpoint[path] = time.monotonic()
            return await super().request(path, **kwargs)

    async def fetch_account(self) -> ProviderEnvelope:
        result = await self.request("/v4/account", cost=0)
        body = result.payload if isinstance(result.payload, dict) else {}
        subscriptions = body.get("subscriptions", [])
        active = next((s for s in subscriptions if s.get("subscription_id") == body.get("current_subscription_id")), None)
        if active is None:
            active = next((s for s in subscriptions if s.get("is_active") is True), None)
        if active:
            limit, used = active.get("request_limit"), active.get("request_count")
            if isinstance(limit, (int, float)) and isinstance(used, (int, float)):
                self.quota.provider_remaining = max(0, int(limit - used))
            self.account_bookmakers = active.get("bookmakers") or {}
            self.account_sports = active.get("sport_ids") or []
        self._account_checked = True
        # Only whitelist capabilities for outbound event data, never account identity/key.
        return replace(result, kind="account_capabilities", payload={
            "bookmakers": self.account_bookmakers, "sport_ids": self.account_sports,
            "quota": self.quota.stats(),
        })

    async def discover_sports(self) -> ProviderEnvelope:
        return replace(await self.request("/v4/sports"), kind="sport_discovery")

    async def discover_bookmakers(self) -> ProviderEnvelope:
        result = await self.request("/v4/bookmakers")
        if isinstance(result.payload, list):
            self.bookmaker_capabilities = {
                row["slug"]: row for row in result.payload if isinstance(row, dict) and "slug" in row
            }
        return replace(result, kind="bookmaker_discovery")

    async def discover_events(self, sport: str, **filters: Any) -> ProviderEnvelope:
        now = datetime.now(UTC)
        params: dict[str, Any] = {
            "sportId": int(sport),
            "from": (now - timedelta(hours=4)).isoformat(),
            "to": (now + timedelta(hours=self.config.discovery_window_hours)).isoformat(),
            "hasOdds": "true",
        }
        if not self.config.include_live:
            params["statusId"] = 0
        if self.config.bookmakers:
            params["bookmakers"] = ",".join(self.config.bookmakers)
        params.update(filters)
        return replace(await self.request("/v4/fixtures", params=params), kind="event_discovery")

    async def discover_markets(self, sport: str | None = None) -> ProviderEnvelope:
        result = await self.request("/v4/markets", params={"language": "en"})
        if isinstance(result.payload, list):
            self.market_catalog = {str(row["marketId"]): row for row in result.payload
                                   if isinstance(row, dict) and "marketId" in row}
        if sport is not None and isinstance(result.payload, list):
            return replace(result, kind="market_discovery",
                           payload=[row for row in result.payload if str(row.get("sportId")) == str(sport)])
        return replace(result, kind="market_discovery")

    def supports(self, bookmaker: str, capability: str) -> bool:
        entitlement = self.account_bookmakers.get(bookmaker, {})
        if capability == "live":
            # Null or missing coverage is unknown and must not be advertised as live.
            return (entitlement.get("has_live_odds") is True
                    and self.bookmaker_capabilities.get(bookmaker, {}).get("liveOdds") is True)
        if capability == "props":
            return entitlement.get("has_player_props") is True
        return False

    async def fetch_fixture(self, fixture_id: str, *, live: bool = False, **filters: Any) -> ProviderEnvelope:
        if self.config.include_props and not self._account_checked:
            await self.fetch_account()
        if self.config.include_props and not self.market_catalog:
            await self.discover_markets()
        if live:
            if not self._account_checked:
                await self.fetch_account()
            if not self.bookmaker_capabilities:
                await self.discover_bookmakers()
            if not self.config.include_live:
                raise UnsupportedOperation("In-play data disabled in configuration")
            if not self.config.bookmakers or not all(self.supports(book, "live") for book in self.config.bookmakers):
                raise UnsupportedOperation("Live coverage and entitlement must be verified for each requested book")
        params: dict[str, Any] = {"fixtureId": fixture_id, "language": "en", "verbosity": 3}
        if self.config.bookmakers:
            params["bookmakers"] = ",".join(self.config.bookmakers)
        params.update(filters)
        result = await self.request("/v4/odds", params=params)
        # A scheduled fixture may have gone live since the slower discovery pass.
        if isinstance(result.payload, dict) and result.payload.get("statusId") == 1:
            permitted = (self.config.include_live and self._account_checked and self.config.bookmakers
                         and all(self.supports(book, "live") for book in self.config.bookmakers))
            if not permitted:
                return replace(result, kind="coverage_blocked", payload={
                    "fixture_id": fixture_id, "reason": "Live coverage not configured and verified",
                })
        native_markets = {
            key for book in result.payload.get("bookmakerOdds", {}).values() for key in book.get("markets", {})
        } if isinstance(result.payload, dict) else set()
        return replace(result, metadata={
            **result.metadata, "allow_props": self.config.include_props,
            "props_books": [book for book in self.account_bookmakers if self.supports(book, "props")],
            "market_catalog": {key: self.market_catalog[key] for key in native_markets if key in self.market_catalog},
        })

    async def fetch_snapshot(self, sport: str, **filters: Any) -> ProviderEnvelope:
        """A single fixture snapshot; explicit fixture ID avoids a full-board fetch."""
        fixture_id = filters.pop("fixture_id", None)
        if fixture_id is None:
            raise UnsupportedOperation("OddsPapi snapshots require fixture_id; stream_updates performs scheduled discovery")
        result = await self.fetch_fixture(fixture_id, **filters)
        return replace(result, metadata={**result.metadata, "sport": sport})

    async def stream_updates(self) -> AsyncIterator[ProviderEnvelope]:
        if not await self.connect():
            return
        backoff = Backoff()
        while not self._closed:
            try:
                if not self._account_checked:
                    yield await self.fetch_account()
                    yield await self.discover_bookmakers()
                for fixture in self.config.fixture_ids:
                    yield await self.fetch_fixture(fixture)
                for sport in sorted(self._subscriptions):
                    if time.monotonic() - self._discovered_at.get(sport, 0) >= self.config.discovery_interval_seconds:
                        discovery = await self.discover_events(sport)
                        self._fixture_cache[sport] = discovery.payload if isinstance(discovery.payload, list) else []
                        self._discovered_at[sport] = time.monotonic()
                        yield replace(discovery, kind="event_discovery")
                    for fixture in self._fixture_cache.get(sport, []):
                        if self._closed:
                            return
                        if fixture.get("statusId") in {2, 3}:
                            continue
                        try:
                            yield await self.fetch_snapshot(
                                sport, fixture_id=fixture["fixtureId"], live=fixture.get("statusId") == 1,
                            )
                        except UnsupportedOperation:
                            # Explicit unsupported coverage is observable, never replaced with mock odds.
                            yield ProviderEnvelope(self.provider_id, "coverage_blocked", {
                                "fixture_id": fixture["fixtureId"], "reason": "Requested coverage not verified",
                            }, datetime.now(UTC), time.perf_counter_ns())
                backoff.reset()
                await asyncio.sleep(self.config.poll_interval_seconds)
            except ProviderDisabled:
                return
            except ProviderError:
                self.state = ProviderState.BACKOFF
                await asyncio.sleep(max(backoff.next_delay(), min(self.quota.stats()["cooldown_seconds"], 60)))

    async def disconnect(self) -> None:
        logging.getLogger("httpx").removeFilter(self._log_filter)
        await super().disconnect()


OddsPapiRest.manifest = provider_manifest(
    "oddspapi_rest", "OddsPapi REST v4", transports=["rest"], env="ODDSPAPI_API_KEY",
    config=OddsPapiConfig, docs=[
        "https://oddspapi.io/en/docs/methods", "https://oddspapi.io/en/docs/get-odds",
        "https://oddspapi.io/en/docs/get-account", "https://oddspapi.io/en/docs/get-bookmakers",
        "https://oddspapi.io/en/docs/requests-and-quota",
    ],
    limitations=[
        "v4 REST only; separate B2B v5/WebSocket protocols are not implemented.",
        "Native sport IDs are numeric strings. Live and props permissions are bookmaker-specific.",
        "Both active sides need bookmakerChangedAt and changedAt; price-change clocks never refresh unchanged odds.",
        "Fixture updatedAt and REST receipt are not quote freshness.",
    ],
)
