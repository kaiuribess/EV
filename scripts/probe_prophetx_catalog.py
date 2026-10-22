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


async def discover(sports: list[str], limit: int) -> dict:
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
                                   "start": row.get("start_time") or row.get("scheduled_start")})
            groups.append({"sport": sport, "tournament_id": TOURNAMENTS[sport],
                           "total_events": len(rows), "sample": events})
        return {"ok": True, "observed_at": datetime.now(timezone.utc).isoformat(),
                "read_only": True, "groups": groups}
    except Exception as exc:
        # Provider error messages or raw payloads may contain private details.
        return {"ok": False, "reason": "catalog_read_failed", "error_type": type(exc).__name__}
    finally:
        await client.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sports", nargs="+", choices=sorted(TOURNAMENTS), default=["nba", "nhl"])
    parser.add_argument("--limit", type=int, default=12)
    args = parser.parse_args()
    if not 1 <= args.limit <= 30:
        parser.error("--limit must be between 1 and 30")
    print(json.dumps(asyncio.run(discover(args.sports, args.limit)), separators=(",", ":")))


if __name__ == "__main__":
    main()
