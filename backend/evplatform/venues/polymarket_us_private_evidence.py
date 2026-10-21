"""Bounded private-wire evidence for comparing retail SDK and served contracts.

This captures subscriptions only. SDK identifiers can be absent or blank; their
presence does not attest accounting semantics, cash posting, or replay coverage.
Raw frames belong only in protected backend artifacts, never dashboard events.
"""
from __future__ import annotations

import asyncio
from collections import Counter
import json

from ..core.models import utcnow
from ..core.lifecycle_capture import sanitize


def private_field_presence(payload):
    """Allowlisted shape evidence. No identifier or account value is returned."""
    result = {}
    fields = {
        "accountBalancesUpdate": ("balanceChange", ("id", "accountName", "modifiedSecurityId", "entryType", "updateTime")),
        "positionSubscription": (None, ("tradeId", "referenceId", "allocationGroupId", "entryType", "updateTime")),
    }
    for envelope, (nested, names) in fields.items():
        body = payload.get(envelope)
        if not isinstance(body, dict):
            continue
        if nested:
            body = body.get(nested)
        if not isinstance(body, dict):
            continue
        for field in names:
            value = body.get(field)
            result[f"{envelope}.{field}"] = ("absent" if field not in body else
                "null" if value is None else "blank" if value == "" else
                "nonempty_string" if isinstance(value, str) and value.strip() else "invalid_type_or_blank")
    return result


async def capture_private_evidence(client, *, seconds=15, max_frames=256, max_bytes=4*1024*1024):
    if (type(seconds) is not int or not 1 <= seconds <= 60 or type(max_frames) is not int
            or not 1 <= max_frames <= 1000 or not 1024 <= max_bytes <= 16*1024*1024):
        raise ValueError("Use bounded private evidence limits")
    if client.venue_id != "polymarket_us" or not client.authenticated or client.settings.environment != "production":
        raise ValueError("Authenticated personal US account required")
    if client.market_slugs or not client.private_updates:
        raise ValueError("Private-only subscription scope required")
    frames, counts, presence = [], Counter(), Counter()
    stopped, total_bytes = "time_bound", 0
    stream = client.stream_updates()
    started = utcnow()
    try:
        async with asyncio.timeout(seconds):
            async for frame in stream:
                clean = sanitize(frame)
                size = len(json.dumps(clean, allow_nan=False).encode())
                if total_bytes + size > max_bytes:
                    stopped = "byte_bound"
                    break
                if frame.get("type") == "transport_invalidated":
                    counts["transport_invalidated"] += 1
                elif frame.get("channel") == "private":
                    payload = frame.get("payload", {})
                    if isinstance(payload, dict):
                        for name in ("orderSubscriptionSnapshot", "orderSubscriptionUpdate", "positionSubscription",
                                     "accountBalancesSnapshot", "accountBalancesUpdate"):
                            if name in payload:
                                counts[name] += 1
                        for name, state in private_field_presence(payload).items():
                            presence[f"{name}:{state}"] += 1
                frames.append({"received_at": utcnow().isoformat(), "frame": clean})
                total_bytes += size
                if len(frames) >= max_frames:
                    stopped = "frame_bound"
                    break
            else:
                stopped = "stream_ended"
    except TimeoutError:
        pass
    finally:
        # Capture before closing: the client clears connection-specific evidence
        # on teardown. Expose only known subscription kinds and status enums.
        health = client.health_check() if callable(getattr(client, "health_check", None)) else {}
        known = {"SUBSCRIPTION_TYPE_ORDER", "SUBSCRIPTION_TYPE_ORDER_SNAPSHOT",
                 "SUBSCRIPTION_TYPE_POSITION", "SUBSCRIPTION_TYPE_ACCOUNT_BALANCE"}
        subscriptions = {key: value for key, value in health.get("private_subscription_evidence", {}).items()
                         if key in known and value in {"awaiting_data", "data_observed"}}
        await stream.aclose()
    return {"started_at": started.isoformat(), "finished_at": utcnow().isoformat(), "frames": frames,
        "summary": {"frame_count": len(frames), "counts": dict(counts), "field_presence": dict(presence),
            "stopped": stopped, "replay_coverage_verified": False, "cash_posting_join_verified": False,
            "subscription_evidence": subscriptions,
            "orders_submitted": 0, "note": "Observed wire fields only; no account or settlement certification"}}
