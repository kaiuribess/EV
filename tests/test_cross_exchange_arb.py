"""The two-leg research view never authorizes or sends native orders."""
from decimal import Decimal as D

from evplatform.core.cross_exchange_arb import compare_hedge, scan_arbitrage_research


def offer(venue, outcome, *, price="0.45", fee="0.01", depth="10", **changes):
    row = {"venue": venue, "outcome": outcome, "price": price,
           "native_market_id": f"{venue}-market", "native_outcome_id": outcome,
           "source_market_fingerprint": "exact:event:full:moneyline",
           "native_start": "2026-10-25T20:00:00+00:00", "rules_sha256": "review-lead",
           "identity_limitations": [], "normal_payout_usd": "1",
           "cash_cost_usd": price, "taker_fee_usd": fee,
           "available_at_best_price": depth, "price_denomination_usd": "1",
           "execution_eligible": False}
    return row | changes


def test_opposite_native_outcomes_give_only_conditional_hedge_lead():
    result = compare_hedge(offer("kalshi", "Home"),
                           offer("polymarket_us", "Away", price="0.48"))
    assert result["status"] == "normal_outcome_hedge_lead"
    assert D(result["combined_debit_usd"]) == D("0.95")
    assert D(result["worst_normal_profit_usd"]) == D("0.05")
    assert D(result["best_level_max_hedgeable_payout_usd"]) == D("10")
    assert D(result["combined_buying_power_required_usd"]) == D("0.95")
    assert result["settlement_reviewed"] is False
    assert result["execution_eligible"] is False


def test_fees_depth_identity_and_stale_offers_reject_false_hedges():
    first = offer("kalshi", "Home")
    assert compare_hedge(first, offer("novig", "Away", price="0.54"))["status"] == "no_normal_hedge_edge"
    assert compare_hedge(first, offer("novig", "Away", depth="0.5")) is None
    assert compare_hedge(first, offer("novig", "Away", rejection="Disconnected book")) is None
    assert compare_hedge(first, offer("novig", "Away", price=None)) is None
    assert compare_hedge(first, offer("novig", "Away", source_market_fingerprint="other")) is None
    assert compare_hedge(first, offer("kalshi", "Away")) is None
    assert compare_hedge(first, offer("novig", "Home")) is None
    assert compare_hedge(first, offer("novig", "Away", native_start="2026-10-25T20:05:00+00:00"))["status"] == "identity_unresolved"


def test_novig_cent_contract_and_prophetx_winnings_fee_are_normalized():
    novig = offer("novig", "Home", price="0.43", fee="0", depth="150",
                  price_denomination_usd="0.01")
    prophetx = offer("prophetx", "Away", price="0.48", normal_payout_usd="2.1",
                     cash_cost_usd="1", available_at_best_stake_usd="5",
                     comparison_stake_usd="1", fee_evidence={"rate_on_winnings": "0.02"})
    result = compare_hedge(novig, prophetx)
    assert D(result["best_level_max_hedgeable_payout_usd"]) == D("1.5")
    assert D(result["combined_debit_usd"]) == D("0.43") + D(1)/D("2.1")
    assert D(result["normal_outcome_profit_usd"]["Away"]) < D(result["normal_outcome_profit_usd"]["Home"])
    assert result["execution_eligible"] is False


def test_short_margin_and_buying_power_are_reported_separately():
    short = offer("polymarket_us", "Away", price="0.45",
                  margin_locked_usd="1", buying_power_required_usd="0.46")
    result = compare_hedge(offer("kalshi", "Home"), short)
    poly = next(leg for leg in result["legs"] if leg["venue"] == "polymarket_us")
    assert poly["margin_locked"] == "1"
    assert D(result["combined_buying_power_required_usd"]) == D("0.92")


def test_reviewed_exceptional_case_can_erase_a_normal_hedge():
    first, second = offer("kalshi", "Home"), offer("novig", "Away")
    result = compare_hedge(first, second, settlement_reviewed=True,
        settlement_cases={"cancelled": {"kalshi": "0", "novig": "0"}})
    assert result["status"] == "settlement_risk_exceeds_edge"
    assert D(result["worst_all_reviewed_outcomes_usd"]) < 0
    assert result["execution_eligible"] is False


def test_scanner_uses_existing_exactly_matched_native_offers_without_fair_probability():
    row = {"market_id": "m1", "event_name": "Away at Home", "sport": "basketball",
           "market_type": "moneyline", "strategy": "multi_book_consensus",
           "probabilities": {"Home": "0.6", "Away": "0.4"},
           "native_research_estimates": [offer("kalshi", "Home"), offer("novig", "Away")]}
    results = scan_arbitrage_research([row])
    assert len(results) == 1 and results[0]["market_id"] == "m1"
    assert scan_arbitrage_research([{**row, "historical": True}]) == []
    # A current sharp row can supply a native offer even when the preferred
    # consensus row has no depth; duplicated offers remain one native leg.
    sharp = {**row, "strategy": "sharp_books"}
    consensus = {**row, "native_research_estimates": [offer("kalshi", "Home")]}
    assert len(scan_arbitrage_research([sharp, consensus])) == 1
