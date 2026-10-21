# Polymarket US retail settlement evidence

Initially reviewed **2026-10-20** against official documentation. On **2026-10-21**, the diagnostic activity reader was implemented, tested offline and used for an authenticated read-only capture of the configured personal US account. That capture proves access to the served history, not an order-to-settlement lifecycle.

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

- `backend/evplatform/venues/polymarket_us_activities.py` now provides bounded pagination and typed diagnostic trade, resolution and balance records. It requires explicit `eof`, rejects malformed or looping cursors, preserves bust/state revisions, distinguishes exact duplicate deliveries from conflicting revisions, and never substitutes rounded legacy quantities for decimal fields. Unknown/malformed rows remain in protected raw evidence and make parsed coverage incomplete.
- `backend/evplatform/venues/polymarket_us_execution.py::normalize_execution()` now preserves the documented `execution.tradeId` separately from the execution ID as `NativeFill.trade_id`. A supplied malformed identifier is rejected. A legacy fill can gain the ID only from the exact same execution identity with every financial/source-clock field unchanged; that enrichment is persisted before the in-memory journal changes. A later omitted ID does not erase the known binding. Two different nonempty trade IDs still conflict. No binding is inferred from similar price, quantity or time, and metadata enrichment never adds the fill twice.
- `backend/evplatform/venues/polymarket_us_account.py` intentionally returns unverified identity and spendable-cash flags. The executor declares `fill_replay=False` and `settlement_reconciliation=False`. A diagnostic resolution reader alone must not turn those flags on or emit authoritative paid `NativeSettlement` records.
- `docs/VENUE_PROTOCOLS.md` should retain the launch limitation, with the narrower explanation above: resolution/trade identifiers exist, but the reviewed retail cash-posting and recovery contracts do not complete the join.

## Actual read-only capture

At **2026-10-21 06:52 UTC**, the configured retail credentials authenticated successfully. The documented RFQ participant lookup succeeded, but that participant is not automatically a verified cash-account identity. The served activity history contained **two balance activities, zero trades and zero position resolutions**. Pagination ended explicitly and both rows parsed successfully. No paid settlement, order linkage or cash-posting join was established; no orders, cancellations or fund transfers were issued.

Protected files are `data/polymarket-activities-20261021-readonly/activities.json` and `coverage.json`. They are local ignored account records, not repository fixtures. Capture another read-only snapshot using a new output directory:

```powershell
.\.venv\Scripts\python.exe scripts\capture_polymarket_us_activities.py --output data\polymarket-activities-review-2
```

The script signs only the documented retail balance, participant and activity GET routes. It does not call the institutional API. Its summary always reports `cash_posting_join_verified: false`, zero emitted settlements and no OMS certification. A diagnostic resolution never becomes a paid `NativeSettlement` merely because a public result, balance delta or timestamp appears to agree.

## Institutional API is a separate integration

The [institutional balance ledger](https://docs.polymarket.us/api-reference/funding/get-account-balance-ledger) documents `GET https://api.prod.polymarketexchange.com/v1/funding/balance-ledger`, bearer JWT authentication, and an account such as `firms/{firm}/accounts/{id}`. Entries include a unique ID, account, symbol/security, typed before/after cash balances, and time; resolution and commission entries are supported. This is materially stronger cash-ledger evidence, but it uses a different host, identity model, credentials, and access scope. A personal retail Ed25519 key does not establish institutional eligibility. Do not route personal credentials to that API or claim its ledger is available to this account.

## Evidence needed to remove the remaining block

Obtain an official retail contract or authorized, documented account statement that links a durable cash-posting identifier to the account and market/trade, specifies paid settlement cash and adjustments, and supports complete replay across restart. Then implement and test exact execution/trade/cash joins, partial positions, NO collateral release, busts/corrections, duplicate delivery, missing pages, and restart recovery. Any unlinked or ambiguous record must remain unreconciled.

### Exact retail support questions (prepared, not sent)

For a personal US retail API account, which supported endpoint or account statement links `execution.tradeId` and `positionResolution.tradeId` to the final posted USD settlement, commissions, refunds and corrections? Please identify the stable account identifier, cash-posting ID, market/trade linkage, posting status and pagination/replay guarantees. The documented account balance change has a transaction ID without a trade/market link, while the private balance-change message has neither that link nor a durable replay sequence. Is there a supported retail ledger or export that supplies the missing linkage?

Please also specify cash-only spendable funds after open-order reservations and short-YES/NO collateral, separately from buying power. Institutional bearer-token APIs are not assumed available to this personal retail account. No production settlement validation can be claimed until these identities and cash semantics are established.
