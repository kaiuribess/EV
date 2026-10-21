"""Small, manual, read-only balance observations for dashboard display.

This service deliberately has no OMS, launch-profile, risk-engine or account-
snapshot dependency. Reported balances cannot become Kelly sizing funds. It
uses existing authenticated monitor clients and never connects, changes account
scope, discovers inventory, submits orders, or transfers money.
"""
from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable, Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any, Awaitable, Literal
from urllib.parse import quote
from uuid import UUID

from pydantic import field_validator

from .models import Contract, utcnow


VENUES = ("kalshi", "novig", "prophetx", "polymarket_us")


class MonitorSourceClock(Contract):
    source_clock_status: Literal["missing", "invalid", "future", "valid"] = "missing"
    source_updated_at: datetime | None = None

    @field_validator("source_updated_at")
    @classmethod
    def aware_clock(cls, value):
        if value is not None and value.utcoffset() is None:
            raise ValueError("Source clocks need a timezone")
        return value


class MonitorCashObservation(Contract):
    """Stable, secret-free output contract for registered read-only cash readers."""
    venue: str
    environment: Literal["sandbox", "production"]
    money_type: Literal["real", "play"]
    reported_balance_usd: Decimal
    reported_buying_power_usd: Decimal | None = None
    reported_exposure_credit_usd: Decimal | None = None
    reported_matched_order_balance_usd: Decimal | None = None
    reported_unmatched_order_balance_usd: Decimal | None = None
    reported_balance_reservation_usd: Decimal | None = None
    reported_unsettled_funds_usd: Decimal | None = None
    observed_at: datetime
    request_started_at: datetime
    identity_verified: bool = False
    identity_status: Literal["authenticated_read_only", "wallet_key_verified", "signed_private_session"] = "authenticated_read_only"
    account_scope: str
    cash_semantics: Literal["reported_balance"] = "reported_balance"
    cash_available_verified: Literal[False] = False
    available_for_kelly: Literal[False] = False
    read_only: Literal[True] = True
    source_clock_status: Literal["missing", "invalid", "future", "valid"] = "missing"
    source_updated_at: datetime | None = None
    unmatched_balance_synchronized: bool | None = None
    unmatched_balance_clock: MonitorSourceClock | None = None

    @field_validator("reported_balance_usd", "reported_buying_power_usd", "reported_exposure_credit_usd",
                     "reported_matched_order_balance_usd", "reported_unmatched_order_balance_usd",
                     "reported_balance_reservation_usd", "reported_unsettled_funds_usd", mode="before")
    @classmethod
    def exact_money(cls, value):
        return None if value is None else _money(value)

    @field_validator("observed_at", "request_started_at", "source_updated_at")
    @classmethod
    def aware_clock(cls, value):
        if value is not None and value.utcoffset() is None:
            raise ValueError("Monitor clocks need a timezone")
        return value


def _money(value) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ValueError("Exact balance required")
    amount = Decimal(value)
    if not amount.is_finite() or amount < 0:
        raise ValueError("Finite nonnegative balance required")
    return str(amount)


def _object(value):
    if not isinstance(value, dict):
        raise ValueError("Balance object required")
    return value


def _clock(value, now):
    if value is None:
        return {"source_clock_status": "missing", "source_updated_at": None}
    if not isinstance(value, str):
        return {"source_clock_status": "invalid", "source_updated_at": None}
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.utcoffset() is None:
            raise ValueError("Timezone required")
    except ValueError:
        return {"source_clock_status": "invalid", "source_updated_at": None}
    return {"source_clock_status": "future" if stamp > now else "valid",
            "source_updated_at": stamp.isoformat()}


def _binding(client):
    """In-memory fence only; neither key identifiers nor private user IDs escape."""
    settings = getattr(client, "settings", None)
    return (id(client), getattr(client, "venue_id", None), getattr(settings, "environment", None),
            getattr(settings, "api_key_id", None), getattr(settings, "api_key", None),
            getattr(client, "_authenticated_user_id", None))


def _authenticated(client, venue):
    if (client is None or getattr(client, "venue_id", None) != venue
            or getattr(getattr(client, "settings", None), "environment", None) not in {"sandbox", "production"}):
        return False
    try:
        return client.health_check().get("authenticated") is True
    except Exception:
        return False


async def observe_monitor_cash(client, *, now: Callable[[], datetime] = utcnow) -> dict:
    """One balance read. Authentication is required beforehand; no auto-login call."""
    venue = getattr(client, "venue_id", None)
    if venue not in VENUES or not _authenticated(client, venue):
        raise ValueError("Already authenticated supported monitor required")
    environment = client.settings.environment
    if venue == "polymarket_us" and environment != "production":
        raise ValueError("No personal retail sandbox is configured")
    started, binding = now(), _binding(client)
    result = {"venue": venue, "environment": environment, "money_type": "real" if environment == "production" else "play",
              "identity_verified": False, "identity_status": "authenticated_read_only",
              "cash_semantics": "reported_balance", "cash_available_verified": False,
              "available_for_kelly": False, "read_only": True,
              "source_clock_status": "missing", "source_updated_at": None,
              "request_started_at": started.isoformat()}
    if venue == "novig":
        key = client.settings.api_key
        UUID(key)
        raw = _object(await client.request("GET", f"/v3/account/subaccounts/{quote(key, safe='')}/balance"))
        if raw.get("keyId") != key:
            raise ValueError("Wallet identity mismatch")
        result.update(reported_balance_usd=_money(raw.get("balance")), identity_verified=True,
                      identity_status="wallet_key_verified", account_scope="Configured trading-key wallet")
    elif venue == "kalshi":
        raw = _object(await client.request("GET", "/portfolio/balance", params={"subaccount": 0}))
        result.update(reported_balance_usd=_money(raw.get("balance_dollars")),
                      account_scope="Primary subaccount 0 requested; full identity not verified")
    elif venue == "prophetx":
        row = _object(_object(await client.fetch_balance()).get("data"))
        result.update(reported_balance_usd=_money(row.get("balance")),
                      account_scope="Connected monitor session; reported balance may include exposure credits")
        for field, output in (("gec_balance", "reported_exposure_credit_usd"),
                              ("matched_order_balance", "reported_matched_order_balance_usd"),
                              ("unmatched_order_balance", "reported_unmatched_order_balance_usd")):
            if field in row:
                result[output] = _money(row[field])
        # This timestamp only covers unmatched-order synchronization, not cash.
        result["unmatched_balance_synchronized"] = row.get("unmatched_order_balance_status") == "succeed"
        result["unmatched_balance_clock"] = _clock(row.get("unmatched_order_last_synced_at"), now())
        if getattr(client, "_signin_confirmed", False) is True and isinstance(binding[-1], str) and binding[-1].strip():
            result.update(identity_verified=True, identity_status="signed_private_session")
    else:
        rows = _object(await client.account_balances()).get("balances")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError("Balance collection required")
        usd = [row for row in rows if row.get("currency") == "USD"]
        if len(usd) != 1:
            raise ValueError("Exactly one USD balance required")
        row = usd[0]
        result.update(reported_balance_usd=_money(row.get("currentBalance")),
                      account_scope="Personal retail API; stable cash account identity unverified",
                      **_clock(row.get("lastUpdated"), now()))
        for field, output in (("buyingPower", "reported_buying_power_usd"),
                              ("balanceReservation", "reported_balance_reservation_usd"),
                              ("unsettledFunds", "reported_unsettled_funds_usd")):
            if field in row:
                result[output] = _money(row[field])
    if binding != _binding(client) or not _authenticated(client, venue):
        raise ValueError("Monitor identity changed during balance read")
    result["observed_at"] = now().isoformat()
    return result


class MonitorCashService:
    def __init__(self, client_supplier: Callable[[], Mapping[str, Any]], *, min_refresh_seconds=30.0,
                 observation_max_age_seconds=60.0, request_timeout_seconds=10.0,
                 now: Callable[[], datetime] = utcnow, monotonic: Callable[[], float] = time.monotonic):
        if not (1 <= min_refresh_seconds <= 3600 and 1 <= observation_max_age_seconds <= 3600
                and 0 < request_timeout_seconds <= 30):
            raise ValueError("Bounded monitor refresh settings required")
        self._clients, self._now, self._monotonic = client_supplier, now, monotonic
        self.min_refresh_seconds, self.observation_max_age_seconds = min_refresh_seconds, observation_max_age_seconds
        self.request_timeout_seconds = request_timeout_seconds
        self._observations, self._bindings, self._attempts, self._errors = {}, {}, {}, {}
        self._locks, self._readers = {}, {}
        for venue in VENUES:
            self.register_reader(venue, lambda client: observe_monitor_cash(client, now=self._now))

    def register_reader(self, venue: str, reader: Callable[[Any], Awaitable[dict | MonitorCashObservation]]) -> None:
        """Plugin registration; no core/UI edit needed for another cash venue.

        Reader must use only documented account GETs on the supplied authenticated
        client and return MonitorCashObservation. It cannot certify available
        cash or trading permission; no risk/OMS object is supplied to it.
        """
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", venue) or not callable(reader) or venue in self._readers:
            raise ValueError("A unique venue ID and callable cash reader are required")
        self._readers[venue], self._locks[venue] = reader, asyncio.Lock()

    def state(self) -> dict:
        clients, now, tick = self._clients(), self._now(), self._monotonic()
        accounts = {}
        for venue in self._readers:
            client = clients.get(venue)
            configured_environment = getattr(getattr(client, "settings", None), "environment", None)
            environment = configured_environment if configured_environment in {"production", "sandbox"} else None
            authenticated = _authenticated(client, venue)
            current_binding = _binding(client) if client else None
            observation = self._observations.get(venue)
            binding_matches = observation is not None and self._bindings.get(venue) == current_binding
            attempt = self._attempts.get(venue)
            cooldown = max(0.0, self.min_refresh_seconds - (tick-attempt[1])) if attempt and attempt[0] == current_binding else 0.0
            row = {"venue": venue, "environment": environment,
                   "money_type": "real" if environment == "production" else "play" if environment == "sandbox" else "unknown",
                   "status": "never_observed" if authenticated else "not_connected",
                   "reported_balance_usd": None, "observed_at": None, "age_seconds": None,
                   "identity_verified": False, "identity_status": "unknown",
                   "cash_available_verified": False, "available_for_kelly": False, "read_only": True,
                   "refreshing": self._locks[venue].locked(), "refresh_after_seconds": cooldown,
                   "refresh_eligible": authenticated and not cooldown and not self._locks[venue].locked()}
            if binding_matches:
                row.update(observation)
                age = (now-datetime.fromisoformat(observation["observed_at"])).total_seconds()
                row["age_seconds"] = age
                row["status"] = "observed" if 0 <= age <= self.observation_max_age_seconds else "stale"
                if not authenticated:
                    row.update(status="disconnected", identity_verified=False, identity_status="disconnected_since_observation")
                error = self._errors.get(venue)
                if error and error[0] == current_binding:
                    row.update(status="refresh_failed", last_refresh_error=error[1])
            elif observation is not None:
                row["status"] = "client_changed" if client else "not_connected"
            error = self._errors.get(venue)
            if error and error[0] == current_binding and not binding_matches:
                row.update(status="refresh_failed", last_refresh_error=error[1])
            accounts[venue] = row
        return {"schema_version": 1, "accounts": accounts, "read_only": True, "available_for_kelly": False,
                "observed_at": now.isoformat(),
                "note": "Last reported account balances only. No inventory reconciliation, available-cash certification, or trading authorization."}

    async def refresh(self, venue: str | None = None) -> dict:
        if venue is not None and venue not in self._readers:
            raise ValueError("Unsupported cash monitor venue")
        await asyncio.gather(*(self._refresh_one(name) for name in ((venue,) if venue else self._readers)))
        return self.state()

    async def _refresh_one(self, venue):
        if self._locks[venue].locked():
            return
        async with self._locks[venue]:
            client = self._clients().get(venue)
            if not _authenticated(client, venue):
                return
            binding, tick = _binding(client), self._monotonic()
            prior = self._attempts.get(venue)
            if prior and prior[0] == binding and tick-prior[1] < self.min_refresh_seconds:
                return
            self._attempts[venue] = (binding, tick)
            try:
                raw = await asyncio.wait_for(self._readers[venue](client), self.request_timeout_seconds)
                observation = MonitorCashObservation.model_validate(raw).model_dump(mode="json", exclude_none=True)
                current = self._clients().get(venue)
                if current is not client or _binding(current) != binding or not _authenticated(current, venue):
                    return
                stamp = datetime.fromisoformat(observation["observed_at"])
                started = datetime.fromisoformat(observation["request_started_at"])
                if (observation["venue"] != venue or observation["environment"] != client.settings.environment
                        or observation["money_type"] != ("real" if client.settings.environment == "production" else "play")
                        or stamp > self._now() or started > stamp):
                    raise ValueError("Cash reader observation scope or clock mismatch")
                self._observations[venue], self._bindings[venue] = observation, binding
                self._errors.pop(venue, None)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Never echo response bodies, headers, IDs, exception messages,
                # unrecognized server codes, or local private-key paths.
                category = "timeout" if isinstance(exc, TimeoutError) else "invalid_balance" if isinstance(exc, (ValueError, ArithmeticError)) else "read_failed"
                self._errors[venue] = (binding, category)
