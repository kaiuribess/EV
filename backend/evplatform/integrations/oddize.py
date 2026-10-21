"""Oddize v1 read-only REST adapter, based on the published OpenAPI contract.

Site subscriptions and API allowances are separate. This adapter never purchases
a plan, infers live coverage from a scheduled start, or treats aggregate exchange
prices as an executable order book.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import httpx
from pydantic import Field, field_validator

from .base import (
    Backoff, ProviderConfig, ProviderDisabled, ProviderEnvelope, ProviderError,
    ProviderState, QuotaExceeded, QuotaGuard, RestProvider, UnsupportedOperation, parse_timestamp,
    provider_manifest, safe_component,
)


class OddizeConfig(ProviderConfig):
    markets: list[str] = Field(default_factory=lambda: ["ml"])
    poll_interval_seconds: float = Field(default=3600, ge=60)
    session_request_budget: int = Field(default=30, ge=1)
    # Schedule this credit budget over a month across all configured sports.
    # The server's remaining allowance is an additional independent restriction.
    monthly_credit_budget: int = Field(default=1200, ge=1)
    limit: int = Field(default=100, ge=1, le=500)
    include_past: bool = False
    # Published pricing lists delta polling under Starter. Explicit opt-in only.
    use_delta_polling: bool = False
    include_props: bool = False
    # Use the fully specified flat event-props schema. The broad latest-props
    # endpoint has intentionally untyped nested objects in the published spec.
    event_ids: dict[str, list[str]] = Field(default_factory=dict)
    prop_types: dict[str, list[str]] = Field(default_factory=dict)

    @field_validator("markets")
    @classmethod
    def supported_main_markets(cls, value):
        if any(item not in {"ml", "s", "t", "rl", "tt"} for item in value):
            raise ValueError("Use Oddize main-line codes ml, s, t, rl or tt")
        return value


class _OddizeQuota(QuotaGuard):
    """Observe Oddize's documented credit and per-key minute headers."""
    last_cost: int | None = None
    _reserved_cost: int = 0

    def reserve(self, cost: int = 1) -> None:
        super().reserve(cost)
        self._reserved_cost = cost

    def observe_headers(self, headers: httpx.Headers) -> None:
        reset_at = self.provider_reset_at
        super().observe_headers(headers)
        if self.provider_reset_at is None:
            self.provider_reset_at = reset_at

        def integer(name):
            try:
                value = int(headers[name])
                return value if value >= 0 else None
            except (KeyError, ValueError):
                return None

        actual_cost = integer("x-credits-cost")
        if actual_cost is not None:
            self.last_cost = actual_cost
            self.billable_requests = max(0, self.billable_requests + actual_cost - self._reserved_cost)
        remaining = integer("x-credits-remaining")
        if remaining is not None:
            self.provider_remaining = remaining
        rate = integer("x-ratelimit-limit")
        if rate is not None and rate > 0:
            self.max_per_minute = min(self.max_per_minute, rate)
        minute = integer("x-ratelimit-remaining")
        if minute is not None:
            self.minute_remaining = minute
            if minute == 0:
                # No reset-unit semantics are specified by the OpenAPI contract.
                # A full minute pause is conservative; 429 Retry-After is honored.
                self.blocked_until = max(self.blocked_until, self.clock() + 60)


def _event_path(sport: str, event_id: str) -> str:
    # Oddize explicitly documents event IDs containing literal slashes. Validate
    # every segment; never permit a URL, query, fragment or traversal component.
    if not isinstance(event_id, str) or not event_id or len(event_id) > 256:
        raise ValueError("Invalid Oddize event identifier")
    return f"/api/v1/events/{safe_component(sport)}/" + "/".join(
        safe_component(part) for part in event_id.split("/")
    )


class OddizeRest(RestProvider):
    provider_id = "oddize_rest"
    base_url = "https://oddize.com"
    credential_env = "ODDIZE_API_KEY"
    config_class = OddizeConfig

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.quota = _OddizeQuota(self.config.max_requests_per_minute, self.config.session_request_budget)
        self._oddize_request_lock = asyncio.Lock()
        self._newest: dict[str, int] = {}
        self.account: dict[str, Any] = {}
        self.sport_catalog: list[dict] = []
        self.book_catalog: list[dict] = []
        self.market_catalog: list[dict] = []

    def auth(self) -> tuple[dict[str, str], dict[str, str]]:
        return {"X-API-Key": self._credential}, {}

    async def request(self, path: str, **kwargs: Any) -> ProviderEnvelope:
        # Keep accounting header reconciliation associated with its reservation.
        async with self._oddize_request_lock:
            try:
                return await super().request(path, **kwargs)
            except ProviderError as exc:
                if str(exc) == "Provider HTTP 402":
                    self.quota.provider_remaining = 0
                    self.state, self.reason = ProviderState.QUOTA_EXHAUSTED, "Oddize credit allowance exhausted (HTTP 402)"
                    raise QuotaExceeded(self.reason) from None
                raise

    async def fetch_account(self) -> ProviderEnvelope:
        result = await self.request("/api/v1/account/usage", cost=0)
        body = result.payload if isinstance(result.payload, dict) else {}
        # Explicit allowlist excludes key labels/prefixes and future account data.
        self.account = {name: body[name] for name in (
            "plan_id", "credits_limit", "credits_used", "credits_remaining", "requests_made",
            "rate_limit_rpm", "period_start", "period_end", "resets_in_seconds",
        ) if name in body}
        remaining, rpm = body.get("credits_remaining"), body.get("rate_limit_rpm")
        if type(remaining) is int and remaining >= 0:
            self.quota.provider_remaining = remaining
        if type(rpm) is int and rpm > 0:
            self.quota.max_per_minute = min(self.quota.max_per_minute, self.config.max_requests_per_minute, rpm)
        self.quota.provider_reset_at = parse_timestamp(body.get("period_end"))
        return replace(result, kind="account_capabilities", payload=dict(self.account))

    async def discover_sports(self) -> ProviderEnvelope:
        result = await self.request("/api/v1/sports", cost=0)
        self.sport_catalog = result.payload.get("sports", []) if isinstance(result.payload, dict) else []
        return replace(result, kind="sport_discovery")

    async def discover_bookmakers(self) -> ProviderEnvelope:
        result = await self.request("/api/v1/books", cost=0)
        self.book_catalog = result.payload.get("books", []) if isinstance(result.payload, dict) else []
        return replace(result, kind="bookmaker_discovery")

    async def discover_markets(self, sport: str | None = None) -> ProviderEnvelope:
        # /offer-types is a global catalog. Per-sport prop availability comes from
        # /sports, not an assumed book-by-sport coverage matrix.
        result = await self.request("/api/v1/offer-types", cost=0)
        self.market_catalog = result.payload.get("offer_types", []) if isinstance(result.payload, dict) else []
        return replace(result, kind="market_discovery")

    async def discover_teams(self, sport: str) -> ProviderEnvelope:
        return replace(await self.request(f"/api/v1/sports/{safe_component(sport)}/teams", cost=0), kind="team_discovery")

    async def discover_events(self, sport: str, **filters: Any) -> ProviderEnvelope:
        # No separate documented event-list API exists; this is metered at 1.
        return replace(await self.fetch_snapshot(sport, **filters), kind="event_discovery")

    async def fetch_snapshot(self, sport: str, **filters: Any) -> ProviderEnvelope:
        sport = safe_component(sport)
        params: dict[str, Any] = {"sport": sport, "limit": self.config.limit,
                                  "include_past": str(self.config.include_past).lower()}
        if self.config.bookmakers:
            params["books"] = list(self.config.bookmakers)
        if self.config.markets:
            params["offer_types"] = list(self.config.markets)
        params.update(filters)
        # Authentication and sport identity cannot be overridden by filters.
        params["sport"] = sport
        result = await self.request("/api/v1/odds/latest", params=params, cost=1)
        return replace(result, snapshot_complete="updated_since" not in params,
                       metadata={**result.metadata, "sport": sport,
                                 "delta": "updated_since" in params, "odds_format": "american"})

    async def fetch_event(self, sport: str, event_id: str) -> ProviderEnvelope:
        result = await self.request(_event_path(sport, event_id), cost=1)
        return replace(result, metadata={**result.metadata, "sport": sport, "odds_format": "american"})

    async def fetch_event_props(self, sport: str, event_id: str, *, prop_types: list[str]) -> ProviderEnvelope:
        if not prop_types:
            raise UnsupportedOperation("Choose discovered prop_types explicitly; the endpoint defaults to team totals")
        result = await self.request(_event_path(sport, event_id) + "/props", params={"prop_types": prop_types}, cost=2)
        return replace(result, kind="props_snapshot", metadata={**result.metadata, "sport": sport,
                                                                "event_id": event_id, "odds_format": "american"})

    def effective_poll_interval(self) -> float:
        per_cycle = len(self._subscriptions)
        if self.config.include_props:
            per_cycle += 2 * sum(len(self.config.event_ids.get(sport, [])) for sport in self._subscriptions
                                 if self.config.prop_types.get(sport))
        # Scale the free-tier budget with the number of sports/props, rather than
        # assuming 1,500 credits can fund fast polling of every league.
        interval = 31 * 86400 * max(1, per_cycle) / self.config.monthly_credit_budget
        if self.quota.provider_remaining is not None and self.quota.provider_reset_at:
            seconds = max(0, (self.quota.provider_reset_at - datetime.now(UTC)).total_seconds())
            interval = max(interval, seconds * max(1, per_cycle) / max(1, self.quota.provider_remaining))
        return max(self.config.poll_interval_seconds, interval)

    async def stream_updates(self) -> AsyncIterator[ProviderEnvelope]:
        if not await self.connect():
            return
        backoff = Backoff()
        bootstrapped = False
        while not self._closed:
            try:
                if not bootstrapped:
                    yield await self.fetch_account()
                    yield await self.discover_sports()
                    yield await self.discover_bookmakers()
                    yield await self.discover_markets()
                    bootstrapped = True
                for sport in sorted(self._subscriptions):
                    if self._closed:
                        return
                    filters = {}
                    # Pricing only promises updated_since for Starter and above.
                    delta_allowed = self.account.get("plan_id") in {"api_starter", "api_pro", "api_elite"}
                    if self.config.use_delta_polling and delta_allowed and sport in self._newest:
                        filters["updated_since"] = self._newest[sport]
                    result = await self.fetch_snapshot(sport, **filters)
                    events = result.payload.get("events", []) if isinstance(result.payload, dict) else []
                    stamps = [price.get("ts") for event in events if isinstance(event, dict)
                              for price in event.get("odds", []) if isinstance(price, dict)
                              and type(price.get("ts")) is int
                              and 0 < price["ts"] <= result.received_at.timestamp()]
                    if stamps:
                        self._newest[sport] = max(self._newest.get(sport, 0), max(stamps))
                    yield result
                    if self.config.include_props and self.config.prop_types.get(sport):
                        for event_id in self.config.event_ids.get(sport, []):
                            yield await self.fetch_event_props(sport, event_id, prop_types=self.config.prop_types[sport])
                backoff.reset()
                await asyncio.sleep(self.effective_poll_interval())
            except ProviderDisabled:
                return
            except QuotaExceeded:
                # A stopped stream invalidates its mapped prices in ProviderBridge.
                # Explicitly restart after quota review; do not spin on 402.
                self.state = ProviderState.QUOTA_EXHAUSTED
                return
            except ProviderError:
                self.state = ProviderState.BACKOFF
                yield ProviderEnvelope(self.provider_id, "feed_invalidated", {}, datetime.now(UTC), time.perf_counter_ns())
                await asyncio.sleep(backoff.next_delay())

    async def health_check(self) -> dict[str, Any]:
        health = await super().health_check()
        return {**health, "streaming": False, "polling": True, "account": dict(self.account),
                "effective_poll_interval_seconds": self.effective_poll_interval(),
                "discovered_sports": [row.get("sport") for row in self.sport_catalog if isinstance(row, dict)],
                "discovered_books": [row.get("code") for row in self.book_catalog if isinstance(row, dict)],
                "last_credits_cost": self.quota.last_cost,
                "source_clock": "Oddize observation time; sportsbook-origin latency is unknown"}


OddizeRest.manifest = provider_manifest(
    "oddize_rest", "Oddize REST v1", transports=["rest"], env="ODDIZE_API_KEY", config=OddizeConfig,
    docs=["https://oddize.com/odds-api", "https://oddize.com/api/v1/docs", "https://oddize.com/api/v1/openapi.json"],
    limitations=[
        "REST polling only; no published WebSocket or SSE protocol.",
        "API allowance is separate from the site subscription; account email confirmation is required.",
        "Developer allowance is 1,500 credits/month and 30 requests/minute; cache hits still consume credits.",
        "Actual sports and prop coverage come from metadata; a book listing is not a per-market coverage guarantee.",
        "No executable depth, settlement rules or reliable market-status field is provided by this odds schema.",
        "All identities and settlement equivalence require verified mapping; quote observation time is preserved.",
    ],
)
OddizeRest.manifest["rate_limits"] = {"developer_rpm": 30, "developer_monthly_credits": 1500,
                                      "local_monthly_budget_default": 1200,
                                      "policy": "Account/header limits and local session/monthly budget all apply"}


async def live_connection_check(*, sample_sport: str | None = None) -> dict[str, Any]:
    """Explicit diagnostic: four free reads and optionally one 1-credit snapshot.

    Returns only counts, account allowance, transport health and original quote
    clocks. Load .env in the caller; importing or missing credentials makes no
    remote request. A result is evidence only for requests actually made.
    """
    from .normalization import extract_offers

    provider = OddizeRest({"enabled": True, "session_request_budget": 1})
    try:
        if not await provider.connect():
            return {"health": await provider.health_check(), "network_requests": 0}
        await provider.fetch_account()
        await provider.discover_sports()
        await provider.discover_bookmakers()
        await provider.discover_markets()
        result = {"account": dict(provider.account), "sports": len(provider.sport_catalog),
                  "books": len(provider.book_catalog), "offer_types": len(provider.market_catalog)}
        if sample_sport is not None:
            snapshot = await provider.fetch_snapshot(sample_sport)
            offers = extract_offers(snapshot)
            result["sample"] = {"sport": sample_sport, "source_offers": len(offers),
                                "source_books": sorted({offer.bookmaker for offer in offers}),
                                "known_quote_clocks": sum(offer.observed_at is not None for offer in offers),
                                "synthetic": False}
        return {**result, "health": await provider.health_check(), "network_requests": provider.quota.calls}
    finally:
        await provider.disconnect()
