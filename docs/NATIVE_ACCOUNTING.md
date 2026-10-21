# Exact native execution contracts

`core/native_accounting.py` defines versioned native instruments, durable intents,
fills, cumulative cash snapshots and settlement arithmetic. These contracts are
separate from the existing whole-dollar paper simulator. Native quantities are
never rounded into that simulator's integer quantities.

| Venue | Native quantity | Price | Gross winning payout |
| --- | --- | --- | --- |
| Kalshi binary | Decimal contracts, including fractions | USD per $1 contract | Quantity × $1 |
| Novig | Whole cent contracts | Probability | Quantity × $0.01 |
| ProphetX | USD stake | Integer American odds | Stake × native decimal odds |

All money inputs use Decimal, decimal strings or integers. Floats, booleans,
NaN and infinity are rejected. American odds retain their native value and use
rational comparisons. Recurring decimal payouts have explicit conservative
cash bounds; reported cash is retained exactly rather than silently rewritten.

Every instrument binds the complete canonical `Market.identity()`, including
event, player, threshold, outcomes and settlement rules. Its native market and
outcome IDs remain separate. Quantity steps, minimum size, price bands and cash
precision require reviewed venue metadata. Fee caps require their own reviewed,
expiring evidence tied to the exact instrument.

## Pricing and risk

`core/native_pricing.py` performs memory-only sizing and pricing. The
`native_positive_ev` strategy plugin consumes `exchange.native_book@1` and
normalized reference quotes, and emits `trade.native_candidate@1`.

Sizing uses best-price executable depth. The USD reservation includes a fee cap
for every possible minimum-lot partial fill, plus any fixed order charge.
Negative candidates remain visible; they do not qualify for execution. Synthetic
quotes, unknown source times, stale references, duplicate bookmakers, unmatched
rules, missing budgets and unverified fees produce NO SIGNAL.

Push or void rules need reviewed probability bounds in
`Market.extensions.native_settlement_probabilities_v1`. Required fields are
`verified`, `source`, `push_probability_upper_bound`,
`void_probability_upper_bound`, `refund_cost_on_push`, and
`refund_cost_on_void`. Do not enter zero simply to pass validation. This initial
model supports refunded stake on a push/void and conservatively retains all
fees. Other settlement cash conventions require a separate pricing extension.

Standing-order validation evaluates the original limit, current reference
prices, available depth and the remaining fee reservation. A cheaper current
offer does not justify an old, worse limit. The strategy has no order-router or
credential access; central risk and the native order manager remain mandatory.

## Fill reconciliation

Fills need distinct native execution IDs and explicit quantity, cost, payout and
fees. Duplicate IDs are idempotent only when their evidence agrees. Cumulative
snapshots never add their totals to previously counted fills. A fill arriving
after a snapshot needs a cumulative-quantity watermark to establish overlap.
Unknown overlap, contradictory money, a changed native/client order ID, missing
fees or cumulative regressions require reconciliation and retain risk exposure.

A placement acknowledgement is not a fill. A cancellation does not erase an
already filled position. Publication timestamps are stored separately from
source-confirmed match times. Unavailable match times remain unavailable.

## Explicit limited settlement policy

Launch profiles default to `"settlement_policy": "full"`. Complete settlement
coverage is still unsupported, so this default cannot authorize native trading.
An operator may explicitly choose and authorize
`"settlement_policy": "novig_ioc_win_loss_halt_on_exception"` for the limited
Novig implementation. Changing this field changes the profile digest and
invalidates earlier launch evidence and risk authorization. This option is not
enabled automatically by credentials, passing tests, or a successful connection.

The limited policy permits only the actual Novig IOC adapter, matching account
environment, reviewed whole-cent contracts paying USD 0.01, exact full market
identity, and current verified fee bounds. At most one managed order may exist
per native market in that account and environment across all retained history,
including cancelled, rejected and already settled orders. Opposing outcomes in
the same native market share this limit. Do not delete history to evade it.

Only ordinary WIN/LOSS settlement can reconcile automatically, after actual
personal position history and all fills prove the quantity, cash and fees.
Pushes, voids, fair-market-value settlements, remediation, maker credits,
netting, shared positions and every uncertain or inconsistent result halt
automation and retain or restore exposure. They require audited cash repair
before trading resumes. This policy accepts that interruption risk; it does
not claim generic settlement coverage or assume an event cannot be voided.

Capability admission does not prove account access or a real payment. Current
account checks, genuine settlement/lifecycle evidence and explicit risk
authorization remain mandatory independent launch gates.

## Official protocol references

Reviewed October 21, 2026:

- [Novig monetary representations](https://docs.novig.com/api/concepts/money):
  cent payouts, whole contracts, banded price grid and exact money precision.
- [Kalshi fixed-point representation](https://docs.kalshi.com/getting_started/fixed_point_migration):
  fractional quantities and market-specific price ranges.
- [Kalshi fee rounding](https://docs.kalshi.com/getting_started/fee_rounding):
  trade fees, account precision, rounding adjustments and accumulated rebates.
- [ProphetX order lifecycle](https://docs.prophetx.co/docs/direct-link-order-lifecycle):
  asynchronous matching, partial cancellations, private cumulative quantities,
  per-fill price evidence and eventual consistency of reporting reads.

The focused tests and `test_native_engine.py` use offline fixtures and injected
transports. They verify arithmetic, risk integration, audit persistence and
restart behavior; they are not proof of genuine native connectivity or fills.
Production remains subject to current authenticated account permissions,
reviewed mappings, native lifecycle evidence, settlement integration and an
explicit successful launch preflight.
