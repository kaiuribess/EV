# Kalshi private account and settlement support

The connector subscribes to all-account `fill`, `user_orders`, and
`market_positions` channels. Market filtering is used only for public books.
All three private acknowledgements must match subscriptions issued on the
current authenticated connection. Disconnects and every private notification
invalidate the account-read fence before the event reaches the trading engine.

Kalshi does not document contiguous sequence numbers for these private
channels. The local connection generation and revision are explicitly local
invalidation markers. They are never reported as exchange sequence numbers or
lossless delivery proof. A fresh REST inventory must agree with durable orders,
including exact quantities, costs, fees and buy-only position cash. Unexpected
netting, duplicate native identities, external orders and realized activity
block execution. A zero net position does not hide external round-trip trades.

An authenticated `/exchange/user_data_timestamp` response is an additional
freshness barrier. Its documented meaning is approximate validation time; it
is not an atomic snapshot guarantee. The probe requires the data time to pass
the subscription/private-message barrier before reading the inventory, and
the connection/revision must remain unchanged throughout. Ordinary REST lag
leaves the private fence unverified while preserving any observed financial
inventory agreement. Trading remains blocked by private synchronization until
a later probe succeeds. Runtime snapshot freshness and periodic reconciliation
remain required.

`balance_dollars` is the documented available cash field. `portfolio_value`
must never be substituted. `cash_available_verified` is true only after the
account inventory agrees; this flag alone does not authorize an order. Runtime
must require a current synchronized account snapshot and subtract durable
pending reserves. Balance may aggregate exchange shards, so an account cash
cap does not prove capacity on an individual shard. Key-bound account identity
is not stable across key rotation: an OS execution lease must cover the entire
venue/environment to prevent separate keys from evading process exclusion.

## Explicit limited settlement policy

`kalshi_ioc_binary_halt_on_exception` admits ordinary YES/NO cash settlement for
one historical managed order per native ticker and primary direct account.
Full rule equivalence, current fees, central risk approval and actual lifecycle
evidence remain separate launch requirements. Selecting the policy accepts an
automation halt with retained exposure for every unsupported settlement or
contradiction; it does not approve an order or certify an account.

The adapter checks the authenticated key/account and a unique settlement row,
actual settlement revenue in integer cents, fractional contract counts,
side-specific cost basis, total fees and source settlement timestamp. It then
reads the complete market fill history from both current and historical
endpoints and deduplicates overlap by fill ID and identical economics. Every
fill must match the durable order, account, shard, side, quantity, cost, price
limit and actual fee. No public score or market result alone releases money.

Unsupported cases include scalar/FMV/refund outcomes, netted/shared positions,
maker fills, multiple managed orders in the same native market, subaccounts,
FCM accounts and ambiguous cash corrections. When a settlement record migrates
out of the live dataset, historical positions lack the same explicit settlement
cash/time contract. The adapter returns no new credit; a previously accepted
credit must be invalidated and exposure restored until audited repair. Neither
missing history nor an acknowledgement means a loss, fill, cancellation or
payment. Previously accepted records must be rechecked for changes.

Kalshi cancellation/FMV rules are not automatically equivalent to a sportsbook
refund. Mapping review must compare actual contract terms; this adapter does
not manufacture probability or rule compatibility.

## Dynamic fees

`kalshi_taker_fee_bound` consumes current official series metadata and scheduled
fee changes after market→event→series identity has been independently verified.
It supports the quadratic taker structures and their exact decimal multiplier.
Flat and unknown structures are rejected. The bound expires after at most five
minutes (default one minute), or earlier at the next scheduled change.

The conservative reserve uses the worst quadratic price and every possible
0.01-contract partial fill, including six-decimal fee rounding and balance-grid
rounding, without assuming future rebates. Unknown membership uses the larger
documented cent grid; an independently verified direct member can use the
centicent grid. These are upper bounds, not expected actual fees. FCM extras and
maker execution are not covered. The helper performs no account or order calls.

## Validation and sources

The automated tests use offline official-shape fixtures. They establish local
logic only, not live fills, live cancellation, payment certification, or future
profitability. This implementation does not enable trading or place orders.

Official documentation checked October 21, 2026:

- [Private orders](https://docs.kalshi.com/websockets/user-orders),
  [private fills](https://docs.kalshi.com/websockets/user-fills),
  [private positions](https://docs.kalshi.com/websockets/market-positions).
- [User data timestamp](https://docs.kalshi.com/api-reference/exchange/get-user-data-timestamp),
  [available balance](https://docs.kalshi.com/api-reference/portfolio/get-balance),
  [positions](https://docs.kalshi.com/api-reference/portfolio/get-positions).
- [Settlements](https://docs.kalshi.com/api-reference/portfolio/get-settlements),
  [fills](https://docs.kalshi.com/api-reference/portfolio/get-fills),
  [historical fills](https://docs.kalshi.com/api-reference/historical/get-historical-fills),
  [historical positions](https://docs.kalshi.com/api-reference/historical/get-historical-positions).
- [Series metadata](https://docs.kalshi.com/api-reference/market/get-series),
  [scheduled fee changes](https://docs.kalshi.com/api-reference/exchange/get-series-fee-changes),
  [fee rounding](https://docs.kalshi.com/getting_started/fee_rounding),
  [fee schedule](https://kalshi.com/docs/kalshi-fee-schedule.pdf), effective July 7, 2026.
