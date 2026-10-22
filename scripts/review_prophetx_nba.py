"""Read-only ProphetX NBA moneyline crosswalk through the running local monitor."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import httpx

from evplatform.integrations.prophetx_event_crosswalk import fixture_candidates
from evplatform.integrations.event_crosswalk import moneyline_candidates
from evplatform.integrations.review_report import fetch_pending_reviews


def collect_market_catalogs(client: httpx.Client, endpoint: str, events: dict,
                            headers: dict[str, str]) -> tuple[dict, list[dict]]:
    """Use ProphetX's documented batch read; reject a partial response."""
    ids = [event.get("event_id") for event in events.get("events", [])[:10]
           if isinstance(event, dict) and event.get("status") == "not_started"]
    ids = [value for value in ids if isinstance(value, str) and value.isascii() and value.isdigit()]
    if not ids:
        return {}, []
    response = None
    try:
        response = client.post(endpoint, json={"event_ids": ids, "limit": 50,
            "market_sub_type": "moneyline"}, headers=headers)
        response.raise_for_status()
        payload = response.json()
        catalog = payload.get("by_event") if isinstance(payload, dict) else None
        if (payload.get("read_only") is not True or not isinstance(catalog, dict)
                or set(catalog) != set(ids) or any(not isinstance(row, dict)
                    or row.get("read_only") is not True for row in catalog.values())):
            raise ValueError("Incomplete native market catalog")
        return catalog, []
    except (httpx.HTTPError, ValueError, AttributeError):
        # A provider 429 can be relayed as 502. Do not fall back to ten
        # separate requests or misrepresent the unread events as reviewed.
        return {}, [{"event_ids": ids, "reason": "native_batch_catalog_unavailable",
                     "http_status": response.status_code if response is not None else None}]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8002")
    parser.add_argument("--output", type=Path, help="Write a new local review report")
    args = parser.parse_args()
    try:
        with httpx.Client(timeout=20, trust_env=False, follow_redirects=False) as client:
            sources = fetch_pending_reviews(args.url, max_rows=5000, client=client)
            endpoint = args.url.rstrip("/") + "/api/native/discovery/prophetx"
            headers = {"X-EV-Control": "local"}
            response = client.post(endpoint, json={"tournament_id": 132, "limit": 50}, headers=headers)
            response.raise_for_status()
            events = response.json()
            catalog, errors = collect_market_catalogs(client, endpoint, events, headers)
            report = fixture_candidates(events, catalog, sources)
            report["native_catalog_read_errors"] = errors
            # A fixture/strike label alone does not identify the sportsbook
            # wager. Retain only exact two-sided full-game source moneylines as
            # additional review leads, with the source evidence flags intact.
            source_leads = []
            for event_id, market_catalog in catalog.items():
                if market_catalog.get("truncated") is not False:
                    continue
                review = moneyline_candidates(report, sources, market_catalog,
                                               native_event_id=event_id)
                source_leads.extend(review["candidates"])
            report["source_moneyline_candidates"] = source_leads
            report["source_moneyline_candidate_count"] = len(source_leads)
            report["source_moneyline_status_verified_count"] = sum(
                lead["source_market_status_verified"] for lead in source_leads)
            if args.output:
                with args.output.open("x", encoding="utf-8") as stream:
                    json.dump(report, stream, indent=2)
                    stream.write("\n")
                result = {key: report[key] for key in (
                    "match_count", "conflicting_source_events", "conflicting_native_events",
                    "cross_provider_start_conflicts", "native_catalog_truncated",
                    "native_market_catalogs_missing", "execution_authorized")}
                result["schedule_disagreements"] = len(report["schedule_disagreements"])
                result["native_catalog_read_errors"] = len(errors)
                result["source_moneyline_candidates"] = len(source_leads)
                result["source_moneyline_status_verified"] = report[
                    "source_moneyline_status_verified_count"]
                print(json.dumps({"ok": True, **result}, separators=(",", ":")))
            else:
                print(json.dumps({"ok": True, **report}, separators=(",", ":")))
    except (ValueError, TypeError, OSError, httpx.HTTPError):
        print(json.dumps({"ok": False, "reason": "bounded_catalog_or_source_read_failed"}))


if __name__ == "__main__":
    main()
