"""One authenticated, read-only Parlay NBA prop-status evidence probe.

The request costs the provider's documented three credits. Counts source
fields only; never prints a key, betslip URL, player, event, or price.
"""
from __future__ import annotations

import asyncio
import argparse
import json
import os
from collections import Counter
from datetime import UTC, datetime

from dotenv import load_dotenv

from evplatform.integrations.base import parse_timestamp
from evplatform.integrations.parlay import ParlayRest


async def probe(*, all_markets: bool = False) -> dict:
    load_dotenv(".env", override=False)
    key = os.getenv("PARLAY_API_KEY")
    if not key:
        return {"ok": False, "reason": "missing_parlay_key"}
    provider = ParlayRest({"enabled": True, "sports": ["basketball_nba"],
                           "bookmakers": ["pinnacle", "fanduel", "draftkings", "caesars"],
                           "markets": ["player_points"], "props_limit": 10000 if all_markets else 5000,
                           "max_age_seconds": 120, "session_request_budget": 3}, credential=key)
    try:
        frame = (await provider.fetch_props("basketball_nba") if all_markets else
                 await provider.fetch_props("basketball_nba", markets="player_points"))
        rows = frame.payload
        if not isinstance(rows, list):
            return {"ok": False, "reason": "unexpected_props_shape"}
        wanted = [row for row in rows if isinstance(row, dict)
                  and (all_markets or row.get("market_key") == "player_points")
                  and row.get("bookmaker") in {"pinnacle", "fanduel", "draftkings", "caesars"}]
        books = Counter(row["bookmaker"] for row in wanted)
        now = frame.received_at
        verified = [row for row in wanted if row.get("last_update_type") == "collector_observation"
                    and isinstance(row.get("last_observed"), str)
                    and (stamp := parse_timestamp(row["last_observed"])) is not None
                    and 0 <= (now - stamp).total_seconds() <= 120]
        two_sided = [row for row in wanted if row.get("over_price") is not None
                     and row.get("under_price") is not None]
        bilateral = [row for row in two_sided if row.get("bookmaker") == "fanduel"
                     and all(isinstance(row.get(f"{side}_link"), str)
                             and row[f"{side}_link"].startswith("https://")
                             for side in ("over", "under"))]
        return {"ok": True, "observed_at": datetime.now(UTC).isoformat(),
                "scope": "basketball_nba/all_props" if all_markets else "basketball_nba/player_points",
                "row_count": len(wanted), "counts_by_book": dict(sorted(books.items())),
                "distinct_market_types": len({row.get("market_key") for row in wanted}),
                "limit_reached": len(rows) >= provider.config.props_limit,
                "two_sided_rows": len(two_sided),
                "recent_collector_observation_rows": len(verified),
                "fanduel_bilateral_link_rows": len(bilateral),
                "bilateral_link_and_recent_observation_rows": sum(row in verified for row in bilateral),
                "live_execution_eligible": False,
                "credits_spent_max": 3}
    finally:
        await provider.disconnect()


def main() -> int:
    args = argparse.ArgumentParser(description=__doc__)
    args.add_argument("--all-markets", action="store_true", help="One three-credit NBA prop request without a market filter")
    parsed = args.parse_args()
    try:
        result = asyncio.run(probe(all_markets=parsed.all_markets))
    except Exception as exc:
        result = {"ok": False, "reason": "probe_failed", "error_type": type(exc).__name__}
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
