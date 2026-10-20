"""Reusable transport seams: no invented endpoint or implicit authentication."""
from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import httpx

from .base import ProviderEnvelope, ProtocolError


async def parse_sse(lines: AsyncIterator[str], *, max_event_bytes: int = 1024 * 1024) -> AsyncIterator[dict]:
    """SSE framing, including multiline data, comments, IDs, and event names."""
    data: list[str] = []
    event_name, last_id = "message", None
    size = 0
    async for line in lines:
        if line == "":
            if data:
                yield {"event": event_name, "id": last_id, "data": "\n".join(data)}
            data, event_name, size = [], "message", 0
            continue
        if line.startswith(":"):
            continue
        name, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if name == "data":
            size += len(value.encode("utf-8"))
            if size > max_event_bytes:
                raise ProtocolError("SSE event exceeds configured size limit")
            data.append(value)
        elif name == "event":
            event_name = value
        elif name == "id" and "\x00" not in value:
            last_id = value
        # Retry is only advice; a plugin defines its own bounded reconnect policy.
    # A truncated frame at EOF is discarded, never presented as a complete event.


async def sse_updates(
    client: httpx.AsyncClient, url: str, *, headers: dict[str, str],
    provider_id: str, last_event_id: str | None = None,
) -> AsyncIterator[ProviderEnvelope]:
    """Supply only a URL/auth scheme verified in that provider's official docs.

    Last-Event-ID is optional: only use it when the provider documents resumable
    event IDs. This utility does not pretend that all SSE streams support replay.
    """
    request_headers = {**headers, "Accept": "text/event-stream"}
    if last_event_id is not None:
        request_headers["Last-Event-ID"] = last_event_id
    async with client.stream("GET", url, headers=request_headers) as response:
        if response.status_code != 200:
            raise ProtocolError(f"SSE HTTP {response.status_code}")
        if "text/event-stream" not in response.headers.get("content-type", ""):
            raise ProtocolError("Expected SSE content type")
        async for item in parse_sse(response.aiter_lines()):
            try:
                payload = json.loads(item["data"])
            except ValueError:
                raise ProtocolError("SSE payload was not JSON") from None
            yield ProviderEnvelope(provider_id, item["event"], payload,
                                   datetime.now(UTC), time.perf_counter_ns(),
                                   metadata={"event_id": item["id"]})


async def run_mapped_provider(
    provider: Any,
    mapper: Callable[[ProviderEnvelope], list[Any]],
    publish: Callable[[Any], Awaitable[None]],
    audit: Callable[[ProviderEnvelope], Awaitable[None]],
) -> None:
    """Independent audit observer and explicit mapper; no execution service exists."""
    try:
        async for raw in provider.stream_updates():
            await audit(raw)
            for normalized in mapper(raw):
                await publish(normalized)
    finally:
        await provider.disconnect()


def map_json_fields(payload: dict, mapping: dict[str, str]) -> dict:
    """Minimal strict mapping example: missing paths are errors, never defaults.

    Example: {"source_time": "quote.observedAt", "native_id": "event.id"}.
    Field projection alone cannot establish matching or settlement equivalence.
    """
    result = {}
    for target, path in mapping.items():
        value: Any = payload
        for segment in path.split("."):
            if not isinstance(value, dict) or segment not in value:
                raise ProtocolError(f"Required mapped field missing: {target}")
            value = value[segment]
        result[target] = value
    return result


async def replay_envelopes(
    envelopes: list[ProviderEnvelope], *, speed: float = 0,
) -> AsyncIterator[ProviderEnvelope]:
    """Replay preserved clocks; speed=0 runs deterministically without wall sleeps."""
    previous = None
    for envelope in envelopes:
        if speed > 0 and previous is not None:
            delay = max(0, (envelope.received_at - previous).total_seconds() / speed)
            await asyncio.sleep(delay)
        yield envelope
        previous = envelope.received_at
