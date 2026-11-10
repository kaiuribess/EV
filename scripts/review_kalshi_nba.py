"""Read-only Kalshi NBA fixture crosswalk; never publishes executable mappings."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import httpx

from evplatform.integrations.kalshi_event_crosswalk import fixture_candidates, moneyline_candidates
from evplatform.integrations.review_report import fetch_pending_reviews


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8002")
    parser.add_argument("--output", type=Path, help="Write a new local review report")
    args = parser.parse_args()
    try:
        with httpx.Client(timeout=15, trust_env=False, follow_redirects=False) as client:
            sources = fetch_pending_reviews(args.url, max_rows=5000, client=client)
            response = client.get("https://external-api.kalshi.com/trade-api/v2/markets",
                params={"series_ticker": "KXNBAGAME", "status": "open", "limit": 1000,
                        "mve_filter": "exclude"})
            response.raise_for_status()
            report = fixture_candidates(response.json(), sources)
            report["moneyline_review"] = moneyline_candidates(report, sources)
            if args.output:
                with args.output.open("x", encoding="utf-8") as stream:
                    json.dump(report, stream, indent=2)
                    stream.write("\n")
                print(json.dumps({"ok": True, "match_count": report["match_count"],
                                  "native_outcome_rule_fixtures": sum(
                                      row["native_outcome_rules_verified"] for row in report["matches"]),
                                  "source_moneyline_candidates": report["moneyline_review"]["candidate_count"],
                                  "quote_evidence_ready": report["moneyline_review"]["quote_evidence_ready_count"],
                                  "conflicting_source_events": report["conflicting_source_events"],
                                  "conflicting_native_events": report["conflicting_native_events"],
                                  "cross_provider_start_conflicts": report["cross_provider_start_conflicts"],
                                  "unknown_native_team_codes": report["unknown_native_team_codes"],
                                  "native_catalog_truncated": report["native_catalog_truncated"],
                                  "execution_authorized": False}))
            else:
                print(json.dumps({"ok": True, **report}, separators=(",", ":")))
    except (ValueError, TypeError, OSError, httpx.HTTPError):
        print(json.dumps({"ok": False, "reason": "bounded_catalog_or_source_read_failed"}))


if __name__ == "__main__":
    main()
