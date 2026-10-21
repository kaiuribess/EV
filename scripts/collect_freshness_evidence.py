"""Bounded read-only capture: existing Parlay socket + at most one Oddize paid read."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from dotenv import load_dotenv

from evplatform.integrations.freshness_evidence import FreshnessEvidence, evidence_digest
from evplatform.integrations.oddize import OddizeRest
from evplatform.integrations.parlay import ParlayWebSocket


async def collect(*, seconds: int, parlay_sport: str, oddize_sport: str,
                  parlay_books: list[str], oddize_books: list[str],
                  parlay_markets: list[str] | None = None,
                  parlay_kinds: list[str] | None = None, skip_oddize: bool = False):
    evidence = FreshnessEvidence()
    diagnostics = {}
    async def parlay():
        adapter = ParlayWebSocket({"enabled": True, "sports": [parlay_sport], "bookmakers": parlay_books,
            "markets": parlay_markets or [], "stream_kinds": parlay_kinds or ["game", "prop"], "max_age_seconds": 30,
            "stream_limit": 1000, "session_request_budget": 1})
        last_health = None
        try:
            async with asyncio.timeout(seconds):
                async for envelope in adapter.stream_updates():
                    evidence.observe(envelope)
                    last_health = await adapter.health_check()
        except TimeoutError:
            pass
        except Exception as error:
            diagnostics["parlay_capture_error_type"] = type(error).__name__
        finally:
            health = last_health or await adapter.health_check()
            diagnostics["parlay"] = {key: health.get(key) for key in (
                "authenticated", "credential_configured", "errors", "server_connections", "ready_sports")}
            diagnostics["parlay"]["capture_closed"] = True
            await adapter.disconnect()
    async def oddize():
        if skip_oddize:
            diagnostics["oddize"] = {"snapshot_skipped": "operator_selected_parlay_only", "requests": 0}
            return
        adapter = OddizeRest({"enabled": True, "sports": [oddize_sport], "bookmakers": oddize_books,
                              "markets": ["ml"], "session_request_budget": 1, "limit": 100})
        try:
            if not await adapter.connect():
                diagnostics["oddize"] = {"credential_configured": False, "requests": 0}
                return
            await adapter.fetch_account()  # Documented free metadata read.
            remaining = adapter.quota.provider_remaining
            if type(remaining) is not int or remaining < 1:
                diagnostics["oddize"] = {"snapshot_skipped": "remaining_credit_not_verified"}
                return
            envelope = await adapter.fetch_snapshot(oddize_sport)
            evidence.observe(envelope)
            diagnostics["oddize"] = {"authenticated": True, "http_roundtrip_ms": adapter.last_http_latency_ms,
                "requests": adapter.quota.calls, "credits_used": adapter.quota.billable_requests,
                "credits_remaining_after": adapter.quota.provider_remaining}
        except Exception as error:
            diagnostics["oddize_capture_error_type"] = type(error).__name__
        finally:
            await adapter.disconnect()
    await asyncio.gather(parlay(), oddize())
    report = evidence.report()
    report["capture"] = {"mode": "authenticated_read_only", "requested_seconds": seconds,
                          "parlay_sport": parlay_sport, "oddize_sport": oddize_sport,
                          "parlay_books": parlay_books, "parlay_markets": parlay_markets or [],
                          "parlay_kinds": parlay_kinds or ["game", "prop"], "skip_oddize": skip_oddize,
                          "diagnostics": diagnostics}
    report["artifact_sha256_without_this_field"] = evidence_digest(report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seconds", type=int, choices=range(5, 61), default=20, metavar="5..60")
    parser.add_argument("--parlay-sport", default="basketball_nba")
    parser.add_argument("--oddize-sport", default="nba")
    parser.add_argument("--parlay-books", default="pinnacle,fanduel,draftkings")
    parser.add_argument("--oddize-books", default="pin,circa,dk,fd")
    parser.add_argument("--parlay-markets", default="", help="Comma-separated documented native market keys")
    parser.add_argument("--parlay-kinds", nargs="+", choices=["game", "prop"], default=["game", "prop"])
    parser.add_argument("--skip-oddize", action="store_true", help="No Oddize calls or credits for a Parlay-only diagnostic")
    args = parser.parse_args()
    if args.output.exists():
        print(json.dumps({"ok": False, "error": "choose_new_report_path"}))
        return 2
    load_dotenv(Path(__file__).resolve().parents[1]/".env", override=False)
    report = asyncio.run(collect(seconds=args.seconds, parlay_sport=args.parlay_sport, oddize_sport=args.oddize_sport,
        parlay_books=args.parlay_books.split(","), oddize_books=args.oddize_books.split(","),
        parlay_markets=[value for value in args.parlay_markets.split(",") if value],
        parlay_kinds=args.parlay_kinds, skip_oddize=args.skip_oddize))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as output:
        json.dump(report, output, indent=2, allow_nan=False)
        output.write("\n")
    print(json.dumps({"ok": True, "offers_seen": report["offers_seen"], "groups": len(report["groups"]),
        "frame_counts": report["frame_counts"], "capture": report["capture"], "execution_enabled": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
