# Kalshi NBA recurring approval package — pending

This is one proposed owner approval scope. It is **not** an applied launch profile, source approval, or trading authorization. No new game ID needs owner approval after a genuinely approved family rule passes its per-market checks. Production remains disarmed.

## Account, contract and family rule

- Venue: Kalshi production; authenticated account fingerprint `005d106938071b58423e986cba1f1c78b9b60a32f32cf87cb57628a05671d166`. This fingerprint identifies an account, not a secret or proof of spendable cash.
- Requested funding scope: exchange index **0**. **Conflict:** the public `KXNBAGAME` catalog read on October 27 returned 60 open markets across 30 events, all reporting exchange index **3**. Example `KXNBAGAME-26OCT31PORUTA-UTA` and its event both report index 3. No current index-0 NBA game market was found in that series. A family rule for index 0 must reject these markets. Changing the proposal to index 3 needs owner review and fresh index-3 spendable cash evidence; no funds were moved.
- Proposed normal question: pregame NBA full-game, two-way team winner with overtime. Require exact event/participants/date, both outcomes, native ticker and team identity, current `rules_primary` parse, exact reviewed `rules_secondary`, `KXNBAGAME` series, fee and price-grid metadata, and current book. Reject time, identity, period, rule-text, index or fee ambiguity. The inspected current Utah listing says the game winner resolves YES; its secondary rule retains a 48-hour postponement window and fair-price handling for cancellation or no start. These exceptional cases are **not** sportsbook payout-equivalent.
- Normal-result pricing: `winner_only_binary`, explicitly conditional on a normally completed game. Actual Kalshi settlement follows its contract. Proposed supported execution handling is `kalshi_ioc_binary_halt_on_exception`; an exceptional payout requires a halt and authoritative reconciliation. This is not an unrestricted settlement policy.
- Current series metadata: `fee_type=quadratic_with_maker_fees`, multiplier `1`, terms `https://assets.kalshi.com/contract_terms/BASKETBALLGAMEWIN.pdf`, series update `2026-10-26T16:44:13.457872Z`. The fee calculation must use the current series/event overrides, account precision, executable price and quantity at each order. Current listed price grid is $0.01. No fixed fee amount is approved independent of a current candidate.

## Proposed live multi-book source policy — requires explicit acceptance

- Scope: this Kalshi NBA moneyline family only; Parlay game-line WebSocket as the named provider. No other sport, venue, period, prop, or ordinary scanner strategy gains trading authority.
- Books: FanDuel, DraftKings, Caesars, bet365. Proposed neutral **25% each** before renormalization; require at least **three distinct complete two-sided books**. One sportsbook returned through two APIs still counts once. These equal weights are an uncalibrated policy choice, not evidence of equal predictive accuracy. Power devig; retain disagreement and leave-one-book-out diagnostics.
- Provider-trusted assumptions to approve expressly: an unknown per-selection reobservation time and unknown present active status may be accepted for otherwise qualifying, complete rows. A bookmaker/fixture heartbeat is labeled as such and never relabeled quote observation. This accepts measurable source risk; it does not attest that a price is presently offered.
- Indicator bounds: local delivery no older than 30 seconds; any available stored-row, book/fixture poll, or genuine quote-observation indicator no older than 30 seconds at final validation. An absent quote-observation clock remains **unknown**, not zero age. Explicitly stale, topped-up, degraded, truncated, suspended, withdrawn, unavailable, cross-frame or incomplete rows are rejected. WebSocket heartbeat alone cannot refresh a row.
- Independent cross-check: at least three named sportsbook pairs through the specified provider. This is book diversity, not independent provider infrastructure. Pinnacle is optional only when separately eligible; Circa is not supplied by Parlay.
- Price gate: at least **1.5%** estimated EV on the existing policy denominator **after** actual applicable fees and a **1% of normal payout** uncertainty deduction. Quarter-Kelly uses verified, correctly scoped spendable cash, and a below-minimum size is rejected without upward rounding. No unapproved price is promoted to execution.
- Factual source hold: official sportsbook rules broadly support overtime-inclusive NBA full-game winner pricing, but Parlay does not identify the feed's governing sportsbook jurisdiction or guarantee that its `h2h` row has currently active selections. The reviewed source normal-question evidence must bind the actual row's full-game period and two outcomes; the owner can accept the stated status/observation uncertainty but cannot certify a missing market identity. The current production policy has no approved normal-rule ID or source evidence.

## Proposed recurring monetary and operating authorization

These are **proposed values from the saved operator draft**, not existing authority. `unit_size_usd=5` is a reporting unit only.

| Control | Proposed value |
|---|---:|
| Maximum debit and maximum possible loss per order | $25 each |
| Maximum event, venue, correlated and global open exposure | $25 each |
| Maximum daily new-order debit | $25 |
| Maximum daily realized loss | $25 |
| Maximum open native orders | 1 |
| Maximum order rate | 1 per minute |
| Daily attempt-count cap | None separately; the monetary and rate limits still apply |
| Sizing | Quarter-Kelly, cash only, no forced minimum-size rounding |
| Authorization | Account/venue/family/source-policy/build/pricing/rule/limits digest-bound; at most 24 hours per approval |

The global $25 exposure cap includes the existing external Kalshi exposure of $9.9977 and Novig exposure of $0.97175 from the saved review; their filled costs are already reflected in venue cash and are not deducted twice. Those amounts require a current inventory fence. The unknown Polymarket reservation prevents proving remaining global capacity at present.

## Factual holds before recording this approval and activating

1. **Index and cash:** owner decides whether to revise the requested index-0 scope to the current index-3 NBA series. If revised, obtain a fresh, correctly scoped index-3 balance and inventory read. The last reviewed index-3 cash was $0; index-0 cash cannot fund index-3 orders. No transfer is authorized by this draft.
2. **Source rule:** record a genuine reviewed normal-outcome rule tied to the actual Parlay full-game NBA h2h row and governing sportsbook terms. Explicit owner acceptance of unknown observation age and status is required; source identity, period and two-sided prices must still be established.
3. **Global exposure:** reconcile Polymarket US's `balanceReservation=25` with an attributable, timestamped account/inventory record or an authoritative provider explanation that bounds *all* open exposure without relying on buying power. The saved read reported $49.61 current balance and buying power, zero reported positions, orders and margin, but omitted `lastUpdated` and stable account scope. Its known raw order is resolved, but the reservation is not. Do not treat $25 as a certified upper bound by itself. Even provisionally charging the whole $25 against the proposed global exposure cap would put the saved $10.96945 known exposure plus reservation above $25, so that conservative scenario cannot activate this package either.
4. **Existing basketball position:** the saved Novig Charlotte external fill is still tracked as an unsettled basketball position. Its native event ID has no reviewed canonical event crosswalk, so the current risk engine conservatively blocks new basketball positions across venues. An authoritative settlement or a reviewed exact event binding is needed before an unrelated Kalshi NBA game can clear that conflict gate.
5. **Activation evidence:** load the reviewed profile and approval in the application with actual account, build, pricing and family-rule digests; refresh account, geographic eligibility, current book, fees and risk at activation and before each order. The prior raw API smoke trades do not supply this authorization.

ProphetX is staged as the next separate venue. A read-only October 27 account probe reported $49.10 available, but open-order/position reconciliation, trading permission and an order-specific location result remained unverified. The current controller supports one venue per recurring authorization, so this Kalshi approval does not arm ProphetX.

## Read-only evidence

- Public Kalshi `GET /trade-api/v2/markets?series_ticker=KXNBAGAME&status=open&limit=100&mve_filter=exclude`, `GET /trade-api/v2/events/KXNBAGAME-26OCT31PORUTA`, `GET /trade-api/v2/markets/KXNBAGAME-26OCT31PORUTA-UTA`, and `GET /trade-api/v2/series/KXNBAGAME` on October 27.
- Existing authenticated local production account probes on October 27: Kalshi identity verified, zero open orders, one position, no profile-bound scoped cash; ProphetX identity and reported cash verified, inventory/trading/location incomplete; Polymarket US no verified cash identity or reconciled reservation.
- Official [Kalshi series API](https://docs.kalshi.com/api-reference/market/get-series), [Kalshi fee schedule](https://kalshi.com/fee-schedule), [Parlay streaming semantics](https://parlay-api.com/docs/streaming), [Polymarket US balances](https://docs.polymarket.us/api-reference/account/get-account-balances), and [Polymarket US collateral](https://docs.polymarket.us/market-structure/collateral-and-margin).
