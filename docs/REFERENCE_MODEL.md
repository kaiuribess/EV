# Reference pricing and its limits

The fair-value engine removes each eligible sportsbook's margin, then combines
the resulting probabilities. It does not learn profitable weights from the feed.
The library's legacy equal weights and thresholds are an uncalibrated starting model.
An estimated positive edge is conditional on the model, equivalent settlement,
usable source observations, executable liquidity, and verified fees.

## Imported sport and market priors

The original owner specification is preserved in
[`config/book_weight_priors_2026-10-20.json`](../config/book_weight_priors_2026-10-20.json).
The importer registers all 11 profiles and selects the first four for research:
NBA core props, NFL volume props, MLB strikeouts and MLB hitter volume.
Weights are attached to canonical bookmakers, never provider names. The supplied
percentages are retained exactly; the [research review](BOOK_WEIGHT_RESEARCH.md)
does not establish a better fitted allocation. No real history has yet been used
to optimize them.

`reference_profile_mode="strict"` requires an exact sport, league, market type
and statistic selector, a scheduled game whose start is still in the future,
the profile's minimum number of distinct books and any required anchor.
Unknown categories and in-play markets receive no automatic inherited blend.
College selectors intersect the specification's shared statistic list with the
appropriate football or basketball vocabulary. This registers vocabulary; it
does not establish external player/market/settlement equivalence or coverage.

Each selected book is devigged separately with **power** and **proportional**
normalization. Both blended EV and ROI values must pass at the executable price,
actual size and conservative fee cap, including standing-order revalidation.
Duplicate sportsbook copies are selected by valid complete observations and
documented observation time, never the most profitable quoted price. Equally
recent conflicting prices withhold execution.

The source JSON deliberately supplies **no verified TTL policies** and marks
every profile `live_execution_allowed=false`. The importer preserves both facts.
An incomplete freshness/status policy can produce a labelled research estimate,
but receives no executable qualification, simulated auto-order or native order.
Verified quotes need outcome-level observation time, complete synchronized
sides, current status and an exact provider/book/market TTL with evidence.
Freshness is currently binary (eligible 1, ineligible 0); no unvalidated time-decay
curve has been invented. Historical/unknown observations can be shown in research
estimates with explicit reasons, but their execution weight is zero.

Generate the validated settings file without network requests or trading:

```powershell
.\.venv\Scripts\python.exe scripts\prepare_reference_profiles.py
```

The output is `data/reference_profiles.pricing.json`. It contains all 11 typed
profiles; it does not alter trading permissions or save to a running engine.
The local dashboard's validated Settings API can save these pricing fields.
Changing a source profile or TTL invalidates cached pricing and launch digests.
The optional BookMaker override and exchange-reference experiment remain off.

Both ordinary and native signal audit events now retain the full normalized
reference universe and pricing policy, including rejected evaluations. Their
asynchronous persistence is separate from pricing. Research estimates and
synthetic fixtures are not realized profit. See
[quote clock contracts](QUOTE_OBSERVATION_CONTRACT.md) for provider limitations.

The [offline comparison tool](MODEL_VALIDATION.md) evaluates equal, fixed-anchor,
prior and learned nonnegative blends on chronological game groups. It requires
explicit verified results and when those results became available, uses equal
event weighting, and reports log loss, Brier score and calibration. It never
updates live settings. No real forecast/result dataset has yet qualified for
fitting; deterministic fixtures verify the software only.

The supplied-file compiler is a one-time vocabulary translator. New plugins do
not need to modify it or the trading core: register arbitrary exact selectors
and book weights directly through the typed `reference_profiles` configuration.
New schema-specific semantics still require their own normalization and evidence.

## Reference policy

These are `PricingConfig` fields, exposed through the strategy's validated
metadata-driven settings. Saved older configurations remain compatible.

| Field | Meaning |
| --- | --- |
| `eligible_books` | Exact reviewed canonical bookmaker identities allowed in fair value. `null` accepts all mapped books for backward compatibility; `[]` intentionally produces no signal. |
| `required_books` | Every listed identity must have an eligible, active, fresh, complete quote. Required books must be in the allowlist when one is set. |
| `min_references` | Number of distinct eligible bookmakers, after deduplication. Multiple vendors supplying Pinnacle still count once. |
| `weights` | Positive finite relative weights. Eligible books omitted from this dictionary receive weight 1. Inclusion or a larger weight is not proof of sharpness. |
| `market_overrides` | Overrides keyed by `sport:market_type`, each applied independently against the global policy. |
| `provider_priority` | Tie-break preference between observations of the same book; source time is ranked first, then quality, then provider priority. |

Excluded books do not contribute to minimum count, disagreement, or consensus.
An excluded fresh book cannot replace a stale required book. Names are exact;
aliases belong in reviewed provider mappings, not fuzzy pricing heuristics.
An allowed book with no valid quote contributes nothing.

The library defaults are proportional devig, two independent references, no
required book, a five-second maximum reference age, a 2.5-second exchange-book
age, and a maximum disagreement of eight probability percentage points.
Workspace settings can differ; inspect the running dashboard's pricing settings.
No validated weight calibration is bundled. Set the eligible universe and requirements for a researched market
scope before evaluating its observed results.

## Calculation

For decimal odds `d_i`, implied probability is `q_i = 1 / d_i`. Proportional
devig computes `p_i = q_i / sum(q)`. The optional power method solves
`sum(q_i ** k) = 1`. All mutually exclusive outcomes must be present in the
reviewed market; absent outcomes cannot be inferred from two rows.

For each outcome the consensus is `sum(weight_b * p_b) / sum(weight_b)`.
Disagreement is the largest difference between books for any outcome and is
checked before averaging. Retained reference evidence identifies the vendor,
book, source clock, per-book no-vig probabilities, and raw weight.

Before either paper or native opportunity pricing, cached evidence is checked
again for current source ages, independent count, required and eligible books,
method, and weights. A changed policy requires recalculation. A recent
calculation timestamp cannot refresh an old source quote. Suspended markets,
future-dated receipt clocks, malformed probability distributions, and missing
source clocks do not qualify.

Native sizing uses venue-native price and quantity grids with a verified USD
fee bound, and keeps the reservation within the configured stake including
fees. Settlement differences require explicit compatible models; equal team
names, thresholds, or prices alone never prove that contracts are interchangeable.

## Parlay clock evidence reviewed on 2026-10-20

Parlay's [main REST reference](https://parlay-api.com/docs) describes block
`last_update_ms` as the oldest retained price, while its
[cookbook](https://parlay-api.com/cookbook) describes the same field using the
newer price-change or verification-poll signal. The main reference separately
warns that a bookmaker heartbeat does not verify each market, selection, and
displayed price. Because these descriptions conflict, the adapter does not use
a block clock as a fallback for a missing market clock. Explicit row/market
timestamps remain distinct from delivery time. The
[stream contract](https://parlay-api.com/docs/websocket) says retained row clocks
can remain unchanged across polls and heartbeat freshness does not verify
every retained quote. This is a conservative integration choice, not proof
that a recent timestamp establishes current sportsbook tradability.

## What offline tests establish

The pricing, reference-policy, native-pricing, and Parlay-schema tests verify
the calculations and rejection rules against controlled fixtures. They do not
establish live connectivity, profitable calibration, withdrawal completeness,
or real exchange fill/settlement behavior. Real lifecycle and market-mapping
checks remain separate launch requirements. This documentation does not enable
execution or change saved workspace settings.
