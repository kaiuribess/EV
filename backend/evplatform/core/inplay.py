"""Phase-specific live execution evidence. No prematch policy is inherited."""
from __future__ import annotations

from datetime import datetime

from .models import GameState, Market
from .native_pricing import NativeBook


def inplay_state_identity_ok(state: GameState | None, *, allow_exact_fixture: bool = False) -> bool:
    return bool(state is not None and state.source_match_id is not None and (
        state.identity_verified and state.identity_basis in {"provider_id", "unknown"}
        or allow_exact_fixture and state.identity_basis == "exact_same_provider_fixture"))


def inplay_execution_rejections(market: Market, state: GameState | None,
                                book: NativeBook | None, *, now: datetime,
                                max_state_age_ms: int, max_book_age_ms: int,
                                allow_exact_fixture_identity: bool = False,
                                native_permission: bool = False,
                                native_permission_source: str | None = None) -> tuple[str, ...]:
    reasons = []
    if market.status != "live" or now < market.start_time:
        reasons.append("Source market is not in a verified in-play phase")
    if (state is None or state.event_id != market.event_id
            or not inplay_state_identity_ok(state, allow_exact_fixture=allow_exact_fixture_identity)
            or state.status != "live" or state.source_match_id is None):
        reasons.append("No attributable current play-by-play identity and live state")
    else:
        age = (now - state.source_time).total_seconds()*1000 if state.source_time else None
        if age is None or not 0 <= age <= max_state_age_ms:
            reasons.append("Play-by-play game state is stale or has no event clock")
        if market.settlement.period != "FULL" and state.period != market.settlement.period:
            reasons.append("Live game period differs from the native contract period")
    if native_permission is not True or not native_permission_source:
        reasons.append("Native market has no explicit attributable in-play trading permission")
    if book is None or not book.synchronized or book.status != "active":
        reasons.append("No current active synchronized native depth")
    else:
        age = book.age_ms(now)
        if age is None or not 0 <= age <= max_book_age_ms or not book.asks:
            reasons.append("Native executable depth is stale or empty")
    return tuple(reasons)
