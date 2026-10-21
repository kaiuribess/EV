# Live readiness audit — October 21, 2026

**Result: blocked.** Rechecked the local application, authenticated account
history, quarantined real prices, and current official API documentation. No
orders, cancellations, transfers, or live activation were performed.

## Verified operational state

The dashboard was offline at the start of this audit. It was restarted on
`http://127.0.0.1:8001` in demo mode with automation off, live execution disabled,
and the kill switch active. All four native monitors authenticated and streamed;
Parlay REST/WebSocket and Oddize authenticated. Connections alone do not satisfy
the launch gate. The running scope had zero reviewed launch profiles, zero
verified native mappings, zero mapped real reference markets, and zero native
trade candidates. Its six displayed candidates were synthetic.

At 14:48 UTC the bounded market review received 169 observations, including one
invalid identity. All 168 reviewable observations lacked approved settlement and
identity mappings and verified market status. None passed quote evidence checks.
The diagnostic used a five-second observation-age threshold; that is not a
validated provider/book/market TTL or evidence that every unchanged price is
stale. Price-change clocks must not substitute for quote observations.

## Remaining work and acceptance evidence

| Gap | What must establish readiness |
| --- | --- |
| Exact market and settlement mapping | Bind real listing IDs, jurisdiction, event/player/line/period and current rules. Resolve postponement, interruption, overtime, pushes and void payouts. Do not equate sportsbook refunds with exchange fair-value settlement. See [NBA rule differences](NBA_SETTLEMENT_EVIDENCE.md). |
| Reference freshness and status | Obtain quote-specific observation and active/withdrawn status semantics for the exact feed/book/market; validate TTLs, complete opposing outcomes, reconnect invalidation and coverage. A received packet or heartbeat is insufficient. See [quote contracts](QUOTE_OBSERVATION_CONTRACT.md). |
| Pricing approval | Validate selected sport/market profiles and sensitivity checks on real evidence. Imported weights remain priors; no demonstrated profitable calibration exists. |
| Executable size and fees | Bind current native instrument quantity/price grids, minimum cash cost, fee bounds, payout rules and order permissions to each reviewed mapping. Never substitute $5 units for venue minima. |
| Account and inventory | Verify spendable cash, identity, permissions, eligibility and private-state reconciliation for each selected account. Use the quarter-Kelly draft only after those facts and scope are approved. |
| Genuine lifecycle | Retain an application-managed intent, native acknowledgement, fills/fees, restart recovery and paid settlement with exact IDs. Offline tests and empty native histories cannot supply this. |
| ProphetX reporting | Establish the end-of-history meaning of the observed `next_cursor: null`, reporting watermark/recovery behavior, and complete funded-cash history. A null cursor remains unverified rather than silently treated as exhaustion. |
| Polymarket US | Implement authoritative retail account/trade-to-posted-cash reconciliation after its identity, cash-only collateral/reservation semantics and replay contract are established. Resolution events alone do not establish paid account cash. See [the detailed gap analysis](POLYMARKET_US_SETTLEMENT_GAPS.md). |

These are separate dependencies. Adding a credential, changing a `verified` flag,
or completing one ordinary winning trade cannot prove all exceptional execution
and settlement paths. A future initial release can select a narrower reviewed
venue/market scope; an unsupported monitor need not become an execution target.
No such passing scope exists in this audit.

## Genuine read-only account checks

Authenticated captures started at **2026-10-21 14:48:52 UTC**:

- Kalshi: served current orders, fills and settlements were empty; pagination
  completed. This does not cover unqueried archives.
- Novig: all five order-status collections, fills and settled positions were
  empty; one wallet transaction was present. Pagination completed.
- ProphetX: the seven-day diagnostic window contained zero fills and one wallet
  transaction. Order history had zero rows but an unverified null cursor, so it
  was **not** marked complete. Reporting history is eventually consistent.
- Polymarket US: two balance activities, zero trades, zero resolutions; activity
  pagination and parsing completed. No trade-to-cash settlement join was proven.
- The local native OMS contained zero orders, fills, reconciliations or settlements.

Raw account captures remain protected and Git-ignored under
`data/native-gap-audit-20261021/` and `data/polymarket-gap-audit-20261021/`.
Local preparation and market summaries are `data/gap-recheck-20261021.json`
and `data/market-gap-audit-20261021.json`. They are dated diagnostics, never
current trading authorization.

## Validation

Full offline run: **1,728 backend tests passed in 47.81 seconds**, with
**88.50% statement coverage** (11,547 of 13,047 statements), plus **89 frontend
tests passed** and a successful TypeScript/Vite production build. The run is
saved in `data/validation-live-gaps-20261021.log`. This is software regression
evidence, not genuine native lifecycle or profitability validation. No new
live fill latency was measured because no native order was placed.

The audit also corrected stale documentation that still described $5 as a stake
cap. The validated local draft already correctly uses quarter Kelly, $5 units,
no fixed dollar cap, no daily-loss stop, cash segregation and event/player overlap
protection. It remains unapplied until a reviewed launch scope passes.

## Official contracts rechecked

- [Parlay streaming schema](https://parlay-api.com/v1/asyncapi.json): bounded
  recent-row snapshots differ from explicitly complete game-board baselines.
- [Polymarket retail activities](https://docs.polymarket.us/api-reference/portfolio/get-activities)
  and [balances](https://docs.polymarket.us/api-reference/account/get-account-balances):
  documented trade/resolution history and balance components do not by themselves
  establish the missing account-attributed cash-posting join.
- [ProphetX order history](https://docs.prophetx.co/reference/get_v4-mm-get-order-history)
  documents creation/update filters and cursor paging; the reviewed page did not
  establish null-cursor exhaustion.
- [ProphetX order lifecycle](https://docs.prophetx.co/docs/direct-link-order-lifecycle)
  distinguishes asynchronous matching, replicated reporting, per-fill quantities
  and cancellation races. An acknowledgement is not a fill.
