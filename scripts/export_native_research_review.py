"""Export one exact, read-only native discovery lead for human mapping review.

This cannot write native_mappings.json or any approval/launch/attempt record.
The existing verified mapping loader remains the sole execution path.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import httpx

from evplatform.core.paths import runtime_data_dir
from evplatform.core.research_review import review_payload, save_review


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-market-id", required=True)
    parser.add_argument("--venue", choices=("kalshi", "novig"), required=True)
    parser.add_argument("--native-market-id", required=True)
    parser.add_argument("--native-outcome-id", required=True)
    parser.add_argument("--strategy", choices=("multi_book_consensus", "sharp_books"))
    parser.add_argument("--url", default="http://127.0.0.1:8002")
    parser.add_argument("--data-dir", type=Path)
    args = parser.parse_args()
    with httpx.Client(base_url=args.url, timeout=15, trust_env=False) as client:
        response = client.get("/api/state")
        response.raise_for_status()
        payload = review_payload(response.json(), source_market_id=args.source_market_id,
            venue=args.venue, native_market_id=args.native_market_id,
            native_outcome_id=args.native_outcome_id, strategy=args.strategy)
    target = save_review(payload, runtime_data_dir(args.data_dir) / "native_review_drafts")
    print(json.dumps({"saved": str(target), "status": payload["status"],
                      "execution_authorized": False}))


if __name__ == "__main__":
    main()
