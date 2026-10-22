"""One bounded, read-only NBA h2h + points capture using saved research weights.

No configuration write, native venue call, arming, or order placement.
"""
from __future__ import annotations

import asyncio
import argparse
import json
import os
import sqlite3
from collections import defaultdict
from datetime import UTC, datetime

from dotenv import load_dotenv

from evplatform.core.config import PricingConfig
from evplatform.core.paths import PROJECT_ROOT, runtime_database_path
from evplatform.core.pricing import NoSignal, fair_value
from evplatform.core.reference_profiles import trusted_quote_research_eligibility
from evplatform.integrations.normalization import TrustedSourceProjector, VerifiedProviderMapper
from evplatform.integrations.parlay import ParlayRest


async def main(database_argument: str | None = None):
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    key = os.getenv("PARLAY_API_KEY")
    if not key:
        return {"ok": False, "reason": "Parlay key is not configured locally"}
    if database_argument is None:
        runtime_file = PROJECT_ROOT / "data" / "runtime-8002.json"
        if not runtime_file.exists():
            return {"ok": False, "reason": "Production runtime database path is unavailable"}
        database_argument = json.loads(runtime_file.read_text(encoding="utf-8-sig"))["database_path"]
    database = runtime_database_path(database_argument)
    with sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True) as db:
        row = db.execute("SELECT data FROM config WHERE key='pricing'").fetchone()
    if row is None:
        return {"ok": False, "reason": "No saved pricing configuration"}
    saved = PricingConfig.model_validate_json(row[0])
    if saved.normal_outcome_policy != "winner_only_binary":
        return {"ok": False, "reason": "Production normal-outcome policy is not winner_only_binary"}
    if (saved.reference_quality_mode != "trusted_provider"
            and not any(override.get("reference_quality_mode") == "trusted_provider"
                        for override in saved.market_overrides.values())):
        return {"ok": False, "reason": "Production trusted-provider research mode is not configured"}
    research = saved
    mapper = VerifiedProviderMapper()
    mapper.research_projector = TrustedSourceProjector(lambda: research)
    client = ParlayRest({"enabled": True, "sports": ["basketball_nba"],
        "bookmakers": ["pinnacle", "fanduel", "draftkings", "caesars"],
        "markets": ["h2h"], "include_props": False,
        "props_limit": 1000, "session_request_budget": 4}, credential=key)
    frames = []
    try:
        frames.append(await client.fetch_snapshot("basketball_nba"))
        frames.append(await client.fetch_props("basketball_nba", markets="player_points"))
    finally:
        await client.disconnect()
    counts = {}
    for frame in frames:
        mapper(frame)
        counts[frame.kind] = {"rows": len(frame.payload),
            "source_only_updates": len(mapper.research_updates),
            "received_at": frame.received_at.isoformat(),
            "completeness_headers": {k: v for k, v in frame.metadata.get("headers", {}).items()
                                     if k.startswith("x-result-") or k.startswith("x-parlay-stale")}}
    grouped = defaultdict(list)
    for market, quote in mapper.research_projector.active.values():
        grouped[market.id].append((market, quote))
    findings = {"moneyline": None, "player_prop": None}
    category_counts = {"moneyline": {"markets": 0, "two_book": 0, "fair": 0},
                       "player_prop": {"markets": 0, "two_book": 0, "fair": 0}}
    now = datetime.now(UTC)
    for entries in grouped.values():
        market = entries[0][0]
        category = "moneyline" if market.market_type == "moneyline" else "player_prop"
        category_counts[category]["markets"] += 1
        quotes = [quote for _, quote in entries]
        if len({q.bookmaker for q in quotes}) >= 2:
            category_counts[category]["two_book"] += 1
        try:
            fair = fair_value(market, quotes, research, now)
            probability = {k: str(v) for k, v in fair.probabilities.items()}
            rejection = None
            references = list(fair.references)
        except NoSignal as exc:
            probability, rejection, references = None, str(exc), []
        if probability is not None:
            category_counts[category]["fair"] += 1
        if category == "moneyline" and len(quotes) < 2 and probability is None:
            continue
        if findings[category] is not None and (findings[category]["fair_probabilities"] is not None or probability is None):
            continue
        findings[category] = {"event": market.event_name, "start_time": market.start_time.isoformat(),
            "market": market.market_type, "player": market.player,
            "statistic": market.statistic, "line": str(market.line) if market.line is not None else None,
            "source_event_id": next(iter(market.original_ids.values())),
            "books": [{"book": q.bookmaker, "prices": {k: str(v) for k, v in q.decimal_odds.items()},
                       "status": q.status, "quote_observed_at": q.source_observed_at.isoformat() if q.source_observed_at else None,
                       "book_poll_at": q.source_scope_poll_at.isoformat() if q.source_scope_poll_at else None,
                       "book_poll_is_current": q.source_scope_is_current,
                       "verification_reason": q.verification_reason,
                       "trusted_research_usable": trusted_quote_research_eligibility(research, market, q, now)[0],
                       "trusted_research_reasons": trusted_quote_research_eligibility(research, market, q, now)[1],
                       "price_row_time": q.source_time.isoformat() if q.source_time else None,
                       "received_at": q.received_at.isoformat()} for q in quotes],
            "fair_probabilities": probability, "reference_weights": [
                {"book": r["bookmaker"], "effective_percent": r["effective_weight_percent"]} for r in references],
            "rejection": rejection, "native_mapping": "none_reviewed_for_source_identity",
            "native_executable_price": None, "live_order_eligible": False}
    return {"ok": True, "captured_at": now.isoformat(), "requests": 2,
        "maximum_documented_credits": 4, "counts": counts,
        "source_only_market_count": len(grouped), "category_counts": category_counts,
        "findings": findings}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Two bounded read-only Parlay research snapshots")
    parser.add_argument("--database", help="Optional saved pricing database; defaults to port-8002 runtime")
    args = parser.parse_args()
    try:
        result = asyncio.run(main(args.database))
    except Exception as exc:
        result = {"ok": False, "reason": "read_only_validation_failed", "error_type": type(exc).__name__}
    print(json.dumps(result, sort_keys=True))
