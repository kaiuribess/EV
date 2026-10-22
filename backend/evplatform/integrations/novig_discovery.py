"""Bounded, read-only Novig catalog selection for monitoring only.

Catalog discovery does not establish sportsbook settlement equivalence or
authorize native orders. The venue validates every selected ID again before a
WebSocket subscription is opened.
"""

from datetime import datetime
from uuid import UUID


def open_market_ids(payload: dict, *, now: datetime, max_markets: int) -> tuple[list[str], bool]:
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise ValueError("Novig catalog response has no market list")
    next_page = payload.get("next")
    if next_page is not None and (not isinstance(next_page, str) or not next_page):
        raise ValueError("Novig catalog cursor is invalid")
    if not 1 <= max_markets <= 100:
        raise ValueError("Novig monitor selection must contain 1 to 100 markets")
    observed: dict[str, tuple[int, str]] = {}
    now_ms = int(now.timestamp() * 1000)
    for row in payload["items"]:
        if not isinstance(row, dict):
            raise ValueError("Novig catalog market is invalid")
        market_id, event_id = row.get("marketId"), row.get("eventId")
        starts = row.get("startsTs")
        if (not isinstance(market_id, str) or not isinstance(event_id, str)
                or type(starts) is not int or starts <= 0):
            raise ValueError("Novig catalog market identity or start is invalid")
        UUID(market_id)
        UUID(event_id)
        if row.get("status") != "OPEN":
            continue
        identity = (starts, event_id)
        if market_id in observed and observed[market_id] != identity:
            raise ValueError("Novig catalog contains conflicting market identities")
        observed[market_id] = identity
    # An OPEN market may already be in play. Prefer scheduled times closest to
    # the current clock without treating the catalog as settlement evidence.
    ordered = sorted(observed, key=lambda market_id: (abs(observed[market_id][0] - now_ms), market_id))
    return ordered[:max_markets], bool(next_page) or len(ordered) > max_markets


def balanced_open_market_ids(payloads: list[dict], *, now: datetime,
                             max_markets: int) -> tuple[list[str], bool]:
    """Reserve nearby coverage in each requested league before filling capacity."""
    if not payloads or not 1 <= max_markets <= 100:
        raise ValueError("Novig discovery needs league responses and a bounded selection")
    queues = []
    truncated = False
    for payload in payloads:
        ids, partial = open_market_ids(payload, now=now, max_markets=100)
        queues.append(ids)
        truncated |= partial
    chosen: list[str] = []
    seen: set[str] = set()
    for index in range(max(map(len, queues), default=0)):
        for queue in queues:
            if index >= len(queue) or queue[index] in seen:
                continue
            seen.add(queue[index])
            if len(chosen) < max_markets:
                chosen.append(queue[index])
            else:
                truncated = True
    return chosen, truncated
