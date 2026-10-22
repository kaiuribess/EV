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


def moneyline_candidates(events: dict, source_snapshot: dict, market_catalog: dict,
                         *, native_event_id: str) -> dict:
    """Review full-game moneyline *labels* for one event; never publish mappings.

    Names and strike IDs are discovery facts. Missing sportsbook jurisdiction,
    cancellation terms, quote availability, or native payout terms still block
    an executable contract, even if the normal sporting outcome is identical.
    """
    if not isinstance(native_event_id, str) or not native_event_id.isascii() or not native_event_id.isdigit():
        raise ValueError("Expected an exact numeric native event ID")
    if (events.get("event_identity_verified") is not False
            or events.get("execution_authorized") is not False):
        raise ValueError("Expected a read-only event candidate report")
    rows = market_catalog.get("markets") if isinstance(market_catalog, dict) else None
    source_rows = source_snapshot.get("items") if isinstance(source_snapshot, dict) else None
    if not isinstance(rows, list) or len(rows) > 100 or not isinstance(source_rows, list) or len(source_rows) > 10000:
        raise ValueError("Expected bounded market and source catalogs")
    matched_events = {(m["provider"], m["source_event_id"]): m for m in events.get("matches", [])
                      if isinstance(m, dict) and m.get("native_event_id") == native_event_id}
    native = []
    for market in rows:
        if (not isinstance(market, dict) or market.get("name") != "Moneyline"
                or market.get("type") != "moneyline" or market.get("sub_type") != "moneyline"
                or market.get("status") != "active" or market.get("period") is not None
                or market.get("selection_shape") != "nested"
                or market.get("selection_review_truncated") is not False):
            continue
        selections = market.get("selection_review")
        if not isinstance(selections, list) or not selections:
            continue
        groups = defaultdict(set)
        for selection in selections:
            if not isinstance(selection, dict) or selection.get("group") not in (0, 1):
                break
            value = (selection.get("name"), selection.get("strike_id"), selection.get("outcome_id"))
            if not all(isinstance(v, str) and v for v in value):
                break
            groups[selection["group"]].add(value)
        else:
            if set(groups) == {0, 1} and all(len(group) == 1 for group in groups.values()):
                a, b = next(iter(groups[0])), next(iter(groups[1]))
                if a[0] != b[0] and a[1] != b[1] and a[2] != b[2]:
                    native.append((market["id"], {a[0]: {"strike_id": a[1], "outcome_id": a[2]},
                                                   b[0]: {"strike_id": b[1], "outcome_id": b[2]}}))
    candidates = []
    for row in source_rows:
        if not isinstance(row, dict):
            continue
        identity, observation = row.get("identity"), row.get("observation")
        if not isinstance(identity, dict) or not isinstance(observation, dict):
            continue
        match = matched_events.get((identity.get("provider"), identity.get("event_id")))
        if (match is None or identity.get("market_key") not in {"h2h", "ml"}
                or identity.get("player_key") is not None or identity.get("line") is not None
                or identity.get("period") is not None):
            continue
        teams = (match["home"], match["away"])
        prices = observation.get("decimal_odds")
        points = identity.get("outcome_points")
        if not isinstance(prices, dict) or not isinstance(points, dict) or set(prices) != set(points):
            continue
        if any(value is not None for value in points.values()) or len(prices) != 2:
            continue
        if set(prices) == {"home", "away"}:
            source_outcomes = {"home": teams[0], "away": teams[1]}
        else:
            source_outcomes = {key: NBA_TEAMS.get(key, key) for key in prices}
        if set(source_outcomes.values()) != set(teams) or len(set(source_outcomes.values())) != 2:
            continue
        for market_id, strikes in native:
            if set(strikes) != set(teams):
                continue
            candidates.append({"provider": identity["provider"], "bookmaker": identity.get("bookmaker"),
                "source_event_id": identity["event_id"], "source_signature": row.get("signature"),
                "source_market_key": identity["market_key"], "source_outcomes": source_outcomes,
                "native_event_id": native_event_id, "native_market_id": market_id,
                "native_outcomes": strikes, "source_observed_at": observation.get("source_observed_at"),
                "source_observation_verified": observation.get("observation_verified") is True,
                "source_market_status_verified": observation.get("market_status_verified") is True,
                "candidate_only": True})
    return {"schema_version": 1, "native_event_id": native_event_id,
            "candidates": candidates, "candidate_count": len(candidates),
            "source_window_truncated": events.get("source_window_truncated") is True,
            "source_quarantine_capped": events.get("source_quarantine_capped") is True,
            "native_catalog_truncated": market_catalog.get("truncated") is True,
            "market_mapping_verified": False, "settlement_equivalence_verified": False,
            "execution_authorized": False}
