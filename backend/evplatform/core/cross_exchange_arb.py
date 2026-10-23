"""Read-only, size-bounded cross-venue hedge research.

One $1 normal winning payout is the comparison lot. Native fee and payout
models come from the existing research adapters; no order or mapping is made.
Exceptional settlement payoff tables are unknown in production, so a normal
WIN/LOSS discrepancy is never labeled a guaranteed arbitrage.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from itertools import combinations

D = Decimal


def _amount(value):
    try:
        number = D(str(value))
    except (TypeError, ValueError, InvalidOperation):
        return None
    return number if number.is_finite() and number >= 0 else None


def _leg(offer):
    if offer.get("price") is None or offer.get("rejection") or offer.get("execution_eligible"):
        return None
    payout = _amount(offer.get("normal_payout_usd"))
    cost = _amount(offer.get("cash_cost_usd"))
    if not payout or cost is None or payout < 1:
        return None
    venue = offer.get("venue")
    if venue == "prophetx":
        rate = _amount((offer.get("fee_evidence") or {}).get("rate_on_winnings"))
        stake_depth = _amount(offer.get("available_at_best_stake_usd"))
        stake = _amount(offer.get("comparison_stake_usd"))
        if rate is None or rate > 1 or stake_depth is None or not stake:
            return None
        capacity = stake_depth * payout / stake
        deterministic_fee, winning_fee = D(0), max(D(0), payout-cost) * rate / payout
    else:
        fee = _amount(offer.get("taker_fee_usd"))
        depth = _amount(offer.get("available_at_best_price"))
        denomination = _amount(offer.get("price_denomination_usd"))
        if fee is None or depth is None or not denomination:
            return None
        capacity = depth * denomination
        deterministic_fee, winning_fee = fee / payout, D(0)
    if capacity < 1:
        return None  # Do not scale a rounded one-lot fee into a smaller order.
    buying_power = _amount(offer.get("buying_power_required_usd"))
    margin_locked = _amount(offer.get("margin_locked_usd"))
    return {"venue": venue, "market_id": offer.get("native_market_id"),
        "outcome": offer.get("outcome"), "native_outcome_id": offer.get("native_outcome_id"),
        "source_market_fingerprint": offer.get("source_market_fingerprint"),
        "native_start": offer.get("native_start"),
        "rules_sha256": offer.get("rules_sha256"),
        "identity_limitations": offer.get("identity_limitations") or [],
        "cost": cost / payout, "deterministic_fee": deterministic_fee,
        "winning_fee": winning_fee, "best_level_capacity": capacity,
        "buying_power_required": buying_power / payout if buying_power is not None
            else (cost / payout + deterministic_fee),
        "margin_locked": margin_locked / payout if margin_locked is not None else None}


def compare_hedge(offer_a, offer_b, *, settlement_cases=None,
                  settlement_reviewed=False):
    """Evaluate opposing $1 payouts; exceptional cases need explicit evidence."""
    a, b = _leg(offer_a), _leg(offer_b)
    if (a is None or b is None or a["venue"] == b["venue"]
            or a["outcome"] == b["outcome"] or not a["source_market_fingerprint"]
            or a["source_market_fingerprint"] != b["source_market_fingerprint"]):
        return None
    debit = a["cost"]+b["cost"]+a["deterministic_fee"]+b["deterministic_fee"]
    normal = {a["outcome"]: D(1)-debit-a["winning_fee"],
              b["outcome"]: D(1)-debit-b["winning_fee"]}
    worst_normal = min(normal.values())
    exceptional = None
    identity_aligned = bool(a["native_start"] and a["native_start"] == b["native_start"]
        and not a["identity_limitations"] and not b["identity_limitations"])
    exact = bool(settlement_reviewed and settlement_cases and identity_aligned)
    if exact:
        try:
            exceptional = {name: D(str(payoffs[a["venue"]])) + D(str(payoffs[b["venue"]]))
                - debit for name, payoffs in settlement_cases.items()}
            exact = bool(exceptional) and all(value.is_finite() for value in exceptional.values())
        except (KeyError, TypeError, ValueError, InvalidOperation):
            exact = False
            exceptional = None
    worst_all = min([*normal.values(), *exceptional.values()]) if exact else None
    status = ("identity_unresolved" if not identity_aligned else
              "settlement_reviewed_hedge" if exact and worst_all > 0 else
              "settlement_risk_exceeds_edge" if exact and worst_all <= 0 else
              "normal_outcome_hedge_lead" if worst_normal > 0 else
              "no_normal_hedge_edge")
    return {"strategy": "cross_exchange_arbitrage_research", "status": status,
        "source_market_fingerprint": a["source_market_fingerprint"],
        "legs": [{key: str(value) if isinstance(value, D) else value for key, value in leg.items()}
                 for leg in (a, b)],
        "priced_lot_winning_payout_usd": "1",
        "best_level_max_hedgeable_payout_usd": str(min(a["best_level_capacity"], b["best_level_capacity"])),
        "combined_debit_usd": str(debit),
        "combined_buying_power_required_usd": str(
            a["buying_power_required"]+b["buying_power_required"]),
        "normal_outcome_profit_usd": {key: str(value) for key, value in normal.items()},
        "worst_normal_profit_usd": str(worst_normal),
        "exceptional_profit_usd": {key: str(value) for key, value in exceptional.items()} if exact else None,
        "worst_all_reviewed_outcomes_usd": str(worst_all) if exact else None,
        "worst_single_leg_loss_usd": str(max(
            a["cost"]+a["deterministic_fee"], b["cost"]+b["deterministic_fee"])),
        "settlement_reviewed": exact, "identity_aligned": identity_aligned,
        "execution_eligible": False,
        "limitations": ["Two separate legs can move or fill only partially",
            "Exceptional venue settlements and payout correlations are unreviewed",
            *(["Native event start or outcome identity differs across venues"]
              if not identity_aligned else [])] if not exact else
            ["Two separate legs can move or fill only partially; no executor is implemented"]}


def scan_arbitrage_research(reference_rows):
    """Keep venue-only economics independent of the sportsbook probability."""
    chosen = {}
    for row in reference_rows:
        if row.get("historical") or len(row.get("probabilities", {})) != 2:
            continue
        market_id = row.get("market_id")
        group = chosen.setdefault(market_id, {"row": row, "offers": {}})
        if row.get("strategy") == "multi_book_consensus":
            group["row"] = row
        for offer in row.get("native_research_estimates", ()):
            if offer.get("historical"):
                continue
            identity = (offer.get("venue"), offer.get("native_market_id"),
                        offer.get("native_outcome_id"), offer.get("side"))
            previous = group["offers"].get(identity)
            if previous is None or str(offer.get("calculated_at") or "") > str(
                    previous.get("calculated_at") or ""):
                group["offers"][identity] = offer
    output = []
    for group in chosen.values():
        row = group["row"]
        for first, second in combinations(group["offers"].values(), 2):
            comparison = compare_hedge(first, second)
            if comparison:
                output.append({**comparison, "market_id": row["market_id"],
                    "event_name": row.get("event_name"), "sport": row.get("sport"),
                    "market_type": row.get("market_type"), "player": row.get("player"),
                    "line": row.get("line"), "source_strategy": row.get("strategy")})
    return sorted(output, key=lambda item: D(item["worst_normal_profit_usd"]), reverse=True)[:50]
