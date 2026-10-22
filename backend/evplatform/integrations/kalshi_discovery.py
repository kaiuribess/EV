"""Bounded current Kalshi sports market selection for book monitoring only."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone


def _time(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Kalshi occurrence clock is missing")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("Invalid Kalshi occurrence clock") from None
    if result.tzinfo is None:
        raise ValueError("Kalshi occurrence clock must include a timezone")
    return result.astimezone(timezone.utc)


def current_open_markets(payload: dict, *, series: str, now: datetime,
                         max_markets: int) -> tuple[list[tuple[datetime, str]], bool]:
    if not 1 <= max_markets <= 100 or now.tzinfo is None:
        raise ValueError("Kalshi monitoring discovery needs bounded capacity and time")
    if (not isinstance(payload, dict) or not isinstance(payload.get("markets"), list)
            or not isinstance(payload.get("cursor"), str)):
        raise ValueError("Invalid Kalshi public market catalog")
    selected: list[tuple[datetime, str]] = []
    seen: set[str] = set()
    for market in payload["markets"]:
        if not isinstance(market, dict):
            raise ValueError("Invalid Kalshi market row")
        ticker = market.get("ticker")
        if not isinstance(ticker, str) or not re.fullmatch(r"[A-Z0-9-]{1,128}", ticker):
            raise ValueError("Invalid Kalshi ticker")
        if ticker in seen:
            raise ValueError("Duplicate Kalshi market in a catalog page")
        seen.add(ticker)
        event_ticker = market.get("event_ticker")
        if (not ticker.startswith(series + "-") or not isinstance(event_ticker, str)
                or not event_ticker.startswith(series + "-")):
            raise ValueError("Kalshi series identity mismatch")
        if market.get("status") not in {"active", "open"} or market.get("market_type") != "binary":
            continue
        starts = _time(market.get("occurrence_datetime"))
        if now - timedelta(hours=6) <= starts <= now + timedelta(days=3):
            selected.append((starts, ticker))
    selected.sort(key=lambda item: (abs((item[0] - now).total_seconds()), item[0], item[1]))
    return selected[:max_markets], bool(payload["cursor"]) or len(selected) > max_markets


def balanced_open_markets(payloads: list[dict], *, series: list[str], now: datetime,
                          max_markets: int) -> tuple[list[str], bool]:
    if not payloads or len(payloads) != len(series) or not 1 <= max_markets <= 100:
        raise ValueError("Kalshi public discovery scope is invalid")
    per_series = [current_open_markets(payload, series=name, now=now,
                                       max_markets=max_markets)
                  for payload, name in zip(payloads, series)]
    selected: list[str] = []
    for index in range(max_markets):
        for rows, _ in per_series:
            if index < len(rows) and len(selected) < max_markets:
                selected.append(rows[index][1])
    return selected, any(truncated for _, truncated in per_series) or sum(
        len(rows) for rows, _ in per_series) > len(selected)
