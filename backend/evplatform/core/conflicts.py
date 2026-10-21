"""Conservative position families, separate from exact settlement equivalence.

Only canonical identifiers are compared. A family is an anti-stacking guard,
not a claim that different lines or settlement terms form a hedge/arbitrage.
Cross-type correlations require explicitly reviewed adapter metadata:
``extensions.risk_correlation_v1 = {"verified": true, "groups": ["..."],
"evidence": "review reference"}``. Group names are scoped to canonical event,
sport and league; display names and guessed player/team aliases are never used.
"""

import hashlib
import json

from .models import Market, Order, OrderStatus


UNRESOLVED = frozenset({
    OrderStatus.INTENT, OrderStatus.SUBMITTING, OrderStatus.OPEN,
    OrderStatus.PARTIAL, OrderStatus.CANCEL_PENDING, OrderStatus.UNKNOWN,
})


def _fingerprint(payload) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def risk_family(market: Market) -> str:
    """Keep alternate thresholds and both sides in one conservative family.

    Settlement rules remain in the exact market fingerprint and snapshot. This
    broader family deliberately blocks stacking even when their rules differ;
    it never interprets them as equivalent contracts or offsets their exposure.
    """
    return _fingerprint({
        "version": 1, "event": market.event_id, "sport": market.sport,
        "league": market.league, "market_type": market.market_type,
        "player": market.player_id, "statistic": market.statistic,
        "period": market.settlement.period, "scope": market.settlement.scope,
    })


def reviewed_correlations(market: Market) -> tuple[str, ...]:
    metadata = market.extensions.get("risk_correlation_v1")
    if metadata is None:
        return ()
    if (not isinstance(metadata, dict) or metadata.get("verified") is not True or
            not isinstance(metadata.get("evidence"), str) or not metadata["evidence"].strip() or
            not isinstance(metadata.get("groups"), (tuple, list)) or not metadata["groups"] or
            any(not isinstance(group, str) or not group.strip() for group in metadata["groups"])):
        raise ValueError("Risk correlation metadata requires reviewed groups and evidence")
    return tuple(sorted({
        _fingerprint({"version": 1, "event": market.event_id, "sport": market.sport,
                      "league": market.league, "reviewed_group": group})
        for group in metadata["groups"]
    }))


def reserves_position(order: Order, settled: set[str]) -> bool:
    if order.id in settled:
        return False
    # Even malformed terminal records with fills must not free a position.
    return (order.status in UNRESOLVED or order.filled_quantity > 0 or
            order.status == OrderStatus.FILLED)


def metadata_consistent(order: Order) -> bool:
    market = order.market_snapshot
    if market is None:
        return False
    try:
        return (market.id == order.market_id and market.event_id == order.event_id and
                market.player_id == order.player_id and order.correlation_id == market.event_id and
                order.outcome in market.outcomes and
                order.market_fingerprint == market.identity() and
                order.risk_family_id == risk_family(market) and
                order.reviewed_correlation_groups == reviewed_correlations(market))
    except ValueError:
        return False
