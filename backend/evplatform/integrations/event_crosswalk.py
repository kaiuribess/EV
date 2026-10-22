"""Read-only exact NBA event candidates across source odds and native catalog.

Matching event participants and scheduled start is useful discovery evidence,
but does not identify an equivalent wager, outcome or settlement contract.
"""
from __future__ import annotations

from collections import defaultdict
from .base import parse_timestamp
from .team_aliases import NBA_TEAMS


_NAMES = set(NBA_TEAMS.values())


def _teams(value):
    if not isinstance(value, list) or len(value) != 2 or any(not isinstance(x, str) for x in value):
        return None
    result = tuple(NBA_TEAMS.get(x, x) for x in value)
    return result if result[0] != result[1] and set(result) <= _NAMES else None


def _native_event(row):
    if not isinstance(row, dict) or row.get("status") not in {"not_started", "live"}:
        return None
    event_id, label = row.get("event_id"), row.get("name")
    if not isinstance(event_id, str) or not event_id.isascii() or not event_id.isdigit() or not isinstance(label, str):
        return None
    start = parse_timestamp(row.get("scheduled"))
    if start is None:
        return None
    pairs = [(home, away) for home in _NAMES for away in _NAMES if home != away
             and label == f"{away} at {home}"]
    return (event_id, pairs[0], start, row["status"]) if len(pairs) == 1 else None


def event_candidates(native_catalog: dict, source_snapshot: dict, *, max_start_delta_seconds=120) -> dict:
    """Require unambiguous exact names and bounded start times; never publish mappings."""
    if not 0 <= max_start_delta_seconds <= 900:
        raise ValueError("Event start tolerance must be bounded")
    events = native_catalog.get("events") if isinstance(native_catalog, dict) else None
    rows = source_snapshot.get("items") if isinstance(source_snapshot, dict) else None
    if not isinstance(events, list) or len(events) > 100 or not isinstance(rows, list) or len(rows) > 10000:
        raise ValueError("Expected bounded native catalog and source review window")
    native = [item for row in events if (item := _native_event(row)) is not None]
    grouped, conflicts = {}, set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        identity = row.get("identity")
        if not isinstance(identity, dict):
            continue
        context = identity.get("context") or {}
        if not isinstance(context, dict):
            continue
        provider, source_id = identity.get("provider"), identity.get("event_id")
        if not isinstance(provider, str) or not isinstance(source_id, str) or not source_id:
            continue
        if context.get("event_sport") not in (None, "nba", "basketball_nba"):
            continue
        teams, start = _teams(context.get("participants")), parse_timestamp(context.get("scheduled_start"))
        if teams is None or start is None:
            continue
        key = provider, source_id
        candidate = (teams, start)
        prior = grouped.get(key)
        if prior is not None and prior["identity"] != candidate:
            conflicts.add(key)
            continue
        if prior is None:
            grouped[key] = {"identity": candidate, "books": set(), "rows": 0}
        grouped[key]["rows"] += 1
        if isinstance(identity.get("bookmaker"), str):
            grouped[key]["books"].add(identity["bookmaker"])
    for key in conflicts:
        grouped.pop(key, None)
    matches, ambiguous = [], 0
    for (provider, source_id), item in sorted(grouped.items()):
        teams, start = item["identity"]
        possible = [event for event in native if event[1] == teams
                    and abs((event[2]-start).total_seconds()) <= max_start_delta_seconds]
        if len(possible) > 1:
            ambiguous += 1
        if len(possible) != 1:
            continue
        native_id, _, native_start, status = possible[0]
        matches.append({"provider": provider, "source_event_id": source_id,
                        "native_venue": "prophetx", "native_event_id": native_id,
                        "home": teams[0], "away": teams[1],
                        "source_start": start.isoformat(), "native_start": native_start.isoformat(),
                        "start_delta_seconds": abs((native_start-start).total_seconds()),
                        "native_event_status": status, "bookmakers": sorted(item["books"]),
                        "source_observations": item["rows"], "candidate_only": True})
    collection = source_snapshot.get("collection") or {}
    providers = source_snapshot.get("providers") or []
    capped = any(isinstance(item, dict) and isinstance(item.get("pending"), int)
                 and isinstance(item.get("capacity"), int) and item["pending"] >= item["capacity"]
                 for item in providers) if isinstance(providers, list) else True
    return {"schema_version": 1, "matches": matches, "ambiguous_source_events": ambiguous,
            "match_count": len(matches),
            "conflicting_source_events": len(conflicts), "source_window_truncated": bool(
                isinstance(collection, dict) and collection.get("truncated")),
            "source_quarantine_capped": capped,
            "native_catalog_truncated": native_catalog.get("truncated") is True,
            "event_identity_verified": False, "market_mapping_verified": False,
            "settlement_equivalence_verified": False, "execution_authorized": False}
