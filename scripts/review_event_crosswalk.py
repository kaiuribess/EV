"""Compare current NBA event labels/times across source feeds and ProphetX.

Read-only candidate discovery. Never creates mappings, launch evidence, orders,
or source-status claims. Reuses the running native monitor's authenticated
session and prints only event identifiers, participants, clocks and book names.
"""
from __future__ import annotations

import argparse
import json

import httpx

from evplatform.integrations.event_crosswalk import event_candidates, moneyline_candidates
from evplatform.integrations.review_report import fetch_pending_reviews


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8002")
    parser.add_argument("--tournament-id", type=int, default=132)
    parser.add_argument("--event-id", help="Also review the exact native event's moneyline strike labels")
    args = parser.parse_args()
    if not 1 <= args.tournament_id <= 1_000_000_000:
        parser.error("Use a positive documented tournament ID")
    if args.event_id is not None and (not args.event_id.isascii() or not args.event_id.isdigit()):
        parser.error("Use a numeric ProphetX event ID")
    try:
        with httpx.Client(timeout=20, trust_env=False, follow_redirects=False) as client:
            snapshot = fetch_pending_reviews(args.url, max_rows=5000, client=client)
            response = client.post(args.url.rstrip("/") + "/api/native/discovery/prophetx",
                json={"tournament_id": args.tournament_id, "limit": 50},
                headers={"X-EV-Control": "local"})
            if response.status_code != 200:
                print(json.dumps({"ok": False, "reason": "native_catalog_unavailable",
                                  "http_status": response.status_code}))
                return
            report = event_candidates(response.json(), snapshot)
            if args.event_id is not None:
                market_response = client.post(args.url.rstrip("/") + "/api/native/discovery/prophetx",
                    json={"event_id": args.event_id, "limit": 50},
                    headers={"X-EV-Control": "local"})
                if market_response.status_code != 200:
                    print(json.dumps({"ok": False, "reason": "native_market_catalog_unavailable",
                                      "http_status": market_response.status_code}))
                    return
                report["moneyline_review"] = moneyline_candidates(
                    report, snapshot, market_response.json(), native_event_id=args.event_id)
            print(json.dumps({"ok": True, **report}, separators=(",", ":")))
    except (ValueError, TypeError, httpx.HTTPError):
        print(json.dumps({"ok": False, "reason": "local_read_or_schema_failed"}))


if __name__ == "__main__":
    main()
