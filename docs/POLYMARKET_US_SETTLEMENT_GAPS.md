# Polymarket US retail settlement evidence

Initially reviewed **2026-10-20** against official documentation. On **2026-10-21**, the diagnostic activity reader was implemented, tested offline and used for an authenticated read-only capture of the configured personal US account. That capture proves access to the served history, not an order-to-settlement lifecycle.

## Conclusion

The remaining work is partly ordinary reader implementation and partly missing documented retail cash-ledger evidence. Polymarket US **does document resolution events**. Those events can support a diagnostic resolution history, but the reviewed retail contracts do not establish a replayable join from a specific managed trade to the cash actually posted for its settlement, fees, refunds, or corrections. Keep authoritative settlement reconciliation disabled until that join and its recovery behavior are verified. Do not describe the blocker as “Polymarket has no settlement API.”

## What the retail API documents

| Official source | Documented evidence | Limit of that evidence |
| --- | --- | --- |
| [Get Activities](https://docs.polymarket.us/api-reference/portfolio/get-activities) | Authenticated `GET https://api.polymarket.us/v1/portfolio/activities`; cursor pagination with `nextCursor` and `eof`. `positionResolution` includes `marketSlug`, `beforePosition`, `afterPosition`, `side`, `tradeId`, and `updateTime`. Trade activities include an exchange trade ID and state. | Resolution has no explicit paid-cash amount, cash-posting transaction ID/status, settlement fee, or refund amount. Trade activities have no documented order ID. `AccountBalanceChange` has transaction ID/status/amount but no market/trade/order link. |
| [Private WebSocket](https://docs.polymarket.us/api-reference/websocket/private) | Order executions contain `tradeId`. Position updates contain `tradeId` and an entry type. Balance changes contain before/after balances, time, and entry type; ledger types include resolution, commission, correction, and netting. | The prose example omits additional identifiers present in the official SDK, described below. Their served values, financial meaning and durable recovery remain unverified. Joining messages by time or equal amounts would be inference. |
| [Get Market Settlement](https://docs.polymarket.us/api-reference/markets/get-market-settlement) | Public `GET https://gateway.polymarket.us/v1/markets/{slug}/settlement` returns `slug` and settlement price. | Public resolution price does not prove cash was posted to this account. |
| [Get Account Balances](https://docs.polymarket.us/api-reference/account/get-account-balances) and [Portfolio Overview](https://docs.polymarket.us/api-reference/portfolio/overview) | Cash, buying power, asset collateral, reservations, and other balance components are documented. `buyingPower` is described as capital available for trading. | No stable cash-account identifier appears in the reviewed balance response. Buying power includes collateral semantics and should not silently replace a cash-only budget. `lastUpdated` is the last balance-change time, not a fresh account-verification timestamp. |

The [collateral and margin rules](https://docs.polymarket.us/market-structure/collateral-and-margin) also matter: buying NO is a short YES position, with sale proceeds and collateral. Releasing collateral is not the same cash flow as a normalized “NO contract pays $1” model. An eventual accounting adapter must distinguish economic payout from actual cash and collateral movements.

## Official SDK and GitHub comparison, October 21

The [official Python SDK types](https://github.com/Polymarket/polymarket-us-python/blob/6fde8f2b6ea88198a199b38f7b6d8951d015a06a/polymarket_us/websocket/types.py) expose additional balance-change fields: `id`, `accountName`, and `modifiedSecurityId`. Position subscriptions also include `referenceId` and `allocationGroupId`. The earlier blanket statement that private balance changes have no identifiers was too broad. These are useful leads for exact accounting, not evidence that the live account supplies nonempty identifiers with the necessary cash-posting semantics. The SDK's private position/balance test fixture leaves the three balance identifiers empty. Its fixtures are synthetic.

Read-only source inspection also covered [Synpath](https://github.com/Synpath-ai/synpath/blob/4cb5b6d0a8fae866aef4befddfcc1c7f336e5ee6/synpath/trading/polymarket_us.py) and [a community Rust SDK](https://github.com/mbordash/polymarket-us). Synpath derives settlement P&L from position changes and does not advertise replayable fills. That implementation does not establish a retail trade-to-posted-cash ledger for this application. Community code was inspected as implementation evidence; it was not executed or treated as an authoritative API guarantee.

`polymarket_us_private_evidence.py` now preserves a bounded authenticated private stream in protected local artifacts. Its public summary reports field presence only, without account identifiers or amounts. Missing, blank and malformed fields remain distinguishable; a disconnect remains visible. Neither a field's presence nor an empty stream certifies cash posting or replay coverage.

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

At **2026-10-21 15:05:37 UTC**, another authenticated read-only capture included a 20-second private subscription window. It received one order snapshot and no balance or position changes. Activity history still had two balance activities, zero trades and zero resolutions, with complete pagination/parsing and no capture errors. Consequently the extra SDK identifiers were **not observed**, rather than proven unsupported. Protected files are under `data/polymarket-sdk-wire-20261021/`.

```powershell
.\.venv\Scripts\python.exe scripts\capture_polymarket_us_activities.py --stream-seconds 20 --output data\polymarket-private-review-new
```

The optional socket signs the documented private WebSocket route and sends subscriptions only. The collector has time, frame and byte limits, closes the stream on completion, and does not send orders, cancellations or transfers.

## Institutional API is a separate integration

### October 21 subscription isolation check

The **16:11 UTC** authenticated recheck again parsed two balance activities and
no trades/resolutions. The 30-second combined private subscription received one
order snapshot. Separate 20-second balance-only and position-only subscriptions
received no data. This rules out neither delayed delivery nor an idle account;
it does not establish a missing permission or unsupported API. No account
mutation was used to provoke an update.

The monitor now reports `private_subscription_evidence` by subscription type,
separating `awaiting_data` from `data_observed`. Evidence resets on disconnect,
reconnect and teardown; heartbeats and old request IDs cannot satisfy a pending
subscription. A streaming connection remains delivery health, never account
reconciliation. Diagnostic captures preserve this allowlisted status before
teardown. Protected artifacts: `data/polymarket-final-audit-20261021/` and
`data/polymarket-isolated-scopes-20261021/`.

### Institutional scope

The [institutional balance ledger](https://docs.polymarket.us/api-reference/funding/get-account-balance-ledger) documents `GET https://api.prod.polymarketexchange.com/v1/funding/balance-ledger`, bearer JWT authentication, and an account such as `firms/{firm}/accounts/{id}`. Entries include a unique ID, account, symbol/security, typed before/after cash balances, and time; resolution and commission entries are supported. This is materially stronger cash-ledger evidence, but it uses a different host, identity model, credentials, and access scope. A personal retail Ed25519 key does not establish institutional eligibility. Do not route personal credentials to that API or claim its ledger is available to this account.

## Evidence needed to remove the remaining block

Obtain an official retail contract or authorized, documented account statement that links a durable cash-posting identifier to the account and market/trade, specifies paid settlement cash and adjustments, and supports complete replay across restart. Then implement and test exact execution/trade/cash joins, partial positions, NO collateral release, busts/corrections, duplicate delivery, missing pages, and restart recovery. Any unlinked or ambiguous record must remain unreconciled.

### Unresolved contract questions

For a personal US retail API account, which supported endpoint or account statement links `execution.tradeId` and `positionResolution.tradeId` to the final posted USD settlement, commissions, refunds and corrections? Establish whether the SDK's `accountName`, `id`, and `modifiedSecurityId` provide this linkage, how those IDs relate to activity records, and how missed events are recovered. The activity balance record has no documented trade/market link; SDK field names alone do not establish a complete cash join.

Please also specify cash-only spendable funds after open-order reservations and short-YES/NO collateral, separately from buying power. Institutional bearer-token APIs are not assumed available to this personal retail account. No production settlement validation can be claimed until these identities and cash semantics are established.
