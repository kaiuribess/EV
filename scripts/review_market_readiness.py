"""Inspect quarantined live market evidence without publishing mappings or orders."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from evplatform.integrations.review_report import ReviewPairs, build_review_report, fetch_pending_reviews


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--url", help="Local app URL, e.g. http://127.0.0.1:8001")
    source.add_argument("--snapshot", type=Path, help="Saved /api/market-review response")
    parser.add_argument("--comparisons", type=Path, help="Explicit ContractReview pairs; these are not certificates")
    parser.add_argument("--max-source-age", type=float, default=5.0)
    parser.add_argument("--max-rows", type=int, default=3000)
    parser.add_argument("--output", type=Path, help="New report file; existing files are never overwritten")
    args = parser.parse_args(argv)
    try:
        snapshot = fetch_pending_reviews(args.url, max_rows=args.max_rows) if args.url else json.loads(args.snapshot.read_text(encoding="utf-8"))
        pairs = ReviewPairs.model_validate_json(args.comparisons.read_text(encoding="utf-8")) if args.comparisons else None
        report = build_review_report(snapshot, max_age_seconds=args.max_source_age, pairs=pairs)
        payload = json.dumps(report, indent=2)
        if args.output:
            with args.output.open("x", encoding="utf-8") as stream:
                stream.write(payload + "\n")
            print(json.dumps(report["summary"], indent=2))
        else:
            print(payload)
        # Success means only that a report was produced, never readiness.
        return 0
    except Exception as exc:
        print(json.dumps({"error": "market_review_failed", "type": type(exc).__name__}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
