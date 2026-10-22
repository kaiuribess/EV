"""ParlayAPI documented REST and snapshot/diff WebSocket clients."""
from __future__ import annotations

import asyncio
import json
import math
import time
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlencode

from pydantic import Field, model_validator
from websockets.asyncio.client import connect as websocket_connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from .base import (
    Backoff, ProtocolError, ProviderConfig, ProviderEnvelope, ProviderState,
    RestProvider, UnsupportedOperation, parse_timestamp, provider_manifest, safe_component,
)


class ParlayConfig(ProviderConfig):
    regions: list[str] = Field(default_factory=lambda: ["us"])
    include_props: bool = True
    stream_kinds: list[Literal["game", "prop"]] = Field(default_factory=list)
    max_age_seconds: int = Field(default=30, ge=1, le=3600)
    # REST /props applies maxAgeSec to the last price write for most books,
    # rather than to a verified observation of an unchanged quote. Keep that
    # optional filter separate from the WebSocket's max_age_s setting.
    props_max_age_seconds: int | None = Field(default=None, ge=1, le=3600)
    stream_limit: int = Field(default=1000, ge=1, le=1000)
    props_limit: int = Field(default=1000, ge=1, le=10000)
    watchdog_seconds: float = Field(default=90, ge=10, le=180)
    account_tier: Literal["unknown", "free", "starter", "pro", "business", "enterprise", "scale"] = "unknown"


class ParlayGameBoardConfig(ParlayConfig):
    """The provider's documented complete-baseline request shape."""
    bookmakers: list[str] = Field(default_factory=lambda: [
        "pinnacle", "fanduel", "draftkings", "caesars", "betonline", "bet365",
    ])
    markets: list[str] = Field(default_factory=lambda: ["h2h", "spreads", "totals"])
    stream_kinds: list[Literal["game", "prop"]] = Field(default_factory=lambda: ["game"])
    include_props: bool = False

    @model_validator(mode="after")
    def complete_board_filters(self):
        if (not self.bookmakers or len(set(self.bookmakers)) != len(self.bookmakers)
                or any(not book or book != book.strip() for book in self.bookmakers)
                or set(self.markets) != {"h2h", "spreads", "totals"}
                or len(self.markets) != 3 or self.stream_kinds != ["game"] or self.include_props):
            raise ValueError("Complete game board needs named books and full-game h2h, spreads, totals only")
        return self


class ParlayRest(RestProvider):
    provider_id = "parlay_rest"
    base_url = "https://parlay-api.com"
    credential_env = "PARLAY_API_KEY"
    config_class = ParlayConfig

    def auth(self) -> tuple[dict[str, str], dict[str, str]]:
        return {"X-API-Key": self._credential}, {}

    async def discover_sports(self) -> ProviderEnvelope:
        return await self.request("/v1/sports", cost=0)

    async def discover_events(self, sport: str, **filters: Any) -> ProviderEnvelope:
        return await self.request(f"/v1/sports/{safe_component(sport)}/events", params=filters, cost=0)

    async def discover_markets(self, sport: str | None = None) -> ProviderEnvelope:
        path = f"/v1/sports/{safe_component(sport)}/props/markets" if sport else "/v1/markets"
        return await self.request(path)

    async def discover_bookmakers(self) -> ProviderEnvelope:
        return await self.request("/v1/bookmakers")

    async def discover_props_coverage(self, sport: str, **filters: Any) -> ProviderEnvelope:
        """Free diagnostic using the same filters as the documented props feed."""
        return await self.request(f"/v1/sports/{safe_component(sport)}/props/coverage",
                                  params=filters, cost=0)

    async def discover_odds_coverage(self, sport: str, **filters: Any) -> ProviderEnvelope:
        """Free diagnostic for the current game-line board and source coverage."""
        return await self.request(f"/v1/sports/{safe_component(sport)}/odds/coverage",
                                  params=filters, cost=0)

    async def fetch_snapshot(self, sport: str, **filters: Any) -> ProviderEnvelope:
        params: dict[str, Any] = {
            "markets": ",".join(self.config.markets),
            "regions": ",".join(self.config.regions),
            "oddsFormat": "decimal", "dateFormat": "iso",
            # Book/fixture poll heartbeats are diagnostics. They do not
            # certify the exact market's current price or active status.
            "include": "normalized,verification",
            # The current OpenAPI adds per-outcome FanDuel links only when a
            # matching native quote was observed on an OPEN/ACTIVE selection.
            # Links are evidence metadata; they are never order URLs here.
            "includeLinks": True,
        }
        if self.config.bookmakers:
            params["bookmakers"] = ",".join(self.config.bookmakers)
        params.update(filters)
        # Conservative local estimate; provider usage headers remain authoritative.
        books = [x for x in str(params.get("bookmakers", "")).split(",") if x]
        regions = [x for x in str(params.get("regions", "us")).split(",") if x]
        count_markets = len({x for x in str(params["markets"]).split(",") if x})
        cost = max(1, count_markets * (math.ceil(len(books) / 10) if books else len(regions)))
        result = await self.request(f"/v1/sports/{safe_component(sport)}/odds", params=params, cost=cost)
        return replace(result, metadata={**result.metadata, "sport": sport, "odds_format": "decimal"})

    async def fetch_props(self, sport: str, **filters: Any) -> ProviderEnvelope:
        # Parlay documents FanDuel per-side links only for stored native OPEN /
        # ACTIVE observations. They are evidence fields, never order URLs here.
        params: dict[str, Any] = {"limit": self.config.props_limit, "includeLinks": True}
        if self.config.props_max_age_seconds is not None:
            params["maxAgeSec"] = self.config.props_max_age_seconds
        if self.config.bookmakers:
            params["bookmakers"] = ",".join(self.config.bookmakers)
        params.update(filters)
        result = await self.request(f"/v1/sports/{safe_component(sport)}/props", params=params, cost=3)
        return replace(result, kind="props_snapshot", metadata={**result.metadata, "sport": sport})

    async def _poll_sport(self, sport: str) -> AsyncIterator[ProviderEnvelope]:
        yield await self.fetch_snapshot(sport)
        if self.config.include_props:
            yield await self.fetch_props(sport)


class ParlayWebSocket(ParlayRest):
    provider_id = "parlay_ws"

    def __init__(self, *args: Any, ws_factory: Any = websocket_connect, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._ws_factory = ws_factory
        self._sockets: dict[str, Any] = {}
        self._event_filters: dict[str, str] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._queue: asyncio.Queue[ProviderEnvelope] | None = None
        self._server_connections: dict[str, dict[str, Any]] = {}
        self._ready_sports: set[str] = set()
        self._complete_baseline_sports: set[str] = set()
        self.reconnects = 0

    async def connect(self) -> bool:
        if not await super().connect():
            return False
        if self.config.account_tier in {"free", "starter", "pro"}:
            self.state = ProviderState.ACCESS_DENIED
            self.reason = "WebSocket requires Business, Enterprise, or Scale access"
            return False
        return True

    def _record_connection(self, sport: str, payload: dict[str, Any]) -> None:
        # Health exposes only documented non-secret connection fields.
        self._ready_sports.discard(sport)
        self._complete_baseline_sports.discard(sport)
        details: dict[str, Any] = {}
        tier = payload.get("tier")
        if isinstance(tier, str) and tier in {"free", "starter", "pro", "business", "enterprise", "scale"}:
            details["tier"] = tier
        push_mode = payload.get("push_mode")
        if isinstance(push_mode, str) and push_mode in {"raw", "coalesced"}:
            details["push_mode"] = push_mode
        interval = payload.get("min_push_interval_s")
        if isinstance(interval, (int, float)) and not isinstance(interval, bool) and math.isfinite(interval) and interval >= 0:
            details["min_push_interval_s"] = interval
        self._server_connections[sport] = details

    async def health_check(self) -> dict[str, Any]:
        health = await super().health_check()
        authenticated_sports = self._server_connections.keys()
        ready_sports = self._ready_sports.intersection(authenticated_sports)
        return {
            **health,
            "authenticated": bool(authenticated_sports),
            "streaming": bool(ready_sports),
            "ready_sports": sorted(ready_sports),
            "complete_baseline_sports": sorted(self._complete_baseline_sports.intersection(ready_sports)),
            "awaiting_snapshot_sports": sorted(authenticated_sports - ready_sports),
            "configured_account_tier": self.config.account_tier,
            "server_connections": {sport: dict(details) for sport, details in self._server_connections.items()},
        }

    def stream_url(self, sport: str) -> str:
        params: dict[str, Any] = {
            "max_age_s": self.config.max_age_seconds,
            "limit": self.config.stream_limit,
            # Full rows avoid inventing row-identity semantics for diff patches.
            "diff": "false",
        }
        if self.config.bookmakers:
            params["bookmakers"] = ",".join(self.config.bookmakers)
        if self.config.markets:
            params["markets"] = ",".join(self.config.markets)
        if self.config.stream_kinds:
            params["kinds"] = ",".join(self.config.stream_kinds)
        return f"wss://parlay-api.com/v1/ws/odds/{safe_component(sport)}?{urlencode(params)}"

    def parse_frame(self, frame: str | bytes, *, has_snapshot: bool) -> ProviderEnvelope:
        try:
            body = json.loads(frame)
        except (ValueError, TypeError):
            raise ProtocolError("Invalid WebSocket JSON") from None
        if not isinstance(body, dict) or not isinstance(body.get("type"), str):
            raise ProtocolError("Invalid WebSocket envelope")
        body = self.sanitize(body)
        kind = body["type"]
        if kind in {"initial_state", "odds_update"}:
            if not isinstance(body.get("data"), list) or any(not isinstance(row, dict) for row in body["data"]):
                raise ProtocolError("Invalid odds row array")
            if kind == "odds_update" and not has_snapshot:
                raise ProtocolError("Update arrived before snapshot")
        elif kind not in {"connected", "heartbeat", "subscribed", "unsubscribed"}:
            # Unknown extension frames are observable but never treated as prices.
            kind = "provider_extension"
        complete = (
            body.get("snapshot_complete") is True
            and body.get("snapshot_scope") == "current_game_board"
            and body.get("truncated") is False
        )
        return ProviderEnvelope(
            self.provider_id, kind, body, datetime.now(UTC), time.perf_counter_ns(),
            published_at=parse_timestamp(body.get("timestamp")),
            snapshot_complete=complete,
            metadata={"timestamp_semantics": "provider_send_time", "requires_verified_mapping": True},
        )

    async def subscribe(self, sports: list[str]) -> None:
        await super().subscribe(sports)
        if self._queue is not None:
            for sport in sports:
                if sport not in self._tasks:
                    self._tasks[sport] = asyncio.create_task(self._run_sport(sport, self._queue))

    async def unsubscribe(self, sports: list[str]) -> None:
        await super().unsubscribe(sports)
        for sport in sports:
            task = self._tasks.pop(sport, None)
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            socket = self._sockets.pop(sport, None)
            if socket:
                await socket.close()

    async def subscribe_event(self, sport: str, event_id: str) -> None:
        if sport not in self._subscriptions:
            raise UnsupportedOperation("Subscribe the sport before filtering its events")
        self._event_filters[sport] = event_id
        if socket := self._sockets.get(sport):
            await socket.send(json.dumps({"type": "subscribe", "event_id": event_id}))

    async def unsubscribe_event(self, sport: str) -> None:
        self._event_filters.pop(sport, None)
        if socket := self._sockets.get(sport):
            await socket.send(json.dumps({"type": "unsubscribe"}))

    def _apply_close_code(self, code: int | None) -> bool:
        terminal = {
            1008: (ProviderState.UNAUTHORIZED, "Invalid API key"),
            4001: (ProviderState.ACCESS_DENIED, "WebSocket tier entitlement required"),
            4003: (ProviderState.UNAUTHORIZED, "Provider reported missing authentication"),
            4004: (ProviderState.QUOTA_EXHAUSTED, "Provider monthly credits exhausted"),
            4005: (ProviderState.ACCESS_DENIED, "Provider rejected subscription filters"),
        }
        if code in terminal:
            self.state, self.reason = terminal[code]
            return False
        self.state, self.reason = ProviderState.BACKOFF, f"WebSocket interrupted (code {code or 'unknown'})"
        return True

    async def _run_sport(self, sport: str, queue: asyncio.Queue[ProviderEnvelope]) -> None:
        backoff = Backoff()
        while not self._closed and sport in self._subscriptions:
            retry = True
            has_snapshot = False
            try:
                async with self._ws_factory(
                    self.stream_url(sport),
                    additional_headers={"X-API-Key": self._credential},
                    ping_interval=20, ping_timeout=20, open_timeout=self.config.request_timeout_seconds,
                    max_size=8 * 1024 * 1024, max_queue=32,
                ) as socket:
                    self._sockets[sport] = socket
                    while not self._closed:
                        frame = await asyncio.wait_for(socket.recv(), timeout=self.config.watchdog_seconds)
                        envelope = self.parse_frame(frame, has_snapshot=has_snapshot)
                        self.last_received_at = envelope.received_at
                        if envelope.kind == "connected":
                            self._record_connection(sport, envelope.payload)
                            self.state = ProviderState.CONNECTED
                            self.reason = "Authenticated; awaiting initial snapshot"
                        if envelope.kind == "initial_state":
                            has_snapshot = True
                            if sport in self._server_connections:
                                self._ready_sports.add(sport)
                            if envelope.snapshot_complete:
                                self._complete_baseline_sports.add(sport)
                            else:
                                self._complete_baseline_sports.discard(sport)
                            backoff.reset()
                            self.state, self.reason = ProviderState.CONNECTED, "Receiving authenticated stream"
                            if event_id := self._event_filters.get(sport):
                                await socket.send(json.dumps({"type": "subscribe", "event_id": event_id}))
                        await queue.put(replace(envelope, metadata={**envelope.metadata, "sport": sport}))
            except asyncio.CancelledError:
                raise
            except ConnectionClosed as error:
                retry = self._apply_close_code(error.rcvd.code if error.rcvd else None)
            except InvalidStatus as error:
                status = error.response.status_code
                if status in {401, 403}:
                    self.state, self.reason = ProviderState.ACCESS_DENIED, f"WebSocket handshake refused (HTTP {status})"
                    retry = False
                else:
                    self.state, self.reason = ProviderState.BACKOFF, f"WebSocket handshake failed (HTTP {status})"
            except (OSError, TimeoutError, ProtocolError):
                self.errors += 1
                self.state, self.reason = ProviderState.BACKOFF, "WebSocket interrupted; snapshot required"
            finally:
                self._sockets.pop(sport, None)
                self._server_connections.pop(sport, None)
                self._ready_sports.discard(sport)
                self._complete_baseline_sports.discard(sport)
            await queue.put(ProviderEnvelope(
                self.provider_id, "feed_invalidated", {"sport": sport, "reason": self.reason},
                datetime.now(UTC), time.perf_counter_ns(),
            ))
            if not retry or self._closed:
                return
            self.reconnects += 1
            await asyncio.sleep(backoff.next_delay())

    async def stream_updates(self) -> AsyncIterator[ProviderEnvelope]:
        if not await self.connect():
            return
        self._queue = asyncio.Queue(maxsize=1024)
        self._tasks = {sport: asyncio.create_task(self._run_sport(sport, self._queue))
                       for sport in sorted(self._subscriptions)}
        try:
            while not self._closed:
                if not self._tasks:
                    return
                try:
                    yield await asyncio.wait_for(self._queue.get(), timeout=1)
                except TimeoutError:
                    if all(task.done() for task in self._tasks.values()) and self._queue.empty():
                        # Surface an unexpected adapter failure without leaking its message.
                        for task in self._tasks.values():
                            if not task.cancelled() and task.exception() is not None:
                                self.state, self.reason = ProviderState.ERROR, "Stream task failed"
                        return
        finally:
            for task in self._tasks.values():
                task.cancel()
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
            self._tasks.clear()
            self._queue = None

    async def disconnect(self) -> None:
        self._closed = True
        self._server_connections.clear()
        self._ready_sports.clear()
        self._complete_baseline_sports.clear()
        for socket in list(self._sockets.values()):
            await socket.close()
        await super().disconnect()


class ParlayGameBoardWebSocket(ParlayWebSocket):
    """Independent game-line stream; prop coverage remains on parlay_ws."""
    provider_id = "parlay_game_ws"
    config_class = ParlayGameBoardConfig

    def stream_url(self, sport: str) -> str:
        # The documented current_game_board replacement baseline requires
        # snapshot=complete in addition to the exact game/book filters.
        # Completeness does not certify any individual unchanged price.
        return super().stream_url(sport) + "&snapshot=complete"


ParlayRest.manifest = provider_manifest(
    "parlay_rest", "ParlayAPI REST", transports=["rest"], env="PARLAY_API_KEY",
    config=ParlayConfig, docs=["https://parlay-api.com/docs", "https://parlay-api.com/openapi.json"],
    limitations=["Returned market coverage varies by book and query.", "Native settlement/identity mapping must be verified before pricing."],
)
ParlayWebSocket.manifest = provider_manifest(
    "parlay_ws", "ParlayAPI WebSocket", transports=["websocket"], env="PARLAY_API_KEY",
    config=ParlayConfig, docs=["https://parlay-api.com/docs/websocket", "https://parlay-api.com/v1/asyncapi.json"],
    limitations=["Business tier or higher; 4001 disables the stream.", "No documented lossless sequence/replay cursor.",
                 "A recent_rows snapshot is incomplete; frame timestamps are provider send clocks."],
)
ParlayGameBoardWebSocket.manifest = provider_manifest(
    "parlay_game_ws", "ParlayAPI complete game board WebSocket", transports=["websocket"],
    env="PARLAY_API_KEY", config=ParlayGameBoardConfig,
    docs=["https://parlay-api.com/docs/websocket", "https://parlay-api.com/v1/asyncapi.json"],
    limitations=["Business tier or higher; 4001 disables the stream.",
                 "The baseline is complete only when the server confirms its scope, completeness and no truncation.",
                 "A complete board does not prove each retained quote is currently active upstream."],
)
