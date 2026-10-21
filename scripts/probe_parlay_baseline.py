"""Read-only Parlay game-board snapshot diagnostic; prints no odds or credentials.

Run from the repository root. This makes one authenticated WebSocket connection
per requested book. It does not persist frames, alter the dashboard, or trade.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from datetime import datetime, timezone
from urllib.parse import urlencode

from dotenv import load_dotenv
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed


async def probe(sport: str, book: str, key: str, max_age_s: int, event_id: str | None = None) -> dict:
    params = {"bookmakers": book, "kinds": "game", "markets": "h2h,spreads,totals",
              "limit": "1000", "max_age_s": str(max_age_s), "diff": "false"}
    if event_id:
        params["event_id"] = event_id
    url = f"wss://parlay-api.com/v1/ws/odds/{sport}?{urlencode(params)}"
    result = {"observed_at": datetime.now(timezone.utc).isoformat(), "sport": sport,
              "bookmaker": book, "request_scope": "full_game_board", "max_age_s": max_age_s,
              "event_filtered": event_id is not None}
    try:
        async with asyncio.timeout(15):
            async with connect(url, additional_headers={"X-API-Key": key}, open_timeout=8,
                               max_size=8 * 1024 * 1024, max_queue=8) as ws:
                while True:
                    frame = json.loads(await ws.recv())
                    if not isinstance(frame, dict):
                        result["result"] = "invalid_frame"
                        return result
                    kind = frame.get("type")
                    if kind == "initial_state":
                        result.update({"result": "snapshot", "snapshot_scope": frame.get("snapshot_scope"),
                                       "snapshot_complete": frame.get("snapshot_complete"),
                                       "truncated": frame.get("truncated"),
                                       "resume_mode": frame.get("resume_mode"),
                                       "count": frame.get("count"),
                                       "books": frame.get("books"),
                                       "first_event_id": next((row.get("event_id") for row in frame.get("data", [])
                                            if isinstance(row, dict) and isinstance(row.get("event_id"), str)), None),
                                       "partial": frame.get("partial"),
                                       "missing_books": frame.get("missing_books"),
                                       "replacement_eligible": (frame.get("snapshot_scope") == "current_game_board"
                                            and frame.get("snapshot_complete") is True
                                            and frame.get("truncated") is False)})
                        return result
                    if kind not in {"connected", "heartbeat"}:
                        result["result"] = "unexpected_frame"
                        return result
    except ConnectionClosed as exc:
        result.update({"result": "closed", "code": exc.rcvd.code if exc.rcvd else None})
    except TimeoutError:
        result["result"] = "timeout"
    except (OSError, ValueError, TypeError, KeyError):
        result["result"] = "connection_or_protocol_error"
    return result


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sport", default="basketball_nba")
    parser.add_argument("--books", nargs="+", default=["fanduel", "draftkings", "pinnacle"])
    parser.add_argument("--event-id")
    parser.add_argument("--max-age-s", type=int, default=120)
    args = parser.parse_args()
    if not 1 <= args.max_age_s <= 3600:
        parser.error("--max-age-s must be between 1 and 3600")
    if not re.fullmatch(r"[a-z0-9_]+", args.sport):
        parser.error("--sport must be a simple sport key")
    if args.event_id and not re.fullmatch(r"[A-Za-z0-9_:\-]+", args.event_id):
        parser.error("--event-id must be a simple provider event ID")
    load_dotenv(".env", override=False)
    key = os.getenv("PARLAY_API_KEY")
    if not key:
        print(json.dumps({"result": "missing_PARLAY_API_KEY"}))
        return
    for book in args.books:
        if not all(re.fullmatch(r"[a-z0-9_]+", part) for part in book.split(",")):
            parser.error("book names must be simple comma-separated bookmaker keys")
        print(json.dumps(await probe(args.sport, book, key, args.max_age_s, args.event_id)))


if __name__ == "__main__":
    asyncio.run(main())
