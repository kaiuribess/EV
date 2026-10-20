"""ProphetX v4 Trading API and Talaria event/event_subtype subscriptions.

Market selections remain their own typed native schema: price is American odds,
value is executable USD stake, and quantity is NOT executable book liquidity.
"""
from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from decimal import Decimal
from typing import AsyncIterator
from urllib.parse import quote, urlsplit

import httpx
from pydantic import Field, SecretStr
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed

from .common import (
    CredentialsMissing, NativeSettings, NativeTransport, VenueError,
    decimal_value, json_body,
)


class ProphetXSettings(NativeSettings):
    requests_per_second: float = Field(default=1, gt=0, le=100)
    api_key: str = Field(default="", repr=False)
    secret_key: SecretStr = Field(default_factory=lambda: SecretStr(""), repr=False)


def decode_talaria(raw: str | bytes | dict) -> dict:
    frame = json.loads(raw) if isinstance(raw, (str, bytes)) else dict(raw)
    if not isinstance(frame, dict):
        raise ValueError("Talaria frame must be an object")
    data = frame.get("data")
    if isinstance(data, str):
        data = json.loads(data)
    if isinstance(data, dict) and "payload" in data:
        encoded = data["payload"]
        if not isinstance(encoded, str):
            raise ValueError("Talaria payload must be base64 JSON")
        payload = json.loads(base64.b64decode(encoded, validate=True))
        # Preserve provider publication time separately from per-level updated_at.
        frame["envelope"] = data
        frame["data"] = payload
    else:
        frame["data"] = data
    return frame


def executable_level(selection: dict) -> dict:
    """Return unambiguous stake and payout units; missing `value` has no depth."""
    price = decimal_value(selection["price"])
    stake = decimal_value(selection.get("value", 0))
    if price != price.to_integral_value() or abs(price) < 100 or stake < 0:
        raise ValueError("Invalid ProphetX American-price level")
    gross_per_stake = Decimal(1) + (price / 100 if price > 0 else 100 / -price)
    return {
        "strike_id": str(selection["strike_id"]), "american_price": price,
        "executable_stake_usd": stake, "gross_payout_per_usd_stake": gross_per_stake,
        "source_observed_at": selection.get("updated_at"),
    }


def order_progress(order: dict, original_quantity: Decimal | None = None, *, websocket=False) -> dict:
    """Preserve partial fills even when a killed remainder is rewritten as full."""
    filled_key = "total_filled_quantity" if websocket else "filled_quantity"
    if filled_key not in order or "open_quantity" not in order:
        return {"state": "unknown", "filled": None, "remaining": None, "terminal": False}
    filled = decimal_value(order[filled_key])
    remaining = decimal_value(order["open_quantity"])
    original = decimal_value(original_quantity if original_quantity is not None else order.get("original_quantity", order["quantity"]))
    if filled < 0 or remaining < 0 or original < filled:
        raise ValueError("Invalid ProphetX order quantities")
    if remaining == 0:
        state = "filled" if filled == original else "partially_filled_canceled" if filled else "canceled"
    else:
        state = "partially_filled" if filled else "open"
    return {"state": state, "filled": filled, "remaining": remaining, "terminal": remaining == 0}


class ProphetXClient(NativeTransport):
    venue_id = "prophetx"
    HOSTS = {
        "sandbox": "https://api.sandbox.prophetx.dev/partner",
        "production": "https://cash.api.prophetx.co/partner",
    }

    def __init__(self, settings: ProphetXSettings | None = None, client: httpx.AsyncClient | None = None):
        super().__init__(settings or ProphetXSettings(), client)
        self._access_token = ""
        self._refresh_token = ""
        self._access_expires = 0.0
        self._auth_lock = asyncio.Lock()
        self.subscriptions: list[dict] = []
        self.channels: dict[str, dict] = {}
        self._last_sequences: dict[str, int] = {}
        self.price_ladder: set[int] = set()

    def _require_credentials(self):
        if not self.settings.api_key or not self.settings.secret_key.get_secret_value():
            self.health = "credentials_missing"
            raise CredentialsMissing("ProphetX requires access_key and secret_key")

    async def _auth_request(self, path: str, body: dict) -> dict:
        request = self.http.build_request(
            "POST", self.HOSTS[self.settings.environment] + path,
            headers={"Content-Type": "application/json"}, content=json_body(body),
        )
        response = await self._request(request)
        session = response["data"]
        self._access_token = session["access_token"]
        self._refresh_token = session["refresh_token"]
        # Honor the server's expiration instead of conflicting prose lifetimes.
        self._access_expires = float(session["access_expire_time"])
        self.health = "authenticated"
        return {"authenticated": True, "expires_at": self._access_expires}

    async def connect(self) -> dict:
        self._require_credentials()
        async with self._auth_lock:
            if self._access_token and self._access_expires > time.time() + 60:
                return {"authenticated": True, "expires_at": self._access_expires}
            if self._refresh_token:
                return await self._auth_request("/auth/refresh", {"refresh_token": self._refresh_token})
            return await self._auth_request("/auth/login", {
                "access_key": self.settings.api_key, "secret_key": self.settings.secret_key.get_secret_value(),
            })

    async def request(self, method: str, path: str, *, params=None, body=None, mutation_id=None):
        await self.connect()
        request = self.http.build_request(
            method, self.HOSTS[self.settings.environment] + path, params=params, content=json_body(body),
            headers={"Authorization": f"Bearer {self._access_token}", "Content-Type": "application/json",
                     "X-CFTC-Terminology": "true"},
        )
        return await self._request(request, mutation_id=mutation_id)

    async def discover_sports(self) -> dict:
        return await self.request("GET", "/mm/get_tournaments")

    async def discover_events(self, **params) -> dict:
        return await self.request("GET", "/mm/get_sport_events", params=params)

    async def discover_markets(self, event_ids: list[str]) -> dict:
        return await self.request("GET", "/v4/mm/get_multiple_markets",
                                  params={"event_ids": ",".join(map(str, event_ids))})

    async def fetch_snapshot(self, event_id: str) -> dict:
        return await self.request("GET", "/v4/mm/get_markets", params={"event_id": event_id})

    async def fetch_price_ladder(self) -> dict:
        # Keep raw fixture to allow strict account-specific schema validation.
        # A caller must validate and install its exact allowed prices before orders.
        return await self.request("GET", "/v4/mm/get_price_ladder")

    def set_verified_price_ladder(self, prices: list[int]):
        if not prices or any(isinstance(x, bool) or not isinstance(x, int) or abs(x) < 100 for x in prices):
            raise ValueError("Invalid ProphetX price ladder")
        self.price_ladder = set(prices)

    async def reconcile_order(self, order_id: str) -> dict:
        return await self.request("GET", f"/v4/mm/get_order/{quote(order_id, safe='')}")

    async def reconcile_account(self, **params) -> dict:
        return await self.request("GET", "/v4/mm/get_order_history", params=params)

    async def fetch_fills(self, **params) -> dict:
        return await self.request("GET", "/v4/mm/get_order_matched_detail", params=params)

    @staticmethod
    def order_body(strike_id: str, american_price: int, stake_usd: Decimal, client_order_id: str,
                   strategy: str | None = None) -> dict:
        stake = decimal_value(stake_usd)
        if isinstance(american_price, bool) or not isinstance(american_price, int) or abs(american_price) < 100:
            raise ValueError("ProphetX limit price must be integer American odds")
        if stake < 1 or stake != stake.quantize(Decimal("0.01")):
            raise ValueError("ProphetX stake must be USD with two decimals, at least $1")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", client_order_id):
            raise ValueError("Invalid ProphetX external_id")
        if strategy not in {None, "fillOrKill", "ALO"}:
            raise ValueError("Unsupported ProphetX strategy; no synchronous all-or-none order exists")
        body = {"strike_id": strike_id, "price": american_price, "quantity": float(stake),
                "external_id": client_order_id}
        if strategy:
            body["order_strategy"] = strategy
        return body

    async def submit_order(self, body: dict) -> dict:
        client_id = body.get("external_id", "")
        self.settings.gate.require(client_id, self.settings.environment)
        if body.get("price") not in self.price_ladder:
            raise VenueError("Price must be validated against the current native price ladder")
        return await self.request("POST", "/v4/mm/submit_order", body=body, mutation_id=client_id)

    async def cancel_order(self, order_id: str, *, external_id: str) -> dict:
        # 409/425 and eventually consistent 404 are not interpreted as cancellation.
        return await self.request("POST", "/v4/mm/cancel_order",
                                  body={"order_id": order_id, "external_id": external_id}, mutation_id=external_id)

    async def subscribe(self, event_ids: list[str], prop_subtypes: dict[str, list[str]] | None = None):
        self.subscriptions = [{"type": "event", "ids": list(map(str, event_ids))}] if event_ids else []
        props = [f"{event}:{subtype}" for event, types in (prop_subtypes or {}).items() for subtype in types]
        if props:
            self.subscriptions.append({"type": "event_subtype", "ids": props})
        if self._ws:
            # Reconnect guarantees removed channels actually disappear as well.
            await self._ws.close()

    async def unsubscribe(self, channel_names: list[str]):
        for channel in channel_names:
            await self._send_ws({"event": "pusher:unsubscribe", "data": {"channel": channel}})
            self.channels.pop(channel, None)

    @staticmethod
    def websocket_url(config: dict) -> str:
        # Talaria host is retrieved, never inferred from an obsolete region.
        host = config.get("ws_host")
        if not host:
            raise VenueError("Talaria ws_host missing; legacy Pusher fallback is unsupported")
        parsed = urlsplit(host if "://" in host else f"wss://{host}")
        if parsed.scheme != "wss" or not parsed.hostname or parsed.username or parsed.password or parsed.path not in {"", "/"}:
            raise ValueError("Expected a secure Talaria hostname without credentials/path")
        return f"wss://{parsed.netloc}/app/{quote(config['key'], safe='')}?protocol=7"

    async def _register(self, socket_id: str):
        response = await self.request("POST", "/v4/mm/websocket", body={
            "socket_id": socket_id, "service": "pusher", "subscriptions": self.subscriptions,
        })
        data = response["data"]
        if not data.get("success") or data.get("rejected"):
            raise VenueError("ProphetX rejected one or more requested subscriptions")
        if data.get("channel_count", 0) > data.get("channel_limit", 0):
            raise VenueError("ProphetX channel quota exceeded")
        self.channels = {item["channel_name"]: item for item in data["authorized_channel"]}
        await self._send_ws({"event": "pusher:signin", "data": data["authenticated"]})

    async def _subscribe_authorized_channels(self):
        for channel in self.channels.values():
            await self._send_ws({"event": "pusher:subscribe",
                                 "data": {"channel": channel["channel_name"], "auth": channel["auth"]}})

    def accept_message(self, frame: dict) -> bool:
        """Sequence values are ordering tokens, NOT documented contiguous counters."""
        channel = frame.get("channel", "")
        if channel not in self.channels:
            return frame.get("event", "").startswith("pusher")
        frame["scope"] = self.channels[channel].get("scope")
        permitted = {x["name"] for x in self.channels[channel].get("binding_events", [])}
        if frame.get("event") not in permitted:
            return False
        if frame["event"] == "market_selections":
            return False  # avoid double-counting legacy and CFTC payloads
        data = frame.get("data") or {}
        info = data.get("info", {})
        sequence = info.get("sequence_number")
        if sequence is not None:
            identity = info.get("id", data.get("market_id", ""))
            subject = f"{channel}:{frame['event']}:{identity}"
            previous = self._last_sequences.get(subject)
            if previous is not None and int(sequence) <= previous:
                return False
            self._last_sequences[subject] = int(sequence)
        return True

    async def stream_updates(self) -> AsyncIterator[dict]:
        self._require_credentials()
        failures = 0
        while not self._closed:
            self._last_sequences.clear()
            try:
                config = await self.request("GET", "/websocket/connection-config")
                async with ws_connect(self.websocket_url(config), ping_interval=20,
                                      max_size=16 * 1024 * 1024) as socket:
                    self._ws, self.health = socket, "streaming"
                    connected_at = time.monotonic()
                    async for raw in socket:
                        frame = decode_talaria(raw)
                        self.last_message_time = time.time()
                        event = frame.get("event")
                        if event == "pusher:connection_established":
                            await self._register(frame["data"]["socket_id"])
                            # Core must seed/reconcile; socket reconnect does not prove book freshness.
                            yield {"type": "snapshot_required", "venue": self.venue_id,
                                   "subscriptions": self.subscriptions, "reconcile_private": True}
                        elif event == "pusher:signin_success":
                            await self._subscribe_authorized_channels()
                        elif event == "pusher:ping":
                            await self._send_ws({"event": "pusher:pong", "data": {}})
                        elif event == "pusher:error":
                            raise VenueError("Talaria protocol error")
                        elif self.accept_message(frame):
                            yield frame
                        failures = 0
                        if time.monotonic() - connected_at > 25 * 60:
                            break  # rediscover config and refresh token before server migration
                    raise VenueError("Talaria stream closed; reconcile and resubscribe")
            except (OSError, ConnectionClosed, ValueError, VenueError, KeyError, TypeError, AttributeError) as exc:
                self.error_count += 1
                yield {"type": "transport_invalidated", "venue": self.venue_id,
                       "reason": type(exc).__name__, "all_books_invalid": True, "reconcile_private": True}
                if not self._closed:
                    await self._backoff(failures)
                    failures += 1
            finally:
                self._ws = None

    @staticmethod
    def get_manifest() -> dict:
        return {
            "id": "prophetx", "name": "ProphetX Talaria v4", "version": "1.0.0", "type": "execution",
            "contract_version": "1.0", "sports": ["discovered"], "markets": ["discovered"],
            "input_schemas": ["order.intent.v1"], "output_schemas": ["native.prophetx.v4"],
            "transports": ["rest", "websocket"], "authentication": "access_key/secret_key token exchange",
            "credentials": ["PROPHETX_API_KEY", "PROPHETX_SECRET_KEY"],
            "capabilities": {"discovery": True, "orderbook": True, "private_fills": True,
                             "limit_orders": True, "cancel": True, "reconcile": True, "sandbox": True,
                             "streaming": True, "polling": True, "quantity_unit": "usd_stake",
                             "book_depth_field": "value", "price_unit": "american_odds",
                             "all_or_none": False, "asynchronous_matching": True, "native_live_tested": False},
            "permissions": ["network", "secret:prophetx", "orders:conditional"],
            "health_check": True, "rate_limits": {"local_default_rps": 2, "read_poll_min_seconds": 1},
            "config_schema": {"environment": {"type": "string", "enum": ["sandbox", "production"], "default": "sandbox"}},
            "docs": "https://docs.prophetx.co/docs/integrate-with-websockets",
        }
