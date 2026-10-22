"""Credit-free Parlay board coverage check; never prints a credential or price.

Coverage is an availability diagnostic over Parlay's serving window, not
quote-level active status, freshness, market matching, or live authorization.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import UTC, datetime

from dotenv import load_dotenv

from evplatform.integrations.parlay import ParlayRest


def _game_summary(body: dict) -> dict:
    return {
        "games_covered": body.get("total_games_covered"),
        "sources": [{"book": row.get("source"), "games_covered": row.get("games_covered"),
                     "h2h": row.get("markets_with_h2h"),
                     "spreads": row.get("markets_with_spread"),
                     "totals": row.get("markets_with_total")}
                    for row in body.get("sources", []) if isinstance(row, dict)],
    }


def _prop_summary(body: dict) -> dict:
    return {
        "window_minutes": body.get("window_minutes"),
        "sources": [{"book": row.get("bookmaker"), "api_visible_rows": row.get("api_visible_rows"),
                     "latest_price_change": row.get("latest_update"), "status": row.get("status")}
                    for row in body.get("sources", []) if isinstance(row, dict)],
    }


async def check(sport: str, books: str, prop_market: str) -> dict:
    load_dotenv(".env", override=False)
    credential = os.getenv("PARLAY_API_KEY")
    if not credential:
        return {"ok": False, "reason": "missing_parlay_key"}
    provider = ParlayRest({"enabled": True, "sports": [sport], "session_request_budget": 1},
                          credential=credential)
    try:
        game = await provider.discover_odds_coverage(sport, markets="h2h,spreads,totals",
                                                      bookmakers=books)
        props = await provider.discover_props_coverage(sport, markets=prop_market,
                                                       bookmakers=books)
        if not isinstance(game.payload, dict) or not isinstance(props.payload, dict):
            return {"ok": False, "reason": "unexpected_coverage_shape"}
        return {"ok": True, "checked_at": datetime.now(UTC).isoformat(), "sport": sport,
                "books_requested": books.split(","), "prop_market": prop_market,
                "game": _game_summary(game.payload), "props": _prop_summary(props.payload),
                "credits_reserved": provider.quota.billable_requests,
                "quote_level_live_eligibility": "not_evaluated"}
    finally:
        await provider.disconnect()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sport", default="basketball_nba")
    parser.add_argument("--books", default="pinnacle,fanduel,draftkings,caesars")
    parser.add_argument("--prop-market", default="player_points")
    args = parser.parse_args()
    try:
        result = asyncio.run(check(args.sport, args.books, args.prop_market))
    except Exception as exc:
        result = {"ok": False, "reason": "coverage_check_failed", "error_type": type(exc).__name__}
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
