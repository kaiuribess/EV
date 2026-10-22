# Live readiness audit — October 21, 2026

## October 21 evening follow-up (01:23 UTC October 22)

The paused production workspace now uses ProphetX's authenticated tournament
catalog to refresh NBA event subscriptions every five minutes. A read-only
catalog call returned 11 open/live NBA events; the monitor subscribed to all
11 plus its private/system channels, confirmed 13 of 13 channels, and received
139 market frames in the bounded check. This verifies live native market-data
delivery for that scope, not a mapped executable opportunity. The catalog
endpoint reuses the monitor session, avoiding a second ProphetX login and its
separate session slot. Event IDs rotate with the native catalog rather than
requiring a daily static list. The client still invalidates books on a scope
change and resynchronizes after reconnect.

The ProphetX wallet sync timestamp was observed 0.6 seconds ahead of the local
receipt clock. The account parser now allows at most two seconds of future
clock skew **for that wallet marker only**. Reporting/order-history clocks
remain strict. A fresh diagnostic now verifies the signed account identity,
but cannot certify spendable cash because account inception and funded-cash
history review remain absent. The production workspace still has zero reviewed
launch profiles, zero verified source/native mappings, zero native candidates
and zero native orders; `auto_trade=false`. Parlay's documented per-book
verification heartbeat does not certify each outcome, and Oddize's current
Price/PropRow schema has observation times but no authoritative market-active
field. The currently sampled source observations had zero verified active
statuses. No native order was submitted. The full backend suite passed 2,029
tests, plus the added one-cycle discovery regression passed separately.

A subsequent read-only event crosswalk found the same Lakers–Warriors scheduled
NBA fixture at 02:00 UTC under distinct ProphetX, Parlay REST/WebSocket and
Oddize IDs. Across the bounded NBA window it produced 23 candidate
source-to-ProphetX event pairs; 11 source IDs had conflicting start times in
their retained Parlay rows (typically a later tip time) and were rejected as
unambiguous candidates. This is **event discovery only**. No outcome,
settlement, quote status, or execution mapping was published.

A production smoke check caught a config regression before any order could be
armed: ProphetX's discovery-only `event_ids` had been passed to the strict
Polymarket US monitor config. The dispatch is now venue-specific and has a
regression test. After the repair and restart, Kalshi, Novig, ProphetX and
Polymarket US all reported authenticated streaming again; automatic orders
remained off and the native order ledger remained empty. The full backend
suite then passed **2,034 tests**.

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
| ProphetX reporting | Empty order-history null cursors now have a narrowly scoped, timestamp-checked compatibility path verified against production. Complete funded-cash account scope and genuine financial lifecycle evidence remain required. See [the repair and its limits](PROPHETX_RECONCILIATION.md). |
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
  was **not** marked complete in the initial capture. After the compatibility
  repair, the **15:45:52 UTC** authenticated recheck completed all three requested
  collections without errors, still with zero orders/fills and one transaction.
  Reporting remains eventually consistent; this is bounded window evidence.
- Polymarket US: two balance activities, zero trades, zero resolutions; activity
  pagination and parsing completed. No trade-to-cash settlement join was proven.
- The local native OMS contained zero orders, fills, reconciliations or settlements.

Raw account captures remain protected and Git-ignored under
`data/native-gap-audit-20261021/` and `data/polymarket-gap-audit-20261021/`.
Local preparation and market summaries are `data/gap-recheck-20261021.json`
and `data/market-gap-audit-20261021.json`. They are dated diagnostics, never
current trading authorization.

## Validation

Latest full offline run: **1,880 backend tests passed in 48.14 seconds**, with
**88.67% statement coverage** (11,846 of 13,359 statements), plus **89 frontend
tests passed** and a successful TypeScript/Vite production build. The run is
saved in `data/validation-source-gates-final-20261021.log`. This is software regression
evidence, not genuine native lifecycle or profitability validation. No new
live fill latency was measured because no native order was placed.

The audit also corrected stale documentation that still described $5 as a stake
cap. The validated local draft already correctly uses quarter Kelly, $5 units,
no fixed dollar cap, no daily-loss stop, cash segregation and event/player overlap
protection. It remains unapplied until a reviewed launch scope passes.

## Follow-up implementation and source review

- Detailed contingency rules now participate in runtime market identity and durable serialization. Mapping evidence validation rejects omitted or contradictory runtime rules. Existing identities remain stable when this extension is absent; missing evidence stays missing.
- The official Polymarket US Python SDK reveals private balance identifiers omitted by the prose example. A bounded private capture now preserves these fields and reports their presence without exposing values. The real 20-second account capture contained only an order snapshot, so settlement identifiers and cash joins were not established. See [the SDK comparison and capture](POLYMARKET_US_SETTLEMENT_GAPS.md).
- Read ProphetX's full order-history/get-order OpenAPI schemas, wallet, integration, lifecycle, enum and WebSocket guides. Added a narrow compatibility rule for the observed timestamped empty-order/null-cursor response and exact-revision history lookup for fees omitted by get-order. The published cursor schema still differs from that observed empty response; no whole-account or lifecycle certification follows. See [the implementation evidence](PROPHETX_RECONCILIATION.md).
- No new support messages were sent. GitHub examples and official documentation were inspected directly; Chrome was unavailable through this session's browser connection.

After loading these changes at approximately **15:11 UTC**, the local dashboard was healthy in demo mode with automation off, the kill switch active and zero native orders. All four native monitors authenticated and reached streaming; Novig retained nine startup request errors, so this is not a claim of an error-free session. Parlay's server reported `tier: scale`, `push_mode: raw`, and `min_push_interval_s: 0` for the NBA subscription, confirming the previously transferred temporary trial. That interval is a coalescing setting, not measured end-to-end latency. Real quote mappings still numbered zero.

### Subsequent live feed repair

A narrower NBA game-board capture found eight frames rejected by strict extraction. Captured examples identified FanDuel/DraftKings spread rows that duplicate the named outcome's `price` in `over_price`. The adapter now handles that observed shape without inventing an over/under market; signed handicaps and conflicting-price checks remain intact. Malformed flat rows invalidate their affected source scope while unrelated valid updates continue. Supplied streaming start times and sport labels now survive discovery.

All 25 retained failing rows replayed successfully. A fresh **15:34:58–15:35:25 UTC** read-only capture parsed 68 offers across FanDuel/DraftKings moneylines and spreads with **zero extraction errors**. No Pinnacle offer was observed in that short window; it does not establish absent provider coverage. None of these rows carried the required verified observation/active-status evidence, so they remain review data. Details and regression behavior are in [the quote contract](QUOTE_OBSERVATION_CONTRACT.md).

An offline replay benchmark of 200 batches of those 25 rows measured **0.765 ms median, 0.982 ms p95, and 1.090 ms p99** for parsing, failure isolation and quarantine. It excludes network, pricing, risk, routing and fills; it is not order latency. Artifact: `data/parser-replay-latency-20261021.json`.

## Official contracts rechecked

### Follow-up at 16:11–16:16 UTC

Authenticated current-history requests for Kalshi, Novig and ProphetX completed
without errors. All served order/fill/settlement collections remained empty;
Novig and ProphetX each returned one wallet transaction. The local OMS still
had no native orders or lifecycle records. Polymarket's separate capture still
had two balance activities and no trade/resolution history. These are scoped
reads, not evidence that all historical archives or exceptional outcomes were
covered. Artifacts: `data/native-final-audit-20261021/` and
`data/polymarket-final-audit-20261021/`.

Two status-reporting defects were corrected. Without a launch profile, venue
and account flags are now reported as **unassessed scope**, rather than falsely
asserting that credentials or adapter implementation are absent. Global risk,
storage and unresolved-order checks still apply, technical readiness stays
false, and selecting a profile restores every venue-specific gate. Polymarket
also exposes per-subscription delivery evidence instead of allowing one order
snapshot to imply balance/position coverage.

The fresh feed sample and ProphetX NBA filings are recorded in
[quote evidence](QUOTE_OBSERVATION_CONTRACT.md) and
[settlement evidence](NBA_SETTLEMENT_EVIDENCE.md). No exact live mapping was
approved. The API's bounded review contained 368 quarantined observations;
all lacked verified market status and complete settlement/identity mappings.
Completing local code cannot manufacture those upstream facts or a genuine
paid settlement. Live execution remains disabled.

### Owned-order replay and ledger-scope repair at 16:28 UTC

The ProphetX forensic collector now captures individual owned order, matching,
history and wallet responses for offline comparison through the runtime financial
normalizer. It rejects foreign account IDs, malformed native IDs, incomplete
pagination, altered summary rows and stale reporting clocks. It shares the exact
history-fee fallback with the running adapter. The new offline rehearsal includes
a $1 submitted stake, a $0.40 partial fill and a killed remainder; these are test
fixtures with no exchange submission.

The capture script also previously selected `data/evplatform.db` by default,
even during production diagnostics. That is the demo ledger. It now selects
`data/native-production.db` or `data/native-sandbox.db`, matching native startup,
and records explicit overrides. Earlier empty local-OMS findings describe only
the selected demo database; they do not establish the production ledger state.

At **16:28:48 UTC**, the authenticated ProphetX recheck completed its seven-day
history with zero orders/fills and one wallet transaction. The expected native
production database did not exist and was reported unavailable. Offline review
therefore reported `local_inventory_complete: false` and issued no lifecycle or
launch certification. No real wager, sale, cancellation or transfer occurred.
Protected evidence: `data/prophetx-native-ledger-audit-20261021/`.

### Source and production permission audit

Regression tests exposed two real source-state defects: a price-less suspended
notice could preserve an active cache entry, and adverse rows could be undone
by a later usable-looking row in the same delivery. All adverse rows now dominate
their source scope for that batch in either order. Unrelated markets continue;
unused nullable columns from other price families are not withdrawals. The
new tests reproduced 22 failures before the repair.

The legacy pricing path also implicitly set `live_execution_allowed: true`
without a selected reference profile. It now remains available for paper/research
and returns false for live permission. Production requires explicit approval
of a strict profile with its independent observation, active-status, exact-source
TTL and sensitivity gates. Validation re-derives permission from current policy,
so editing a cached fair-value permission flag cannot bypass that requirement.
Three new tests reproduced this gap before the fix.

These repairs strengthen software checks; they do not establish the missing
external facts. The official Parlay contract explicitly says the sampled
DraftKings/FanDuel game-line views lack guaranteed removals and a complete
replacement baseline. Oddize's flat price schema supplies no authoritative active
status. No provider status was inferred from timestamps, no real mapping was
approved, and no live order was submitted.

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
