"""Narrow, owner-approved exception to strict sportsbook source verification.

The pilot trusts named provider rows as inputs to a conditional WIN/LOSS
estimate.  It never asserts that a bookmaker poll re-observed a selection.
Native identity, depth, fees, cash, sizing, risk and one-attempt authorization
remain independent gates.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from .config import PricingConfig
from .models import Market


def trusted_pilot_reference_check(config: PricingConfig, market: Market,
                                  references: list[dict[str, Any]] | tuple[dict[str, Any], ...],
                                  now: datetime) -> tuple[bool, tuple[str, ...]]:
    policy = config.trusted_pilot_policy
    if not policy.enabled or not policy.owner_approved:
        return False, ("Owner-approved trusted pilot is disabled",)
    reasons: list[str] = []
    if (config.reference_quality_mode != "trusted_provider"
            or config.normal_outcome_policy != "winner_only_binary"
            or config.reference_profile_mode != "strict"):
        reasons.append("Trusted pilot requires the reviewed winner-only reference profile")
    phase = policy.market_scope.phase if policy.market_scope is not None else "pregame"
    phase_ok = ((phase == "pregame" and market.status == "scheduled" and market.start_time > now)
                or (phase == "inplay" and market.status == "live" and market.start_time <= now
                    and market.extensions.get("inplay_price_after_start") is True))
    if not policy.permits_market(market) or not phase_ok:
        reasons.append("Pilot market is outside the reviewed exact selection or normal-question rule")
    books = {ref.get("bookmaker") for ref in references}
    providers = {ref.get("provider") for ref in references}
    if (len(books) != len(references) or len(books) < max(policy.min_distinct_books, config.min_references)
            or not books <= set(policy.trusted_books)):
        reasons.append("Pilot references are duplicated, insufficient or outside approved books")
    if policy.independent_crosscheck == "two_distinct_books":
        if providers != {policy.provider_id}:
            reasons.append("Pilot references are outside the named provider")
    elif policy.independent_crosscheck == "separate_provider":
        if providers != {policy.provider_id, policy.crosscheck_provider_id}:
            reasons.append("Pilot lacks the named independent provider cross-check")
    else:
        reasons.append("Pilot independent cross-check is undefined")

    def age(stamp: str | None, label: str, maximum: int) -> None:
        if stamp is None:
            reasons.append(f"{label} is missing")
            return
        try:
            parsed = datetime.fromisoformat(stamp)
            milliseconds = Decimal(str((now - parsed).total_seconds() * 1000))
            if parsed.tzinfo is None or not milliseconds.is_finite() or not 0 <= milliseconds <= maximum:
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            reasons.append(f"{label} is invalid, expired or future-dated")

    for ref in references:
        book = str(ref.get("bookmaker", "unknown"))
        if phase == "inplay":
            # A price movement, provider heartbeat, or local delivery is not a
            # selection observation. Unknown status/observation age can only
            # pass under the separately approved live source policy, while a
            # short retained-row clock remains mandatory.
            if ref.get("provider") not in {"parlay_ws", "parlay_game_ws"}:
                reasons.append(f"{book}: in-play source is not the approved odds stream")
            if (ref.get("status") != "active" or ref.get("market_status_verified") is not True):
                if not policy.accept_unknown_active_status:
                    reasons.append(f"{book}: no affirmative current in-play selection status")
            if ref.get("observation_verified") is True:
                age(ref.get("source_observed_at"), f"{book} in-play selection observation",
                    policy.inplay_max_row_age_ms or 0)
            elif not policy.accept_unknown_quote_observation_age:
                reasons.append(f"{book}: no verified in-play selection observation")
            age(ref.get("source_retained_row_at"), f"{book} in-play retained price row",
                policy.inplay_max_row_age_ms or 0)
            age(ref.get("local_receipt_at"), f"{book} in-play delivery",
                policy.inplay_max_row_age_ms or 0)
            if ref.get("provider_delivery_at") is not None:
                age(ref["provider_delivery_at"], f"{book} provider delivery",
                    policy.inplay_max_row_age_ms or 0)
            try:
                stored = datetime.fromisoformat(ref["source_retained_row_at"])
                if stored.tzinfo is None or stored < market.start_time:
                    raise ValueError
            except (KeyError, TypeError, ValueError):
                reasons.append(f"{book}: retained price row did not occur in-play")
        if ref.get("market_fingerprint") != market.identity():
            reasons.append(f"{book}: normal-outcome market identity is unreviewed")
        if ref.get("normal_outcome_verified") is not True or not ref.get("normal_outcome_evidence"):
            reasons.append(f"{book}: normal WIN/LOSS condition has no reviewed source evidence")
        elif (policy.market_scope is not None and
              policy.market_scope.reviewed_normal_rule_id not in ref["normal_outcome_evidence"]):
            reasons.append(f"{book}: normal WIN/LOSS evidence is outside the approved rule")
        status = ref.get("status")
        if status in {"suspended", "withdrawn"} or status not in {"active", "unknown"}:
            reasons.append(f"{book}: sportsbook selection is unavailable")
        elif (status != "active" or ref.get("market_status_verified") is not True):
            if not policy.accept_unknown_active_status:
                reasons.append(f"{book}: unverified active status was not accepted by owner")
        if ref.get("source_scope_is_current") is False:
            reasons.append(f"{book}: provider reports the book/fixture poll is not current")
        if ref.get("verification_reason") in {
                "retained_top_up", "partial_response", "truncated_response",
                "stale_cached_board", "degraded_source",
                "future_observation_clock", "incomplete_or_cross_frame_outcomes",
                "unsynchronized_outcomes", "trusted_game_cross_frame_pair"}:
            reasons.append(f"{book}: provider row is cached, degraded or assembled across frames")
        if ref.get("provider") == "parlay_rest":
            age(ref.get("source_stored_at"), f"{book} stored price row", policy.max_indicator_age_ms)
        elif ref.get("provider") in {"parlay_ws", "parlay_game_ws"} and ref.get("source_retained_row_at") is not None:
            # The stream's last_update is a retained row/source timestamp,
            # not a fresh observation merely because a new baseline arrived.
            # Unknown remains explicitly owner-accepted; a known old row does
            # not become current when the socket reconnects.
            age(ref["source_retained_row_at"], f"{book} retained price row", policy.max_indicator_age_ms)
        age(ref.get("local_receipt_at"), f"{book} delivery",
            min(config.trusted_max_delivery_age_ms, policy.max_indicator_age_ms))
        if ref.get("source_scope_poll_at") is not None:
            age(ref["source_scope_poll_at"], f"{book} book/fixture poll", policy.max_indicator_age_ms)
        if ref.get("observation_verified") is True and ref.get("source_observed_at"):
            age(ref["source_observed_at"], f"{book} quote observation", policy.max_indicator_age_ms)
        elif not policy.accept_unknown_quote_observation_age:
            reasons.append(f"{book}: unknown quote-observation age was not accepted by owner")
    return not reasons, tuple(dict.fromkeys(reasons))
