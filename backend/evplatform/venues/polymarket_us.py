"""Personal Polymarket US retail API monitor; deliberately no order operations.

This is not the international wallet/CLOB API or the institutional API. Public
discovery is unsigned; account REST and both WebSockets use Ed25519 signatures.
Native payloads stay backend-only; canonical pricing needs separately reviewed
mappings. Only the separate risk-gated executor can issue order operations.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import re
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import AsyncIterator, Literal
from uuid import UUID, uuid4

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus

from .common import CredentialsMissing, NativeSettings, NativeTransport, VenueError, VenueHTTPError


def validated_slugs(values: list[str]) -> list[str]:
    if len(values) > 50 or any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,249}", value) for value in values):
        raise ValueError("Use at most 50 discovered Polymarket US market slugs")
    return list(dict.fromkeys(values))


class PolymarketUSMonitorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    environment: Literal["production"] = "production"
    market_slugs: list[str] = Field(default_factory=list, description="Discovered US market slugs; empty monitors only the account")
    private_updates: bool = True
    discovery_leagues: list[str] = Field(default_factory=list, max_length=4,
        description="Public league catalogs to monitor for current open moneylines; empty disables discovery")
    discovery_interval_seconds: int = Field(default=300, ge=60, le=3600)
    max_discovered_markets: int = Field(default=8, ge=1, le=50)

    @field_validator("market_slugs")
    @classmethod
    def check_slugs(cls, values):
        return validated_slugs(values)

    @field_validator("discovery_leagues")
    @classmethod
    def check_leagues(cls, values):
        if len(set(values)) != len(values) or any(not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,31}", value)
                                             for value in values):
            raise ValueError("Use distinct public Polymarket US league slugs")
        return values

    @model_validator(mode="after")
    def bounded_combined_scope(self):
        if self.discovery_leagues and len(self.market_slugs) + self.max_discovered_markets > 50:
            raise ValueError("Explicit and discovered Polymarket US market caps must total at most 50")
        return self


class PolymarketUSSettings(NativeSettings):
    # No documented personal retail sandbox is assumed. The plugin is disabled
    # by default and must be explicitly enabled with production configuration.
    environment: Literal["production"] = "production"
    api_key_id: str = Field(default="", repr=False, exclude=True)
    secret_key: str = Field(default="", repr=False, exclude=True)
    requests_per_second: float = Field(default=2, gt=0, le=10)


def alias(frame: dict, camel: str, snake: str):
    """Both forms appear in official docs; conflicting aliases are invalid."""
    if camel in frame and snake in frame and frame[camel] != frame[snake]:
        raise VenueError("Polymarket US returned conflicting field aliases")
    return frame.get(camel, frame.get(snake))


class PolymarketUSClient(NativeTransport):
    venue_id = "polymarket_us"
    PUBLIC_HOST = "https://gateway.polymarket.us"
    PRIVATE_HOST = "https://api.polymarket.us"
    WS_HOST = "wss://api.polymarket.us"

    def __init__(self, settings: PolymarketUSSettings | None = None, client: httpx.AsyncClient | None = None):
        super().__init__(settings or PolymarketUSSettings(), client)
        self._key = None
        self.authenticated = False
        self.reason = "Read-only personal US API monitor; native execution is unsupported"
        self.market_slugs: list[str] = []
        self.private_updates = True
        self.account_snapshot: dict | None = None
        self.books: dict[str, dict] = {}
        self.book_received_at: dict[str, float] = {}
        self.rest_books: dict[str, dict] = {}
        self.rest_book_received_at: dict[str, float] = {}
        self._public_sdk = None
        self._private_sdk = None
        self.market_messages = 0
        self.private_messages = 0
        self.heartbeat_messages = 0
        self.unknown_messages = 0
        self._sockets: dict = {}
        self._ready: dict[str, bool] = {}
        self._subscriptions: dict[str, dict[str, str]] = {}
        self._subscription_evidence: dict[str, dict[str, str]] = {}

    def _load_key(self):
        if not self.settings.api_key_id or not self.settings.secret_key:
            self.health = "credentials_missing"
            raise CredentialsMissing("Polymarket US requires its personal API key ID and secret")
        if self._key is None:
            try:
                UUID(self.settings.api_key_id)
                raw = base64.b64decode(self.settings.secret_key, validate=True)
                if len(raw) not in {32, 64}:
                    raise ValueError("Invalid signing key length")
                self._key = Ed25519PrivateKey.from_private_bytes(raw[:32])
            except (ValueError, binascii.Error):
                self.health = "unauthorized"
                raise CredentialsMissing("Invalid Polymarket US API credentials") from None
        return self._key

    def headers(self, method: str, path: str, timestamp: str | None = None) -> dict:
        # Restrict the signing surface itself to read-only documented routes.
        if method.upper() != "GET" or not self._allowed_path(path):
            raise VenueError("Polymarket US monitor permits documented read-only requests only")
        timestamp = timestamp or str(time.time_ns() // 1_000_000)
        signature = self._load_key().sign(f"{timestamp}GET{path}".encode())
        return {"X-PM-Access-Key": self.settings.api_key_id, "X-PM-Timestamp": timestamp,
                "X-PM-Signature": base64.b64encode(signature).decode()}

    @staticmethod
    def _allowed_path(path):
        return path in {"/v1/account/balances", "/v1/portfolio/positions", "/v1/portfolio/activities",
                        "/v1/orders/open", "/v1/rfqs/user-id", "/v1/ws/private", "/v1/ws/markets"} or bool(
                            re.fullmatch(r"/v1/order/[A-Za-z0-9_-]{1,128}", path))

    async def _get(self, path: str, *, private=False, params=None):
        if private and not self._allowed_path(path):
            raise VenueError("Unsupported private monitoring route")
        if not private and path != "/v1/markets" and not re.fullmatch(
                r"/v1/markets/[A-Za-z0-9][A-Za-z0-9_-]{0,249}/book|/v2/leagues/[a-z0-9][a-z0-9-]{0,31}/events", path):
            raise VenueError("Unsupported public monitoring route")
        host = self.PRIVATE_HOST if private else self.PUBLIC_HOST
        request = self.http.build_request("GET", host + path, params=params,
                                          headers=self.headers("GET", path) if private else {})
        return await self._request(request)

    async def account_balances(self):
        response = await self._get("/v1/account/balances", private=True)
        if not isinstance(response.get("balances"), list) or any(not isinstance(row, dict) for row in response["balances"]):
            raise VenueError("Invalid Polymarket US account balance envelope")
        return response

    async def positions(self, **params):
        # Raw account evidence only: this does not claim a complete risk inventory.
        return await self._get("/v1/portfolio/positions", private=True, params=params)

    async def reconcile_order(self, order_id: str):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", order_id):
            raise ValueError("Invalid exchange order identity")
        return await self._get(f"/v1/order/{order_id}", private=True)

    async def open_orders(self):
        return await self._get("/v1/orders/open", private=True)

    async def activities(self, **params):
        return await self._get("/v1/portfolio/activities", private=True, params=params)

    async def authenticated_participant(self):
        # This documented participant identifier may require RFQ read access.
        # Failure never falls back to treating a configured key as account proof.
        return await self._get("/v1/rfqs/user-id", private=True)

    async def preview_order(self, request: dict) -> dict:
        """Use the official retail SDK's non-placement preview endpoint only.

        The central executor owns order creation. Keeping this method on the
        monitor's allowlisted surface makes a preview incapable of falling
        through to placement or cancellation.
        """
        if not self.authenticated or not isinstance(request, dict):
            raise VenueError("Authenticated Polymarket US order preview required")
        if self._private_sdk is None:
            from polymarket_us import AsyncPolymarketUS
            self._private_sdk = AsyncPolymarketUS(
                key_id=self.settings.api_key_id, secret_key=self.settings.secret_key,
                max_retries=0)
        from polymarket_us.errors import PolymarketUSError
        try:
            result = await self._private_sdk.orders.preview({"request": request})
        except PolymarketUSError as exc:
            status = getattr(exc, "status_code", None)
            raise VenueError(f"Polymarket US order preview failed ({status or type(exc).__name__})") from None
        if not isinstance(result, dict) or not isinstance(result.get("order"), dict):
            raise VenueError("Invalid Polymarket US order preview response")
        return result

    async def connect(self):
        self._closed = False
        self.authenticated = False
        self.health = "connecting"
        try:
            self.account_snapshot = await self.account_balances()
        except VenueHTTPError as exc:
            self.health = "unauthorized" if exc.status_code == 401 else "access_denied" if exc.status_code == 403 else "error"
            self.reason = f"Polymarket US account check returned HTTP {exc.status_code}"
            raise
        self.authenticated = True
        self.health = "authenticated"
        return True

    async def discover_markets(self, *, limit=25, max_pages=2, active=True):
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("Discovery page size must be 1 to 100")
        if isinstance(max_pages, bool) or not isinstance(max_pages, int) or not 1 <= max_pages <= 5:
            raise ValueError("Discovery is limited to five pages per call")
        rows, seen = [], set()
        for page in range(max_pages):
            response = await self._get("/v1/markets", params={"limit": limit, "offset": page * limit,
                                                            "active": str(bool(active)).lower(), "closed": "false"})
            batch = response.get("markets")
            if not isinstance(batch, list) or len(batch) > limit:
                raise VenueError("Invalid bounded Polymarket US market page")
            for row in batch:
                if not isinstance(row, dict) or not isinstance(row.get("slug"), str):
                    raise VenueError("Polymarket US market lacks a slug")
                validated_slugs([row["slug"]])
                if row["slug"] in seen:
                    raise VenueError("Repeated market in Polymarket US discovery pages")
                seen.add(row["slug"])
                rows.append(row)
            if len(batch) < limit:
                break
        # Bounded result is explicitly not an exhaustive venue catalog.
        return {"markets": rows, "bounded": True}

    async def discover_league_events(self, league: str, *, limit=50, max_pages=2) -> list[dict]:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,31}", league):
            raise ValueError("Invalid Polymarket US public league slug")
        if (isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100
                or isinstance(max_pages, bool) or not isinstance(max_pages, int) or not 1 <= max_pages <= 3):
            raise ValueError("Polymarket US league discovery must stay bounded")
        pages = []
        for page in range(max_pages):
            result = await self._get(f"/v2/leagues/{league}/events", params={
                "limit": limit, "offset": page * limit, "type": "sport"})
            if not isinstance(result, dict) or not isinstance(result.get("events"), list):
                raise VenueError("Invalid Polymarket US league discovery response")
            if len(result["events"]) > limit:
                raise VenueError("Polymarket US league page exceeds requested bound")
            pages.append({**result, "bounded": page == max_pages - 1 and len(result["events"]) == limit})
            if len(result["events"]) < limit:
                break
        return pages

    async def fetch_snapshot(self, market_slug: str):
        validated_slugs([market_slug])
        sdk = self._public_client()
        result = await self._public_read(sdk.markets.book, market_slug)
        data = result.get("marketData") if isinstance(result, dict) else None
        if not isinstance(data, dict) or data.get("marketSlug") != market_slug:
            raise VenueError("Polymarket US SDK returned a mismatched public book")
        # Public REST is a separate, timestamped baseline. It never refreshes
        # or impersonates the authenticated WebSocket book.
        self.rest_books[market_slug] = data
        self.rest_book_received_at[market_slug] = time.time()
        return result

    async def _public_read(self, method, slug: str):
        """Share the adapter's read budget with SDK calls; never auto-retry 429."""
        from polymarket_us.errors import RateLimitError

        async with self._request_lock:
            wait = self._next_request - time.monotonic()
            while wait > 0:
                await asyncio.sleep(wait)
                wait = self._next_request - time.monotonic()
            # The public gateway rejected the former unpaced batch. One read
            # per second is a conservative local cap, not a claimed venue tier.
            self._next_request = time.monotonic() + max(1.0, 1/self.settings.requests_per_second)
            self.request_count += 1
            try:
                return await method(slug)
            except RateLimitError:
                self.error_count += 1
                self._next_request = time.monotonic() + 30
                raise VenueError("Polymarket US public read rate limited") from None

    def _public_client(self):
        if self._public_sdk is None:
            from polymarket_us import AsyncPolymarketUS
            self._public_sdk = AsyncPolymarketUS(max_retries=0)
        return self._public_sdk

    async def public_market_metadata(self, market_slug: str) -> dict:
        validated_slugs([market_slug])
        result = await self._public_read(self._public_client().markets.retrieve_by_slug, market_slug)
        market = result.get("market", result) if isinstance(result, dict) else None
        if not isinstance(market, dict) or market.get("slug") != market_slug:
            raise VenueError("Polymarket US SDK returned a mismatched public market")
        return market

    async def configure_subscriptions(self, config):
        parsed = PolymarketUSMonitorConfig.model_validate(config)
        if self._sockets:
            raise VenueError("Restart the monitor to change subscription scope")
        self.market_slugs = list(parsed.market_slugs)
        self.private_updates = parsed.private_updates

    def subscription_requests(self, channel: str):
        kinds = ["SUBSCRIPTION_TYPE_MARKET_DATA"] if channel == "markets" else [
            "SUBSCRIPTION_TYPE_ORDER", "SUBSCRIPTION_TYPE_ORDER_SNAPSHOT",
            "SUBSCRIPTION_TYPE_POSITION", "SUBSCRIPTION_TYPE_ACCOUNT_BALANCE"]
        self._subscriptions[channel] = {}
        self._subscription_evidence[channel] = {}
        requests = []
        for kind in kinds:
            request_id = str(uuid4())
            self._subscriptions[channel][request_id] = kind
            self._subscription_evidence[channel][kind] = "awaiting_data"
            subscribe = {"requestId": request_id, "subscriptionType": kind}
            # Empty private order/position scope means the whole authenticated
            # account; never use empty public scope to request the entire venue.
            if channel == "markets":
                if not self.market_slugs:
                    raise VenueError("Market streaming requires explicit discovered slugs")
                subscribe["marketSlugs"] = self.market_slugs
            requests.append({"subscribe": subscribe})
        return requests

    def _refresh_health(self):
        self.health = "streaming" if self._ready and all(self._ready.values()) else "connecting"

    def observe_frame(self, channel: str, frame: dict) -> str:
        if not isinstance(frame, dict):
            raise VenueError("Invalid Polymarket US stream envelope")
        self.last_message_time = time.time()
        if "heartbeat" in frame:
            self.heartbeat_messages += 1
            return "heartbeat"
        if frame.get("error"):
            raise VenueError("Polymarket US rejected a monitoring subscription")
        request_id = alias(frame, "requestId", "request_id")
        expected = self._subscriptions.get(channel, {}).get(request_id)
        if not expected:
            self.unknown_messages += 1
            return "unknown"
        subscription_type = alias(frame, "subscriptionType", "subscription_type")
        numeric_types = {"SUBSCRIPTION_TYPE_MARKET_DATA": 1, "SUBSCRIPTION_TYPE_ORDER": 1,
                         "SUBSCRIPTION_TYPE_ORDER_SNAPSHOT": 2, "SUBSCRIPTION_TYPE_POSITION": 3,
                         "SUBSCRIPTION_TYPE_ACCOUNT_BALANCE": 4}
        if isinstance(subscription_type, bool) or subscription_type not in {expected, numeric_types[expected]}:
            raise VenueError("Polymarket US subscription identity mismatch")
        if channel == "markets":
            data = alias(frame, "marketData", "market_data")
            if not isinstance(data, dict):
                self.unknown_messages += 1
                return "unknown"
            slug = alias(data, "marketSlug", "market_slug")
            if slug not in self.market_slugs or not isinstance(data.get("bids"), list) or not isinstance(data.get("offers"), list):
                raise VenueError("Unexpected Polymarket US market snapshot identity")
            stamp = alias(data, "transactTime", "transact_time")
            try:
                observed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                if observed.tzinfo is None:
                    raise ValueError("Naive market-data clock")
                previous = self.books.get(slug)
                if previous is not None:
                    old_stamp = alias(previous, "transactTime", "transact_time")
                    old = datetime.fromisoformat(old_stamp.replace("Z", "+00:00"))
                    if observed.astimezone(timezone.utc) <= old.astimezone(timezone.utc):
                        return "out_of_order"
            except (AttributeError, TypeError, ValueError):
                raise VenueError("Invalid Polymarket US book timestamp") from None
            for row in data["bids"] + data["offers"]:
                if not isinstance(row, dict) or not isinstance(row.get("px"), dict) or row["px"].get("currency") != "USD":
                    raise VenueError("Invalid Polymarket US book currency")
                try:
                    px, qty = Decimal(str(row["px"]["value"])), Decimal(str(row["qty"]))
                    if not px.is_finite() or not qty.is_finite() or not 0 <= px <= 1 or qty < 0:
                        raise ValueError("Invalid book number")
                except (KeyError, ValueError, ArithmeticError):
                    raise VenueError("Invalid Polymarket US book level") from None
            self.books[slug] = data
            self.book_received_at[slug] = self.last_message_time
            self.market_messages += 1
            self._ready[channel] = True
        else:
            fields = {
                "SUBSCRIPTION_TYPE_ORDER": [("orderSubscriptionSnapshot", "order_subscription_snapshot"), ("orderSubscriptionUpdate", "order_subscription_update")],
                "SUBSCRIPTION_TYPE_ORDER_SNAPSHOT": [("orderSubscriptionSnapshot", "order_subscription_snapshot")],
                "SUBSCRIPTION_TYPE_POSITION": [("positionSubscription", "position_subscription")],
                "SUBSCRIPTION_TYPE_ACCOUNT_BALANCE": [("accountBalancesSnapshot", "account_balances_snapshot"), ("accountBalancesUpdate", "account_balances_update")],
            }
            if not any(isinstance(alias(frame, camel, snake), dict) for camel, snake in fields[expected]):
                self.unknown_messages += 1
                return "unknown"
            self.private_messages += 1
            self._ready[channel] = True
        self._subscription_evidence.setdefault(channel, {})[expected] = "data_observed"
        self._refresh_health()
        return "data"

    async def _pump(self, channel, queue):
        path = f"/v1/ws/{channel}"
        failures = 0
        while not self._closed:
            terminal = False
            try:
                async with ws_connect(self.WS_HOST + path, additional_headers=self.headers("GET", path),
                                      open_timeout=self.settings.timeout_seconds, ping_interval=20, ping_timeout=20,
                                      max_size=4 * 1024 * 1024, max_queue=32) as socket:
                    self._sockets[channel] = socket
                    for request in self.subscription_requests(channel):
                        await socket.send(json.dumps(request, separators=(",", ":")))
                    while not self._closed:
                        # Account and market channels may legitimately be quiet.
                        # Protocol ping/pong above detects transport failure;
                        # message inactivity must not force a reconnect or make
                        # the last received market snapshot appear fresh again.
                        raw = await socket.recv()
                        frame = json.loads(raw, parse_float=Decimal)
                        kind = self.observe_frame(channel, frame)
                        failures = 0
                        # WebSocket ping/pong above supplies the documented
                        # keep-alive alternative; heartbeat never refreshes books.
                        await queue.put((False, {"type": "polymarket_us_monitor", "channel": channel,
                                                 "kind": kind, "payload": frame}))
            except asyncio.CancelledError:
                raise
            except InvalidStatus as exc:
                status = exc.response.status_code
                terminal = status in {401, 403}
                if terminal:
                    self.health = "unauthorized" if status == 401 else "access_denied"
                    self.reason = f"Polymarket US WebSocket returned HTTP {status}"
                    if status == 401:
                        self.authenticated = False
            except VenueError:
                terminal = True
                self.health = "subscription_rejected"
                self.reason = "Polymarket US subscription or stream schema was rejected; review configuration"
            except (ConnectionClosed, InvalidHandshake, OSError, TimeoutError, ValueError, TypeError):
                pass
            finally:
                self._sockets.pop(channel, None)
                self._ready[channel] = False
                self._subscription_evidence[channel] = {}
                if not terminal and not self._closed:
                    self.health = "reconnecting"
                if channel == "markets":
                    self.books.clear()
                    self.book_received_at.clear()
                    self.rest_books.clear()
                    self.rest_book_received_at.clear()
            if self._closed:
                return
            self.error_count += 1
            await queue.put((terminal, {"type": "transport_invalidated", "venue": self.venue_id,
                                       "channel": channel, "reason": self.health if terminal else "connection_lost"}))
            if terminal:
                return True
            await self._backoff(failures)
            failures += 1

    async def _supervised_pump(self, channel, queue):
        """A dead reader must wake the consumer, never strand queue.get()."""
        try:
            reported_terminal = await self._pump(channel, queue)
            if reported_terminal or self._closed:
                return
        except asyncio.CancelledError:
            raise
        except Exception:
            # Unexpected parser/library failures are not exposed or silently
            # retried. The other channel is stopped by the consumer's finally.
            pass
        self._ready[channel] = False
        self._subscription_evidence[channel] = {}
        self.books.clear()
        self.book_received_at.clear()
        self.rest_books.clear()
        self.rest_book_received_at.clear()
        self.error_count += 1
        self.health = "error"
        self.reason = "Polymarket US monitor worker stopped unexpectedly; restart and review logs"
        await queue.put((True, {"type": "transport_invalidated", "venue": self.venue_id,
                               "channel": channel, "reason": "error"}))

    async def stream_updates(self) -> AsyncIterator[dict]:
        if not self.authenticated:
            raise CredentialsMissing("Authenticate Polymarket US before monitoring streams")
        channels = (["private"] if self.private_updates else []) + (["markets"] if self.market_slugs else [])
        if not channels:
            self.health = "subscription_rejected"
            self.reason = "Enable account updates or select discovered market slugs"
            return
        self._ready = {channel: False for channel in channels}
        queue = asyncio.Queue(maxsize=256)
        tasks = [asyncio.create_task(self._supervised_pump(channel, queue)) for channel in channels]
        try:
            while not self._closed:
                terminal, frame = await queue.get()
                yield frame
                if terminal:
                    self.health = frame["reason"]
                    return
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self._ready = {}
            self._subscription_evidence = {}
            self.books.clear()
            self.book_received_at.clear()

    async def disconnect(self):
        self._closed = True
        await asyncio.gather(*(socket.close() for socket in list(self._sockets.values())), return_exceptions=True)
        self.authenticated = False
        self.account_snapshot = None
        self.books.clear()
        self.book_received_at.clear()
        self.rest_books.clear()
        self.rest_book_received_at.clear()
        self._ready = {}
        self._subscription_evidence = {}
        if self._public_sdk is not None:
            await self._public_sdk.close()
            self._public_sdk = None
        if self._private_sdk is not None:
            await self._private_sdk.close()
            self._private_sdk = None
        await super().disconnect()

    def health_check(self):
        return {**super().health_check(), "authenticated": self.authenticated,
                "streaming": bool(self._ready) and all(self._ready.values()), "reason": self.reason,
                "read_only": True, "execution_supported": False, "reconciled": False,
                "configured_markets": len(self.market_slugs), "monitored_books": len(self.books),
                "market_messages": self.market_messages, "private_messages": self.private_messages,
                "heartbeat_messages": self.heartbeat_messages, "unknown_messages": self.unknown_messages,
                "private_subscription_evidence": dict(self._subscription_evidence.get("private", {})),
                "private_delivery_note": "Observed subscription data only; no complete account or replay certification",
                "private_streaming": self._ready.get("private", False), "market_streaming": self._ready.get("markets", False)}

    @staticmethod
    def get_manifest():
        return {"id": "polymarket_us", "name": "Polymarket US Personal Monitor", "version": "1.0.0", "type": "observer",
                "sports": ["discoverable"], "markets": ["discoverable"], "input_schemas": [], "output_schemas": [],
                "transports": ["rest", "websocket"],
                "authentication": {"credential_env": ["POLYMARKET_US_API_KEY_ID", "POLYMARKET_US_SECRET_KEY"]},
                "capabilities": {"monitor_only": True, "discovery": True, "orderbook": True, "private_updates": True,
                                 "limit_orders": False, "cancel": False, "reconcile": False, "sandbox": False,
                                 "native_execution": False, "pricing": False, "streaming": True, "polling": True,
                                 "native_event_forwarding": True, "settlement_reconciliation": False,
                                 "supported_settlement_policies": []},
                "permissions": ["network", "secret:polymarket_us", "account:read"],
                "rate_limits": {"local_default_rps": 2, "documented_retail_general_rps": 25, "shared_by_source_ip": True},
                "limitations": ["Personal US API only; international and institutional APIs are separate",
                                "Monitor is read-only; separate guarded executor remains blocked by account identity, available cash and settlement proof",
                                "Streaming health is delivery evidence, not complete order/position reconciliation",
                                "Bounded discovery; explicit market slugs required for market streaming"],
                "config_schema": PolymarketUSMonitorConfig.model_json_schema(),
                "documentation": ["https://docs.polymarket.us/api-reference/authentication",
                                  "https://docs.polymarket.us/api-reference/introduction",
                                  "https://docs.polymarket.us/api-reference/websocket/markets",
                                  "https://docs.polymarket.us/api-reference/websocket/private"]}
