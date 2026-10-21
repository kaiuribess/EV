# Signal history and trading safeguards

Signal tracking is independent of execution. An enabled strategy publishes
`signal.observation@1` for every market evaluation. A separate critical observer
writes signal history through the asynchronous SQLite writer; pricing does not
query SQLite. Queue overflow, writer failure or a stopped critical worker halts
automatic orders rather than silently discarding the audit.

## What is saved

All generated candidates are recorded, including negative opportunities, trades
not taken while automation is off, and NO SIGNAL transitions. Exact canonical
market rules, reference clocks, fees, thresholds, first/last/best prices and
update counts are retained. Existing pre-upgrade signal history cannot be
recovered as a complete decision record; tracking begins when this release runs.

A continuous strategy/contract/venue/outcome/origin opportunity is one episode.
Qualification or pricing-model changes close an episode. Restart closes prior
open episodes as interrupted. Tick counts are not reported as independent bets;
even different episodes can concern the same event. Reports show distinct event
and contract counts and suppress confidence intervals until independent clustering
is validated. Late status updates cannot rewrite newer decisions.

Performance separates synthetic observations from real observations. A hypothetical
result assumes one contract purchased at the qualifying episode's **first** offer,
with the fee schedule recorded at that time. It does not assume an actual fill,
current liquidity, or profitable execution. Negative episodes remain inspectable
but do not enter qualifying-signal profit or calibration totals.

Results remain pending until supplied. Win/loss, partial payouts and refunds use
the exact outcome set and settlement identity. Missing fees are unavailable,
not zero. Refund fee treatment must be explicit. Repeated identical results are
idempotent; contradictory results are rejected. Binary calibration excludes
refunds and partial outcomes. Paper-order P&L and hypothetical signal P&L are
different measures.

## Result imports and APIs

- `GET /api/signals?synthetic=true&limit=25&offset=0`: paginated episodes.
- `GET /api/signals/summary?synthetic=true`: qualified hypothetical metrics and
  groups by venue, strategy, sport, market and reference model.
- `GET /api/signals/transitions?synthetic=true`: positive/negative/NO SIGNAL
  transitions. Use `synthetic=false` to inspect only real observations.
- `POST /api/signals/result`: exact `market`, `result`, `provenance`, and optional
  `refund_fees_returned`. The API schema is available in local `/docs`.
- `GET /api/readiness`: remaining live-execution and evidence requirements.

Result imports require the local `X-EV-Control: local` header and an allowed
origin. Provenance requires `source`, `reference`, `verified_by`, aware
`verified_at`, `verification: "canonical_settlement_verified"`, and `synthetic`.
This is an explicit **local operator assertion**, not independent verification
by the application. A future reviewed result plugin can publish the same typed
`signal.result@1` contract. Importing a signal result never releases native order
exposure or asserts that an order filled.

Supplying a synthetic paper settlement also grades compatible synthetic signal
episodes. There is currently no connected automatic real-result feed, verified
closing-price feed, or CLV result. Hypothetical performance is not evidence of
real execution or guaranteed profit.

## Position and order protection

The central risk engine enforces one unsettled position per conservative market
family across **all venues and strategies**. Exact opposing sides and duplicate
positions are blocked. Alternate thresholds for the same event/player/statistic
and period share a family. Different settlement terms retain separate exact
fingerprints and are never netted into a fabricated hedge.

Each order stores its complete canonical market snapshot and risk identities.
Unknown orders, cancellation-pending orders, filled positions and cancelled
orders with partial fills retain their position locks. Cancelled unfilled orders
can release their lock; a late fill is still recorded and halts routing if it
reveals a conflict. Legacy positions with missing or inconsistent metadata block
further matching-event orders until reconciled. These checks survive restart.

Cross-type correlation is not inferred from names. Reviewed adapters may provide
`extensions.risk_correlation_v1` with `verified: true`, nonempty `groups` and
verification `evidence`. Groups are scoped to the canonical event/sport/league.
Unmodeled correlations still require review and gross event/player exposure caps;
this is not a universal mathematical portfolio-correlation model.

Default submission caps are 20 unresolved orders and 30 reserved attempts per
rolling minute. These are engineering safeguards, not a recommended stake or
trading frequency. Cancelled and rejected intents still count toward the rolling
cap, including after restart. Stake, fees, exposure, identity, conflict and rate
checks run again after each durable submission barrier. Standing orders are
revalidated at their original limit price and current worst-case fees before
simulated fills. Disabled automation, a kill switch, or the daily-loss gate
cancels remaining paper orders.

There is no hedge/arbitrage bypass of this position lock. A future coordinated
multi-leg strategy requires a separately reviewed policy and risk reservation
contract. Native exchange self-match controls remain venue-specific; native
automated routing is still disabled.
