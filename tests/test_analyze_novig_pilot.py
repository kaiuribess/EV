from decimal import Decimal as D

from evplatform.core.pricing import devig
from scripts.analyze_novig_pilot import analyze, scenario_sizing


def offer(fair):
    return {"venue": "novig", "source_market_id": "research:fixture",
        "native_market_id": "native:fixture", "native_outcome_id": "under:fixture",
        "outcome": "under", "price": "0.400", "price_denomination_usd": "0.01",
        "available_depth_contracts": "1000", "comparison_quantity": "100",
        "normal_payout_usd": "1.00", "fair_probability": str(fair),
        "gross_estimated_profit_usd": str(fair-D("0.4")),
        "fee_adjusted_estimated_profit_usd": str(fair-D("0.4")),
        "taker_fee_usd": "0", "fee_evidence": {"coefficient": "0.06",
            "charged": "WHEN_LIVE", "event_phase": "OPEN_PREGAME"}}


def test_archive_review_reconstructs_books_and_censors_sampled_duration():
    observed = "2026-10-23T21:00:00+00:00"
    prices = {"fanduel": {"under": "1.8", "over": "2.2"},
              "draftkings": {"under": "1.75", "over": "2.25"}}
    probabilities = {book: devig({side: D(odds) for side, odds in pair.items()}, "power")["under"]
                     for book, pair in prices.items()}
    fair = sum(probabilities.values()) / 2
    references = [{"bookmaker": book, "provider": "parlay_game_ws",
                   "local_receipt_at": observed, "effective_weight_percent": "50",
                   "status": "unknown", "verification_reason": "unverified_observation_clock"}
                  for book in prices]
    sample = {"observed_at": observed,
        "markets": {"research:fixture": {"event_name": "A at B", "player": "Example Player",
                                      "statistic": "receptions", "line": "2.5"}},
        "quotes": [{"market_id": "research:fixture", "provider": "parlay_game_ws",
                    "bookmaker": book, "received_at": observed, "decimal_odds": pair}
                   for book, pair in prices.items()],
        "reference_estimates": [{"market_id": "research:fixture", "references": references,
            "native_research_estimates": [offer(fair)]}]}
    expired = {**sample, "observed_at": "2026-10-23T21:00:30+00:00", "reference_estimates": []}
    report = analyze([sample, expired], cash_scenario_usd=D("25"))
    row = report["directions"][0]
    assert row["book_evidence"]["matches_captured_fair"] is True
    assert row["positive_samples"] == 1 and row["observed_positive_span_seconds"] == 0
    assert row["first_sample_left_censored"] is True
    assert row["disappearance"] == "sportsbook_reference_expired_or_no_longer_qualified"
    assert row["native_update_proof_unavailable_in_archive"] is True
    assert D(row["scenarios"]["2.00%"]["illustrative_sizing"]["maximum_debit_usd"]) <= D(1)
    assert row["historical_only"] and not row["execution_authorized"]


def test_novig_sizing_never_rounds_small_kelly_budget_up_to_a_contract():
    example = offer(D("0.5001")) | {"price": "0.500"}
    result = scenario_sizing(example, D("0.5001"), cash_usd=D("25"))
    assert D(result["quarter_kelly_budget_usd"]) < D("0.005")
    assert result["whole_contract_quantity"] == 0
    assert D(result["maximum_debit_usd"]) == 0
    assert result["documented_minimum_contracts"] == 1
    assert not result["meets_documented_minimum"] and not result["execution_authorized"]


def test_novig_sizing_uses_market_phase_fee_and_one_dollar_cap():
    example = offer(D("0.8")) | {"price": "0.500", "available_depth_contracts": "10000",
        "fee_evidence": {"coefficient": "0.06", "charged": "ALWAYS", "event_phase": "OPEN_PREGAME"}}
    result = scenario_sizing(example, D("0.8"), cash_usd=D("10000"))
    assert 0 < result["whole_contract_quantity"] <= 200
    assert D(result["maximum_debit_usd"]) <= D("1")
    assert D(result["estimated_profit_usd"]) > 0
    assert result["price_on_documented_grid"] and result["meets_documented_minimum"]
