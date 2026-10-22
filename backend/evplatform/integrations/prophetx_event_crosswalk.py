"""Read-only NBA fixture and strike leads from ProphetX's live catalog.

Catalog labels and strike IDs identify a possible sporting question. They do
not establish sportsbook settlement equivalence, a current executable offer,
account readiness, or permission to submit an order.
"""
from __future__ import annotations

from collections import defaultdict
import re

from .base import parse_timestamp
from .team_aliases import NBA_TEAMS


_STRIKE = re.compile(r"[0-9a-f]{32}")
_NUMBER = re.compile(r"[0-9]+")
_NAMES = frozenset(NBA_TEAMS.values())


def _native_moneyline(catalog: dict, *, home: str, away: str) -> dict | None:
    """Require one complete, unambiguous two-outcome catalog identity."""
    if (not isinstance(catalog, dict) or catalog.get("read_only") is not True
            or catalog.get("truncated") is not False or not isinstance(catalog.get("markets"), list)):
        return None
    if catalog.get("filter_sub_type") not in (None, "moneyline"):
        return None
    rows = [row for row in catalog["markets"] if isinstance(row, dict)
            and row.get("type") == "moneyline" and row.get("sub_type") == "moneyline"]
    if len(rows) != 1:
        return None
    market = rows[0]
    if (market.get("status") != "active" or market.get("selection_shape") != "nested"
            or market.get("selection_count") != 2 or market.get("selection_review_truncated") is not False
            or not isinstance(market.get("selection_review"), list)
            or not isinstance(market.get("id"), str) or not _NUMBER.fullmatch(market["id"])):
        return None
    groups = defaultdict(set)
    for row in market["selection_review"]:
        if not isinstance(row, dict) or type(row.get("group")) is not int or row["group"] not in (0, 1):
            return None
        strike, outcome, name = row.get("strike_id"), row.get("outcome_id"), row.get("name")
        if (not isinstance(strike, str) or not _STRIKE.fullmatch(strike)
                or not isinstance(outcome, str) or not _NUMBER.fullmatch(outcome)
                or name not in {home, away}):
            return None
        groups[row["group"]].add((name, strike, outcome))
    if set(groups) != {0, 1} or any(len(group) != 1 for group in groups.values()):
        return None
    sides = [next(iter(groups[index])) for index in (0, 1)]
    if {side[0] for side in sides} != {home, away} or len({side[1] for side in sides}) != 2:
        return None
    return {"native_market_id": market["id"], "outcomes": [
        {"team": name, "strike_id": strike, "outcome_id": outcome}
        for name, strike, outcome in sorted(sides)]}


def fixture_candidates(event_catalog: dict, markets_by_event: dict, source_snapshot: dict) -> dict:
    """Join exact NBA team labels and tipoff times; never publish a mapping."""
    events = event_catalog.get("events") if isinstance(event_catalog, dict) else None
    rows = source_snapshot.get("items") if isinstance(source_snapshot, dict) else None
    if (not isinstance(events, list) or len(events) > 50 or not isinstance(rows, list)
            or len(rows) > 10000 or not isinstance(markets_by_event, dict)
            or len(markets_by_event) > 50 or event_catalog.get("read_only") is not True):
        raise ValueError("Bounded read-only ProphetX and source reviews required")
    unreviewed_events = sum(isinstance(event, dict) and event.get("status") == "not_started"
                            and str(event.get("event_id")) not in markets_by_event for event in events)
    native, native_conflicts = {}, set()
    for event in events:
        if not isinstance(event, dict) or event.get("status") != "not_started":
            continue
        event_id, label = event.get("event_id"), event.get("name")
        if (not isinstance(event_id, str) or not _NUMBER.fullmatch(event_id)
                or not isinstance(label, str) or label.count(" at ") != 1):
            continue
        away, home = label.split(" at ")
        start = parse_timestamp(event.get("scheduled"))
        if home not in _NAMES or away not in _NAMES or home == away or start is None:
            continue
        moneyline = _native_moneyline(markets_by_event.get(event_id), home=home, away=away)
        if moneyline is None:
            continue
        candidate = (home, away, start, moneyline)
        if event_id in native and native[event_id] != candidate:
            native_conflicts.add(event_id)
        else:
            native[event_id] = candidate
    for event_id in native_conflicts:
        native.pop(event_id, None)

    sources, source_conflicts = {}, set()
    for row in rows:
        identity = row.get("identity") if isinstance(row, dict) else None
        context = identity.get("context") if isinstance(identity, dict) else None
        if not isinstance(context, dict):
            continue
        provider, source_id = identity.get("provider"), identity.get("event_id")
        nba = context.get("event_sport") in ("nba", "basketball_nba")
        oddspapi_nba = (provider == "oddspapi_rest" and context.get("event_sport") == 11
                         and context.get("tournament_id") == 132 and context.get("event_league") == "NBA")
        if not (nba or oddspapi_nba) or not isinstance(provider, str) or not isinstance(source_id, str):
            continue
        participants = context.get("participants")
        start = parse_timestamp(context.get("scheduled_start"))
        if (not isinstance(participants, list) or len(participants) != 2
                or not all(isinstance(name, str) for name in participants) or start is None):
            continue
        teams = tuple(NBA_TEAMS.get(name, name) for name in participants)
        if teams[0] not in _NAMES or teams[1] not in _NAMES or teams[0] == teams[1]:
            continue
        key, value = (provider, source_id), (teams, start)
        if key in sources and sources[key]["identity"] != value:
            source_conflicts.add(key)
            continue
        sources.setdefault(key, {"identity": value, "books": set(), "rows": 0})
        sources[key]["rows"] += 1
        if isinstance(identity.get("bookmaker"), str):
            sources[key]["books"].add(identity["bookmaker"])
    for key in source_conflicts:
        sources.pop(key, None)

    matches, schedule_disagreements = [], []
    for event_id, (home, away, start, moneyline) in sorted(native.items()):
        for (provider, source_id), source in sorted(sources.items()):
            (source_home, source_away), source_start = source["identity"]
            if (source_home, source_away) != (home, away):
                continue
            delta = abs((source_start - start).total_seconds())
            if delta > 120:
                if delta <= 3600 and len(schedule_disagreements) < 100:
                    schedule_disagreements.append({"provider": provider, "source_event_id": source_id,
                        "native_event_id": event_id, "source_start": source_start.isoformat(),
                        "native_start": start.isoformat(), "difference_seconds": delta})
                continue
            matches.append({"provider": provider, "source_event_id": source_id,
                "native_venue": "prophetx", "native_event_id": event_id,
                "native_market_id": moneyline["native_market_id"],
                "native_outcomes": moneyline["outcomes"], "home": home, "away": away,
                "source_start": source_start.isoformat(), "native_start": start.isoformat(),
                "native_start_informational": True, "schedule_verified": False,
                "bookmakers": sorted(source["books"]), "source_observations": source["rows"],
                "source_start_conflict": False, "candidate_only": True,
                "market_mapping_verified": False, "settlement_equivalence_verified": False,
                "native_quote_freshness_verified": False, "executable_depth_verified": False})
    starts_by_event = defaultdict(list)
    for match in matches:
        starts_by_event[match["native_event_id"]].append(parse_timestamp(match["source_start"]))
    disputed = {event_id for event_id, starts in starts_by_event.items()
                if (max(starts) - min(starts)).total_seconds() > 120}
    disputed.update(item["native_event_id"] for item in schedule_disagreements)
    for match in matches:
        match["source_start_conflict"] = match["native_event_id"] in disputed
    providers = source_snapshot.get("providers")
    capped = any(isinstance(p, dict) and isinstance(p.get("pending"), int)
                 and isinstance(p.get("capacity"), int) and p["pending"] >= p["capacity"]
                 for p in providers) if isinstance(providers, list) else True
    return {"schema_version": 1, "matches": matches, "match_count": len(matches),
            "conflicting_source_events": len(source_conflicts),
            "conflicting_native_events": len(native_conflicts),
            "cross_provider_start_conflicts": len(disputed),
            "schedule_disagreements": schedule_disagreements,
            "native_catalog_truncated": event_catalog.get("truncated") is True,
            "native_market_catalogs_missing": unreviewed_events,
            "source_quarantine_truncated": source_snapshot.get("collection", {}).get("truncated") is True,
            "source_quarantine_capped": capped,
            "event_identity_verified": False, "market_mapping_verified": False,
            "settlement_equivalence_verified": False, "execution_authorized": False}
