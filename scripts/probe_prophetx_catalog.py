"""Read-only ProphetX event discovery for native stream setup.

Uses the configured production Trading API key for one authentication session
and GET-only fixture reads. Prints only bounded public event identifiers and
labels; never prints credentials, tokens, account data, or raw responses.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import datetime, timezone

from dotenv import load_dotenv

from evplatform.venues.prophetx import ProphetXClient, ProphetXSettings


TOURNAMENTS = {"nba": 132, "nfl": 31, "nhl": 234, "mlb": 109}


async def discover(sports: list[str], limit: int, event_ids: list[str] | None = None) -> dict:
    load_dotenv(".env", override=False)
    key, secret = os.getenv("PROPHETX_API_KEY"), os.getenv("PROPHETX_SECRET_KEY")
    if not key or not secret:
        return {"ok": False, "reason": "production_credentials_missing"}
    client = ProphetXClient(ProphetXSettings(
        environment="production", api_key=key, secret_key=secret,
    ))
    groups = []
    try:
        for sport in sports:
            response = await client.discover_events(tournament_id=TOURNAMENTS[sport])
            data = response.get("data") if isinstance(response, dict) else None
            rows = data.get("sport_events") if isinstance(data, dict) else None
            if not isinstance(rows, list):
                groups.append({"sport": sport, "result": "unexpected_event_schema"})
                continue
            events = []
            for row in rows[:limit]:
                if not isinstance(row, dict):
                    continue
                event_id = row.get("event_id")
                if isinstance(event_id, (int, str)) and str(event_id).isdigit():
                    events.append({"event_id": str(event_id),
                                   "name": str(row.get("name", ""))[:120],
                                   "start": row.get("scheduled")})
            groups.append({"sport": sport, "tournament_id": TOURNAMENTS[sport],
                           "total_events": len(rows), "event_fields": sorted(rows[0]) if rows and isinstance(rows[0], dict) else [],
                           "sample": events})
        market_shapes = []
        for event_id in event_ids or []:
            response = await client.fetch_snapshot(event_id)
            data = response.get("data") if isinstance(response, dict) else None
            markets = data.get("markets") if isinstance(data, dict) else None
            market_shapes.append({"event_id": event_id,
                                  "data_fields": sorted(data) if isinstance(data, dict) else [],
                                  "market_count": len(data.get("markets", [])) if isinstance(data, dict)
                                      and isinstance(data.get("markets"), list) else None,
                                  "first_market_fields": sorted(data["markets"][0]) if isinstance(data, dict)
                                      and isinstance(data.get("markets"), list) and data["markets"]
                                      and isinstance(data["markets"][0], dict) else [],
                                  "first_selections_type": type(data["markets"][0].get("selections")).__name__
                                      if isinstance(data, dict) and isinstance(data.get("markets"), list)
                                      and data["markets"] and isinstance(data["markets"][0], dict) else None,
                                  "first_selection_fields": sorted(data["markets"][0]["selections"][0])
                                      if isinstance(data, dict) and isinstance(data.get("markets"), list)
                                      and data["markets"] and isinstance(data["markets"][0], dict)
                                      and isinstance(data["markets"][0].get("selections"), list)
                                      and data["markets"][0]["selections"]
                                      and isinstance(data["markets"][0]["selections"][0], dict) else [],
                                  "first_selection_type": type(data["markets"][0]["selections"][0]).__name__
                                      if isinstance(data, dict) and isinstance(data.get("markets"), list)
                                      and data["markets"] and isinstance(data["markets"][0], dict)
                                      and isinstance(data["markets"][0].get("selections"), list)
                                      and data["markets"][0]["selections"] else None,
                                  "first_nested_selection_fields": sorted(data["markets"][0]["selections"][0][0])
                                      if isinstance(data, dict) and isinstance(data.get("markets"), list)
                                      and data["markets"] and isinstance(data["markets"][0], dict)
                                      and isinstance(data["markets"][0].get("selections"), list)
                                      and data["markets"][0]["selections"]
                                      and isinstance(data["markets"][0]["selections"][0], list)
                                      and data["markets"][0]["selections"][0]
                                      and isinstance(data["markets"][0]["selections"][0][0], dict) else [],
                                  "markets": [{"id": row.get("id"), "name": str(row.get("name", ""))[:100],
                                               "type": row.get("type"), "sub_type": row.get("sub_type"),
                                               "status": row.get("status"),
                                               "selections_type": type(row.get("selections")).__name__,
                                               "selection_count": len(row["selections"]) if isinstance(row.get("selections"), list) else None}
                                              for row in markets[:10] if isinstance(row, dict)]
                                      if isinstance(markets, list) else []})
        return {"ok": True, "observed_at": datetime.now(timezone.utc).isoformat(),
                "read_only": True, "groups": groups, "market_shapes": market_shapes}
    except Exception as exc:
        # Provider error messages or raw payloads may contain private details.
        return {"ok": False, "reason": "catalog_read_failed", "error_type": type(exc).__name__}
    finally:
        await client.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sports", nargs="+", choices=sorted(TOURNAMENTS), default=["nba", "nhl"])
    parser.add_argument("--limit", type=int, default=12)
    parser.add_argument("--event-ids", nargs="*", default=[], help="Up to three discovered numeric event IDs for market schema review")
    args = parser.parse_args()
    if not 1 <= args.limit <= 30:
        parser.error("--limit must be between 1 and 30")
    if len(args.event_ids) > 3 or any(not value.isdigit() for value in args.event_ids):
        parser.error("--event-ids accepts at most three numeric IDs")
    print(json.dumps(asyncio.run(discover(args.sports, args.limit, args.event_ids)), separators=(",", ":")))


if __name__ == "__main__":
    main()
