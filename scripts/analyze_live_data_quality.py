"""Summarize read-only /api/state observations at explicit market/book/time grains."""

from __future__ import annotations

import argparse
import gzip
import json
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from itertools import combinations
from pathlib import Path

TARGET_BOOKS = ("pinnacle", "fanduel", "draftkings", "circa", "betonline")
SCENARIOS = (Decimal("0"), Decimal("0.005"), Decimal("0.01"), Decimal("0.02"))


def _datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _family(market: dict) -> str:
    family = market.get("market_type") or "unknown"
    return f"{family}:{market['statistic']}" if family == "player_prop" and market.get("statistic") else family


def _complete_binary_pair(quote: dict, market: dict) -> bool:
    outcomes, prices = market.get("outcomes"), quote.get("decimal_odds")
    if not isinstance(outcomes, list) or len(outcomes) != 2 or len(set(outcomes)) != 2:
        return False
    if not isinstance(prices, dict) or set(prices) != set(outcomes):
        return False
    try:
        return all(Decimal(str(value)).is_finite() and Decimal(str(value)) > 1 for value in prices.values())
    except (InvalidOperation, ValueError, TypeError):
        return False


def analyze(path: Path) -> dict:
    provider_rows = defaultdict(lambda: {"seen_snapshots": 0, "complete_snapshots": 0,
                                       "eligible_snapshots": 0, "markets_seen": set(),
                                       "markets_complete": set(), "markets_eligible": set(),
                                       "new_receipt_rows": set(), "new_eligible_receipt_rows": set()})
    category_rows = defaultdict(lambda: {"market_snapshots": 0, "two_book_snapshots": 0,
                                       "fair_snapshots": 0, "native_depth_snapshots": 0,
                                       "markets_seen": set(), "markets_two_book": set(),
                                       "markets_fair": set(), "markets_native": set()})
    book_snapshots = Counter()
    book_markets = defaultdict(set)
    pair_snapshots = Counter()
    pair_markets = defaultdict(set)
    target_market_snapshots = 0
    all_five_snapshots = 0
    all_five_markets = set()
    reference_sources = Counter()
    reference_sets = Counter()
    native_venues = Counter()
    native_rejections = Counter()
    native_directions = set()
    fee_positive_snapshots = 0
    scenario_snapshots = Counter()
    fee_positive_directions = set()
    scenario_directions = defaultdict(set)
    quote_reasons = Counter()
    new_receipt_evidence = Counter()
    new_receipt_seen = set()
    new_pinnacle_rest_stored_ages = []
    new_pinnacle_ws_observation_to_receipt = []
    pinnacle_reasons = Counter()
    pinnacle_stored_buckets = Counter()
    pinnacle_eligible = 0
    pinnacle_total = 0
    pinnacle_latest_receipt = {}
    pinnacle_latest_observation = {}
    plugin_states = Counter()
    observed_quote_keys = set()
    complete_book_market_keys = set()
    eligible_book_market_keys = set()
    eligible_market_ids = set()
    observed_quote_records = 0
    complete_quote_records = 0
    eligible_quote_records = 0
    eligible_statuses = Counter()
    eligible_observation_evidence = Counter()
    market_snapshots = 0
    eligible_market_snapshots = 0
    fair_market_snapshots = 0
    fair_market_ids = set()
    native_market_snapshots = 0
    native_depth_direction_snapshots = 0
    errors = Counter()
    history = {}
    first = last = None
    sample_count = 0
    order_counts = set()
    controls = set()
    build_starts = set()

    source = gzip.open(path, "rt", encoding="utf-8") if path.suffix == ".gz" else path.open(encoding="utf-8")
    with source as handle:
        for line in handle:
            if not line.strip():
                continue
            wrapper = json.loads(line)
            if "error" in wrapper:
                errors[wrapper["error"]["type"]] += 1
                continue
            sample = wrapper["sample"]
            now = _datetime(sample["observed_at"])
            if now is None:
                errors["invalid_sample_time"] += 1
                continue
            first = first or now
            last = now
            sample_count += 1
            order_counts.add(sample["counts"]["native_orders"])
            controls.add(tuple(sorted(sample["controls"].items())))
            build_starts.add(sample.get("started_at"))
            for plugin in sample["plugins"]:
                health = plugin.get("health") or {}
                plugin_states[(plugin.get("name"), health.get("status"), health.get("streaming"))] += 1
            live_native_streams = {plugin.get("name") for plugin in sample["plugins"]
                                   if plugin.get("name") in {"kalshi", "novig"}
                                   and plugin.get("enabled") is True
                                   and (plugin.get("health") or {}).get("streaming") is True}
            markets = sample["markets"]
            per_market_seen = set()
            per_market_eligible = defaultdict(set)
            per_provider_seen = set()
            per_provider_complete = set()
            per_provider_eligible = set()
            for quote in sample["quotes"]:
                market_id = quote.get("market_id")
                market = markets.get(market_id)
                if not market:
                    continue
                provider, book = quote.get("provider"), quote.get("bookmaker")
                if not provider or not book:
                    continue
                sport, family = market.get("sport") or "unknown", _family(market)
                row_key = (provider, book, sport, family)
                source_key = (provider, book, market_id)
                per_provider_seen.add((row_key, market_id))
                per_market_seen.add(market_id)
                observed_quote_records += 1
                observed_quote_keys.add(source_key)
                complete = _complete_binary_pair(quote, market)
                eligible = complete and quote.get("trusted_research_usable") is True
                if complete:
                    complete_quote_records += 1
                    complete_book_market_keys.add((book, market_id))
                    per_provider_complete.add((row_key, market_id))
                if eligible:
                    eligible_quote_records += 1
                    eligible_book_market_keys.add((book, market_id))
                    eligible_market_ids.add(market_id)
                    eligible_statuses[quote.get("status") or "missing_status"] += 1
                    eligible_observation_evidence["verified" if quote.get("observation_verified")
                                                  else "unknown"] += 1
                    per_provider_eligible.add((row_key, market_id))
                    per_market_eligible[market_id].add(book)
                else:
                    quote_reasons[(provider, quote.get("verification_reason") or "unspecified",
                                   quote.get("status") or "missing_status")] += 1
                if book == "pinnacle" and provider.startswith("parlay"):
                    pinnacle_total += 1
                    pinnacle_eligible += int(eligible)
                    pinnacle_reasons[(provider, quote.get("verification_reason") or "unspecified")] += 1
                    for key, sink in (("received_at", pinnacle_latest_receipt),
                                      ("source_observed_at", pinnacle_latest_observation)):
                        stamp = _datetime(quote.get(key))
                        if stamp is not None and (provider not in sink or stamp > sink[provider]):
                            sink[provider] = stamp
                    age = quote.get("stored_price_age_ms")
                    if age is None:
                        pinnacle_stored_buckets["unknown"] += 1
                    elif age < 0:
                        pinnacle_stored_buckets["future"] += 1
                    elif age <= 180000:
                        pinnacle_stored_buckets["0-180s"] += 1
                    else:
                        pinnacle_stored_buckets[">180s"] += 1
                receipt = _datetime(quote.get("received_at"))
                if receipt is not None and first <= receipt <= now:
                    provider_rows[row_key]["new_receipt_rows"].add((market_id, quote["received_at"]))
                    if eligible:
                        provider_rows[row_key]["new_eligible_receipt_rows"].add((market_id, quote["received_at"]))
                    receipt_key = (provider, book, market_id, quote["received_at"])
                    if receipt_key not in new_receipt_seen:
                        new_receipt_seen.add(receipt_key)
                        new_receipt_evidence[(provider, book,
                                              quote.get("verification_reason") or "unspecified")] += 1
                        stored = _datetime(quote.get("source_stored_at"))
                        if provider == "parlay_rest" and book == "pinnacle" and stored:
                            new_pinnacle_rest_stored_ages.append((receipt-stored).total_seconds())
                        observed = _datetime(quote.get("source_observed_at"))
                        if provider == "parlay_game_ws" and book == "pinnacle" and observed:
                            new_pinnacle_ws_observation_to_receipt.append((receipt-observed).total_seconds())
            for row_key, market_id in per_provider_seen:
                record = provider_rows[row_key]
                record["seen_snapshots"] += 1
                record["markets_seen"].add(market_id)
            for row_key, market_id in per_provider_complete:
                record = provider_rows[row_key]
                record["complete_snapshots"] += 1
                record["markets_complete"].add(market_id)
            for row_key, market_id in per_provider_eligible:
                record = provider_rows[row_key]
                record["eligible_snapshots"] += 1
                record["markets_eligible"].add(market_id)
            fair_by_market = {row["market_id"]: row for row in sample["reference_estimates"]
                              if row.get("market_id") in markets}
            market_snapshots += len(per_market_seen)
            eligible_market_snapshots += len(per_market_eligible)
            fair_market_snapshots += len(fair_by_market)
            fair_market_ids.update(fair_by_market)
            for market_id in per_market_seen | set(fair_by_market):
                market = markets[market_id]
                category = (market.get("sport") or "unknown", _family(market))
                category_row = category_rows[category]
                category_row["market_snapshots"] += 1
                category_row["markets_seen"].add(market_id)
                books = per_market_eligible.get(market_id, set())
                if len(books) >= 2:
                    category_row["two_book_snapshots"] += 1
                    category_row["markets_two_book"].add(market_id)
                target = set(TARGET_BOOKS) & books
                if target:
                    target_market_snapshots += 1
                    for book in target:
                        book_snapshots[book] += 1
                        book_markets[book].add(market_id)
                    for first_book, second_book in combinations(sorted(target), 2):
                        pair_snapshots[(first_book, second_book)] += 1
                        pair_markets[(first_book, second_book)].add(market_id)
                    if len(target) == len(TARGET_BOOKS):
                        all_five_snapshots += 1
                        all_five_markets.add(market_id)
                fair = fair_by_market.get(market_id)
                if fair is None:
                    continue
                category_row["fair_snapshots"] += 1
                category_row["markets_fair"].add(market_id)
                selected_books = tuple(sorted({ref.get("bookmaker") for ref in fair.get("references", [])
                                               if ref.get("bookmaker")}))
                reference_sets[selected_books] += 1
                for reference in fair.get("references", []):
                    reference_sources[(reference.get("provider"), reference.get("bookmaker"))] += 1
                comparisons = fair.get("native_research_estimates", [])
                valid_native = []
                seen_native = set()
                # Public state may include an earlier recalculation plus a
                # just-in-time stream comparison. Prefer the latter and count
                # each native direction only once in this sample.
                for native in reversed(comparisons):
                    venue = native.get("venue")
                    direction = (market_id, venue, native.get("native_market_id"),
                                 native.get("native_outcome_id"), native.get("side"), native.get("outcome"))
                    if direction in seen_native:
                        continue
                    seen_native.add(direction)
                    if native.get("rejection"):
                        native_rejections[(venue, native["rejection"])] += 1
                    try:
                        depth = Decimal(str(native.get("available_depth_contracts")))
                        quantity = Decimal(str(native.get("comparison_quantity")))
                        after_fee = Decimal(str(native.get("fee_adjusted_estimated_profit_usd")))
                        payout = Decimal(str(native.get("normal_payout_usd")))
                    except (InvalidOperation, TypeError, ValueError):
                        continue
                    calculated = _datetime(native.get("calculated_at"))
                    if not (depth > 0 and quantity > 0 and depth >= quantity and payout > 0
                            and native.get("price") is not None and native.get("fee_evidence")
                            and not native.get("historical") and calculated is not None
                            and 0 <= (now-calculated).total_seconds() <= 30
                            and venue in live_native_streams):
                        continue
                    valid_native.append(native)
                    native_depth_direction_snapshots += 1
                    native_venues[venue] += 1
                    native_directions.add(direction)
                    if after_fee > 0:
                        fee_positive_snapshots += 1
                        fee_positive_directions.add(direction)
                    for margin in SCENARIOS:
                        if after_fee - margin * payout > 0:
                            scenario_snapshots[str(margin)] += 1
                            scenario_directions[str(margin)].add(direction)
                if valid_native:
                    native_market_snapshots += 1
                    category_row["native_depth_snapshots"] += 1
                    category_row["markets_native"].add(market_id)
            for historic in sample.get("research_history", []):
                key = (historic.get("market_id"), historic.get("expired_at"))
                if all(key) and key not in history:
                    history[key] = historic

    if not sample_count:
        raise ValueError("No successful samples")
    expired_rows = [row for (_, expired_at), row in history.items()
                    if (stamp := _datetime(expired_at)) and first <= stamp <= last]
    expired_during = Counter(row.get("expiration_reason") or "unspecified" for row in expired_rows)
    expired_native = [item for row in expired_rows for item in row.get("native_research_estimates", [])]
    expired_fee_positive = sum(
        1 for item in expired_native
        if item.get("fee_adjusted_estimated_profit_usd") is not None
        and Decimal(str(item["fee_adjusted_estimated_profit_usd"])) > 0)
    return {
        "window": {"first_sample_utc": first.isoformat(), "last_sample_utc": last.isoformat(),
                   "elapsed_minutes": round((last-first).total_seconds()/60, 2),
                   "successful_samples": sample_count, "errors": dict(errors),
                   "process_started_at": sorted(build_starts),
                   "native_order_counts": sorted(order_counts),
                   "controls_observed": [dict(value) for value in controls]},
        "grains": {"quote_record_instances_across_samples": observed_quote_records,
                   "unique_provider_book_market_keys": len(observed_quote_keys),
                   "complete_binary_quote_record_instances": complete_quote_records,
                   "distinct_complete_book_market_pairs": len(complete_book_market_keys),
                   "eligible_binary_quote_record_instances": eligible_quote_records,
                   "distinct_eligible_book_market_pairs": len(eligible_book_market_keys),
                   "distinct_markets_with_any_eligible_book": len(eligible_market_ids),
                   "eligible_research_statuses": dict(eligible_statuses),
                   "eligible_research_observation_evidence": dict(eligible_observation_evidence),
                   "market_snapshots_with_quote": market_snapshots,
                   "market_snapshots_with_eligible_book": eligible_market_snapshots,
                   "two_book_market_snapshots": sum(row["two_book_snapshots"]
                                                    for row in category_rows.values()),
                   "distinct_two_book_markets": sum(len(row["markets_two_book"])
                                                    for row in category_rows.values()),
                   "fair_market_snapshots": fair_market_snapshots,
                   "distinct_fair_markets": len(fair_market_ids),
                   "native_depth_market_snapshots": native_market_snapshots,
                   "native_depth_direction_snapshots": native_depth_direction_snapshots,
                   "distinct_native_directions": len(native_directions)},
        "providers": [{"provider": key[0], "book": key[1], "sport": key[2], "market_family": key[3],
                       **{name: len(value) if isinstance(value, set) else value for name, value in record.items()}}
                      for key, record in sorted(provider_rows.items())],
        "categories": [{"sport": key[0], "market_family": key[1],
                        **{name: len(value) if isinstance(value, set) else value for name, value in record.items()}}
                       for key, record in sorted(category_rows.items())],
        "simultaneous_target_books": {
            "market_snapshots_with_any_target_book": target_market_snapshots,
            "per_book": dict(book_snapshots),
            "distinct_markets_per_book": {key: len(value) for key, value in book_markets.items()},
            "pairwise": {"+".join(key): value for key, value in sorted(pair_snapshots.items())},
            "distinct_markets_per_pair": {"+".join(key): len(value)
                                          for key, value in sorted(pair_markets.items())},
            "all_five": all_five_snapshots,
            "distinct_markets_all_five": len(all_five_markets)},
        "reference_book_sets": {"+".join(key): value for key, value in reference_sets.most_common()},
        "selected_reference_sources": {"/".join(str(part) for part in key): value
                                       for key, value in reference_sources.most_common()},
        "native": {"venue_direction_snapshots": dict(native_venues),
                   "rejections": {"/".join(str(part) for part in key): value
                                  for key, value in native_rejections.most_common()},
                   "fee_positive_direction_snapshots": fee_positive_snapshots,
                   "distinct_fee_positive_directions": len(fee_positive_directions),
                   "scenario_margin_probability": {key: {"direction_snapshots": scenario_snapshots[key],
                                                       "distinct_directions": len(scenario_directions[key])}
                                                   for key in map(str, SCENARIOS)}},
        "pinnacle_parlay": {"quote_record_instances": pinnacle_total,
                            "eligible_instances": pinnacle_eligible,
                            "latest_local_receipt_by_provider": {
                                key: value.isoformat() for key, value in pinnacle_latest_receipt.items()},
                            "latest_source_observation_by_provider": {
                                key: value.isoformat() for key, value in pinnacle_latest_observation.items()},
                            "evidence_classes": {"/".join(str(part) for part in key): value
                                                 for key, value in pinnacle_reasons.most_common()},
                            "stored_row_age_buckets": dict(pinnacle_stored_buckets)},
        "quote_evidence_and_status": {"/".join(str(part) for part in key): value
                                      for key, value in quote_reasons.most_common()},
        "new_receipt_evidence": {"/".join(str(part) for part in key): value
                                 for key, value in new_receipt_evidence.most_common()},
        "new_pinnacle_rest_stored_age_seconds": {
            "count": len(new_pinnacle_rest_stored_ages),
            "minimum": round(min(new_pinnacle_rest_stored_ages), 1) if new_pinnacle_rest_stored_ages else None,
            "median": round(statistics.median(new_pinnacle_rest_stored_ages), 1)
                      if new_pinnacle_rest_stored_ages else None,
            "maximum": round(max(new_pinnacle_rest_stored_ages), 1) if new_pinnacle_rest_stored_ages else None},
        "new_pinnacle_ws_observation_to_receipt_seconds": {
            "count": len(new_pinnacle_ws_observation_to_receipt),
            "minimum": round(min(new_pinnacle_ws_observation_to_receipt), 1)
                       if new_pinnacle_ws_observation_to_receipt else None,
            "median": round(statistics.median(new_pinnacle_ws_observation_to_receipt), 1)
                      if new_pinnacle_ws_observation_to_receipt else None,
            "maximum": round(max(new_pinnacle_ws_observation_to_receipt), 1)
                       if new_pinnacle_ws_observation_to_receipt else None},
        "plugin_states": {"/".join(str(part) for part in key): value
                          for key, value in plugin_states.most_common()},
        "new_expirations": {"reasons": dict(expired_during),
                            "expired_market_snapshots": len(expired_rows),
                            "historical_native_directions": len(expired_native),
                            "historical_fee_positive_directions": expired_fee_positive},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    report = analyze(args.input)
    rendered = json.dumps(report, indent=2, sort_keys=True)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
