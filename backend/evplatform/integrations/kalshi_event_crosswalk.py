"""Read-only NBA fixture leads from Kalshi's public game-winner catalog.

The sampled game markets' occurrence_datetime equals expected expiration,
three hours after the official tipoff. Never use it as basketball tipoff.
Exact schedule and settlement review remain separate requirements.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date
import hashlib
import re
from zoneinfo import ZoneInfo

from .base import parse_timestamp
from .team_aliases import NBA_TEAMS


_EVENT = re.compile(r"^KXNBAGAME-(?P<year>\d{2})(?P<month>[A-Z]{3})(?P<day>\d{2})(?P<away>[A-Z]{3})(?P<home>[A-Z]{3})$")
_MONTHS = {name: index for index, name in enumerate(
    ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"), 1)}
_EASTERN = ZoneInfo("America/New_York")
# These three spellings are present in the live Kalshi KXNBAGAME catalog;
# Oddize's catalogue instead spells them BRK, CHO and PHO.
_KALSHI_TEAMS = {**NBA_TEAMS, "BKN": "Brooklyn Nets", "CHA": "Charlotte Hornets",
                 "PHX": "Phoenix Suns"}


def _fixture(event_ticker: str):
    match = _EVENT.fullmatch(event_ticker)
    if match is None or match["away"] not in _KALSHI_TEAMS or match["home"] not in _KALSHI_TEAMS:
        return None
    try:
        local_day = date(2000 + int(match["year"]), _MONTHS[match["month"]], int(match["day"]))
    except (ValueError, KeyError):
        return None
    if match["away"] == match["home"]:
        return None
    return local_day, _KALSHI_TEAMS[match["home"]], _KALSHI_TEAMS[match["away"]]


def fixture_candidates(kalshi_catalog: dict, source_snapshot: dict) -> dict:
    """Compare date/participants, retaining expiration and rule differences.

    This is a discovery report only. It neither verifies a schedule nor
    establishes that a source sportsbook pays the same way as Kalshi.
    """
    markets = kalshi_catalog.get("markets") if isinstance(kalshi_catalog, dict) else None
    rows = source_snapshot.get("items") if isinstance(source_snapshot, dict) else None
    if not isinstance(markets, list) or len(markets) > 1000 or not isinstance(rows, list) or len(rows) > 10000:
        raise ValueError("Expected bounded Kalshi catalog and source review window")
    if not isinstance(kalshi_catalog.get("cursor"), str):
        raise ValueError("Kalshi catalog cursor is required to assess truncation")
    native = defaultdict(dict)
    conflicting_native = set()
    unknown_native_codes = set()
    for market in markets:
        if (not isinstance(market, dict) or market.get("status") not in {"active", "open"}
                or market.get("market_type") != "binary"):
            continue
        event_id, ticker = market.get("event_ticker"), market.get("ticker")
        if not isinstance(event_id, str) or not isinstance(ticker, str):
            continue
        parsed = _EVENT.fullmatch(event_id)
        if parsed:
            unknown_native_codes.update({parsed["home"], parsed["away"]} - _KALSHI_TEAMS.keys())
        fixture = _fixture(event_id)
        if fixture is None or ticker not in {f"{event_id}-{code}" for code, name in _KALSHI_TEAMS.items()
                                                  if name in fixture[1:]}:
            continue
        occurrence = parse_timestamp(market.get("occurrence_datetime"))
        if (occurrence is None or not isinstance(market.get("rules_primary"), str)
                or not market["rules_primary"].strip()
                or not isinstance(market.get("rules_secondary"), str)
                or not market["rules_secondary"].strip()):
            continue
        if ticker in native[event_id]:
            conflicting_native.add(event_id)
            continue
        native[event_id][ticker] = (fixture, occurrence, market)
    grouped, conflicts = {}, set()
    for row in rows:
        identity = row.get("identity") if isinstance(row, dict) else None
        context = identity.get("context") if isinstance(identity, dict) else None
        if not isinstance(context, dict) or context.get("event_sport") not in ("nba", "basketball_nba"):
            continue
        provider, source_id = identity.get("provider"), identity.get("event_id")
        participants = context.get("participants")
        start = parse_timestamp(context.get("scheduled_start"))
        if (not isinstance(provider, str) or not isinstance(source_id, str) or not source_id
                or not isinstance(participants, list) or len(participants) != 2
                or any(not isinstance(name, str) for name in participants) or start is None):
            continue
        teams = tuple(NBA_TEAMS.get(name, name) for name in participants)
        if teams[0] == teams[1] or set(teams) - set(NBA_TEAMS.values()):
            continue
        key, candidate = (provider, source_id), (teams, start)
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
    candidates = []
    for event_id, outcomes in sorted(native.items()):
        if event_id in conflicting_native or len(outcomes) != 2:
            continue
        fixtures = {entry[0] for entry in outcomes.values()}
        if len(fixtures) != 1:
            continue
        local_day, home, away = next(iter(fixtures))
        for (provider, source_id), item in sorted(grouped.items()):
            teams, start = item["identity"]
            if teams != (home, away) or start.astimezone(_EASTERN).date() != local_day:
                continue
            native_rows = [entry[2] for entry in outcomes.values()]
            secondary = "\n".join(row["rules_secondary"] for row in native_rows).casefold()
            rules = {row["ticker"]: {
                "listing_url": ("https://external-api.kalshi.com/trade-api/v2/markets/" + row["ticker"]),
                "text_sha256": hashlib.sha256((row["rules_primary"] + "\n" + row["rules_secondary"])
                                              .encode("utf-8")).hexdigest()}
                for row in native_rows}
            candidates.append({"provider": provider, "source_event_id": source_id,
                "native_venue": "kalshi", "native_event_id": event_id,
                "home": home, "away": away, "source_start": start.isoformat(),
                "native_occurrence_times": sorted({entry[1].isoformat() for entry in outcomes.values()}),
                "occurrence_is_tipoff": False,
                "native_tickers": sorted(outcomes), "native_rule_evidence": rules,
                "bookmakers": sorted(item["books"]),
                "source_observations": item["rows"],
                "native_fair_price_on_cancel": "resolve to a fair price" in secondary,
                "native_postponement_48h": "within 48 hours" in secondary,
                "schedule_verified": False, "settlement_equivalence_verified": False,
                "candidate_only": True})
    # Different vendor IDs can still describe one fixture with conflicting
    # scheduled starts. Keep each lead visible, but call out the discrepancy.
    starts_by_event = defaultdict(list)
    for candidate in candidates:
        starts_by_event[candidate["native_event_id"]].append(parse_timestamp(candidate["source_start"]))
    disputed = {event_id for event_id, starts in starts_by_event.items()
                if (max(starts) - min(starts)).total_seconds() > 120}
    for candidate in candidates:
        candidate["source_start_conflict"] = candidate["native_event_id"] in disputed
    providers = source_snapshot.get("providers")
    capped = any(isinstance(p, dict) and isinstance(p.get("pending"), int)
                 and isinstance(p.get("capacity"), int) and p["pending"] >= p["capacity"]
                 for p in providers) if isinstance(providers, list) else True
    return {"schema_version": 1, "matches": candidates, "match_count": len(candidates),
            "conflicting_source_events": len(conflicts),
            "conflicting_native_events": len(conflicting_native),
            "cross_provider_start_conflicts": len(disputed),
            "unknown_native_team_codes": sorted(unknown_native_codes),
            "native_catalog_truncated": bool(kalshi_catalog["cursor"]),
            "source_quarantine_capped": capped,
            "event_identity_verified": False, "market_mapping_verified": False,
            "settlement_equivalence_verified": False, "execution_authorized": False}
