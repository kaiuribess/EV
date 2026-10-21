"""Kalshi documented V2 REST / WebSocket transport (no default live writes)."""
from __future__ import annotations

import base64
import json
import time
from decimal import Decimal
from pathlib import Path
from typing import AsyncIterator
from urllib.parse import quote, urlsplit

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa
from pydantic import Field
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus

from .common import (
    CredentialsMissing, NativeSettings, NativeTransport, SequenceTracker,
    SequenceGap, VenueError, VenueHTTPError, decimal_value, json_body,
)


class KalshiSettings(NativeSettings):
    api_key_id: str = Field(default="", repr=False)
    private_key_path: str = Field(default="", repr=False)


def sign_kalshi(private_key, timestamp: str, method: str, path: str) -> str:
    message = f"{timestamp}{method.upper()}{path.split('?')[0]}".encode()
    if isinstance(private_key, ed25519.Ed25519PrivateKey):
        signature = private_key.sign(message)
    elif isinstance(private_key, rsa.RSAPrivateKey):
        signature = private_key.sign(
            message, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
    else:
        raise ValueError("Kalshi supports RSA-PSS or Ed25519 signing keys")
    return base64.b64encode(signature).decode()


class KalshiBook:
    """YES/NO bids with executable asks obtained from opposing bids.

    The sequence is per subscription (sid), not per ticker. All books on a
    subscription are invalidated together if a delta is missed.
    """

    def __init__(self):
        self.sequences = SequenceTracker()
        self.books: dict[str, dict[str, dict[Decimal, Decimal]]] = {}
        self.market_sids: dict[str, int] = {}
        self.invalid: set[str] = set()

    def reset(self):
        self.sequences.reset()
        self.books.clear()
        self.market_sids.clear()
        self.invalid.clear()

    def apply(self, frame: dict) -> bool:
        kind = frame.get("type")
        if kind not in {"orderbook_snapshot", "orderbook_delta"}:
            return False
        msg, seq, sid = frame["msg"], int(frame["seq"]), int(frame["sid"])
        ticker = msg["market_ticker"]
        subject = str(sid)
        if kind == "orderbook_snapshot":
            sides = {}
            for side in ("yes", "no"):
                levels = {}
                for price, quantity in msg.get(f"{side}_dollars_fp", []):
                    p, q = decimal_value(price), decimal_value(quantity)
                    if not Decimal(0) <= p <= Decimal(1) or q < 0:
                        raise ValueError("Invalid Kalshi snapshot level")
                    if q:
                        levels[p] = q
                sides[side] = levels
            self.books[ticker] = sides
            self.market_sids[ticker] = sid
            self.invalid.discard(ticker)
            self.sequences.snapshot(subject, seq)
            return True
        try:
            if not self.sequences.delta(subject, seq):
                return False
        except SequenceGap:
            self.invalid.update(k for k, value in self.market_sids.items() if value == sid)
            raise
        if ticker not in self.books or ticker in self.invalid:
            raise SequenceGap(subject, None, seq)
        side = msg["side"]
        if side not in {"yes", "no"}:
            raise ValueError("Unknown Kalshi side")
        price = decimal_value(msg["price_dollars"])
        change = decimal_value(msg["delta_fp"])
        if not Decimal(0) <= price <= Decimal(1):
            raise ValueError("Invalid Kalshi delta price")
        new_quantity = self.books[ticker][side].get(price, Decimal(0)) + change
        if new_quantity < 0:
            self.invalid.add(ticker)
            raise SequenceGap(subject, None, seq)
        if new_quantity:
            self.books[ticker][side][price] = new_quantity
        else:
            self.books[ticker][side].pop(price, None)
        return True

    def asks(self, ticker: str, outcome: str) -> list[tuple[Decimal, Decimal]]:
        if ticker in self.invalid or ticker not in self.books:
            return []
        if outcome not in {"yes", "no"}:
            raise ValueError("Unknown Kalshi outcome")
        opposite = "no" if outcome == "yes" else "yes"
        return sorted((Decimal(1) - p, q) for p, q in self.books[ticker][opposite].items())


class KalshiClient(NativeTransport):
    venue_id = "kalshi"
    HOSTS = {
        "sandbox": "https://external-api.demo.kalshi.co",
        "production": "https://external-api.kalshi.com",
    }
    WS_HOSTS = {
        "sandbox": "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2",
        "production": "wss://external-api-ws.kalshi.com/trade-api/ws/v2",
    }

    def __init__(self, settings: KalshiSettings | None = None, client: httpx.AsyncClient | None = None):
        super().__init__(settings or KalshiSettings(), client)
        self._key = None
        self.book = KalshiBook()
        self.tickers: list[str] = []
        self._command_id = 0
        self.authenticated = False
        self.health_reason = "Kalshi is disconnected"

    def _load_key(self):
        if not self.settings.api_key_id or not self.settings.private_key_path:
            self.health = "credentials_missing"
            self.authenticated = False
            self.health_reason = "Kalshi requires an API key ID and local private key file"
            raise CredentialsMissing("Kalshi requires an API key ID and private key file")
        if self._key is None:
            self._key = serialization.load_pem_private_key(
                Path(self.settings.private_key_path).read_bytes(), password=None
            )
        return self._key

    def headers(self, method: str, path: str, timestamp: str | None = None) -> dict:
        timestamp = timestamp or str(time.time_ns() // 1_000_000)
        return {
            "KALSHI-ACCESS-KEY": self.settings.api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
            "KALSHI-ACCESS-SIGNATURE": sign_kalshi(self._load_key(), timestamp, method, path),
        }

    async def request(self, method: str, path: str, *, params=None, body=None, authenticated=True, mutation_id=None):
        url = self.HOSTS[self.settings.environment] + "/trade-api/v2" + path
        headers = self.headers(method, urlsplit(url).path) if authenticated else {}
        headers["Content-Type"] = "application/json"
        request = self.http.build_request(method, url, params=params, headers=headers, content=json_body(body))
        try:
            result = await self._request(request, mutation_id=mutation_id)
        except VenueHTTPError as exc:
            if authenticated and exc.status_code in {401, 403, 451}:
                self.authenticated = False
                self.health, self.health_reason = {
                    401: ("unauthorized", "Kalshi rejected authentication; check key, environment, signature and clock"),
                    403: ("access_denied", "Kalshi denied this API permission; review the key scope in the official account"),
                    451: ("geolocation_required", "Kalshi requires official location/eligibility verification"),
                }[exc.status_code]
            raise
        if authenticated:
            self.authenticated = True
        return result

    async def connect(self) -> dict:
        self.authenticated = False
        result = await self.request("GET", "/portfolio/balance")
        self.health = "authenticated"
        self.health_reason = "Kalshi signed account read succeeded; streaming and trading permissions are separate"
        return result

    async def disconnect(self) -> None:
        self.authenticated = False
        try:
            await super().disconnect()
        finally:
            self._ws = None
            self.health = "disconnected"
            self.health_reason = "Kalshi is disconnected"

    def health_check(self) -> dict:
        return {**super().health_check(), "authenticated": self.authenticated and not self._closed,
                "streaming": self.health == "streaming" and self._ws is not None and not self._closed,
                "reason": self.health_reason}

    async def discover_events(self, **params) -> dict:
        return await self.request("GET", "/events", params=params, authenticated=False)

    async def discover_markets(self, **params) -> dict:
        return await self.request("GET", "/markets", params=params, authenticated=False)

    async def fetch_snapshot(self, market_id: str) -> dict:
        return await self.request("GET", f"/markets/{quote(market_id, safe='')}/orderbook", authenticated=False)

    async def reconcile_order(self, order_id: str) -> dict:
        return await self.request("GET", f"/portfolio/orders/{quote(order_id, safe='')}")

    async def reconcile_account(self, **params) -> dict:
        return await self.request("GET", "/portfolio/orders", params=params)

    async def fetch_fills(self, **params) -> dict:
        return await self.request("GET", "/portfolio/fills", params=params)

    @staticmethod
    def order_body(ticker: str, outcome: str, price: Decimal, quantity: Decimal, client_order_id: str,
                   time_in_force: str = "immediate_or_cancel") -> dict:
        p, q = decimal_value(price), decimal_value(quantity)
        if outcome not in {"yes", "no"} or not 0 < p < 1 or q <= 0:
            raise ValueError("Invalid binary limit order")
        if q != q.quantize(Decimal("0.01")) or p != p.quantize(Decimal("0.0001")):
            raise ValueError("Kalshi fixed-point precision exceeded")
        if time_in_force not in {"immediate_or_cancel", "fill_or_kill", "good_till_canceled"}:
            raise ValueError("Unsupported Kalshi time in force")
        return {
            "ticker": ticker, "side": "bid" if outcome == "yes" else "ask",
            "price": format(p if outcome == "yes" else Decimal(1) - p, ".4f"),
            "count": format(q, ".2f"), "client_order_id": client_order_id,
            "time_in_force": time_in_force, "self_trade_prevention_type": "taker_at_cross",
            "cancel_order_on_pause": True,
        }

    async def submit_order(self, body: dict) -> dict:
        client_id = body.get("client_order_id", "")
        self.settings.gate.require(client_id, self.settings.environment)
        return await self.request("POST", "/portfolio/events/orders", body=body, mutation_id=client_id)

    async def cancel_order(self, order_id: str, *, market_ticker: str) -> dict:
        # Cancellation reduces risk, so remains usable after the placement gate closes.
        return await self.request("DELETE", f"/portfolio/events/orders/{quote(order_id, safe='')}",
                                  params={"market_ticker": market_ticker}, mutation_id=order_id)

    async def subscribe(self, market_ids: list[str]) -> None:
        self.tickers = list(dict.fromkeys(market_ids))
        if self._ws:
            await self._subscribe_current()

    async def _subscribe_current(self):
        if self.tickers:
            self._command_id += 1
            await self._send_ws({"id": self._command_id, "cmd": "subscribe",
                                 "params": {"channels": ["orderbook_delta"], "market_tickers": self.tickers}})
        self._command_id += 1
        await self._send_ws({"id": self._command_id, "cmd": "subscribe", "params": {"channels": ["fill"]}})

    async def unsubscribe(self, subscription_ids: list[int]):
        self._command_id += 1
        await self._send_ws({"id": self._command_id, "cmd": "unsubscribe", "params": {"sids": subscription_ids}})

    async def stream_updates(self) -> AsyncIterator[dict]:
        self._load_key()
        failures = 0
        while not self._closed:
            self.book.reset()
            self._command_id = 0
            try:
                async with ws_connect(self.WS_HOSTS[self.settings.environment],
                                      additional_headers=self.headers("GET", "/trade-api/ws/v2"),
                                      ping_interval=20, max_size=8 * 1024 * 1024) as socket:
                    self.authenticated = True  # Successful signed WebSocket handshake.
                    self._ws, self.health = socket, "subscribing"
                    self.health_reason = "Kalshi WebSocket authenticated; waiting for subscription data"
                    await self._subscribe_current()
                    async for raw in socket:
                        frame = json.loads(raw, parse_float=Decimal)
                        if not isinstance(frame, dict):
                            raise ValueError("Kalshi frame must be an object")
                        self.last_message_time = time.time()
                        if frame.get("type") == "error":
                            self.error_count += 1
                            self.book.reset()
                            self.health = "subscription_rejected"
                            self.health_reason = "Kalshi rejected a WebSocket command; review subscription scope and permissions"
                            yield {"type": "transport_invalidated", "venue": self.venue_id,
                                   "reason": "subscription_rejected", "all_books_invalid": True}
                            return
                        self.book.apply(frame)
                        if frame.get("type") in {"subscribed", "orderbook_snapshot", "orderbook_delta", "fill"}:
                            self.health = "streaming"
                            self.health_reason = "Kalshi authenticated subscription is receiving native messages"
                        failures = 0
                        yield frame
                    raise VenueError("Kalshi stream closed; reconcile and resubscribe")
            except (OSError, TimeoutError, ConnectionClosed, InvalidHandshake, VenueError, ValueError, KeyError, TypeError, AttributeError) as exc:
                self.error_count += 1
                self.book.reset()
                status = exc.response.status_code if isinstance(exc, InvalidStatus) else None
                if status in {401, 403, 451}:
                    self.authenticated = False
                    self.health, self.health_reason = {
                        401: ("unauthorized", "Kalshi WebSocket authentication failed; check key, environment and clock"),
                        403: ("access_denied", "Kalshi WebSocket permission denied; review the API key scope"),
                        451: ("geolocation_required", "Kalshi requires official location/eligibility verification"),
                    }[status]
                else:
                    self.health = "reconnecting"
                    self.health_reason = "Kalshi WebSocket interrupted; snapshots must be recovered"
                yield {"type": "transport_invalidated", "venue": self.venue_id,
                       "reason": self.health, "all_books_invalid": True}
                if status in {401, 403, 451}:
                    return
                if not self._closed:
                    await self._backoff(failures)
                    failures += 1
            finally:
                self._ws = None
                if self.health in {"streaming", "subscribing"}:
                    self.health = "disconnected"
                    self.health_reason = "Kalshi WebSocket disconnected"

    @staticmethod
    def get_manifest() -> dict:
        return {
            "id": "kalshi", "name": "Kalshi Native", "version": "1.0.0", "type": "execution",
            "contract_version": "1.0", "sports": ["discovered"], "markets": ["binary_event_contract"],
            "input_schemas": ["order.intent.v1"], "output_schemas": ["native.kalshi.v2"],
            "transports": ["rest", "websocket"], "authentication": "RSA-PSS or Ed25519 signed requests",
            "credentials": ["KALSHI_API_KEY_ID", "KALSHI_PRIVATE_KEY_PATH"],
            "capabilities": {"discovery": True, "orderbook": True, "private_fills": True,
                             "limit_orders": True, "cancel": True, "reconcile": True, "sandbox": True,
                             "streaming": True, "polling": True, "payout_usd": "1.00",
                             "quantity_unit": "contracts", "native_live_tested": False},
            "permissions": ["network", "secret:kalshi", "orders:conditional"],
            "health_check": True, "rate_limits": {"local_default_rps": 2, "account_tier_dependent": True},
            "config_schema": {"environment": {"type": "string", "enum": ["sandbox", "production"], "default": "sandbox"}},
            "docs": "https://docs.kalshi.com/getting_started/api_environments",
        }
