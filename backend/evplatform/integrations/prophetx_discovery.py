"""Allowlisted, read-only ProphetX catalog diagnostics from an existing session.

Discovery is not market matching or execution evidence. No raw provider body,
token, account identity, or credential crosses this boundary.
"""
from __future__ import annotations

from datetime import datetime, timedelta


def active_tournament_ids(payload: dict, leagues: list[str]) -> dict[str, int]:
    """Accept only exact named leagues from the authenticated active catalog.

    A tournament name is identity evidence for research discovery, not proof
    that any particular market is equivalent or executable.
    """
    data = payload.get("data") if isinstance(payload, dict) else None
    rows = data.get("tournaments") if isinstance(data, dict) else None
    if not isinstance(rows, list) or len(rows) > 5000:
        raise ValueError("Unexpected ProphetX tournament catalog shape")
    requested = set(leagues)
    selected = {}
    for row in rows:
        if not isinstance(row, dict) or row.get("name") not in requested:
            continue
        native_id = row.get("id")
        if (isinstance(native_id, bool) or not isinstance(native_id, (int, str))
                or not str(native_id).isascii() or not str(native_id).isdigit()
                or not 0 < int(native_id) <= 1_000_000_000):
            raise ValueError("Invalid ProphetX tournament ID")
        name = row["name"]
        if name in selected and selected[name] != int(native_id):
            raise ValueError("Conflicting ProphetX tournament identity")
        if any(other_name != name and other_id == int(native_id)
               for other_name, other_id in selected.items()):
            raise ValueError("Tournament ID belongs to multiple leagues")
        selected[name] = int(native_id)
    return selected


def summarize_tournaments(payload: dict, *, limit: int = 50) -> dict:
    data = payload.get("data") if isinstance(payload, dict) else None
    rows = data.get("tournaments") if isinstance(data, dict) else None
    if not isinstance(rows, list) or len(rows) > 5000:
        raise ValueError("Unexpected ProphetX tournament catalog shape")
    tournaments = []
    for row in rows[:limit]:
        if not isinstance(row, dict):
            continue
        native_id = row.get("id")
        if (isinstance(native_id, bool) or not isinstance(native_id, (int, str))
                or not str(native_id).isascii() or not str(native_id).isdigit()):
            continue
        tournaments.append({"id": str(native_id), "name": _label(row.get("name"))})
    return {"total": len(rows), "truncated": len(rows) > limit,
            "tournaments": tournaments, "read_only": True, "mapping_verified": False}


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


def summarize_markets(payload: dict, *, limit: int, sub_type: str | None = None) -> dict:
    data = payload.get("data") if isinstance(payload, dict) else None
    rows = data.get("markets") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise ValueError("Unexpected ProphetX market catalog shape")
    unfiltered_total = len(rows)
    if sub_type is not None:
        if not isinstance(sub_type, str) or not sub_type or len(sub_type) > 64:
            raise ValueError("Bounded ProphetX market subtype required")
        # Filter the complete REST response before the review cap. A single
        # moneyline found among hundreds of props can then be checked against
        # every other moneyline without exporting the full board.
        rows = [row for row in rows if isinstance(row, dict) and row.get("sub_type") == sub_type]
    markets = []
    for row in rows[:limit]:
        if not isinstance(row, dict):
            continue
        selections = row.get("selections")
        # Only explicit fields relevant to a review. Do not turn the catalog
        # representation into an executable order book or infer settlement.
        selection_count = len(selections) if isinstance(selections, list) else None
        # The catalog may group opposing selections in nested lists. Expose
        # only bounded public strike identifiers and labels for manual review;
        # no executable price, stake, wallet, or raw provider object leaves
        # this endpoint. Unknown shapes stay unknown rather than being parsed
        # into a purportedly complete two-sided market.
        selection_review = []
        selection_review_truncated = False
        if isinstance(selections, list):
            groups = selections if selections and all(isinstance(group, list) for group in selections) else [selections]
            selection_review_truncated = len(groups) > 20 or any(
                isinstance(group, list) and len(group) > 20 for group in groups[:20])
            for group_index, group in enumerate(groups[:20]):
                if not isinstance(group, list):
                    continue
                for selection in group[:20]:
                    if not isinstance(selection, dict):
                        continue
                    selection_review.append({
                        "group": group_index,
                        "strike_id": _label(selection.get("strike_id"), 64),
                        "outcome_id": _label(selection.get("outcome_id"), 64),
                        "name": _label(selection.get("name"), 120),
                        "line": _label(selection.get("line"), 40),
                        "status": _label(selection.get("status"), 40),
                        "updated_at": _label(selection.get("updated_at"), 40),
                    })
                    if len(selection_review) >= 50:
                        break
                if len(selection_review) >= 50:
                    break
        markets.append({"id": _label(row.get("id"), 40), "name": _label(row.get("name")),
                        "type": _label(row.get("type"), 50), "sub_type": _label(row.get("sub_type"), 100),
                        "status": _label(row.get("status"), 40), "period": _label(row.get("period"), 40),
                        "selection_count": selection_count,
                        "selection_review": selection_review,
                        "selection_review_truncated": selection_review_truncated or len(selection_review) >= 50,
                        "selection_shape": ("nested" if isinstance(selections, list) and selections and isinstance(selections[0], list)
                                            else "flat" if isinstance(selections, list) and selections and isinstance(selections[0], dict)
                                            else "empty" if selections == [] else "unknown")})
    return {"total": len(rows), "unfiltered_total": unfiltered_total,
            "filter_sub_type": sub_type, "truncated": len(rows) > limit, "markets": markets,
            "read_only": True, "mapping_verified": False}


def summarize_batched_markets(payload: dict, *, event_ids: list[str], limit: int,
                              sub_type: str | None = None) -> dict:
    """Review the documented event-ID keyed batch as one complete API read."""
    data = payload.get("data") if isinstance(payload, dict) else None
    if (not isinstance(data, dict) or not event_ids or len(event_ids) > 10
            or len(set(event_ids)) != len(event_ids) or set(data) != set(event_ids)):
        raise ValueError("Incomplete ProphetX batched market catalog")
    by_event = {}
    for event_id in event_ids:
        if not isinstance(data[event_id], list):
            raise ValueError("Invalid ProphetX batched market collection")
        by_event[event_id] = summarize_markets({"data": {"markets": data[event_id]}},
                                                 limit=limit, sub_type=sub_type)
    return {"event_ids": event_ids, "by_event": by_event,
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
