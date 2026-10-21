"""Novig v3 signed REST and native streaming/trading protocol."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from typing import AsyncIterator
from urllib.parse import quote, quote_from_bytes, unquote_to_bytes
from uuid import UUID

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from pydantic import Field
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus

from .common import (
    CredentialsMissing, NativeSettings, NativeTransport, SequenceGap,
    SequenceTracker, UnknownOrderOutcome, VenueError, VenueHTTPError, decimal_value, json_body,
)


class NovigSettings(NativeSettings):
    api_key: str = Field(default="", repr=False)  # v3 key UUID, NOT a bearer token
    private_key_path: str = Field(default="", repr=False)


class SubscriptionSelectionError(VenueError):
    """Requested subscription identity could not be validated; never drop it."""


def canonical_query(query: str) -> str:
    pairs = []
    for pair in query.split("&"):
        if not pair:
            continue
        name, _, value = pair.partition("=")
        # Unlike application/x-www-form-urlencoded, a bare "+" is literal.
        pairs.append(tuple(quote_from_bytes(unquote_to_bytes(part), safe="-._~") for part in (name, value)))
    return "&".join(f"{name}={value}" for name, value in sorted(pairs))


def novig_canonical(timestamp: str, method: str, path: str, query: str = "", body: bytes = b"") -> bytes:
    return "\n".join(
        ("NOVIG-V3", timestamp, method.upper(), path, canonical_query(query), hashlib.sha256(body).hexdigest())
    ).encode()


def sign_novig(private_key, canonical: bytes) -> str:
    if isinstance(private_key, ed25519.Ed25519PrivateKey):
        signature = private_key.sign(canonical)
    elif isinstance(private_key, ec.EllipticCurvePrivateKey) and isinstance(private_key.curve, ec.SECP256R1):
        signature = private_key.sign(canonical, ec.ECDSA(hashes.SHA256()))
    else:
        raise ValueError("Novig supports Ed25519 or P-256 signing keys")
    return base64.b64encode(signature).decode()


class NovigBook:
    """L3 order book with atomic delta batches and per-market/channel sequences."""

    def __init__(self):
        self.sequences = SequenceTracker()
        self.books: dict[str, dict[str, dict]] = {}
        self.statuses: dict[str, str] = {}

    def reset(self):
        self.sequences.reset()
        self.books.clear()
        self.statuses.clear()

    @staticmethod
    def _identifier(row: dict, documented: str, observed: str) -> str:
        if not isinstance(row, dict):
            raise ValueError("Invalid Novig book row")
        values = [row[key] for key in (documented, observed) if key in row]
        if not values or any(not isinstance(value, str) or not value for value in values):
            raise ValueError("Invalid Novig book identifier")
        if len(values) == 2 and values[0] != values[1]:
            raise ValueError("Conflicting Novig book identifier aliases")
        return values[0]

    def apply(self, frame: dict) -> None:
        snapshot = "snapshot" in frame
        for market, channels in frame.get("snapshot" if snapshot else "delta", {}).items():
            # A newly opened market can include its first book delta alongside
            # OPEN. Seed its counters before processing that book delta.
            lifecycle = channels.get("lifecycle", {})
            if not snapshot and market not in self.statuses and lifecycle.get("deltas") == ["OPEN"]:
                self.sequences.snapshot(f"{market}:book", 0)
                self.books.setdefault(market, {})
            for channel in ("book", "lifecycle"):
                if channel not in channels:
                    continue
                data = channels[channel]
                subject = f"{market}:{channel}"
                if snapshot:
                    self.sequences.snapshot(subject, int(data["seq"]))
                elif channel == "lifecycle" and market not in self.statuses and data.get("deltas", []) == ["OPEN"]:
                    # Newly opened markets under event subscriptions have no snapshot.
                    self.sequences.snapshot(subject, int(data["seq"]))
                    self.books.setdefault(market, {})
                elif not self.sequences.delta(subject, int(data["seq"])):
                    continue
                if channel == "lifecycle":
                    if snapshot:
                        self.statuses[market] = data["status"]
                    else:
                        for transition in data["deltas"]:
                            if transition in {"OPEN", "CLOSE", "GRADE"}:
                                self.statuses[market] = {"OPEN": "OPEN", "CLOSE": "CLOSED", "GRADE": "SETTLED"}[transition]
                            if transition in {"GOLIVE", "UNLIVE", "CLOSE", "GRADE"}:
                                self.books[market] = {}
                    continue
                if snapshot:
                    try:
                        rebuilt = {
                            self._identifier(row, "order", "orderId"): {
                                "outcome": outcome, "price": decimal_value(row["price"]), "qty": int(row["qty"])}
                            for outcome, rows in data["orders"].items() for row in rows
                        }
                    except (KeyError, TypeError, ValueError):
                        self.sequences.invalid.add(subject)
                        raise
                    if any(not 0 < row["price"] < 1 or row["qty"] <= 0 for row in rebuilt.values()):
                        self.sequences.invalid.add(subject)
                        raise ValueError("Invalid Novig snapshot level")
                    self.books[market] = rebuilt
                else:
                    # Validate a whole batch before publishing any changed book.
                    updated = deepcopy(self.books.get(market, {}))
                    try:
                        for delta in data["deltas"]:
                            order_id = self._identifier(delta, "order", "orderId")
                            if delta["kind"] == "remove":
                                if order_id not in updated:
                                    self.sequences.invalid.add(subject)
                                    raise SequenceGap(subject, None, int(data["seq"]))
                                del updated[order_id]
                            elif delta["kind"] == "add":
                                price, qty = decimal_value(delta["price"]), int(delta["qty"])
                                if not 0 < price < 1 or qty <= 0:
                                    raise ValueError("Invalid Novig book level")
                                outcome = self._identifier(delta, "outcome", "outcomeId")
                                updated[order_id] = {"outcome": outcome, "price": price, "qty": qty}
                            else:
                                raise ValueError("Unknown Novig book delta")
                    except (KeyError, TypeError, ValueError):
                        self.sequences.invalid.add(subject)
                        raise
                    self.books[market] = updated
        for channel in ("orders", "positions"):
            if channel not in frame:
                continue
            data = frame[channel]
            subject = f"PRIVATE:{channel}"
            if snapshot:
                self.sequences.snapshot(subject, int(data["seq"]))
            else:
                self.sequences.delta(subject, int(data["seq"]))
        # Private heartbeat sequence proves whether a private frame was dropped.
        for channel, seq in frame.get("heartbeat", {}).items():
            if channel not in {"orders", "positions"}:
                continue
            subject = f"PRIVATE:{channel}"
            previous = self.sequences.values.get(subject)
            if previous is not None and int(seq) > previous:
                self.sequences.invalid.add(subject)
                raise SequenceGap(subject, previous + 1, int(seq))

    def asks(self, market: str, opposing_outcome: str) -> list[tuple[Decimal, int]]:
        if f"{market}:book" in self.sequences.invalid or self.statuses.get(market) != "OPEN":
            return []
        totals: dict[Decimal, int] = {}
        for order in self.books.get(market, {}).values():
            if order["outcome"] == opposing_outcome:
                price = Decimal(1) - order["price"]
                totals[price] = totals.get(price, 0) + order["qty"]
        return sorted(totals.items())


class NovigClient(NativeTransport):
    venue_id = "novig"
    HOSTS = {"sandbox": "https://api.paper.novig.com", "production": "https://api.novig.com"}

    def __init__(self, settings: NovigSettings | None = None, client: httpx.AsyncClient | None = None):
        super().__init__(settings or NovigSettings(), client)
        self._key = None
        self._nonce = 0
        self.book = NovigBook()
        self.selection: dict = {"private": ["orders", "positions"]}
        self._configured_market_ids: tuple[str, ...] | None = None
        self._excluded_settled_markets: set[str] = set()
        self._pending: dict[int, asyncio.Future] = {}
        self.authenticated = False
        self.health_reason = "Novig is disconnected"
        self.last_error_type: str | None = None
        self.received_close_code: int | None = None
        self.sent_close_code: int | None = None

    def _load_key(self):
        if not self.settings.api_key or not self.settings.private_key_path:
            self.health = "credentials_missing"
            raise CredentialsMissing("Novig v3 requires a key UUID and Ed25519/P-256 private key file")
        if self._key is None:
            self._key = serialization.load_pem_private_key(
                Path(self.settings.private_key_path).read_bytes(), password=None
            )
        return self._key

    def headers(self, method: str, path: str, query="", body=b"", timestamp=None) -> dict:
        timestamp = timestamp or str(time.time_ns() // 1_000_000)
        return {
            "Novig-Key-Id": self.settings.api_key, "Novig-Timestamp": timestamp,
            "Novig-Signature": sign_novig(self._load_key(), novig_canonical(timestamp, method, path, query, body)),
            "Content-Type": "application/json",
        }

    async def request(self, method: str, path: str, *, params=None, body=None, mutation_id=None):
        raw_body = json_body(body)
        request = self.http.build_request(method, self.HOSTS[self.settings.environment] + path,
                                          params=params, content=raw_body)
        raw_path = request.url.raw_path.decode("ascii").split("?")[0]
        request.headers.update(self.headers(method, raw_path, request.url.query.decode("ascii"), raw_body))
        return await self._request(request, mutation_id=mutation_id)

    async def connect(self) -> dict:
        result = await self.request("POST", "/v3/echo", body={"probe": "evplatform"})
        self.authenticated = True
        self.health = "authenticated"
        self.health_reason = "Novig REST signature verified; WebSocket subscription not yet confirmed"
        return result

    async def disconnect(self) -> None:
        await super().disconnect()
        self.authenticated = False
        self.health_reason = "Novig is disconnected"

    def health_check(self) -> dict:
        return {**super().health_check(), "authenticated": self.authenticated,
                "streaming": self.health == "streaming" and self._ws is not None,
                "orderbook_markets": len(self.book.books),
                "orderbook_orders": sum(len(orders) for orders in self.book.books.values()),
                "invalid_book_subjects": len(self.book.sequences.invalid),
                "requested_monitor_markets": len(self._configured_market_ids or ()),
                "excluded_settled_markets": len(self._excluded_settled_markets),
                "reason": self.health_reason, "last_error_type": self.last_error_type,
                "received_close_code": self.received_close_code, "sent_close_code": self.sent_close_code}

    def _invalidate_stream(self, state: str, reason: str) -> dict:
        self.error_count += 1
        self.book.reset()
        self.health, self.health_reason = state, reason
        # Only local classifications are emitted; never expose response bodies or headers.
        return {"type": "transport_invalidated", "venue": self.venue_id,
                "reason": state, "all_books_invalid": True, "reconcile_private": True}

    async def discover_events(self, **params) -> dict:
        return await self.request("GET", "/v3/catalog/events", params=params)

    async def discover_markets(self, **params) -> dict:
        return await self.request("GET", "/v3/catalog/markets", params=params)

    async def reconcile_order(self, order_id: str) -> dict:
        return await self.request("GET", f"/v3/orders/{quote(order_id, safe='')}")

    async def reconcile_account(self, **params) -> dict:
        return await self.request("GET", "/v3/orders", params=params)

    async def fetch_fills(self, **params) -> dict:
        return await self.request("GET", "/v3/portfolio/fills", params=params)

    @staticmethod
    def order_body(outcome_id: str, price: Decimal, quantity: int, client_order_id: str, tif="IOC") -> dict:
        price = decimal_value(price)
        milli = price * 1000
        if milli != milli.to_integral_value() or not 1 <= milli <= 999:
            raise ValueError("Novig price is outside its three-decimal grid")
        if 50 < milli < 950 and int(milli) % 5:
            raise ValueError("Novig middle price band requires increments of 0.005")
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
            raise ValueError("Novig quantity must be positive whole 1-cent contracts")
        if tif not in {"GTC", "IOC", "FOK", "PO"}:
            raise ValueError("Unsupported Novig order time in force")
        UUID(outcome_id)
        UUID(client_order_id)
        return {"outcomeId": outcome_id, "price": format(price, ".3f"),
                "qty": quantity, "clientId": client_order_id, "tif": tif}

    async def submit_order(self, body: dict) -> dict:
        client_id = body.get("clientId", "")
        self.settings.gate.require(client_id, self.settings.environment)
        return await self.request("POST", "/v3/orders", body=body, mutation_id=client_id)

    async def cancel_order(self, order_id: str) -> dict:
        return await self.request("DELETE", f"/v3/orders/{quote(order_id, safe='')}", mutation_id=order_id)

    async def _command(self, verb: str, payload, *, mutation_id: str | None = None, wait=True):
        if self._ws is None:
            raise VenueError("Novig stream must be running before sending WebSocket commands")
        self._nonce += 1
        nonce = self._nonce
        future = asyncio.get_running_loop().create_future()
        if wait:
            self._pending[nonce] = future
        try:
            await self._send_ws({"nonce": nonce, verb: payload})
            if not wait:
                return nonce
            return await asyncio.wait_for(future, self.settings.timeout_seconds)
        except (TimeoutError, ConnectionClosed, OSError, VenueError):
            if mutation_id:
                raise UnknownOrderOutcome(mutation_id) from None
            raise VenueError("Novig command requires stream reconciliation") from None
        finally:
            self._pending.pop(nonce, None)

    async def submit_ws(self, body: dict) -> dict:
        client_id = body.get("clientId", "")
        self.settings.gate.require(client_id, self.settings.environment)
        return await self._command("place", {"orders": [body]}, mutation_id=client_id)

    async def cancel_ws(self, order_id: str) -> dict:
        return await self._command("cancel", {"orderIds": [order_id]}, mutation_id=order_id)

    async def fetch_snapshot(self, market_id: str) -> dict:
        return await self._command("snapshot", {"markets": {market_id: "book"}})

    async def subscribe(self, market_ids: list[str]):
        self.selection["markets"] = {market: "book" for market in market_ids}
        if self._ws:
            await self._command("subscribe", self.selection, wait=False)

    async def configure_subscriptions(self, config: dict):
        """Keep requested scope; only history-confirmed settled IDs are skipped.

        Validation runs before every connection attempt, outside the pricing
        path. We never discover replacement markets or alter persisted config.
        """
        requested = config.get("market_ids", [])
        if not isinstance(requested, list) or len(requested) > 100:
            raise ValueError("Novig monitor accepts at most 100 explicitly requested markets")
        for market in requested:
            if not isinstance(market, str):
                raise ValueError("Novig monitor market IDs must be UUID strings")
            UUID(market)
        self._configured_market_ids = tuple(dict.fromkeys(requested))
        self._excluded_settled_markets.clear()

    async def _refresh_configured_subscriptions(self):
        if self._configured_market_ids is None:
            return
        active, settled = {}, set()
        try:
            # Bounded request count, time and existing per-client quota pacing.
            async with asyncio.timeout(60):
                for market in self._configured_market_ids:
                    try:
                        row = await self.request("GET", f"/v3/catalog/markets/{quote(market, safe='')}")
                    except VenueHTTPError as exc:
                        if exc.status_code != 404 or exc.code != "MARKET_NOT_FOUND":
                            raise
                        row = await self.request("GET", f"/v3/history/markets/{quote(market, safe='')}")
                        if row.get("marketId") == market and row.get("status") == "SETTLED":
                            settled.add(market)
                            continue
                        raise SubscriptionSelectionError("Missing market is not confirmed settled")
                    if row.get("marketId") != market or row.get("status") not in {"OPEN", "CLOSED"}:
                        raise SubscriptionSelectionError("Catalog market identity/status is not verified")
                    active[market] = "book"
        except (VenueError, ValueError, TypeError, AttributeError, TimeoutError) as exc:
            if isinstance(exc, VenueHTTPError) and exc.status_code == 401:
                self.authenticated = False
            # Preserve the complete previous selection on any uncertain read.
            raise SubscriptionSelectionError("Novig requested markets could not be validated; no unknown IDs were removed") from None
        self.selection["markets"] = active
        self._excluded_settled_markets = settled

    async def unsubscribe(self, subjects: list[str]):
        for subject in subjects:
            if subject.startswith("market:"):
                self.selection.get("markets", {}).pop(subject[7:], None)
        await self._command("unsubscribe", subjects, wait=False)

    async def stream_updates(self) -> AsyncIterator[dict]:
        self._load_key()
        failures = 0
        while not self._closed:
            self.book.reset()
            self._nonce = 0
            try:
                await self._refresh_configured_subscriptions()
                url = self.HOSTS[self.settings.environment].replace("https://", "wss://") + "/v3/ws"
                async with ws_connect(url, additional_headers=self.headers("GET", "/v3/ws"),
                                      ping_interval=15, max_size=16 * 1024 * 1024) as socket:
                    self.authenticated = True
                    self._ws, self.health = socket, "subscribing"
                    self.health_reason = "Waiting for Novig WebSocket subscription acknowledgement"
                    subscription_nonce = await self._command("subscribe", self.selection, wait=False)
                    async for raw in socket:
                        frame = json.loads(raw, parse_float=Decimal)
                        if not isinstance(frame, dict):
                            raise ValueError("Novig frame must be an object")
                        self.last_message_time = time.time()
                        nonce = frame.get("nonce")
                        if self.health == "subscribing" and nonce in {None, subscription_nonce}:
                            if "error" in frame or "code" in frame:
                                error = frame.get("error")
                                code = error.get("code") if isinstance(error, dict) else frame.get("code", error)
                                if code == "RATE_LIMIT_EXCEEDED":
                                    raise VenueError("Novig subscription temporarily throttled")
                                if code == "MARKET_NOT_FOUND" and self._configured_market_ids is not None:
                                    before = dict(self.selection.get("markets", {}))
                                    await self._refresh_configured_subscriptions()
                                    if self.selection.get("markets", {}) != before:
                                        raise VenueError("A requested market settled during subscription; reconnect with confirmed active scope")
                                yield self._invalidate_stream(
                                    "subscription_rejected", "Novig rejected the WebSocket subscription; check requested channels and key permissions")
                                return
                            if nonce == subscription_nonce and (
                                not isinstance(frame.get("subscribed"), dict)
                                or not isinstance(frame.get("snapshot"), dict)
                            ):
                                raise VenueError("Novig subscription acknowledgement is missing its snapshot")
                        self.book.apply(frame)
                        if nonce == subscription_nonce and "subscribed" in frame:
                            self.health = "streaming"
                            self.health_reason = "Novig WebSocket subscription confirmed"
                        if nonce in self._pending and not self._pending[nonce].done():
                            self._pending[nonce].set_result(frame)
                        failures = 0
                        yield frame
                    raise VenueError("Novig stream closed; reconcile and resubscribe")
            except SubscriptionSelectionError:
                yield self._invalidate_stream("subscription_rejected",
                    "Novig monitor validation failed; unknown or inaccessible requested markets need review")
                return
            except (OSError, TimeoutError, ConnectionClosed, InvalidHandshake, VenueError,
                    ValueError, KeyError, TypeError, AttributeError) as exc:
                self.last_error_type = type(exc).__name__
                self.received_close_code = int(exc.rcvd.code) if isinstance(exc, ConnectionClosed) and exc.rcvd else None
                self.sent_close_code = int(exc.sent.code) if isinstance(exc, ConnectionClosed) and exc.sent else None
                status = exc.response.status_code if isinstance(exc, InvalidStatus) else None
                if status in {401, 403, 451}:
                    if status == 401:
                        self.authenticated = False
                    state, reason = {
                        401: ("unauthorized", "Novig WebSocket authentication failed (HTTP 401); check key, environment, signature, and clock"),
                        403: ("access_denied", "Novig WebSocket access denied (HTTP 403); this endpoint requires a trading or trading::read key"),
                        451: ("geolocation_required", "Novig WebSocket location check failed (HTTP 451); review eligibility in the official Novig app"),
                    }[status]
                    yield self._invalidate_stream(state, reason)
                    return
                yield self._invalidate_stream("reconnecting", "Novig WebSocket interrupted; reconnecting and resubscribing")
                if not self._closed:
                    await self._backoff(failures)
                    failures += 1
            finally:
                self._ws = None
                if self.health in {"streaming", "subscribing"}:
                    self.health = "disconnected"
                    self.health_reason = "Novig WebSocket disconnected"
                for future in self._pending.values():
                    if not future.done():
                        future.set_exception(VenueError("Disconnected; reconcile before resubmission"))

    @staticmethod
    def get_manifest() -> dict:
        return {
            "id": "novig", "name": "Novig Native v3", "version": "1.0.0", "type": "execution",
            "contract_version": "1.0", "sports": ["discovered"], "markets": ["discovered"],
            "input_schemas": ["order.intent.v1"], "output_schemas": ["native.novig.v3"],
            "transports": ["rest", "websocket"], "authentication": "NOVIG-V3 Ed25519 or P-256",
            "credentials": ["NOVIG_API_KEY", "NOVIG_PRIVATE_KEY_PATH"],
            "capabilities": {"discovery": True, "orderbook": True, "private_fills": True,
                             "limit_orders": True, "cancel": True, "reconcile": True, "sandbox": True,
                             "websocket_trading": True, "streaming": True, "polling": True,
                             "payout_usd": "0.01", "quantity_unit": "whole_cent_contracts",
                             "client_id_deduplicated_by_exchange": False, "native_live_tested": False},
            "permissions": ["network", "secret:novig", "orders:conditional"],
            "health_check": True, "rate_limits": {"local_default_rps": 2, "separate_buckets": ["read", "place", "cancel", "stream"]},
            "config_schema": {"environment": {"type": "string", "enum": ["sandbox", "production"], "default": "sandbox"}},
            "docs": "https://docs.novig.com/api/streaming/connection",
        }
