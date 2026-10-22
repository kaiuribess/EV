"""Bounded, read-only selection of currently open Polymarket US sports books.

Discovery selects monitoring scope only. A catalog row cannot verify sportsbook
identity, fair value, settlement equivalence, or permission to place an order.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


def _time(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Polymarket US game clock is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("Invalid Polymarket US game clock") from None
    if parsed.tzinfo is None:
        raise ValueError("Polymarket US game clock must include a timezone")
    return parsed.astimezone(timezone.utc)


def current_open_moneylines(pages: list[dict], *, league: str, now: datetime,
                            max_markets: int) -> tuple[list[tuple[datetime, str]], bool]:
    if not pages or not 1 <= max_markets <= 50 or now.tzinfo is None:
        raise ValueError("Bounded Polymarket US discovery needs pages, time and capacity")
    matches: list[tuple[datetime, str]] = []
    seen_events: set[str] = set()
    seen_markets: set[str] = set()
    truncated = False
    for page in pages:
        if not isinstance(page, dict) or not isinstance(page.get("events"), list):
            raise ValueError("Invalid Polymarket US league event page")
        if not isinstance(page.get("league"), dict) or page["league"].get("slug") != league:
            raise ValueError("Polymarket US league catalog identity changed")
        truncated |= page.get("bounded") is True
        for event in page["events"]:
            if not isinstance(event, dict) or not isinstance(event.get("slug"), str):
                raise ValueError("Polymarket US event identity is missing")
            if event["slug"] in seen_events:
                raise ValueError("Duplicate Polymarket US event across discovery pages")
            seen_events.add(event["slug"])
            if not isinstance(event.get("markets"), list):
                raise ValueError("Polymarket US event has no market collection")
            if (event.get("active") is not True or event.get("closed") is not False
                    or event.get("archived") is True):
                continue
            for market in event["markets"]:
                if not isinstance(market, dict) or not isinstance(market.get("slug"), str):
                    raise ValueError("Polymarket US market identity is missing")
                slug = market["slug"]
                if slug in seen_markets:
                    raise ValueError("Duplicate Polymarket US market across discovery pages")
                seen_markets.add(slug)
                if (market.get("sportsMarketTypeV2") != "SPORTS_MARKET_TYPE_MONEYLINE"
                        or market.get("status") != "MARKET_STATUS_OPEN"
                        or market.get("active") is not True or market.get("closed") is not False
                        or market.get("hidden") is True or market.get("archived") is True):
                    continue
                starts = _time(market.get("gameStartTime"))
                if now - timedelta(hours=6) <= starts <= now + timedelta(days=3):
                    matches.append((starts, slug))
    matches.sort(key=lambda item: (abs((item[0] - now).total_seconds()), item[0], item[1]))
    return matches[:max_markets], truncated or len(matches) > max_markets


def balanced_open_moneylines(catalogs: list[list[dict]], *, leagues: list[str],
                             now: datetime, max_markets: int) -> tuple[list[str], bool]:
    if not catalogs or len(catalogs) != len(leagues) or not 1 <= max_markets <= 50:
        raise ValueError("Polymarket US discovery scope is invalid")
    per_league = [current_open_moneylines(pages, league=league, now=now,
                                         max_markets=max_markets)
                  for pages, league in zip(catalogs, leagues)]
    selected: list[str] = []
    for index in range(max_markets):
        for matches, _ in per_league:
            if index < len(matches) and len(selected) < max_markets:
                selected.append(matches[index][1])
    return selected, any(truncated for _, truncated in per_league) or sum(
        len(matches) for matches, _ in per_league) > len(selected)
