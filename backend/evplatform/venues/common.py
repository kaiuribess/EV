"""Shared native transport mechanics, separate from pricing, risk, and the OMS.

Only the OMS may call writes, after risk approval and durable intent. These
clients never silently substitute demo data, retry placements, or infer fills.
"""
from __future__ import annotations

import asyncio
import json
import random
import re
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field


class VenueError(RuntimeError):
    pass


class CredentialsMissing(VenueError):
    pass


class PreflightRequired(VenueError):
    pass


class VenueHTTPError(VenueError):
    def __init__(self, venue: str, status_code: int, code: str | None = None):
        self.status_code, self.code = status_code, code
        super().__init__(f"{venue}: HTTP {status_code}" + (f" ({code})" if code else ""))


class UnknownOrderOutcome(VenueError):
    """Transmission may have succeeded: reconcile; NEVER blindly retry."""

    def __init__(self, client_order_id: str):
        self.client_order_id = client_order_id
        super().__init__(f"Order outcome unknown for {client_order_id}; reconciliation required")


class SequenceGap(VenueError):
    def __init__(self, subject: str, expected: int | None, received: int):
        self.subject, self.expected, self.received = subject, expected, received
        super().__init__(f"Snapshot required for {subject}: expected {expected}, received {received}")


class NativeTradingGate(BaseModel):
    """Configured by the central OMS; missing evidence always fails closed.

    Paper simulation uses a separate venue. Native sandbox writes also require
    explicit authorization and a completed preflight. Expiring evidence avoids
    leaving a native client permanently armed after account eligibility changes.
    """

    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    preflight_valid_until: float = 0
    account_authorized: bool = False
    geographic_eligibility_verified: bool = False
    risk_approval_id: str = ""
    durable_intent_id: str = ""

    def require(self, client_order_id: str, environment: str) -> None:
        if not self.enabled:
            raise PreflightRequired("Native trading is disabled")
        if self.preflight_valid_until <= time.time() or not self.account_authorized:
            raise PreflightRequired("A current account preflight is required")
        if environment == "production" and not self.geographic_eligibility_verified:
            raise PreflightRequired("Official production eligibility verification is required")
        if not self.risk_approval_id or self.durable_intent_id != client_order_id:
            raise PreflightRequired("Central risk approval and durable order intent are required")


class NativeSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    environment: Literal["sandbox", "production"] = "sandbox"
    timeout_seconds: float = Field(default=10, gt=0, le=60)
    requests_per_second: float = Field(default=2, gt=0, le=100)
    gate: NativeTradingGate = Field(default_factory=NativeTradingGate)


@dataclass
class SequenceTracker:
    """Contiguous sequences only for protocols that explicitly guarantee them."""

    values: dict[str, int] = field(default_factory=dict)
    invalid: set[str] = field(default_factory=set)

    def reset(self) -> None:
        self.values.clear()
        self.invalid.clear()

    def snapshot(self, subject: str, sequence: int) -> None:
        self.values[subject] = sequence
        self.invalid.discard(subject)

    def delta(self, subject: str, sequence: int) -> bool:
        previous = self.values.get(subject)
        if subject in self.invalid or previous is None or sequence != previous + 1:
            if previous is not None and sequence <= previous and subject not in self.invalid:
                return False
            self.invalid.add(subject)
            raise SequenceGap(subject, previous + 1 if previous is not None else None, sequence)
        self.values[subject] = sequence
        return True


def decimal_value(value: Any) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except InvalidOperation:
        raise ValueError("Invalid venue number") from None
    if not parsed.is_finite():
        raise ValueError("Non-finite venue number")
    return parsed


def json_body(body: dict | None) -> bytes:
    return b"" if body is None else json.dumps(body, separators=(",", ":"), allow_nan=False).encode()


class NativeTransport:
    """HTTP with bounded pacing, redacted failures, and no mutation retries."""

    venue_id = "native"

    def __init__(self, settings: NativeSettings, client: httpx.AsyncClient | None = None):
        self.settings = settings
        self.http = client or httpx.AsyncClient(timeout=settings.timeout_seconds)
        self._owns_client = client is None
        self._request_lock = asyncio.Lock()
        self._next_request = 0.0
        self._closed = False
        self._ws: Any = None
        self._write_lock = asyncio.Lock()
        self.request_count = 0
        self.error_count = 0
        self.reconnect_count = 0
        self.last_message_time: float | None = None
        self.health = "disconnected"

    async def disconnect(self) -> None:
        self._closed = True
        if self._ws is not None:
            await self._ws.close()
        if self._owns_client:
            await self.http.aclose()
        self.health = "disconnected"

    async def _request(self, request: httpx.Request, *, mutation_id: str | None = None) -> dict:
        async with self._request_lock:
            wait = self._next_request - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._next_request = time.monotonic() + 1 / self.settings.requests_per_second
            self.request_count += 1
            try:
                response = await self.http.send(request)
            except httpx.TransportError as exc:
                self.error_count += 1
                if mutation_id:
                    raise UnknownOrderOutcome(mutation_id) from None
                raise VenueError(f"{self.venue_id}: {type(exc).__name__}") from None
            if response.status_code >= 400:
                self.error_count += 1
                if response.status_code == 429:
                    try:
                        self._next_request = max(
                            self._next_request, time.monotonic() + min(float(response.headers.get("Retry-After", "5")), 300)
                        )
                    except ValueError:
                        self._next_request = time.monotonic() + 5
                if mutation_id and response.status_code >= 500:
                    raise UnknownOrderOutcome(mutation_id)
                # Preserve a bounded machine code for cancellation races, but
                # never log bodies, descriptions, auth headers, or request URLs.
                code = None
                try:
                    error = response.json()
                    candidate = error.get("code")
                    if candidate is None and isinstance(error.get("error"), dict):
                        candidate = error["error"].get("code")
                    if isinstance(candidate, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", candidate):
                        code = candidate
                except (ValueError, AttributeError):
                    pass
                raise VenueHTTPError(self.venue_id, response.status_code, code)
            if response.status_code == 204:
                return {}
            try:
                # Native venues can encode cash as JSON numbers. Parse their
                # original decimal text before any binary-float round trip.
                parsed = response.json(parse_float=Decimal)
            except ValueError:
                if mutation_id:
                    raise UnknownOrderOutcome(mutation_id) from None
                raise VenueError(f"{self.venue_id}: invalid JSON response") from None
            if not isinstance(parsed, dict):
                if mutation_id:
                    raise UnknownOrderOutcome(mutation_id)
                raise VenueError(f"{self.venue_id}: unexpected response envelope")
            return parsed

    async def _send_ws(self, payload: dict) -> None:
        if self._ws is None:
            raise VenueError("WebSocket is not connected")
        async with self._write_lock:
            await self._ws.send(json.dumps(payload, separators=(",", ":"), allow_nan=False))

    async def _backoff(self, failures: int) -> None:
        self.reconnect_count += 1
        self.health = "reconnecting"
        await asyncio.sleep(min(30.0, 0.5 * 2 ** min(failures, 6)) * random.uniform(0.8, 1.2))

    def health_check(self) -> dict:
        return {
            "state": self.health, "requests": self.request_count,
            "errors": self.error_count, "reconnects": self.reconnect_count,
            "last_message_time": self.last_message_time,
            "live_tested": False, "synthetic": False,
        }

    def get_capabilities(self) -> dict:
        return self.get_manifest()["capabilities"]
