"""Compare current NBA event labels/times across source feeds and ProphetX.

Read-only candidate discovery. Never creates mappings, launch evidence, orders,
or source-status claims. Reuses the running native monitor's authenticated
session and prints only event identifiers, participants, clocks and book names.
"""
from __future__ import annotations

import argparse
import json

import httpx

from evplatform.integrations.event_crosswalk import event_candidates
from evplatform.integrations.review_report import fetch_pending_reviews


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8002")
    parser.add_argument("--tournament-id", type=int, default=132)
    args = parser.parse_args()
    if not 1 <= args.tournament_id <= 1_000_000_000:
        parser.error("Use a positive documented tournament ID")
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
            print(json.dumps({"ok": True, **report}, separators=(",", ":")))
    except (ValueError, TypeError, httpx.HTTPError):
        print(json.dumps({"ok": False, "reason": "local_read_or_schema_failed"}))


if __name__ == "__main__":
    main()
