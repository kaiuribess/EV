# Reference pricing and its limits

The fair-value engine removes each eligible sportsbook's margin, then combines
the resulting probabilities. It does not learn profitable weights from the feed.
The default equal weights and thresholds are an uncalibrated starting model.
An estimated positive edge is conditional on the model, equivalent settlement,
usable source observations, executable liquidity, and verified fees.

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
No universal sport-specific sharpness ordering or validated weight calibration
is bundled. Set the eligible universe and requirements for a researched market
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
