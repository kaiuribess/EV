"""Inspect current public Polymarket US sports monitoring scope; no key or order access."""
from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timezone

from evplatform.integrations.polymarket_us_discovery import balanced_open_moneylines
from evplatform.venues.polymarket_us import PolymarketUSClient


async def check(leagues: list[str], max_markets: int):
    client = PolymarketUSClient()
    try:
        catalogs = [await client.discover_league_events(league) for league in leagues]
        selected, truncated = balanced_open_moneylines(catalogs, leagues=leagues,
            now=datetime.now(timezone.utc), max_markets=max_markets)
        return {"ok": True, "read_only": True, "public_catalog": True,
                "leagues": leagues, "selected_slugs": selected,
                "truncated": truncated, "pages": [len(pages) for pages in catalogs],
                "orders_sent": 0}
    finally:
        await client.disconnect()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--leagues", default="nba,nfl,mlb,nhl")
    parser.add_argument("--max-markets", type=int, default=8)
    args = parser.parse_args()
    leagues = args.leagues.split(",")
    if not 1 <= len(leagues) <= 4 or not 1 <= args.max_markets <= 50:
        parser.error("Use one to four leagues and one to fifty monitoring markets")
    print(json.dumps(asyncio.run(check(leagues, args.max_markets)), separators=(",", ":")))


if __name__ == "__main__":
    main()
