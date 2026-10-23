"""Read-only, bounded snapshots of the already-running production scanner.

This only calls the local /api/state endpoint. It neither polls providers nor
changes settings. Run the analysis on the resulting JSONL after sampling.
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _quote(row: dict) -> dict:
    keys = (
        "market_id", "provider", "bookmaker", "decimal_odds", "status",
        "received_at", "source_time", "provider_time", "source_observed_at",
        "source_price_changed_at", "source_stored_at", "source_scope_poll_at",
        "source_scope_is_current", "source_status_historical_hint",
        "observation_verified", "market_status_verified", "verification_reason",
        "trusted_research_usable", "execution_freshness_reasons",
        "delivery_age_ms", "stored_price_age_ms", "synthetic",
    )
    return {key: row.get(key) for key in keys}


def _native(row: dict) -> dict:
    keys = (
        "venue", "source_market_id", "native_market_id", "native_outcome_id",
        "side", "outcome", "price", "available_depth_contracts",
        "comparison_quantity", "normal_payout_usd", "cash_cost_usd",
        "gross_estimated_profit_usd", "taker_fee_usd",
        "fee_adjusted_estimated_profit_usd", "uncertainty_allowance_usd",
        "net_estimated_profit_usd", "positive_after_costs", "positive_estimate",
        "uncertainty_allowance_policy", "uncertainty_allowance_probability",
        "reasons", "rejection", "fair_probability", "calculated_at",
        "catalog_observed_at", "catalog_age_ms", "historical",
        "mapping_reviewed", "execution_eligible", "fee_evidence",
    )
    return {key: row.get(key) for key in keys}


def compact_state(state: dict, observed_at: str) -> dict:
    if state.get("mode") != "production" or state.get("auto_trade") is not False or state.get("kill_switch") is not True:
        raise RuntimeError("Production safety state changed; observation stopped")
    markets = {row["id"]: {
        key: row.get(key) for key in (
            "sport", "league", "market_type", "statistic", "line",
            "player", "start_time", "status", "outcomes", "event_name",
        )
    } for row in state.get("markets", [])}
    quotes = [_quote(row) for row in state.get("quotes", []) if not row.get("synthetic")]
    references = []
    for row in state.get("reference_estimates", []):
        if row.get("synthetic"):
            continue
        references.append({
            "market_id": row.get("market_id"),
            "calculated_at": row.get("calculated_at"),
            "reference_profile_id": row.get("reference_profile_id"),
            "quality_mode": row.get("quality_mode"),
            "references": [{key: ref.get(key) for key in (
                "bookmaker", "provider", "local_receipt_at", "source_observed_at",
                "source_stored_at", "source_scope_poll_at", "delivery_age_ms",
                "status", "verification_reason", "effective_weight_percent",
            )} for ref in row.get("references", [])],
            "native_research_estimates": [_native(item) for item in row.get("native_research_estimates", [])],
            "native_discovery_reason": row.get("native_discovery_reason"),
            "limitations": row.get("limitations", []),
        })
    active_ids = {row["market_id"] for row in quotes} | {row["market_id"] for row in references}
    history = [{
        "market_id": row.get("market_id"),
        "calculated_at": row.get("calculated_at"),
        "expired_at": row.get("expired_at"),
        "expiration_reason": row.get("expiration_reason"),
        "references": [{key: ref.get(key) for key in (
            "bookmaker", "provider", "local_receipt_at", "source_observed_at",
            "source_stored_at", "source_scope_poll_at", "status",
        )} for ref in row.get("references", [])],
        "native_research_estimates": [_native(item) for item in row.get("native_research_estimates", [])],
    } for row in state.get("research_history", []) if row.get("historical")]
    return {
        "observed_at": observed_at,
        "state_at": state.get("timestamp"),
        "started_at": state.get("started_at"),
        "controls": {key: state.get(key) for key in ("mode", "auto_trade", "kill_switch", "live_enabled")},
        "plugins": [{"name": row.get("manifest", {}).get("name"), "enabled": row.get("enabled"),
                     "health": row.get("health")} for row in state.get("plugins", [])],
        "markets": {key: markets.get(key) for key in active_ids},
        "quotes": quotes,
        "reference_estimates": references,
        "research_history": history,
        "counts": {"all_markets": len(state.get("markets", [])),
                   "all_quotes": len(state.get("quotes", [])),
                   "active_references": len(references),
                   "historical_references": len(state.get("research_history", [])),
                   "native_orders": len(state.get("native_orders", []))},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8002")
    parser.add_argument("--duration-seconds", type=int, default=1800)
    parser.add_argument("--interval-seconds", type=int, default=30)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if not (1800 <= args.duration_seconds <= 3600):
        parser.error("duration must be between 30 and 60 minutes")
    if not (10 <= args.interval_seconds <= 60):
        parser.error("interval must be between 10 and 60 seconds")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    deadline = start + args.duration_seconds
    next_sample = start
    successful = 0
    with httpx.Client(base_url=args.base, timeout=25) as client, args.out.open("x", encoding="utf-8") as handle:
        while time.monotonic() <= deadline:
            if time.monotonic() < next_sample:
                time.sleep(min(next_sample - time.monotonic(), 1))
                continue
            observed_at = _utc_now()
            try:
                response = client.get("/api/state")
                response.raise_for_status()
                sample = compact_state(response.json(), observed_at)
                successful += 1
                result = {"sample": sample}
            except RuntimeError:
                raise
            except (httpx.HTTPError, ValueError) as exc:
                result = {"error": {"observed_at": observed_at, "type": type(exc).__name__}}
            handle.write(json.dumps(result, separators=(",", ":"), default=str) + "\n")
            handle.flush()
            print(json.dumps({"at": observed_at, "samples": successful,
                              "quotes": sample["counts"]["all_quotes"] if "sample" in result else None,
                              "references": sample["counts"]["active_references"] if "sample" in result else None}),
                  flush=True)
            next_sample += args.interval_seconds
    print(json.dumps({"completed_at": _utc_now(), "samples": successful,
                      "elapsed_seconds": round(time.monotonic() - start, 1),
                      "output": str(args.out)}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
