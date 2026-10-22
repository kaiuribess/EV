"""Read-only, credit-bounded Oddize NBA prop/event-context probe.

Makes one 1-credit upcoming-event snapshot and, if an exact future event and
at least two further credits exist, one 2-credit flat-props request. Prints
only counts and status evidence; never emits the API key or individual prices.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections import Counter
from datetime import UTC, datetime

from dotenv import load_dotenv

from evplatform.integrations.normalization import extract_offers
from evplatform.integrations.oddize import OddizeRest, prop_event_plan, prop_event_refs


async def probe(lookahead_hours: int) -> dict:
    load_dotenv(".env", override=False)
    key = os.getenv("ODDIZE_API_KEY")
    if not key:
        return {"ok": False, "reason": "missing_oddize_key"}
    provider = OddizeRest({"enabled": True, "sports": ["nba"], "markets": ["ml"],
                           "bookmakers": ["pin", "fd", "dk", "czr", "circa"],
                           "session_request_budget": 3}, credential=key)
    try:
        account = (await provider.fetch_account()).payload
        remaining = account.get("credits_remaining")
        if type(remaining) is not int or remaining < 3:
            return {"ok": False, "reason": "insufficient_or_unknown_credits",
                    "credits_remaining": remaining}
        snapshot = await provider.fetch_snapshot("nba")
        refs = prop_event_refs(snapshot.payload, "nba", now=snapshot.received_at,
                               lookahead_hours=lookahead_hours)
        selected = prop_event_plan(refs, [], 1)
        if not selected:
            return {"ok": True, "reason": "no_exact_future_event_in_snapshot",
                    "future_event_refs": 0, "credits_spent_max": 1}
        event_id = selected[0]
        frame = await provider.fetch_event_props("nba", event_id,
            prop_types=["player_pts", "player_reb", "player_ast", "player_3pm", "player_pra"],
            event_ref=refs[event_id])
        offers = extract_offers(frame)
        books = Counter(offer.bookmaker for offer in offers)
        return {"ok": True, "observed_at": datetime.now(UTC).isoformat(),
                "sport": "nba", "event_id": event_id,
                "future_event_refs": len(refs), "flat_rows": len(frame.payload["props"]),
                "out_of_scope_rows": frame.metadata["out_of_scope_prop_rows"],
                "normalized_two_sided_offers": sum(len(offer.prices) == 2 for offer in offers),
                "event_context_bound": all(offer.context["participants"] ==
                    [refs[event_id]["team2"], refs[event_id]["team1"]]
                    and offer.context["scheduled_start"] == refs[event_id]["start_date"]
                    for offer in offers),
                "observation_complete": sum(offer.observation_complete for offer in offers),
                "market_status_verified": sum(offer.market_status_verified for offer in offers),
                "offer_counts_by_book": dict(sorted(books.items())),
                "credits_spent_max": 3,
                "live_execution_eligible": False}
    finally:
        await provider.disconnect()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lookahead-hours", type=int, default=48, choices=range(1, 73),
                        metavar="1..72")
    args = parser.parse_args()
    try:
        result = asyncio.run(probe(args.lookahead_hours))
    except Exception as exc:
        # Deliberately omit exception text; HTTPX messages may include URLs.
        result = {"ok": False, "reason": "probe_failed", "error_type": type(exc).__name__}
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
