"""Offline, read-only review of saved Novig research comparisons.

The archive is sampled, so durations are observation bounds rather than the
life of an executable opportunity. Cash is an explicitly labeled scenario;
this module never constructs or authorizes a native order.
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime
from decimal import Decimal, ROUND_FLOOR
from pathlib import Path

from evplatform.core.pricing import devig
from evplatform.core.native_research import novig_taker_fee


D = Decimal
ALLOWANCES = (D(0), D("0.005"), D("0.01"), D("0.02"))


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _key(offer: dict) -> tuple[str, str, str]:
    return offer["source_market_id"], offer["native_market_id"], offer["native_outcome_id"]


def _offers(sample: dict):
    for reference in sample.get("reference_estimates", ()):
        for offer in reference.get("native_research_estimates", ()):
            if offer.get("venue") == "novig":
                yield reference, offer


def _fee_for(offer: dict, quantity: int) -> D:
    evidence = offer.get("fee_evidence") or {}
    fee = {"coefficient": evidence.get("coefficient"), "charged": evidence.get("charged")}
    return novig_taker_fee({"eventId": "captured", "fee": fee},
                           {"eventId": "captured", "status": evidence.get("event_phase")},
                           D(offer["price"]), D(quantity))


def _on_price_grid(price: D) -> bool:
    milli = price * 1000
    if milli != milli.to_integral_value():
        return False
    step = int(milli)
    return 1 <= step <= 50 or 950 <= step <= 999 or 55 <= step <= 945 and step % 5 == 0


def scenario_sizing(offer: dict, probability: D, *, cash_usd: D, cap_usd: D = D(1)) -> dict:
    """Whole-contract research illustration; official minimum remains a live gate."""
    price = D(offer["price"])
    payout = (D(offer["price_denomination_usd"]) if offer.get("price_denomination_usd")
              else D(offer["normal_payout_usd"]) / D(offer["comparison_quantity"]))
    if payout != D("0.01") or not _on_price_grid(price) or not 0 <= probability <= 1:
        raise ValueError("Expected one-cent Novig binary economics")
    one_debit = price * payout + _fee_for(offer, 1)
    win_profit = payout - one_debit
    full = max(D(0), (probability * win_profit - (1-probability)*one_debit) / win_profit) if win_profit > 0 else D(0)
    budget = min(cap_usd, cash_usd * D("0.25") * full)
    depth = int(D(offer["available_depth_contracts"]))
    quantity = min(depth, int((budget / one_debit).to_integral_value(rounding=ROUND_FLOOR)))
    while quantity and price * payout * quantity + _fee_for(offer, quantity) > budget:
        quantity -= 1
    fee = _fee_for(offer, quantity) if quantity else D(0)
    debit = price * payout * quantity + fee
    return {"cash_scenario_usd": str(cash_usd), "full_kelly_fraction": str(full),
            "quarter_kelly_budget_usd": str(budget), "whole_contract_quantity": quantity,
            "maximum_debit_usd": str(debit), "estimated_profit_usd": str(
                probability * payout * quantity - debit),
            "documented_minimum_contracts": 1, "meets_documented_minimum": quantity >= 1,
            "price_on_documented_grid": True, "execution_authorized": False}


def _books(sample: dict, reference: dict, offer: dict, method: str) -> dict:
    market_id, outcome = reference["market_id"], offer["outcome"]
    rows = {}
    for ref in reference.get("references", ()):
        candidates = [quote for quote in sample.get("quotes", ())
            if quote.get("market_id") == market_id and quote.get("bookmaker") == ref.get("bookmaker")
            and quote.get("provider") == ref.get("provider")
            and quote.get("decimal_odds")
            and _time(quote["received_at"]) == _time(ref["local_receipt_at"])]
        if len(candidates) != 1:
            return {"reconstructed": False, "reason": "Reference quote not retained at the exact captured receipt time"}
        quote = candidates[0]
        probabilities = devig({side: D(odds) for side, odds in quote["decimal_odds"].items()}, method)
        rows[ref["bookmaker"]] = {
            "provider": ref["provider"], "opposing_decimal_odds": quote["decimal_odds"],
            "devigged_probability": str(probabilities[outcome]),
            "effective_weight_percent": ref["effective_weight_percent"],
            "status": ref["status"], "verification_reason": ref["verification_reason"],
            "local_receipt_at": ref["local_receipt_at"],
            "source_observed_at": ref.get("source_observed_at"),
            "source_stored_at": ref.get("source_stored_at"),
            "source_scope_poll_at": ref.get("source_scope_poll_at"),
            "source_time": quote.get("source_time"),
        }
    if len(rows) < 2:
        return {"reconstructed": False, "reason": "Fewer than two retained independent books"}
    total = sum(D(row["effective_weight_percent"]) for row in rows.values())
    weighted = sum(D(row["devigged_probability"]) * D(row["effective_weight_percent"])
                   for row in rows.values()) / total
    left_out = {book: str(sum(D(row["devigged_probability"]) * D(row["effective_weight_percent"])
                             for other, row in rows.items() if other != book) /
                          (total-D(rows[book]["effective_weight_percent"]))) for book in rows}
    values = [D(row["devigged_probability"]) for row in rows.values()]
    return {"reconstructed": True, "books": rows, "weighted_probability": str(weighted),
            "matches_captured_fair": abs(weighted-D(offer["fair_probability"])) < D("0.000001"),
            "disagreement_probability": str(max(values)-min(values)),
            "leave_one_book_out": left_out}


def analyze(samples: list[dict], *, cash_scenario_usd: D | None = None,
            method: str = "power") -> dict:
    if method not in {"power", "proportional"}:
        raise ValueError("Unsupported devig method")
    observed = defaultdict(list)
    all_rows = []
    for index, sample in enumerate(samples):
        clock = _time(sample["observed_at"])
        rows = {}
        for reference, offer in _offers(sample):
            key = _key(offer)
            rows[key] = (reference, offer)
            fee_profit = offer.get("fee_adjusted_estimated_profit_usd")
            if fee_profit is not None and D(fee_profit) > 0:
                observed[key].append((index, clock, sample, reference, offer))
        all_rows.append(rows)
    result = []
    for key, captures in sorted(observed.items()):
        best = max(captures, key=lambda row: D(row[4]["fee_adjusted_estimated_profit_usd"]))
        index, _, sample, reference, offer = best
        source = sample.get("markets", {}).get(reference["market_id"]) or {}
        last_index = captures[-1][0]
        after = samples[last_index+1] if last_index+1 < len(samples) else None
        later_rows = all_rows[last_index+1] if after else {}
        if after is None:
            disappearance = "end_of_capture; duration right-censored"
        elif key in later_rows:
            later = later_rows[key][1]
            disappearance = ("edge_nonpositive" if later.get("fee_adjusted_estimated_profit_usd") is not None
                              and D(later["fee_adjusted_estimated_profit_usd"]) <= 0 else
                              later.get("rejection") or "economics_unavailable")
        elif not any(row.get("market_id") == key[0] for row in after.get("reference_estimates", ())):
            disappearance = "sportsbook_reference_expired_or_no_longer_qualified"
        else:
            disappearance = "native_match_or_current_depth_absent"
        p, price = D(offer["fair_probability"]), D(offer["price"])
        payout = D(offer["normal_payout_usd"])
        after_fee = D(offer["fee_adjusted_estimated_profit_usd"])
        scenarios = {}
        for allowance in ALLOWANCES:
            key_pct = format(allowance*100, "f") + "%"
            adjusted = after_fee - allowance*payout
            scenario = {"net_estimated_profit_per_usd_payout": str(adjusted),
                        "positive": adjusted > 0}
            if cash_scenario_usd is not None:
                scenario["illustrative_sizing"] = scenario_sizing(
                    offer, p-allowance, cash_usd=cash_scenario_usd)
            scenarios[key_pct] = scenario
        distinct_states = {(row[4].get("price"), row[4].get("available_depth_contracts")) for row in captures}
        sequences = {row[4].get("native_book_sequence") for row in captures
                     if row[4].get("native_book_sequence") is not None}
        result.append({"event": source.get("event_name"), "player": source.get("player"),
            "sport": source.get("sport"), "league": source.get("league"),
            "statistic": source.get("statistic"), "threshold": source.get("line"),
            "outcome": offer["outcome"], "source_market_id": key[0],
            "native_market_id": key[1], "native_outcome_id": key[2],
            "first_seen_at": captures[0][1].isoformat(),
            "last_seen_at": captures[-1][1].isoformat(),
            "observed_positive_span_seconds": (captures[-1][1]-captures[0][1]).total_seconds(),
            "positive_samples": len(captures), "first_sample_left_censored": captures[0][0] == 0,
            "disappearance": disappearance,
            "sampled_price_depth_states": len(distinct_states),
            "genuine_native_book_sequences_observed": len(sequences),
            "native_update_proof_unavailable_in_archive": not sequences,
            "best_capture_at": sample["observed_at"], "fair_probability": str(p),
            "native_price_probability": str(price), "available_depth_contracts": offer["available_depth_contracts"],
            "price_on_documented_grid": _on_price_grid(price),
            "fee_evidence": offer.get("fee_evidence"),
            "gross_estimated_profit_per_usd_payout": offer.get("gross_estimated_profit_usd"),
            "fee_per_usd_payout": offer.get("taker_fee_usd"),
            "fee_adjusted_profit_per_usd_payout": str(after_fee),
            "book_evidence": _books(sample, reference, offer, method),
            "scenarios": scenarios, "historical_only": True, "execution_authorized": False})
    result.sort(key=lambda row: D(row["fee_adjusted_profit_per_usd_payout"]), reverse=True)
    return {"source": "saved_local_observations", "samples": len(samples),
            "sample_window": [samples[0]["observed_at"], samples[-1]["observed_at"]] if samples else [],
            "cash_scenario_only_usd": str(cash_scenario_usd) if cash_scenario_usd is not None else None,
            "devig_method": method, "distinct_fee_positive_directions": len(result),
            "directions": result,
            "limitations": ["Sampled observations bound, but do not establish, opportunity lifetime or fill probability",
                            "An unchanged sampled price/depth cannot prove that no native updates occurred",
                            "Historic cash scenario is not contemporaneous account evidence",
                            "Illustrative allowances and whole-contract sizing are not an approved risk policy or executable order check",
                            "The documented one-contract minimum and price grid do not establish current market acceptance"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--cash-scenario-usd", type=D)
    parser.add_argument("--devig-method", choices=("power", "proportional"), default="power")
    args = parser.parse_args()
    samples = []
    with args.archive.open(encoding="utf-8") as handle:
        for line in handle:
            item = json.loads(line)
            if "sample" in item:
                samples.append(item["sample"])
    report = analyze(samples, cash_scenario_usd=args.cash_scenario_usd, method=args.devig_method)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"out": str(args.out), "directions": report["distinct_fee_positive_directions"],
                      "reconstructed": sum(item["book_evidence"]["reconstructed"] for item in report["directions"])}))


if __name__ == "__main__":
    main()
