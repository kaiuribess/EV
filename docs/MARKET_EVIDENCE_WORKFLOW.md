# Exact market review and current evidence gaps

Reviewed October 20, 2026 (America/Los_Angeles), October 21 UTC. No mapping, execution permission, order, or account transfer was created by this review. A dossier is review evidence, not an approval certificate.

## Actual observed scope

The local quarantine capture at `2026-10-21T06:41:16.802502+00:00` contains **551 entries**: 57 Oddize REST, 28 Parlay REST, and 466 Parlay WebSocket. One entry has no valid source identity. The 550 valid identities include moneylines, totals, ordinary NBA props, and unsupported compound/period market labels. All remain quarantined. Capture: `data/market-review-evidence-20261021.json`; SHA-256 `9a6d0c3797480fca032413e67f191bddeb8ec029121923dd05e8870e9a3929e8`.

Of those entries, 57 have a verified quote observation clock, 493 do not, and **none has verified current market status**. A default `active` label is insufficient. This is a bounded retained window, not a census of each provider's universe or evidence that all current quotes are stale. `data/market-review-evidence-at-capture-20261021.json` evaluates the saved rows at their capture time; rereading a saved snapshot later cannot refresh its quotes.

One concrete candidate is Oddize `pin`, event `202610210ORL`, source market `ml`, labels `ATL`/`ORL`, scheduled `2026-10-21T19:00:00-04:00`, signature `eac9bb1e69e0eba5b700bd434fa53296990f5af489d122c1a8ee66c4dac7a84f`. Its observed price clock is `2026-10-17T19:00:14Z`, with no verified active status, period, or source-specific settlement document. Public Kalshi listings include `KXNBAGAME-26OCT21ATLORL-ATL` and `KXNBAGAME-26OCT21ATLORL-ORL`. These labels suggest a discovery candidate; they do not prove canonical participant IDs, the exact scheduled instant, or rule equivalence.

The saved **production** Novig catalog (`data/novig-production-catalog.json`) contains 500 rows and every sampled row declares `voids: FMV`. Separately, `data/mapping-novig-catalog.json` is explicitly **sandbox**, not production. Neither bounded sample proves complete production coverage.

## Actual contract differences

No exact interchangeable scope was established for the reviewed NBA mainlines. This conclusion is bounded to the reviewed source documents and listings.

| Contingency | Pinnacle NBA game period | Kalshi NBA winner | Novig NBA winner | ProphetX NBA mainline |
|---|---|---|---|---|
| Overtime | Included | Included unless otherwise specified | Included for full game | Included unless contract specifies otherwise |
| Postponement/interruption | General 12-hour start / 30-hour completion rules, subject to explicit exceptions | 48-hour framework | 48-hour ordinary / 45-day postseason framework | Expiration adjusts for postponed/suspended/rescheduled event; equivalent 12/30-hour cutoff not verified |
| Shortened NBA game | Fewer than 43 minutes voids game-period wagers | Official outcome can settle under specified conditions | Official winner can settle under specified conditions | Official-result based; an identical 43-minute rule was not verified in the reviewed filing |
| Unresolvable/void payout | Refunded stake | Fair price in specified cancellation cases | Fair Value Settlement | Reviewed NBA provisions return collateral for specified voids; do not assume every disruption has the same treatment |

Pinnacle's operative general rules and NBA section establish its deadlines, game-duration requirement, and refund definition. [Pinnacle betting rules](https://www.pinnacle.com/en/future/betting-rules/). Kalshi's actual ATL–ORL listing expressly retains the 48-hour/fair-price condition; the series contract details additional contingencies. [Kalshi NBA terms](https://assets.kalshi.com/contract_terms/BASKETBALLGAMEWIN.pdf). Novig describes fair-value settlement as a market-value payout and applies it to otherwise void contracts, including scratches and cancellations. [Novig FVS](https://support.novig.com/en/articles/16163706-what-is-fair-value-settlement-fvs), [Novig contract documents](https://support.novig.com/en/articles/16083642-contracts).

ProphetX's official July 9, 2026 NBA mainline filing covers moneyline/spread/total contracts. It includes overtime, returns collateral for moneyline ties and spread pushes, adjusts expiry for postponed/rescheduled events, and permits specified official-stat corrections within 48 hours. A matching Pinnacle time cutoff and 43-minute completion requirement were not established. The July 9 “NBA Reference Settlement Source Amendment” separately concerns **futures**; it cannot be silently substituted for mainline terms. [Official filing directory](https://www.prophetx.co/lobby/t-c/filings), [NBA mainline filing, Appendix A](https://framerusercontent.com/assets/dPuLRtFql7mQm5oFvuAiCOzSAdk.pdf), [futures amendment](https://framerusercontent.com/assets/5bm2KSelyDxp6fWU8MJ5u7KZJo.pdf).

See [NBA_SETTLEMENT_EVIDENCE.md](NBA_SETTLEMENT_EVIDENCE.md) for document hashes and the earlier jurisdiction-specific FanDuel Indiana comparison. A `fanduel` or `draftkings` feed label alone does not identify the jurisdiction governing its prices. A conditional completed-game probability model could potentially handle contract differences, but would require explicit disruption probabilities and payouts, validation, and a separate strategy/model contract. Copying the same canonical fingerprint is not that model.

## Evidence intake and validation

`ContractReview` now records postponement, interruption, venue change, forfeit, tie, correction, official-result, and payout rules separately. Missing values remain unknown. Comparison reports retain each side's exact value and an action for each mismatch. A shared generic `void_rule` no longer conceals absent contingencies.

`field_evidence` is a list of claims bound to an exact `source` and `source_market_id`. Each item includes a public HTTPS document URL, captured content SHA-256, source jurisdiction, applicability scope, explanation linking the document to the listing, retrieval time, optional effective interval/version, and exact `facts`. Supported facts are typed by `ContractReview`: decimal thresholds, timezone-aware clocks, participant IDs, and settlement fields. Incorrect fields, different listings, future/inapplicable documents, or conflicting facts fail the dossier check. Field claims must be supported by the actual captured document; the software checks consistency, not whether the author told the truth or the URL is official. `external_facts_verified` and `execution_authorized` remain false even for a complete offline dossier.

The existing `evidence_urls` remains accepted for older review files, but URLs alone cannot make `evidence_complete` true. An explicitly documented “not applicable” is different from an absent field. A review must cover the governing source jurisdiction and actual listing overrides, not only the general sport page.

From the repository directory:

```powershell
# Export a machine-readable intake schema; exclusive creation, no network call.
.\.venv\Scripts\python.exe scripts\review_market_readiness.py --write-comparison-schema data\contract-review-schema.json

# Read-only bounded quarantine inventory; never changes mappings or provider quota.
.\.venv\Scripts\python.exe scripts\review_market_readiness.py --url http://127.0.0.1:8001 --max-rows 1000 --output data\market-review-new.json

# Add independently reviewed ContractReview pairs to the inventory.
.\.venv\Scripts\python.exe scripts\review_market_readiness.py --snapshot data\market-review-evidence-20261021.json --comparisons data\contract-comparisons.json --output data\contract-review-new.json

# Require a dossier for each source/native pair in proposed mapping files.
.\.venv\Scripts\python.exe scripts\validate_market_mappings.py --provider data\proposed-provider-mappings.json --native data\proposed-native-mappings.json --comparisons data\contract-comparisons.json --require-evidence
```

The last command binds each pair's `provider_signature`, `canonical_market_id`, and `canonical_identity` to the actual mapping, its bookmaker ID and exact execution instrument. It also compares the real canonical fields with the reviewed fields; copying the correct hash over a different threshold fails. Full review content contributes to the report digest. The validator still performs no publication. Document bytes must be inspected and their hash checked independently; a declared SHA-256 is not a verification signature.

Do not lose these detailed contingencies when creating a canonical mapping. The current runtime settlement schema stores strings for period/scope/void/participation/push rather than this expanded review schema. Any approved mapping must represent **all** applicable rules in its fingerprinted settlement fields or first introduce a validated versioned settlement type. Supplemental review fields are not automatically injected into the runtime.

## Order minima and quarter-Kelly evidence

`minimum_order_cost_usd` is `null` when unknown; zero means explicitly verified absence of a cash floor. `order_minimum_verified` and `order_minimum_source` bind that assertion to the instrument. They supplement its exact native quantity minimum and quantity increment. $5 is the user's unit for reporting, not evidence of a venue minimum. A strategy must skip a result below an applicable minimum, rather than rounding its stake upward.

| Venue/document scope | Verified detail | What remains to bind |
|---|---|---|
| Novig v3 place-order schema | Integer `qty`, minimum 1; each winning native contract pays 1 cent | Price ticks and any independent cash/profit floor for the exact market/account; minimum quantity is not a $1 stake |
| Kalshi fixed-point specification (updated August 20, 2026) | Quantity granularity 0.01 contracts; market `price_ranges` determines legal price grid | Any independent order cash floor and current market-specific constraints; tick size is not stake minimum |
| Polymarket US retail order/fractional documentation | Quantity is contracts; `minimumTradeQty` controls fractional support; most markets use 0.01 steps, some use whole contracts | Actual instrument minimum and step, plus any independent cash floor; “most markets” cannot select an individual market's increment |
| ProphetX Direct Link guide, Step 6 | `quantity` is USD stake, minimum $1; price must be on fetched price ladder | Confirm exact account/API scope and stake increment; do not extrapolate Direct Link wording to every native API account |
| ProphetX NBA filing | $0.01 price tick, $1 notional payout | Neither is proof of an order stake/quantity minimum |

Sources: [Novig v3 order schema](https://docs.novig.com/api-reference/execution/place-an-order), [Kalshi fixed-point representation](https://docs.kalshi.com/getting_started/fixed_point_migration), [Polymarket US order schema](https://docs.polymarket.us/api-reference/orders/create-order), [fractional contract guide](https://docs.polymarket.us/learn/trading/basics/fractional-shares), [ProphetX Direct Link guide](https://docs.prophetx.co/docs/order-aggregator-integration-with-the-prophetx-trading-api). ProphetX's separate Trading API guide documents a below-minimum-stake rejection and a too-small-profit rejection without specifying a universal threshold in that section. [Trading API integration](https://docs.prophetx.co/docs/integration).

No trading request was used to probe a minimum. Quote validity, rule equivalence, executable depth/fees, account eligibility, risk checks, and genuine lifecycle evidence remain independent requirements.
