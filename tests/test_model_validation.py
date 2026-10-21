"""Deterministic statistical tests using synthetic data, never profitability evidence."""
from copy import deepcopy
from datetime import UTC, datetime, timedelta
import importlib.util
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from evplatform.core.model_validation import ValidationConfig, ValidationDataset, evaluate_reference_models


NOW = datetime(2026, 1, 1, tzinfo=UTC)


def row(index, *, market="m", **updates):
    start = NOW+timedelta(days=index)
    return {"event_id": f"event-{index}", "market_identity": market, "outcomes": ["over", "under"],
            "quoted_outcome": "over", "decision_at": start-timedelta(hours=1), "start_at": start,
            "settled_at": start+timedelta(hours=3), "result": index % 2,
            "result_available_at": start+timedelta(hours=3, minutes=1),
            "settlement_type": "binary_no_push", "identity_verified": True, "settlement_verified": True,
            "probabilities_verified": True, "book_probabilities": {"anchor": .8 if index % 2 else .2,
                                                                   "secondary": .2 if index % 2 else .8},
            "provenance": "synthetic", "source": "explicit-offline-fixture", **updates}


def dataset(rows=None):
    return {"schema_version": 1, "profile_id": "synthetic-fixture", "prior_weights_percent": {"anchor": 50, "secondary": 50},
            "anchor_book": "anchor", "evaluation_at": NOW+timedelta(days=100), "rows": rows or [row(i) for i in range(1, 9)]}


def config(**updates):
    return {"min_train_events": 4, "min_test_events": 2, "folds": 2, "test_events_per_fold": 2,
            "prior_strength": .1, "max_iterations": 300, **updates}


def test_chronological_oos_models_and_simplex_shrinkage_are_deterministic():
    data = dataset()
    one = evaluate_reference_models(data, config())
    two = evaluate_reference_models(data, config())
    assert one == two
    assert one["execution_enabled"] is False and one["configuration_changed"] is False
    assert one["optimized_weights_claim"] is False and one["status"] == "synthetic_research_only"
    assert one["learned_comparison_available"]
    assert one["test_events"] == 4
    assert one["folds"][0]["test_events"] == ["event-5", "event-6"]
    assert one["folds"][1]["test_events"] == ["event-7", "event-8"]
    for fold in one["folds"]:
        learned = fold["weights_percent"]["learned_shrunk"]
        assert sum(learned.values()) == pytest.approx(100)
        assert all(0 <= value <= 100 for value in learned.values())
        assert learned["anchor"] > learned["secondary"]
        assert set(fold["train_events"]).isdisjoint(fold["test_events"])
    assert one["metrics"]["single_anchor"]["brier_score"] == pytest.approx(.04)
    assert one["metrics"]["equal"]["brier_score"] == pytest.approx(.25)
    assert one["metrics"]["learned_shrunk"]["log_loss"] < one["metrics"]["equal"]["log_loss"]


def test_training_requires_settlement_before_earliest_test_decision():
    rows = [row(i) for i in range(1, 9)]
    rows[0]["settled_at"] = NOW+timedelta(days=7)
    rows[0]["result_available_at"] = NOW+timedelta(days=7, minutes=1)
    report = evaluate_reference_models(dataset(rows), config())
    first = report["folds"][0]
    assert "event-1" not in first["train_events"]
    assert first["excluded_train_events_not_settled_before_test"] == 1
    assert first["learning"]["status"] == "insufficient_settled_training_events"
    assert "learned_shrunk" not in first["metrics"]
    assert not report["learned_comparison_available"]
    assert report["common_comparison_events"] == 2
    assert {metric["events"] for metric in report["common_comparison_metrics"].values()} == {2}


def test_late_received_result_cannot_leak_into_earlier_training_cutoff():
    rows = [row(i) for i in range(1, 9)]
    rows[0]["result_available_at"] = NOW+timedelta(days=7, minutes=1)
    report = evaluate_reference_models(dataset(rows), config())
    assert "event-1" not in report["folds"][0]["train_events"]
    assert report["folds"][0]["learning"]["status"] == "insufficient_settled_training_events"


def test_test_outcomes_do_not_affect_learned_first_fold_weights():
    data = dataset()
    changed = deepcopy(data)
    for item in changed["rows"][4:]:
        item["result"] = 1-item["result"]
    left = evaluate_reference_models(data, config())
    right = evaluate_reference_models(changed, config())
    assert left["folds"][0]["weights_percent"] == right["folds"][0]["weights_percent"]
    assert left["folds"][0]["metrics"] != right["folds"][0]["metrics"]


def test_all_alternate_lines_and_repeated_observations_stay_with_event():
    rows = [row(i, market=f"line-{line}", decision_at=NOW+timedelta(days=i, hours=-hour))
            for i in range(1, 9) for line in (24.5, 25.5) for hour in (1, 2)]
    report = evaluate_reference_models(dataset(rows), config())
    assert report["eligible_rows"] == 32 and report["eligible_events"] == 8
    assert report["metrics"]["equal"]["observations"] == 16
    assert report["metrics"]["equal"]["events"] == 4
    assert report["metrics"]["equal"]["brier_score"] == pytest.approx(.25)
    for fold in report["folds"]:
        assert set(fold["train_events"]).isdisjoint(fold["test_events"])


def test_opposing_side_duplicate_is_one_forecast_and_does_not_add_weight():
    data = dataset()
    duplicate = deepcopy(data["rows"][-1])
    duplicate["quoted_outcome"] = "under"
    duplicate["result"] = 1-duplicate["result"]
    duplicate["outcomes"].reverse()
    duplicate["book_probabilities"] = {book: 1-p for book, p in duplicate["book_probabilities"].items()}
    data["rows"].extend([duplicate, deepcopy(data["rows"][-1])])
    report = evaluate_reference_models(data, config())
    assert report["excluded_rows"]["duplicate_snapshot_or_opposing_side"] == 2
    assert report["eligible_rows"] == 8


def test_densely_sampled_market_and_event_do_not_dominate_scores():
    data = dataset()
    # One bad market alongside one good market in event 8. The many repeated
    # snapshots of the good market do not outweigh the bad market.
    data["rows"].append(row(8, market="bad", book_probabilities={"anchor": .9, "secondary": .9}))
    for minutes in range(1, 50):
        data["rows"].append(row(8, decision_at=NOW+timedelta(days=8, hours=-1, minutes=-minutes)))
    report = evaluate_reference_models(data, config())
    # Three event scores .04 plus event 8 mean(.04,.81), divided by four events.
    assert report["metrics"]["single_anchor"]["brier_score"] == pytest.approx((3*.04+(.04+.81)/2)/4)


@pytest.mark.parametrize("updates,reason", [
    ({"identity_verified": False}, "unverified_contract_or_probabilities"),
    ({"settlement_verified": False}, "unverified_contract_or_probabilities"),
    ({"probabilities_verified": False}, "unverified_contract_or_probabilities"),
    ({"result": None}, "unsettled"),
    ({"settled_at": None}, "unsettled"),
    ({"result_available_at": None}, "result_availability_unknown"),
    ({"result_available_at": NOW+timedelta(days=200)}, "outcome_not_available_at_evaluation"),
    ({"result_available_at": NOW}, "invalid_result_availability_clock"),
    ({"settled_at": NOW+timedelta(days=200)}, "outcome_not_known_at_evaluation"),
    ({"decision_at": NOW+timedelta(days=1, hours=1)}, "invalid_pregame_or_settlement_clock"),
    ({"book_probabilities": {"anchor": .5}}, "missing_fixed_panel_book"),
])
def test_exclusions_are_explicit_and_shared_across_every_model(updates, reason):
    report = evaluate_reference_models(dataset([row(1, **updates)]), config())
    assert report["excluded_rows"] == {reason: 1}
    assert report["eligible_rows"] == 0 and report["metrics"] == {}
    assert not report["learned_comparison_available"]


@pytest.mark.parametrize("updates", [
    {"book_probabilities": {"anchor": float("nan")}},
    {"book_probabilities": {"anchor": 0}},
    {"book_probabilities": {"anchor": 1}},
    {"decision_at": NOW.replace(tzinfo=None)}, {"result": True},
    {"outcomes": ["over", "over"]}, {"settlement_type": "push_refund"},
    {"quoted_outcome": "tie"},
])
def test_invalid_probability_or_contract_cannot_enter_evaluation(updates):
    with pytest.raises(ValidationError):
        ValidationDataset.model_validate(dataset([row(1, **updates)]))


@pytest.mark.parametrize("changes", [
    {"result": 1}, {"book_probabilities": {"anchor": .7, "secondary": .8}},
    {"start_at": NOW+timedelta(days=2, minutes=1)}, {"provenance": "recorded"},
])
def test_conflicting_same_market_or_event_evidence_is_not_cherry_picked(changes):
    one = row(2)
    other = {**deepcopy(one), **changes}
    with pytest.raises(ValueError):
        evaluate_reference_models(dataset([one, other]), config())


def test_small_sample_reports_no_fitted_or_optimized_model():
    report = evaluate_reference_models(dataset([row(i) for i in range(1, 5)]), config())
    assert report["test_events"] == 0 and report["metrics"] == {}
    assert not report["learned_comparison_available"] and not report["optimized_weights_claim"]


def test_recorded_provenance_is_separate_and_calibration_bins_are_weighted():
    data = dataset([row(i, provenance="recorded") for i in range(1, 9)])
    report = evaluate_reference_models(data, config())
    assert report["provenance"] == "recorded" and report["status"] == "recorded_out_of_sample_report"
    bins = report["metrics"]["equal"]["calibration"]
    occupied = [bucket for bucket in bins if bucket["weight"]]
    assert len(occupied) == 1
    assert occupied[0]["mean_probability"] == pytest.approx(.5)
    assert occupied[0]["observed_frequency"] == pytest.approx(.5)
    assert sum(bucket["weight"] for bucket in bins) == pytest.approx(1)


def test_stronger_prior_shrinkage_moves_weights_less():
    weak = evaluate_reference_models(dataset(), config(prior_strength=.01))
    strong = evaluate_reference_models(dataset(), config(prior_strength=100))
    assert weak["folds"][0]["weights_percent"]["learned_shrunk"]["anchor"] > strong["folds"][0]["weights_percent"]["learned_shrunk"]["anchor"]
    assert strong["folds"][0]["weights_percent"]["learned_shrunk"]["anchor"] > 50


def test_config_rejects_unbounded_or_nonfinite_optimization():
    for settings in ({"max_iterations": 5001}, {"prior_strength": float("inf")}, {"step_size": float("nan")}):
        with pytest.raises(ValidationError):
            ValidationConfig.model_validate(settings)


def test_offline_cli_writes_only_new_report_and_cannot_replace_input(tmp_path, capsys):
    path = Path(__file__).resolve().parents[1]/"scripts"/"evaluate_reference_models.py"
    spec = importlib.util.spec_from_file_location("offline_models_cli", path)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    source, output, settings = tmp_path/"input.json", tmp_path/"report.json", tmp_path/"config.json"
    source.write_text(ValidationDataset.model_validate(dataset()).model_dump_json(), encoding="utf-8")
    settings.write_text(json.dumps(config()), encoding="utf-8")
    assert cli.main(["--input", str(source), "--output", str(output), "--config", str(settings)]) == 0
    assert json.loads(output.read_text())["execution_enabled"] is False
    before = source.read_bytes()
    assert cli.main(["--input", str(source), "--output", str(source)]) == 2
    assert source.read_bytes() == before
    assert cli.main(["--input", str(source), "--output", str(output)]) == 2
    assert '"execution_enabled": false' in capsys.readouterr().out
