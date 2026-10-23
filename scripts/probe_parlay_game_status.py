"""One metered, read-only Parlay game-board quote-evidence probe.

Counts the same normalized offer evidence the engine sees. No odds, native
links, event IDs, or credentials are printed or persisted by this diagnostic.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from collections import defaultdict
from datetime import UTC, datetime

from dotenv import load_dotenv

from evplatform.integrations.normalization import extract_offers
from evplatform.integrations.parlay import ParlayRest


def summarize(envelope, *, max_age_s: int = 120) -> dict:
    offers = extract_offers(envelope)
    by_book = defaultdict(lambda: {"offers": 0, "two_sided": 0, "clock_verified": 0,
                                   "status_verified": 0, "both_verified": 0,
                                   "stored_row_recent": 0, "historical_status_hint": 0})
    for offer in offers:
        counts = by_book[offer.bookmaker]
        counts["offers"] += 1
        counts["two_sided"] += len(offer.prices) >= 2
        age = ((envelope.received_at - offer.source_observed_at).total_seconds()
               if offer.source_observed_at is not None else None)
        clock = bool(offer.observation_verified and offer.observation_complete
                     and age is not None and 0 <= age <= max_age_s)
        status = offer.market_status_verified
        counts["clock_verified"] += clock
        counts["status_verified"] += status
        counts["both_verified"] += clock and status
        stored_age = ((envelope.received_at - offer.source_stored_at).total_seconds()
                      if offer.source_stored_at is not None else None)
        counts["stored_row_recent"] += stored_age is not None and 0 <= stored_age <= max_age_s
        counts["historical_status_hint"] += offer.source_status_historical_hint
    return {"offer_count": len(offers), "by_book": dict(sorted(by_book.items())),
            "both_status_and_recent_observation_count": sum(row["both_verified"] for row in by_book.values()),
            "note": "Stored row age and FanDuel links are not current quote or status verification; no REST offer is execution certified."}


async def probe(sport: str, books: list[str], markets: list[str], *, max_age_s: int = 120) -> dict:
    if (not re.fullmatch(r"[a-z0-9_]+", sport) or not books or not markets
            or len(books) > 10 or any(not re.fullmatch(r"[a-z0-9_]+", book) for book in books)
            or set(markets) - {"h2h", "spreads", "totals"} or len(set(markets)) != len(markets)
            or not 1 <= max_age_s <= 3600):
        return {"ok": False, "reason": "invalid_scope"}
    load_dotenv(".env", override=False)
    key = os.getenv("PARLAY_API_KEY")
    if not key:
        return {"ok": False, "reason": "missing_parlay_key"}
    adapter = ParlayRest({"enabled": True, "sports": [sport], "bookmakers": books,
                          "markets": markets, "include_props": False,
                          "session_request_budget": len(markets)}, credential=key)
    try:
        envelope = await adapter.fetch_snapshot(sport)
        return {"ok": True, "observed_at": datetime.now(UTC).isoformat(), "sport": sport,
                "requested_books": books, "requested_markets": markets,
                "credits_reserved_max": len(markets), "max_observation_age_s": max_age_s,
                **summarize(envelope, max_age_s=max_age_s)}
    finally:
        await adapter.disconnect()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sport", default="basketball_nba")
    parser.add_argument("--books", default="pinnacle,fanduel,draftkings,circa")
    parser.add_argument("--markets", default="h2h")
    parser.add_argument("--max-observation-age-s", type=int, default=120)
    args = parser.parse_args()
    try:
        result = asyncio.run(probe(args.sport, args.books.split(","), args.markets.split(","),
                                   max_age_s=args.max_observation_age_s))
    except Exception as exc:
        result = {"ok": False, "reason": "probe_failed", "error_type": type(exc).__name__}
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
