import gzip
import json
import tempfile
from pathlib import Path

import pytest

from scripts.analyze_live_data_quality import analyze
from scripts.observe_live_data_quality import compact_state


def _market():
    return {"id": "m1", "sport": "basketball", "league": "NBA",
            "market_type": "moneyline", "statistic": None, "line": None,
            "player": None, "start_time": "2026-10-24T00:00:00Z",
            "status": "scheduled", "outcomes": ["A", "B"], "event_name": "A at B"}


def _quote(provider, bookmaker, usable=True):
    return {"market_id": "m1", "provider": provider, "bookmaker": bookmaker,
            "decimal_odds": {"A": "1.80", "B": "2.10"}, "status": "unknown",
            "trusted_research_usable": usable, "verification_reason": "unverified_observation_clock",
            "received_at": "2026-10-23T00:00:00Z", "synthetic": False}


def _state():
    return {"mode": "production", "auto_trade": False, "kill_switch": True,
            "live_enabled": True, "timestamp": "2026-10-23T00:00:00Z",
            "started_at": "2026-10-22T23:00:00Z",
            "plugins": [{"manifest": {"name": "kalshi"}, "enabled": True,
                         "health": {"status": "streaming", "streaming": True}}],
            "markets": [_market()],
            "quotes": [_quote("parlay_ws", "fanduel"), _quote("oddize_rest", "fanduel"),
                       _quote("parlay_ws", "pinnacle")],
            "reference_estimates": [{"market_id": "m1", "synthetic": False,
                "calculated_at": "2026-10-23T00:00:00Z", "quality_mode": "trusted_provider",
                "references": [{"provider": "parlay_ws", "bookmaker": "fanduel"},
                               {"provider": "parlay_ws", "bookmaker": "pinnacle"}],
                "native_research_estimates": [{"venue": "kalshi", "source_market_id": "m1",
                    "native_market_id": "n1", "native_outcome_id": "yes", "side": "yes",
                    "outcome": "A", "price": "0.50", "available_depth_contracts": "4",
                    "comparison_quantity": "1", "normal_payout_usd": "1",
                    "fee_adjusted_estimated_profit_usd": "0.012",
                    "calculated_at": "2026-10-23T00:00:00Z",
                    "fee_evidence": {"source": "fixture"}, "historical": False}]}],
            "research_history": [], "native_orders": []}


def test_disarmed_observer_keeps_quote_and_expiration_evidence():
    state = _state()
    state["research_history"] = [{"historical": True, "market_id": "old",
                                  "expired_at": "2026-10-23T00:01:00Z",
                                  "expiration_reason": "delivery expired",
                                  "references": [{"bookmaker": "pinnacle",
                                                  "local_receipt_at": "2026-10-22T23:50:00Z"}],
                                  "native_research_estimates": []}]
    sample = compact_state(state, "2026-10-23T00:00:00Z")
    assert sample["quotes"][0]["received_at"] == "2026-10-23T00:00:00Z"
    assert sample["research_history"][0]["expiration_reason"] == "delivery expired"
    state["kill_switch"] = False
    with pytest.raises(RuntimeError, match="safety state changed"):
        compact_state(state, "2026-10-23T00:00:00Z")


def test_analysis_counts_book_pairs_and_directions_at_distinct_grains():
    with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as folder:
        _check_analysis(Path(folder) / "samples.jsonl")


def _check_analysis(path):
    first = compact_state(_state(), "2026-10-23T00:00:00Z")
    second = compact_state(_state(), "2026-10-23T00:00:30Z")
    with path.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps({"sample": first}) + "\n")
        handle.write(json.dumps({"sample": second}) + "\n")
    report = analyze(path)
    assert report["grains"]["quote_record_instances_across_samples"] == 6
    assert report["grains"]["unique_provider_book_market_keys"] == 3
    assert report["categories"][0]["two_book_snapshots"] == 2
    assert report["grains"]["fair_market_snapshots"] == 2
    assert report["grains"]["distinct_fair_markets"] == 1
    assert report["grains"]["native_depth_direction_snapshots"] == 2
    assert report["grains"]["distinct_native_directions"] == 1
    assert report["native"]["fee_positive_direction_snapshots"] == 2
    assert report["native"]["scenario_margin_probability"]["0.01"]["distinct_directions"] == 1
    assert report["native"]["scenario_margin_probability"]["0.02"]["distinct_directions"] == 0
    assert report["simultaneous_target_books"]["per_book"] == {"fanduel": 2, "pinnacle": 2}
    assert report["new_receipt_evidence"]["parlay_ws/fanduel/unverified_observation_clock"] == 1
    alternative = report["research_only_alternative"]
    assert alternative["distinct_two_book_markets"] == 0  # FanDuel plus Pinnacle is not this fallback.
    assert alternative["policy"].startswith("research_only")
    archive = path.with_suffix(".jsonl.gz")
    with path.open("rb") as original, gzip.open(archive, "wb") as compressed:
        compressed.write(original.read())
    assert analyze(archive)["grains"] == report["grains"]


def test_alternative_study_deduplicates_books_and_preserves_movements_without_results():
    with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as folder:
        path = Path(folder) / "samples.jsonl"
        first = _state()
        first["quotes"] = [_quote("parlay_ws", "fanduel"),
                           _quote("oddize_rest", "fanduel"),
                           _quote("parlay_ws", "draftkings")]
        second = json.loads(json.dumps(first))
        second["quotes"][0]["decimal_odds"]["A"] = "1.90"
        second["quotes"][1]["decimal_odds"]["A"] = "1.90"
        rows = [compact_state(first, "2026-10-23T00:00:00Z"),
                compact_state(second, "2026-10-23T00:00:30Z")]
        path.write_text("".join(json.dumps({"sample": sample}) + "\n" for sample in rows),
                        encoding="utf-8")
        alternative = analyze(path)["research_only_alternative"]
        assert alternative["distinct_two_book_markets"] == 1
        assert alternative["two_book_market_snapshots"] == 2
        assert alternative["distinct_all_three_markets"] == 0
        fd = next(row for row in alternative["historical_quote_movements"]
                  if row["book"] == "fanduel")
        assert fd["price_changes"] == 1
        assert fd["first_at"] == "2026-10-23T00:00:00+00:00"
        assert fd["last_at"] == "2026-10-23T00:00:30+00:00"
        assert fd["outcome_result"] is None


def test_reconnecting_venue_or_old_comparison_is_not_current_depth():
    with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as folder:
        path = Path(folder) / "samples.jsonl"
        state = _state()
        state["plugins"][0]["health"]["streaming"] = False
        offline = json.loads(json.dumps(compact_state(state, "2026-10-23T00:00:00Z")))
        state["plugins"][0]["health"]["streaming"] = True
        old = compact_state(state, "2026-10-23T00:01:00Z")
        path.write_text(json.dumps({"sample": offline}) + "\n"
                        + json.dumps({"sample": old}) + "\n", encoding="utf-8")
        report = analyze(path)
        assert report["grains"]["fair_market_snapshots"] == 2
        assert report["grains"]["native_depth_direction_snapshots"] == 0


def test_native_estimate_can_be_computed_during_state_response_and_prophetx_uses_usd_stake():
    with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as folder:
        path = Path(folder) / "samples.jsonl"
        state = _state()
        state["timestamp"] = "2026-10-23T00:00:01Z"
        state["plugins"].append({"manifest": {"name": "prophetx"}, "enabled": True,
                                 "health": {"status": "streaming", "streaming": True}})
        native = state["reference_estimates"][0]["native_research_estimates"][0]
        native.update({"venue": "prophetx", "native_market_id": "px-market",
                       "available_depth_contracts": None, "comparison_quantity": None,
                       "available_stake_usd": "3", "cash_cost_usd": "1",
                       "calculated_at": "2026-10-23T00:00:00.500Z"})
        path.write_text(json.dumps({"sample": compact_state(state, "2026-10-23T00:00:00Z")}) + "\n",
                        encoding="utf-8")
        report = analyze(path)
        assert report["grains"]["native_depth_direction_snapshots"] == 1
        assert report["native"]["venue_direction_snapshots"] == {"prophetx": 1}
