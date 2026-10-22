"""Read-only NBA fixture leads from Kalshi's public game-winner catalog.

The sampled game markets' occurrence_datetime equals expected expiration,
three hours after the official tipoff. Never use it as basketball tipoff.
Exact schedule and settlement review remain separate requirements.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timezone
import hashlib
import re
from zoneinfo import ZoneInfo

from .base import parse_timestamp
from .nba_moneyline import source_moneyline_outcomes
from .review_report import observation_evidence
from .team_aliases import NBA_TEAMS


_EVENT = re.compile(r"^KXNBAGAME-(?P<year>\d{2})(?P<month>[A-Z]{3})(?P<day>\d{2})(?P<away>[A-Z]{3})(?P<home>[A-Z]{3})$")
_MONTHS = {name: index for index, name in enumerate(
    ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"), 1)}
_EASTERN = ZoneInfo("America/New_York")
# These three spellings are present in the live Kalshi KXNBAGAME catalog;
# Oddize's catalogue instead spells them BRK, CHO and PHO.
_KALSHI_TEAMS = {**NBA_TEAMS, "BKN": "Brooklyn Nets", "CHA": "Charlotte Hornets",
                 "PHX": "Phoenix Suns"}
# The winner labels are venue-specific listing text observed in the public
# KXNBAGAME catalog. They are checked against each live listing, not used to
# approve settlement equivalence with a sportsbook.
_KALSHI_WIN_LABELS = {
    "ATL": "Atlanta", "BKN": "Brooklyn", "BOS": "Boston", "CHA": "Charlotte",
    "CHI": "Chicago", "CLE": "Cleveland", "DAL": "Dallas", "DEN": "Denver",
    "DET": "Detroit", "GSW": "Golden State", "HOU": "Houston",
    "IND": "Indiana", "LAC": "Los Angeles C", "LAL": "Los Angeles L",
    "MEM": "Memphis", "MIA": "Miami", "MIL": "Milwaukee",
    "MIN": "Minnesota", "NOP": "New Orleans", "NYK": "New York",
    "OKC": "Oklahoma City", "ORL": "Orlando", "PHI": "Philadelphia",
    "PHX": "Phoenix", "POR": "Portland", "SAC": "Sacramento",
    "SAS": "San Antonio", "TOR": "Toronto", "UTA": "Utah",
    "WAS": "Washington",
}
_WIN_RULE = re.compile(
    r"^If (?P<winner>.+?) wins the (?P<away>.+?) vs (?P<home>.+?) "
    r"Pro Basketball game originally scheduled for "
    r"(?P<month>[A-Z][a-z]{2}) (?P<day>\d{1,2}), (?P<year>\d{4}), "
    r"then the market resolves to Yes\.$")


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


def _listed_winner(market: dict) -> str | None:
    ticker, event_id = market.get("ticker"), market.get("event_ticker")
    if not isinstance(ticker, str) or not isinstance(event_id, str):
        return None
    code = ticker.removeprefix(event_id + "-")
    rule = market.get("rules_primary")
    match = _WIN_RULE.fullmatch(rule) if isinstance(rule, str) else None
    event = _EVENT.fullmatch(event_id)
    if (match is None or event is None or code not in _KALSHI_WIN_LABELS
            or event["away"] not in _KALSHI_WIN_LABELS
            or event["home"] not in _KALSHI_WIN_LABELS
            or match["winner"] != _KALSHI_WIN_LABELS[code]
            or match["away"] != _KALSHI_WIN_LABELS[event["away"]]
            or match["home"] != _KALSHI_WIN_LABELS[event["home"]]
            or match["month"].upper() != event["month"]
            or int(match["day"]) != int(event["day"])
            or int(match["year"]) != 2000 + int(event["year"])):
        return None
    return _KALSHI_TEAMS[code]


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
        if not isinstance(context, dict):
            continue
        provider, source_id = identity.get("provider"), identity.get("event_id")
        documented_nba = context.get("event_sport") in ("nba", "basketball_nba")
        # OddsPapi documents participant1 as home and participant2 as away;
        # the sport/tournament pair is its published NBA scope. This admits
        # only a read-only fixture lead, never a wager or settlement mapping.
        oddspapi_nba = (provider == "oddspapi_rest" and context.get("event_sport") == 11
                         and context.get("tournament_id") == 132
                         and context.get("event_league") == "NBA")
        if not (documented_nba or oddspapi_nba):
            continue
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
                                              .encode("utf-8")).hexdigest(),
                "winning_team": _listed_winner(row)}
                for row in native_rows}
            candidates.append({"provider": provider, "source_event_id": source_id,
                "native_venue": "kalshi", "native_event_id": event_id,
                "home": home, "away": away, "source_start": start.isoformat(),
                "native_occurrence_times": sorted({entry[1].isoformat() for entry in outcomes.values()}),
                "occurrence_is_tipoff": False,
                "native_tickers": sorted(outcomes), "native_rule_evidence": rules,
                "native_outcome_rules_verified":
                    {item["winning_team"] for item in rules.values()} == {home, away},
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


def moneyline_candidates(fixtures: dict, source_snapshot: dict, *,
                         now: datetime | None = None, max_quote_age_seconds=120.0) -> dict:
    """Pair exact source side labels with live native contract rule labels.

    This report deliberately does not create canonical market mappings. In
    particular, a fresh quote and matching winning team are insufficient to
    establish the two contracts' cancellation and settlement payouts.
    """
    matches = fixtures.get("matches") if isinstance(fixtures, dict) else None
    rows = source_snapshot.get("items") if isinstance(source_snapshot, dict) else None
    if (not isinstance(matches, list) or len(matches) > 1000 or not isinstance(rows, list)
            or len(rows) > 10000):
        raise ValueError("Expected bounded fixture and source review windows")
    now = now or datetime.now(timezone.utc)
    if now.utcoffset() is None:
        raise ValueError("Review clock must include a timezone")
    by_event = defaultdict(list)
    for row in rows:
        identity = row.get("identity") if isinstance(row, dict) else None
        if isinstance(identity, dict):
            by_event[(identity.get("provider"), identity.get("event_id"))].append(row)
    candidates, seen = [], set()
    for fixture in matches:
        if (not isinstance(fixture, dict) or fixture.get("native_outcome_rules_verified") is not True
                or not isinstance(fixture.get("native_event_id"), str)
                or not isinstance(fixture.get("home"), str)
                or not isinstance(fixture.get("away"), str)):
            continue
        home, away, event_id = fixture["home"], fixture["away"], fixture["native_event_id"]
        tickers, rules = fixture.get("native_tickers"), fixture.get("native_rule_evidence")
        if not isinstance(tickers, list) or len(tickers) != 2 or not isinstance(rules, dict):
            continue
        native_outcomes = {}
        for ticker in tickers:
            evidence = rules.get(ticker) if isinstance(ticker, str) else None
            if (not ticker.startswith(event_id + "-") or not isinstance(evidence, dict)
                    or evidence.get("winning_team") not in {home, away}):
                break
            native_outcomes[evidence["winning_team"]] = ticker
        if len(native_outcomes) != 2 or set(native_outcomes) != {home, away}:
            continue
        for row in by_event[(fixture.get("provider"), fixture.get("source_event_id"))]:
            identity, observation = row.get("identity"), row.get("observation")
            signature = row.get("signature")
            if (not isinstance(identity, dict) or not isinstance(observation, dict)
                    or not isinstance(signature, str) or not re.fullmatch(r"[0-9a-f]{64}", signature)):
                continue
            source_outcomes = source_moneyline_outcomes(identity, observation, home=home, away=away)
            if source_outcomes is None:
                continue
            key = (event_id, signature)
            if key in seen:
                continue
            seen.add(key)
            quote = observation_evidence(observation, now=now,
                                         max_age_seconds=max_quote_age_seconds)
            candidates.append({"provider": identity["provider"],
                "bookmaker": identity.get("bookmaker"),
                "source_event_id": identity["event_id"], "source_signature": signature,
                "source_market_key": identity["market_key"],
                "source_outcomes": source_outcomes, "native_event_id": event_id,
                "native_outcomes": native_outcomes,
                "native_rule_evidence": rules,
                "source_start_conflict": fixture.get("source_start_conflict") is True,
                "native_outcome_rules_verified": True,
                "source_status": observation.get("status"),
                "source_price_changed_at": observation.get("source_price_changed_at"),
                **quote, "candidate_only": True,
                "settlement_equivalence_verified": False,
                "execution_authorized": False})
    collection = source_snapshot.get("collection")
    return {"schema_version": 1, "candidates": candidates,
            "candidate_count": len(candidates),
            "quote_evidence_ready_count": sum(c["quote_evidence_ready"] for c in candidates),
            "source_window_truncated": not isinstance(collection, dict) or collection.get("truncated") is not False,
            "source_window_nonatomic": not isinstance(collection, dict) or collection.get("atomic") is not True,
            "source_window_total_changed": isinstance(collection, dict) and collection.get("total_changed") is True,
            "source_quarantine_capped": fixtures.get("source_quarantine_capped") is True,
            "native_catalog_truncated": fixtures.get("native_catalog_truncated") is True,
            "market_mapping_verified": False,
            "settlement_equivalence_verified": False,
            "execution_authorized": False}
