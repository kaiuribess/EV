"""Allowlisted, read-only ProphetX catalog diagnostics from an existing session.

Discovery is not market matching or execution evidence. No raw provider body,
token, account identity, or credential crosses this boundary.
"""
from __future__ import annotations

from datetime import datetime, timedelta


def _label(value, limit=120):
    return str(value)[:limit] if isinstance(value, (str, int, float)) and not isinstance(value, bool) else None


def summarize_events(payload: dict, *, limit: int) -> dict:
    data = payload.get("data") if isinstance(payload, dict) else None
    rows = data.get("sport_events") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise ValueError("Unexpected ProphetX event catalog shape")
    events = []
    for row in rows[:limit]:
        if not isinstance(row, dict):
            continue
        native_id = row.get("event_id")
        if isinstance(native_id, bool) or not str(native_id).isascii() or not str(native_id).isdigit():
            continue
        events.append({"event_id": str(native_id), "name": _label(row.get("name")),
                       "scheduled": _label(row.get("scheduled"), 40),
                       "status": _label(row.get("status"), 40)})
    return {"total": len(rows), "truncated": len(rows) > limit, "events": events,
            "read_only": True, "mapping_verified": False}


def summarize_markets(payload: dict, *, limit: int) -> dict:
    data = payload.get("data") if isinstance(payload, dict) else None
    rows = data.get("markets") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise ValueError("Unexpected ProphetX market catalog shape")
    markets = []
    for row in rows[:limit]:
        if not isinstance(row, dict):
            continue
        selections = row.get("selections")
        # Only explicit fields relevant to a review. Do not turn the catalog
        # representation into an executable order book or infer settlement.
        selection_count = len(selections) if isinstance(selections, list) else None
        markets.append({"id": _label(row.get("id"), 40), "name": _label(row.get("name")),
                        "type": _label(row.get("type"), 50), "sub_type": _label(row.get("sub_type"), 100),
                        "status": _label(row.get("status"), 40), "period": _label(row.get("period"), 40),
                        "selection_count": selection_count,
                        "selection_shape": ("nested" if isinstance(selections, list) and selections and isinstance(selections[0], list)
                                            else "flat" if isinstance(selections, list) and selections and isinstance(selections[0], dict)
                                            else "empty" if selections == [] else "unknown")})
    return {"total": len(rows), "truncated": len(rows) > limit, "markets": markets,
            "read_only": True, "mapping_verified": False}


def open_event_ids(payloads: list[dict], *, now: datetime, max_events: int,
                   lookahead_hours: int = 30, started_hours: int = 6) -> list[str]:
    """Choose bounded active/upcoming catalog IDs for market-data subscriptions.

    This validates only stream scope, not the event's settlement or tradability.
    Unknown status, clocks, identity, or conflicting duplicate IDs are omitted.
    """
    if now.utcoffset() is None or not 1 <= max_events <= 50:
        raise ValueError("Bounded discovery requires an aware clock")
    candidates, conflicts = {}, set()
    for payload in payloads:
        data = payload.get("data") if isinstance(payload, dict) else None
        rows = data.get("sport_events") if isinstance(data, dict) else None
        if not isinstance(rows, list) or len(rows) > 5000:
            raise ValueError("Unexpected ProphetX event catalog shape")
        for row in rows:
            if not isinstance(row, dict) or row.get("status") not in {"live", "not_started"}:
                continue
            native_id = row.get("event_id")
            if (isinstance(native_id, bool) or not isinstance(native_id, (str, int))
                    or not str(native_id).isascii() or not str(native_id).isdigit()):
                continue
            try:
                stamp = datetime.fromisoformat(row["scheduled"].replace("Z", "+00:00"))
            except (KeyError, AttributeError, ValueError, TypeError):
                continue
            if stamp.utcoffset() is None or stamp > now+timedelta(hours=lookahead_hours):
                continue
            if (row["status"] == "live" and stamp < now-timedelta(hours=started_hours)
                    or row["status"] == "not_started" and stamp < now-timedelta(minutes=15)):
                continue
            key = str(native_id)
            identity = (row.get("name"), stamp, row["status"])
            if key in candidates and candidates[key] != identity:
                conflicts.add(key)
            else:
                candidates[key] = identity
    for key in conflicts:
        candidates.pop(key, None)
    return [key for key, _ in sorted(candidates.items(), key=lambda item: (
        item[1][2] != "not_started", item[1][1], item[0]))[:max_events]]
