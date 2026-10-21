# Offline reference-model validation

This evaluator compares an equal-weight blend, one fixed anchor, the supplied prior weights, and a learned blend. It consumes an explicit forecast export; it does not query accounts, infer results from prices, update configuration, or authorize trading. Synthetic fixtures demonstrate the evaluator only. No historical production dataset has been validated by these tests.

Run from the repository in PowerShell:

```powershell
.\.venv\Scripts\python.exe scripts\evaluate_reference_models.py --input data\verified_forecasts.json --output data\reference_comparison.json --config data\reference_validation_settings.json
```

`--config` is optional. Output must be a new file, and cannot overwrite input or configuration. Fix the evaluation settings before examining test results. Defaults require 50 training events and 20 test events, with up to three chronological blocks of 20 test events. These sample thresholds are engineering safeguards, not proof of statistical power.

Input shape (the row below is explicitly synthetic and insufficient for fitting):

```json
{
  "schema_version": 1,
  "profile_id": "nba-points-example-only",
  "prior_weights_percent": {"fanduel": 50, "pinnacle": 20, "draftkings": 20, "caesars": 10},
  "anchor_book": "fanduel",
  "evaluation_at": "2026-10-20T20:00:00Z",
  "rows": [{
    "event_id": "synthetic-game-1",
    "market_identity": "verified-event-player-points-24.5-full-game-rules-fingerprint",
    "outcomes": ["over", "under"],
    "quoted_outcome": "over",
    "decision_at": "2026-10-19T19:00:00Z",
    "start_at": "2026-10-19T20:00:00Z",
    "settled_at": "2026-10-19T23:00:00Z",
    "result_available_at": "2026-10-19T23:01:00Z",
    "result": 1,
    "settlement_type": "binary_no_push",
    "identity_verified": true,
    "settlement_verified": true,
    "probabilities_verified": true,
    "book_probabilities": {"fanduel": 0.53, "pinnacle": 0.51, "draftkings": 0.52, "caesars": 0.50},
    "provenance": "synthetic",
    "source": "offline-example-only"
  }]
}
```

`book_probabilities` must already be validated, margin-removed probabilities for the stated side of the same exact binary contract. Keep one profile and fixed devig method per dataset. A recorded export must retain upstream audit evidence for identity, freshness, prices and settlement; setting the verification booleans does not independently prove them. Pushes, partial outcomes, unresolved voids and incompatible contracts cannot enter this schema.

All panel books must be present for a row to enter any model. The report counts exclusions; complete-case selection can bias coverage. Exact opposing-side duplicates are canonicalized and counted once. Conflicting results or prices for one identity/time fail validation instead of choosing favorable data.

Games are sorted chronologically and held together across all lines, sides and repeated observations. Training requires every retained observation in a prior event to have a verified result available before the earliest decision in that test block. `result_available_at` records when the result became available to the research system; an earlier `settled_at` cannot backdate a delayed result. Missing availability, inconsistent clocks and outcomes unavailable at the report's `evaluation_at` are excluded. Each event has equal weight; markets divide that event's weight, and repeated observations divide their market's weight.

The learned blend minimizes event-weighted training log loss with a squared-distance penalty toward the supplied prior. Projected gradient descent keeps weights nonnegative and summing to one; iteration count is bounded. Shrinkage is `prior_strength / training_event_count`. Hyperparameters never use test outcomes or closing prices. Convergence describes the numerical fit, not the model's predictive validity.

Reports include log loss, Brier score, weighted calibration bins, input SHA-256, exclusions, train/test event IDs, fit diagnostics and per-fold weights. `common_comparison_metrics` evaluates all four models on identical eligible folds. Ordinary pooled `metrics` includes each model's event count; do not compare differing event sets. Small samples, synthetic data, correlated games and complete-case bias limit inference. The evaluator never claims optimized production weights or demonstrated profit, even when an out-of-sample score improves.
