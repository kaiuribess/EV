"""Bounded selection of an exact current Parlay event for a Novig prop lead.

This is discovery only. It neither verifies a sportsbook selection nor creates
an executable native mapping.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime

from .base import parse_timestamp


def exact_parlay_event_id(market, events: list[dict]) -> str | None:
    matches = [row.get("id") for row in events if isinstance(row, dict)
               and row.get("home_team") == market.participants[0]
               and row.get("away_team") == market.participants[1]
               and parse_timestamp(row.get("commence_time")) == market.start_time
               and isinstance(row.get("id"), str) and len(row["id"]) == 32]
    return matches[0] if len(matches) == 1 else None


def needs_focused_references(quotes, *, now: datetime, max_indicator_age_ms: int) -> bool:
    """A fair *research* estimate from a truncated board still needs focus."""
    books = set()
    adverse = {"truncated_response", "paginated_response_not_atomic",
               "degraded_source", "stale_cached_board", "retained_top_up",
               "partial_response", "incomplete_snapshot", "future_observation_clock"}
    for quote in quotes:
        if (not quote.provider.startswith("parlay") or quote.status in {"suspended", "withdrawn"}
                or quote.verification_reason in adverse
                or len(quote.decimal_odds) != 2):
            continue
        stamp = (quote.source_observed_at if quote.observation_verified
                 else quote.source_stored_at)
        if stamp is None:
            continue
        age = (now - stamp).total_seconds() * 1000
        if 0 <= age <= max_indicator_age_ms:
            books.add(quote.bookmaker)
    return len(books) < 2


class FocusedCreditWindow:
    def __init__(self, hourly_limit: int, request_cooldown_seconds: int):
        self.hourly_limit = hourly_limit
        self.request_cooldown_seconds = request_cooldown_seconds
        self.charges: deque[tuple[float, int]] = deque()
        self.last_scope: dict[tuple[str, str, str, str], float] = {}

    def available(self, now: float, scope: tuple[str, str, str, str]) -> int:
        while self.charges and now - self.charges[0][0] >= 3600:
            self.charges.popleft()
        if now - self.last_scope.get(scope, float("-inf")) < self.request_cooldown_seconds:
            return 0
        return max(0, self.hourly_limit - sum(cost for _, cost in self.charges))

    def charge(self, now: float, scope: tuple[str, str, str, str], cost: int) -> None:
        self.last_scope[scope] = now
        if cost > 0:
            self.charges.append((now, cost))
