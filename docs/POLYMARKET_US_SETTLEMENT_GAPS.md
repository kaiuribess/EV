# Polymarket US retail settlement evidence

Reviewed **2026-10-20** against current official documentation and the local connector. This was research only: no private accounts, secrets, orders, or financial actions were accessed. This review does not certify live connectivity.

## Conclusion

The remaining work is partly ordinary reader implementation and partly missing documented retail cash-ledger evidence. Polymarket US **does document resolution events**. Those events can support a diagnostic resolution history, but the reviewed retail contracts do not establish a replayable join from a specific managed trade to the cash actually posted for its settlement, fees, refunds, or corrections. Keep authoritative settlement reconciliation disabled until that join and its recovery behavior are verified. Do not describe the blocker as “Polymarket has no settlement API.”

## What the retail API documents

| Official source | Documented evidence | Limit of that evidence |
| --- | --- | --- |
| [Get Activities](https://docs.polymarket.us/api-reference/portfolio/get-activities) | Authenticated `GET https://api.polymarket.us/v1/portfolio/activities`; cursor pagination with `nextCursor` and `eof`. `positionResolution` includes `marketSlug`, `beforePosition`, `afterPosition`, `side`, `tradeId`, and `updateTime`. Trade activities include an exchange trade ID and state. | Resolution has no explicit paid-cash amount, cash-posting transaction ID/status, settlement fee, or refund amount. Trade activities have no documented order ID. `AccountBalanceChange` has transaction ID/status/amount but no market/trade/order link. |
| [Private WebSocket](https://docs.polymarket.us/api-reference/websocket/private) | Order executions contain `tradeId`. Position updates contain `tradeId` and an entry type. Balance changes contain before/after balances, time, and entry type; ledger types include resolution, commission, correction, and netting. | The documented balance-change payload has no market/trade/transaction ID. Its description is reserved and empty. This page supplies no durable sequence/replay contract for recovering those balance changes. Joining messages by time or equal amounts would be inference. |
| [Get Market Settlement](https://docs.polymarket.us/api-reference/markets/get-market-settlement) | Public `GET https://gateway.polymarket.us/v1/markets/{slug}/settlement` returns `slug` and settlement price. | Public resolution price does not prove cash was posted to this account. |
| [Get Account Balances](https://docs.polymarket.us/api-reference/account/get-account-balances) and [Portfolio Overview](https://docs.polymarket.us/api-reference/portfolio/overview) | Cash, buying power, asset collateral, reservations, and other balance components are documented. `buyingPower` is described as capital available for trading. | No stable cash-account identifier appears in the reviewed balance response. Buying power includes collateral semantics and should not silently replace a cash-only budget. `lastUpdated` is the last balance-change time, not a fresh account-verification timestamp. |

The [collateral and margin rules](https://docs.polymarket.us/market-structure/collateral-and-margin) also matter: buying NO is a short YES position, with sale proceeds and collateral. Releasing collateral is not the same cash flow as a normalized “NO contract pays $1” model. An eventual accounting adapter must distinguish economic payout from actual cash and collateral movements.

## Concrete local implementation work

- `backend/evplatform/venues/polymarket_us.py` already exposes a raw authenticated `activities()` GET. A bounded, validated activity paginator and resolution-history parser are feasible now. Preserve exact decimal fields, pagination completeness, trade states, and correction/bust evidence.
- `backend/evplatform/venues/polymarket_us_execution.py::normalize_execution()` currently stores the execution ID in `NativeFill` but does not preserve the documented `execution.tradeId`. `backend/evplatform/core/native_accounting.py::NativeFill` has no trade-ID field. Persisting a venue-specific execution-to-trade mapping would enable exact joins for future captured executions. Do not recover missing historical bindings by matching price, quantity, or time.
- `backend/evplatform/venues/polymarket_us_account.py` intentionally returns unverified identity and spendable-cash flags. The executor declares `fill_replay=False` and `settlement_reconciliation=False`. A diagnostic resolution reader alone must not turn those flags on or emit authoritative paid `NativeSettlement` records.
- `docs/VENUE_PROTOCOLS.md` should retain the launch limitation, with the narrower explanation above: resolution/trade identifiers exist, but the reviewed retail cash-posting and recovery contracts do not complete the join.

## Institutional API is a separate integration

The [institutional balance ledger](https://docs.polymarket.us/api-reference/funding/get-account-balance-ledger) documents `GET https://api.prod.polymarketexchange.com/v1/funding/balance-ledger`, bearer JWT authentication, and an account such as `firms/{firm}/accounts/{id}`. Entries include a unique ID, account, symbol/security, typed before/after cash balances, and time; resolution and commission entries are supported. This is materially stronger cash-ledger evidence, but it uses a different host, identity model, credentials, and access scope. A personal retail Ed25519 key does not establish institutional eligibility. Do not route personal credentials to that API or claim its ledger is available to this account.

## Evidence needed to remove the remaining block

Obtain an official retail contract or authorized, documented account statement that links a durable cash-posting identifier to the account and market/trade, specifies paid settlement cash and adjustments, and supports complete replay across restart. Then implement and test exact execution/trade/cash joins, partial positions, NO collateral release, busts/corrections, duplicate delivery, missing pages, and restart recovery. Any unlinked or ambiguous record must remain unreconciled.
