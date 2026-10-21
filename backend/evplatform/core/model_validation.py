"""Offline, event-grouped forward validation of already verified binary forecasts.

No provider calls, closing-price targets, live configuration or activation hooks.
The input is an explicit research export, never inferred settlement from odds.
"""
from __future__ import annotations

import math
import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ForecastObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    event_id: str = Field(min_length=1)
    market_identity: str = Field(min_length=1)
    outcomes: tuple[str, str]
    quoted_outcome: str
    decision_at: datetime
    start_at: datetime
    settled_at: datetime | None
    # When the verified outcome was actually available to the research system,
    # not the earlier game-ending/settlement timestamp reported retrospectively.
    result_available_at: datetime | None = None
    result: Literal[0, 1] | None
    settlement_type: Literal["binary_no_push"]
    identity_verified: bool = False
    settlement_verified: bool = False
    probabilities_verified: bool = False
    # These are margin-removed probabilities for quoted_outcome, not odds,
    # provider averages, closing prices or execution venue observations.
    book_probabilities: dict[str, float]
    provenance: Literal["recorded", "synthetic"]
    source: str = Field(min_length=1)

    @field_validator("decision_at", "start_at", "settled_at", "result_available_at")
    @classmethod
    def aware_clock(cls, value):
        if value is not None and value.tzinfo is None:
            raise ValueError("Forecast clocks require timezone offsets")
        return value

    @field_validator("book_probabilities")
    @classmethod
    def valid_probabilities(cls, values):
        if not values or any(not book.strip() or book != book.strip() for book in values):
            raise ValueError("Exact canonical bookmaker identities are required")
        if any(not math.isfinite(value) or not 0 < value < 1 for value in values.values()):
            raise ValueError("Devigged probabilities must be finite and strictly between zero and one")
        return values

    @field_validator("result", mode="before")
    @classmethod
    def binary_result(cls, value):
        if value is not None and type(value) is not int:
            raise ValueError("Result must be an explicit integer zero or one")
        return value

    @model_validator(mode="after")
    def outcome_contract(self):
        if len(set(self.outcomes)) != 2 or any(not value.strip() for value in self.outcomes):
            raise ValueError("Two distinct exact outcome identifiers are required")
        if self.quoted_outcome not in self.outcomes:
            raise ValueError("Quoted outcome must belong to the verified binary contract")
        return self


class ValidationDataset(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    profile_id: str = Field(min_length=1)
    prior_weights_percent: dict[str, float]
    anchor_book: str
    evaluation_at: datetime
    rows: tuple[ForecastObservation, ...]

    @field_validator("evaluation_at")
    @classmethod
    def aware_clock(cls, value):
        return ForecastObservation.aware_clock(value)

    @model_validator(mode="after")
    def fixed_panel(self):
        weights = self.prior_weights_percent
        if (not weights or any(not key.strip() or key != key.strip() for key in weights)
                or any(not math.isfinite(value) or value <= 0 for value in weights.values())
                or not math.isclose(sum(weights.values()), 100, rel_tol=0, abs_tol=1e-8)):
            raise ValueError("Prior weights must be positive, finite and sum to 100 percent")
        if self.anchor_book not in weights:
            raise ValueError("Fixed anchor must belong to the reference panel")
        return self


class ValidationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    min_train_events: int = Field(default=50, ge=2)
    min_test_events: int = Field(default=20, ge=1)
    folds: int = Field(default=3, ge=1, le=20)
    test_events_per_fold: int = Field(default=20, ge=1)
    max_iterations: int = Field(default=500, ge=1, le=5000)
    step_size: float = Field(default=.1, gt=0, le=1)
    prior_strength: float = Field(default=10, gt=0, le=10000)
    calibration_bins: int = Field(default=10, ge=2, le=50)

    @field_validator("step_size", "prior_strength")
    @classmethod
    def finite(cls, value):
        if not math.isfinite(value):
            raise ValueError("Optimization settings must be finite")
        return value


def _prepared(dataset: ValidationDataset):
    """Keep a common panel and canonical orientation, without outcome selection."""
    excluded = Counter()
    groups = defaultdict(list)
    books = tuple(sorted(dataset.prior_weights_percent))
    seen = {}
    contracts = {}
    event_starts = {}
    event_provenance = {}
    for row in dataset.rows:
        reason = None
        if not row.identity_verified or not row.settlement_verified or not row.probabilities_verified:
            reason = "unverified_contract_or_probabilities"
        elif row.result is None or row.settled_at is None:
            reason = "unsettled"
        elif row.result_available_at is None:
            reason = "result_availability_unknown"
        elif row.settled_at > dataset.evaluation_at:
            reason = "outcome_not_known_at_evaluation"
        elif row.result_available_at > dataset.evaluation_at:
            reason = "outcome_not_available_at_evaluation"
        elif not row.decision_at < row.start_at <= row.settled_at:
            reason = "invalid_pregame_or_settlement_clock"
        elif row.result_available_at < row.settled_at:
            reason = "invalid_result_availability_clock"
        elif any(book not in row.book_probabilities for book in books):
            reason = "missing_fixed_panel_book"
        if reason:
            excluded[reason] += 1
            continue
        if row.event_id in event_starts and event_starts[row.event_id] != row.start_at:
            raise ValueError("One verified event cannot have conflicting start clocks")
        event_starts[row.event_id] = row.start_at
        if row.event_id in event_provenance and event_provenance[row.event_id] != row.provenance:
            raise ValueError("Synthetic and recorded observations cannot share an event identity")
        event_provenance[row.event_id] = row.provenance
        canonical = min(row.outcomes)
        positive = row.quoted_outcome == canonical
        result = row.result if positive else 1 - row.result
        probabilities = tuple(row.book_probabilities[book] if positive else 1-row.book_probabilities[book]
                              for book in books)
        contract_key = row.event_id, row.market_identity
        contract = tuple(sorted(row.outcomes)), result, row.settled_at, row.result_available_at
        if contract_key in contracts and contracts[contract_key] != contract:
            raise ValueError("One market identity cannot carry conflicting outcomes or settlement evidence")
        contracts[contract_key] = contract
        snapshot_key = (*contract_key, row.decision_at)
        if snapshot_key in seen:
            if not all(math.isclose(a, b, rel_tol=0, abs_tol=1e-12)
                       for a, b in zip(seen[snapshot_key], probabilities, strict=True)):
                raise ValueError("Conflicting observations at one decision time require upstream resolution")
            excluded["duplicate_snapshot_or_opposing_side"] += 1
            continue
        seen[snapshot_key] = probabilities
        groups[row.event_id].append({"market_identity": row.market_identity, "decision_at": row.decision_at,
            "settled_at": row.settled_at, "start_at": row.start_at, "result": result,
            "result_available_at": row.result_available_at,
            "probabilities": probabilities, "provenance": row.provenance})
    for observations in groups.values():
        observations.sort(key=lambda row: (row["market_identity"], row["decision_at"]))
    return books, dict(groups), dict(excluded)


def _weighted_rows(groups: dict, event_ids: list[str]):
    """One event gets one unit; markets and repeated snapshots divide that unit."""
    if not event_ids:
        return []
    result = []
    for event_id in event_ids:
        counts = Counter(row["market_identity"] for row in groups[event_id])
        for row in groups[event_id]:
            weight = 1 / (len(event_ids) * len(counts) * counts[row["market_identity"]])
            result.append((row, weight))
    return result


def _probability(row, weights):
    return min(1-1e-12, max(1e-12, sum(weight*p for weight, p in zip(weights, row["probabilities"], strict=True))))


def _objective(rows, weights, prior, shrinkage):
    score = 0.
    for row, weight in rows:
        p, y = _probability(row, weights), row["result"]
        score += weight * (-y*math.log(p) - (1-y)*math.log1p(-p))
    return score + shrinkage * sum((weight-base)**2 for weight, base in zip(weights, prior, strict=True))


def _simplex(values):
    ordered = sorted(values, reverse=True)
    running, threshold = 0., 0.
    for i, value in enumerate(ordered, 1):
        running += value
        candidate = (running - 1) / i
        if value > candidate:
            threshold = candidate
    projected = [max(value-threshold, 0.) for value in values]
    total = sum(projected)
    return [value / total for value in projected]


def _fit(rows, prior, event_count, config):
    weights = list(prior)
    shrinkage = config.prior_strength / event_count
    objective = _objective(rows, weights, prior, shrinkage)
    converged = False
    iterations = 0
    for iterations in range(1, config.max_iterations+1):
        gradient = [2*shrinkage*(weight-base) for weight, base in zip(weights, prior, strict=True)]
        for row, influence in rows:
            p, y = _probability(row, weights), row["result"]
            scale = influence * (p-y)/(p*(1-p))
            gradient = [g+scale*x for g, x in zip(gradient, row["probabilities"], strict=True)]
        step = config.step_size
        accepted = False
        for _ in range(25):
            trial = _simplex([weight-step*g for weight, g in zip(weights, gradient, strict=True)])
            loss = _objective(rows, trial, prior, shrinkage)
            if loss <= objective:
                accepted = True
                break
            step *= .5
        if not accepted:
            break
        improvement = objective-loss
        movement = max(abs(a-b) for a, b in zip(weights, trial, strict=True))
        weights, objective = trial, loss
        if improvement <= 1e-12 and movement <= 1e-8:
            converged = True
            break
    return weights, {"iterations": iterations, "converged": converged,
                     "train_penalized_log_loss": objective, "shrinkage_coefficient": shrinkage}


def _scores(predictions, bins):
    if not predictions:
        return None
    mass = sum(weight for _, _, weight in predictions)
    calibration = [{"lower": i/bins, "upper": (i+1)/bins, "observations": 0,
                    "weight": 0., "predicted_sum": 0., "observed_sum": 0.} for i in range(bins)]
    log_loss = brier = 0.
    for p, y, weight in predictions:
        normalized = weight/mass
        log_loss += normalized * (-y*math.log(p) - (1-y)*math.log1p(-p))
        brier += normalized * (p-y)**2
        bucket = calibration[min(bins-1, int(p*bins))]
        bucket["observations"] += 1
        bucket["weight"] += normalized
        bucket["predicted_sum"] += normalized*p
        bucket["observed_sum"] += normalized*y
    for bucket in calibration:
        weight = bucket["weight"]
        bucket["mean_probability"] = bucket.pop("predicted_sum")/weight if weight else None
        bucket["observed_frequency"] = bucket.pop("observed_sum")/weight if weight else None
    return {"log_loss": log_loss, "brier_score": brier, "calibration": calibration,
            "observations": len(predictions)}


def evaluate_reference_models(dataset: ValidationDataset | dict,
                              config: ValidationConfig | dict | None = None) -> dict:
    dataset = ValidationDataset.model_validate(dataset)
    config = ValidationConfig.model_validate(config or {})
    books, groups, excluded = _prepared(dataset)
    ordered = sorted(groups, key=lambda event: (groups[event][0]["start_at"], event))
    prior = [dataset.prior_weights_percent[book]/100 for book in books]
    fixed = {"equal": [1/len(books)]*len(books),
             "single_anchor": [float(book == dataset.anchor_book) for book in books], "proposed_prior": prior}
    # Reserve an initial chronological training prefix. No shuffled lines,
    # opposing sides or later observations from the same event cross folds.
    test_count = min(config.folds*config.test_events_per_fold,
                     max(0, len(ordered)-config.min_train_events))
    first_test = len(ordered)-test_count
    folds = []
    predictions = defaultdict(list)
    common_predictions = defaultdict(list)
    common_events = 0
    model_events = Counter()
    test_event_ids = []
    for start in range(first_test, len(ordered), config.test_events_per_fold):
        test_ids = ordered[start:start+config.test_events_per_fold]
        cutoff = min(row["decision_at"] for event in test_ids for row in groups[event])
        candidate_train = ordered[:start]
        train_ids = [event for event in candidate_train
                     if all(row["result_available_at"] < cutoff and row["decision_at"] < cutoff for row in groups[event])]
        train_rows, test_rows = _weighted_rows(groups, train_ids), _weighted_rows(groups, test_ids)
        weights = dict(fixed)
        learning = {"status": "insufficient_settled_training_events", "fit": None}
        if len(train_ids) >= config.min_train_events:
            learned, fit = _fit(train_rows, prior, len(train_ids), config)
            weights["learned_shrunk"] = learned
            learning = {"status": "fitted_on_prior_settled_events", "fit": fit}
        fold_scores = {}
        for model, blend in weights.items():
            current = [(_probability(row, blend), row["result"], influence) for row, influence in test_rows]
            fold_scores[model] = _scores(current, config.calibration_bins)
            # Each fold's local normalized weights are rescaled to one unit per
            # event before pooled scoring; smaller final folds get less mass.
            predictions[model].extend((p, y, weight*len(test_ids)) for p, y, weight in current)
            if "learned_shrunk" in weights:
                common_predictions[model].extend((p, y, weight*len(test_ids)) for p, y, weight in current)
            model_events[model] += len(test_ids)
        if "learned_shrunk" in weights:
            common_events += len(test_ids)
        folds.append({"index": len(folds)+1, "training_cutoff": cutoff.isoformat(),
            "train_events": train_ids, "test_events": test_ids,
            "excluded_train_events_not_settled_before_test": len(candidate_train)-len(train_ids),
            "learning": learning, "weights_percent": {name: {book: 100*value for book, value in zip(books, blend, strict=True)}
                                                       for name, blend in weights.items()}, "metrics": fold_scores})
        test_event_ids.extend(test_ids)
    synthetic = any(row["provenance"] == "synthetic" for rows in groups.values() for row in rows)
    metrics = {model: {**_scores(values, config.calibration_bins), "events": model_events[model]}
               for model, values in predictions.items()}
    enough_test = len(test_event_ids) >= config.min_test_events
    comparison_complete = bool(folds) and all("learned_shrunk" in fold["metrics"] for fold in folds)
    return {"schema_version": 1, "profile_id": dataset.profile_id, "evaluation_at": dataset.evaluation_at.isoformat(),
        "input_sha256": hashlib.sha256(json.dumps(dataset.model_dump(mode="json"), sort_keys=True,
                                                   separators=(",", ":")).encode()).hexdigest(),
        "execution_enabled": False, "configuration_changed": False,
        "status": "synthetic_research_only" if synthetic else "recorded_out_of_sample_report" if enough_test else "insufficient_test_events",
        "optimized_weights_claim": False,
        "learned_comparison_available": comparison_complete and enough_test,
        "provenance": "contains_synthetic" if synthetic else "recorded" if groups else "no_eligible_data",
        "input_rows": len(dataset.rows), "eligible_rows": sum(map(len, groups.values())),
        "eligible_events": len(groups), "excluded_rows": excluded,
        "training_prefix_events": first_test, "test_events": len(test_event_ids),
        "shared_complete_panel": list(books), "anchor_book": dataset.anchor_book,
        "config": config.model_dump(), "folds": folds, "metrics": metrics,
        "common_comparison_events": common_events,
        "common_comparison_metrics": {model: {**_scores(values, config.calibration_bins), "events": common_events}
                                      for model, values in common_predictions.items()},
        "limitations": ["Caller-supplied identity, clock, probability and settlement evidence is not independently certified by this evaluator.",
            "Missing panel books exclude the row from every model; complete-case selection may bias coverage.",
            "Games, not snapshots or alternate lines, define split and weighting units; cross-game dependence is not modeled.",
            "Learned pooled scores may cover fewer folds; compare only identical fold/event sets.",
            "No closing-price target, hyperparameter search, profit guarantee or activation decision is produced."]}
