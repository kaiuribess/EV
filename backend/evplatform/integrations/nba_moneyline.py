"""Exact source outcome labels for NBA full-game moneyline review.

This identifies the sporting side of an offer. It does not establish current
quote validity, settlement equivalence, or permission to trade.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from .team_aliases import NBA_TEAMS


def source_moneyline_outcomes(identity: dict, observation: dict, *, home: str,
                              away: str) -> dict[str, str] | None:
    if not isinstance(identity, dict) or not isinstance(observation, dict):
        return None
    if identity.get("player_key") is not None:
        return None
    prices, points = observation.get("decimal_odds"), identity.get("outcome_points")
    if (not isinstance(prices, dict) or not isinstance(points, dict)
            or len(prices) != 2 or set(prices) != set(points)
            or any(value is not None for value in points.values())):
        return None
    try:
        if any(not Decimal(str(price)).is_finite() or Decimal(str(price)) <= 1
               for price in prices.values()):
            return None
    except (InvalidOperation, ValueError, TypeError):
        return None

    context = identity.get("context")
    catalog = context.get("market_catalog") if isinstance(context, dict) else None
    odds_papi_nba = (identity.get("provider") == "oddspapi_rest"
        and identity.get("market_key") == "111" and identity.get("period") == "result"
        and identity.get("line") == "0" and isinstance(context, dict)
        and context.get("event_sport") == 11 and context.get("tournament_id") == 132
        and context.get("event_league") == "NBA" and isinstance(catalog, dict)
        and catalog.get("marketType") == "moneyline"
        and catalog.get("marketName") == "Winner (incl. overtime)"
        and catalog.get("period") == "result" and catalog.get("playerProp") is False
        and type(catalog.get("handicap")) in (int, float)
        and catalog["handicap"] == 0)
    if odds_papi_nba:
        if set(prices) != {"111", "112"}:
            return None
        outcomes = {"111": home, "112": away}
    elif (identity.get("market_key") in {"h2h", "ml"}
          and identity.get("period") is None and identity.get("line") is None):
        outcomes = ({"home": home, "away": away} if set(prices) == {"home", "away"}
                    else {key: NBA_TEAMS.get(key, key) for key in prices})
    else:
        return None
    return outcomes if len(set(outcomes.values())) == 2 and set(outcomes.values()) == {home, away} else None
