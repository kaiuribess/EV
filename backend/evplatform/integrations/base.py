"""Credential-safe, read-only provider transport contracts.

These contracts intentionally do not assume every input is an odds quote.
Adapters emit provider envelopes; a registered, verified mapper owns the
translation into canonical domain events and settlement rules.
"""
from __future__ import annotations

import asyncio
import os
import random
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field


class ProviderError(RuntimeError):
    """Safe to surface: never includes a URL, response body, or credentials."""


class UnsupportedOperation(ProviderError):
    pass


class ProviderDisabled(ProviderError):
    pass


class QuotaExceeded(ProviderError):
    pass


class ProtocolError(ProviderError):
    pass


class ProviderState(StrEnum):
    DISABLED = "disabled"
    MISSING_CREDENTIALS = "missing_credentials"
    READY = "ready"
    CONNECTED = "connected"
    BACKOFF = "backoff"
    UNAUTHORIZED = "unauthorized"
    ACCESS_DENIED = "access_denied"
    QUOTA_EXHAUSTED = "quota_exhausted"
    ERROR = "error"


class ProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    sports: list[str] = Field(default_factory=list)
    bookmakers: list[str] = Field(default_factory=list)
    markets: list[str] = Field(default_factory=lambda: ["h2h", "spreads", "totals"])
    poll_interval_seconds: float = Field(default=30, ge=1)
    request_timeout_seconds: float = Field(default=15, ge=1, le=60)
    max_requests_per_minute: int = Field(default=30, ge=1, le=6000)
    # A local spending guard, not a substitute for provider account usage.
    session_request_budget: int = Field(default=1000, ge=1)
    circuit_failure_limit: int = Field(default=5, ge=1, le=100)
    circuit_cooldown_seconds: float = Field(default=30, ge=1, le=600)


@dataclass(frozen=True, slots=True)
class ProviderEnvelope:
    provider_id: str
    kind: str
    payload: Any
    received_at: datetime
    received_monotonic_ns: int
    published_at: datetime | None = None
    snapshot_complete: bool = False
    synthetic: bool = False
    schema: str = "provider.raw.v1"
    # Metadata such as quota is separate from source prices and their clocks.
    metadata: dict[str, Any] = field(default_factory=dict)


def parse_timestamp(value: Any, *, epoch_unit: str = "seconds") -> datetime | None:
    """No arrival-time fallback: unknown timestamps remain unknown."""
    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, (int, float)):
            divisor = 1000 if epoch_unit == "milliseconds" else 1
            return datetime.fromtimestamp(value / divisor, UTC)
        if isinstance(value, str):
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return result.astimezone(UTC) if result.tzinfo else None
    except (ValueError, TypeError, OverflowError, OSError):
        return None
    return None


def safe_component(value: str) -> str:
    """Path identifiers are provider keys, never URLs or path traversal."""
    if not value or not all(c.isalnum() or c in "_-." for c in value) or value in {".", ".."}:
        raise ValueError("Invalid provider identifier")
    return value


@dataclass
class QuotaGuard:
    max_per_minute: int
    session_budget: int
    clock: Callable[[], float] = time.monotonic
    calls: int = 0
    billable_requests: int = 0
    provider_remaining: int | None = None
    minute_remaining: int | None = None
    blocked_until: float = 0
    provider_reset_at: datetime | None = None
    _window_start: float = 0
    _window_calls: int = 0

    def reserve(self, cost: int = 1) -> None:
        now = self.clock()
        if now < self.blocked_until:
            raise QuotaExceeded("Provider cooldown active")
        if self.provider_remaining is not None and self.provider_remaining < cost:
            raise QuotaExceeded("Provider quota exhausted")
        if self.billable_requests + cost > self.session_budget:
            raise QuotaExceeded("Local session quota exhausted")
        if now - self._window_start >= 60:
            self._window_start, self._window_calls = now, 0
            self.minute_remaining = None
        if self._window_calls >= self.max_per_minute:
            self.blocked_until = self._window_start + 60
            raise QuotaExceeded("Local per-minute rate limit")
        if self.minute_remaining is not None and self.minute_remaining <= 0:
            # The server's reset deadline was already honored above.
            self.minute_remaining = None
        self.calls += 1
        self._window_calls += 1
        # Reserve before transmission; failed remote calls can be billable.
        self.billable_requests += cost
        if self.provider_remaining is not None:
            self.provider_remaining -= cost
        if self.minute_remaining is not None:
            self.minute_remaining -= 1

    def observe_headers(self, headers: httpx.Headers) -> None:
        def number(name: str) -> int | None:
            value = headers.get(name)
            try:
                return int(value) if value is not None and value != "unlimited" else None
            except ValueError:
                return None

        remaining = number("x-ratelimit-remaining-month")
        if remaining is None:
            remaining = number("x-requests-remaining")
        if remaining is not None:
            self.provider_remaining = remaining
        minute = number("x-ratelimit-remaining-minute")
        if minute is not None:
            self.minute_remaining = minute
            reset_ms = number("x-ratelimit-reset-minute")
            if minute == 0 and reset_ms is not None:
                delay = max(0, reset_ms / 1000 - datetime.now(UTC).timestamp())
                self.blocked_until = self.clock() + delay
        self.provider_reset_at = parse_timestamp(headers.get("x-ratelimit-reset-month"))

    def retry_after(self, headers: httpx.Headers) -> None:
        try:
            seconds = float(headers.get("retry-after", "60"))
        except ValueError:
            seconds = 60
        self.blocked_until = self.clock() + max(1, min(seconds, 86400))

    def stats(self) -> dict[str, Any]:
        return {
            "requests": self.calls,
            "reserved_cost": self.billable_requests,
            "local_session_budget": self.session_budget,
            "provider_remaining": self.provider_remaining,
            "minute_remaining": self.minute_remaining,
            "reset_at": self.provider_reset_at.isoformat() if self.provider_reset_at else None,
            "cooldown_seconds": max(0, self.blocked_until - self.clock()),
        }


class Backoff:
    def __init__(self, base: float = 1, cap: float = 30, seed: int | None = None):
        self.base, self.cap, self.attempt = base, cap, 0
        self.random = random.Random(seed)

    def next_delay(self) -> float:
        limit = min(self.cap, self.base * 2 ** min(self.attempt, 20))
        self.attempt += 1
        return self.random.uniform(limit * 0.5, limit)

    def reset(self) -> None:
        self.attempt = 0


class SequenceTracker:
    """Use only for protocols with documented monotonic sequence semantics."""
    def __init__(self):
        self.sequence: int | None = None
        self.valid = False

    def snapshot(self, sequence: int) -> None:
        self.sequence, self.valid = sequence, True

    def delta(self, sequence: int) -> bool:
        if not self.valid or self.sequence is None or sequence != self.sequence + 1:
            self.valid = False
            return False
        self.sequence = sequence
        return True


class RestProvider:
    provider_id = "base"
    base_url = ""
    credential_env = ""
    config_class = ProviderConfig
    manifest: dict[str, Any] = {}

    def __init__(
        self,
        config: ProviderConfig | dict[str, Any] | None = None,
        *,
        client: httpx.AsyncClient | None = None,
        credential: str | None = None,
    ):
        self.config = config if isinstance(config, self.config_class) else self.config_class.model_validate(config or {})
        self._credential = credential if credential is not None else os.getenv(self.credential_env, "")
        self._client = client
        self._owns_client = client is None
        self.state = ProviderState.DISABLED
        self.reason = "Disabled by configuration"
        self.quota = QuotaGuard(self.config.max_requests_per_minute, self.config.session_request_budget)
        self.errors = 0
        self._consecutive_errors = 0
        self._circuit_until = 0.0
        self._closed = False
        self._request_lock = asyncio.Lock()
        self._subscriptions: set[str] = set(self.config.sports)
        self.last_received_at: datetime | None = None
        self.last_http_latency_ms: float | None = None

    def __repr__(self) -> str:
        return f"<{type(self).__name__} state={self.state}>"

    async def connect(self) -> bool:
        if not self.config.enabled:
            self.state, self.reason = ProviderState.DISABLED, "Disabled by configuration"
            return False
        if not self._credential:
            self.state, self.reason = ProviderState.MISSING_CREDENTIALS, f"Set {self.credential_env} on the backend"
            return False
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.config.request_timeout_seconds, follow_redirects=False)
        self._closed = False
        self.state, self.reason = ProviderState.READY, "Credential configured; authentication not yet verified"
        return True

    async def disconnect(self) -> None:
        self._closed = True
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None
        self.state, self.reason = ProviderState.DISABLED, "Disconnected"

    def auth(self) -> tuple[dict[str, str], dict[str, str]]:
        raise UnsupportedOperation("Provider must implement its own authentication")

    def sanitize(self, value: Any) -> Any:
        """Some account APIs return the API key in an otherwise useful response."""
        if isinstance(value, dict):
            return {
                key: "[REDACTED]" if key.lower().replace("_", "") in {
                    "apikey", "authorization", "accesstoken", "refreshtoken", "password", "secret",
                } else self.sanitize(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self.sanitize(item) for item in value]
        if isinstance(value, str) and self._credential:
            return value.replace(self._credential, "[REDACTED]")
        return value

    async def request(self, path: str, *, params: dict[str, Any] | None = None, cost: int = 1) -> ProviderEnvelope:
        if self.state in {ProviderState.DISABLED, ProviderState.MISSING_CREDENTIALS}:
            if not await self.connect():
                raise ProviderDisabled(self.reason)
        if self.state in {ProviderState.UNAUTHORIZED, ProviderState.ACCESS_DENIED}:
            raise ProviderDisabled(self.reason)
        if time.monotonic() < self._circuit_until:
            raise ProviderDisabled("Provider circuit breaker is cooling down")
        if not path.startswith("/") or "?" in path or ".." in path or "://" in path:
            raise ValueError("Expected a relative API endpoint")
        headers, auth_params = self.auth()
        supplied = dict(params or {})
        # The adapter owns authentication. Callers cannot override it.
        supplied.update(auth_params)
        async with self._request_lock:
            try:
                self.quota.reserve(cost)
            except QuotaExceeded:
                self.state, self.reason = ProviderState.QUOTA_EXHAUSTED, "Quota guard blocked request"
                raise
        started = time.perf_counter_ns()
        try:
            assert self._client is not None
            response = await self._client.get(self.base_url + path, params=supplied, headers=headers)
            received = datetime.now(UTC)
            received_ns = time.perf_counter_ns()
            self.last_http_latency_ms = (received_ns - started) / 1e6
            self.quota.observe_headers(response.headers)
            if response.status_code in (401, 403):
                self.state = ProviderState.UNAUTHORIZED if response.status_code == 401 else ProviderState.ACCESS_DENIED
                self.reason = f"Provider refused access (HTTP {response.status_code})"
                raise ProviderDisabled(self.reason)
            if response.status_code == 429:
                self.quota.retry_after(response.headers)
                self.state, self.reason = ProviderState.QUOTA_EXHAUSTED, "Provider rate or subscription limit"
                raise QuotaExceeded(self.reason)
            if response.status_code >= 300:
                raise ProviderError(f"Provider HTTP {response.status_code}")
            try:
                body = self.sanitize(response.json())
            except ValueError:
                raise ProtocolError("Provider returned invalid JSON") from None
            self.state, self.reason = ProviderState.CONNECTED, "Authenticated HTTP response received"
            self._consecutive_errors = 0
            self.last_received_at = received
            safe_headers = {
                key: self.sanitize(value) for key, value in response.headers.items()
                if key.startswith(("x-result-", "x-markets-")) or key in {
                    "x-next-offset", "x-provider-state", "x-requests-remaining",
                    "x-requests-used", "x-requests-last",
                }
            }
            return ProviderEnvelope(self.provider_id, "snapshot", body, received, received_ns,
                                    metadata={"quota": self.quota.stats(), "headers": safe_headers})
        except (ProviderDisabled, QuotaExceeded):
            raise
        except (httpx.HTTPError, ProviderError) as error:
            self.errors += 1
            self._consecutive_errors += 1
            self.state = ProviderState.ERROR
            self.reason = str(error) if isinstance(error, ProviderError) else "Provider transport request failed"
            if self._consecutive_errors >= self.config.circuit_failure_limit:
                self._circuit_until = time.monotonic() + self.config.circuit_cooldown_seconds
            # Exception chaining can reveal credential-bearing OddsPapi URLs.
            raise ProviderError(self.reason) from None

    async def discover_sports(self) -> ProviderEnvelope:
        raise UnsupportedOperation("Sport discovery is not supported by this provider")

    async def discover_events(self, sport: str, **filters: Any) -> ProviderEnvelope:
        raise UnsupportedOperation("Event discovery is not supported by this provider")

    async def discover_markets(self, sport: str | None = None) -> ProviderEnvelope:
        raise UnsupportedOperation("Market discovery is not supported by this provider")

    async def subscribe(self, sports: list[str]) -> None:
        self._subscriptions.update(safe_component(sport) for sport in sports)

    async def unsubscribe(self, sports: list[str]) -> None:
        self._subscriptions.difference_update(sports)

    async def fetch_snapshot(self, sport: str, **filters: Any) -> ProviderEnvelope:
        raise UnsupportedOperation("Snapshot is not supported by this provider")

    async def _poll_sport(self, sport: str) -> AsyncIterator[ProviderEnvelope]:
        yield await self.fetch_snapshot(sport)

    async def stream_updates(self) -> AsyncIterator[ProviderEnvelope]:
        """Polling transport: receives snapshots, never masquerades as streaming."""
        if not await self.connect():
            return
        backoff = Backoff()
        while not self._closed:
            try:
                for sport in sorted(self._subscriptions):
                    if self._closed:
                        break
                    async for envelope in self._poll_sport(sport):
                        yield envelope
                backoff.reset()
                await asyncio.sleep(self.config.poll_interval_seconds)
            except ProviderDisabled:
                return
            except (ProviderError, QuotaExceeded):
                self.state = ProviderState.BACKOFF
                await asyncio.sleep(max(backoff.next_delay(), min(self.quota.stats()["cooldown_seconds"], 60)))

    async def health_check(self) -> dict[str, Any]:
        """Local health checks never consume a provider request or refresh prices."""
        return {
            "provider_id": self.provider_id,
            "state": self.state.value,
            "reason": self.reason,
            "credential_configured": bool(self._credential),
            "authenticated": self.state == ProviderState.CONNECTED,
            "errors": self.errors,
            "last_received_at": self.last_received_at.isoformat() if self.last_received_at else None,
            "http_roundtrip_ms": self.last_http_latency_ms,
            "quota": self.quota.stats(),
            "synthetic": False,
        }

    def get_capabilities(self) -> dict[str, Any]:
        return self.manifest


def provider_manifest(
    plugin_id: str, name: str, *, transports: list[str], env: str, docs: list[str],
    config: type[ProviderConfig] = ProviderConfig, limitations: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": plugin_id, "name": name, "version": "1.0.0", "plugin_type": "input",
        "contract_version": "1.0", "sports": ["discovered"], "markets": ["discovered"],
        "input_schemas": [], "output_schemas": ["provider.raw.v1"],
        "transports": transports, "authentication": {"credential_env": [env], "backend_only": True},
        "streaming": "websocket" in transports or "sse" in transports,
        "polling": "rest" in transports,
        "rate_limits": {"policy": "local budget plus observed provider quota; see official documentation"},
        "usage_accounting": True, "config_schema": config.model_json_schema(),
        "health_check": True, "permissions": ["network:provider", f"secret:{env}"],
        "documentation": docs, "limitations": limitations or [],
        "offline_testable": True, "live_tested": False,
    }
