# Operator limits and overlap protection

The current requested launch policy is **quarter Kelly**, with **$5 = one
reporting unit**, each account's verified available cash as its spending cap,
no fixed dollar stake cap, no daily-loss stop, and one active/unsettled bet per
game or player across venues. The earlier $5-per-bet cap is superseded. Unit
size is neither a minimum stake nor a required increment. The engine rounds
quantity down and skips an order that cannot meet its verified venue minimum
within the Kelly budget. See [the sizing calculation](STAKE_SIZING.md).

`data/operator_launch_limits.json` is the local draft of these limits. It is not
a launch profile or permission to submit orders. After the exact account,
market mappings, fees and genuine lifecycle evidence are reviewed, copy its
validated `LiveLimits` fields into that account's launch profile. Scope and
limit changes invalidate old launch evidence.

```json
{
  "max_order_stake_usd": null,
  "max_event_exposure_usd": null,
  "max_player_exposure_usd": null,
  "max_venue_exposure_usd": null,
  "max_correlated_exposure_usd": null,
  "max_total_exposure_usd": null,
  "daily_loss_limit_usd": null,
  "available_cash_only": true,
  "block_overlapping_events": true,
  "block_overlapping_players": true,
  "stake_sizing": "quarter_kelly",
  "unit_size_usd": "5",
  "max_open_orders": 20,
  "max_orders_per_minute": 30
}
```

`daily_loss_limit_usd: null` disables only the daily realized-loss stop. It does
not disable the kill switch, cash checks, price/fee limits, duplicate prevention,
freshness requirements or order reconciliation. Positive estimated EV is not a
guarantee of profit; losses can continue without a daily stop.

Cash must be documented as spendable for the exact wallet and reconciled with
its orders and positions. A generic balance or total portfolio value is not
sufficient. Pending and unknown intents reserve their full cost; confirmed cash
already debited for fills is not subtracted twice. Money in a different account
is never used to justify an order. Unknown cash semantics block this policy.
Paper mode retains explicit simulated bankroll caps because it has no real
authenticated account cash.

The overlap rule includes pending orders, partial fills, different market types
on the same game, alternate lines, and the same player in a second game. A
cancelled remainder does not release the filled part. Verified settlement is
required to release that reservation. This is intentionally broader than merely
blocking the two opposite sides of one market.

`data/launch_portfolio.json` can select one reviewed account per venue in one
controller. Every member has its own identity, market scope, fees and genuine
launch evidence. All members must share the same explicit limits, environment,
build, mapping/pricing configuration and strategy set. Cash is checked separately;
event/player reservations and the order-count/rate caps are shared. Every selected
account must pass readiness before any portfolio order is dispatched. A legacy
`data/launch_profile.json` still selects one account; supplying both files is blocked.

One operating system user may run only one native execution controller per environment,
across all venues. An OS lock enforces this and binds that environment to its
durable database path. Starting a second process or switching to an empty
database cannot bypass the shared risk ledger. An abrupt process exit releases
the OS lock; restarting with the same database restores reservations.

The lock is local to this Windows user and computer. It cannot coordinate
manual bets, separate computers or another Windows account. External inventory
detected by account reconciliation blocks trading for review. Do not delete
the lock metadata or replace the ledger to clear a blocked account; resolve
the inventory with its original records. Separate uncoordinated venue processes
are not a supported substitute for the shared portfolio controller.
