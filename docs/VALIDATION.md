# Validation - 2026-10-20

## Latest automated checks

Executed on this Windows PC with Python 3.12.0:

```powershell
.\run_tests.ps1
```

**1,321 tests passed in 36.38 seconds with coverage enabled.** Backend statement
coverage is **87.65%** (9,606 of 10,960 statements). TypeScript checking and the Vite
production build passed. One upstream Starlette/httpx TestClient deprecation
warning remains. Coverage measures executed statements, not exchange certification.

All order, cancellation, fill, account and settlement exchanges in automated tests
use controlled offline responses. No test submitted a real native order.

The Novig setup addition contributes 52 offline tests covering production signer
identity, protected local preparation, durable unknown outcomes, same-ID transfer
recovery, pending/final states, exact amount and destination checks, concurrent
process exclusion, and export without replacing paper credentials or enabling
trading. Eight additional regressions cover explicit environment credential
selection and account diagnostics. Missing production credentials never fall
back to a paper or management key.

The Windows setup wrapper passed real GET-only production status checks and
reported $25.00 primary cash, zero production trading wallets and zero trading
keys. Local preparation then generated a signer outside the repository, saved
its journal, and verified a protected ACL restricted to the current user and
SYSTEM. No remote wallet opening or funding was submitted. The operator's
selected $25 funding remains an owner-performed step. Actual Windows ACL and
atomic-write checks were separate from the coverage run; those OS operations
are mocked in the offline unit suite. See [Novig setup](NOVIG_SETUP.md).

The updated app restarted on port 8001 with 13 plugins, the existing Novig
sandbox/legacy credential selection, automation off, the kill switch on, and
zero native orders. The $5 cap, null daily-loss limit and event/player overlap
guards persisted. Local evidence: `data/novig-setup-paused-restart.json`.

The latest release adds a reviewed multi-venue launch portfolio with separate
account cash checks and shared event/player exposure guards. Focused regressions
cover concurrent cross-venue reservation, wrong-account cash, busy or timed-out
settlement recovery, and immediate safe retry. Incomplete settlement checks retain
exposure and invalidate cash verification. They cannot authorize new orders.
ProphetX private-heartbeat/history fences and lossless audit compression also have
offline fault tests. An import-time clock in a readiness test was corrected to
create its future timestamp when the case executes; the full suite then passed.

Large audit records are compressed on a worker thread without sampling or
discarding events. A 1,000-record sample shrank from 3,694,524 to 1,273,086 bytes
(65.54%), with exact roundtrips for every record. Legacy TEXT and new BLOB audit
rows are both readable; bounded decoding rejects corruption. Existing historical
database space is not reclaimed. Order intents share the writer queue and may
wait behind an audit batch; dispatch checks freshness again after persistence.

A separate latest 15-second synthetic rehearsal generated nine markets, 18
positive and nine negative offers, nine paper orders and four orders with fills.
Across 2,190 calculations, internal processing was median 0.232 ms, p95 0.529 ms,
p99 0.720 ms. Receipt-to-calculation was median 11.187 ms, p95 25.998 ms, p99
36.001 ms (1,800 samples). Nine durable intents had median 4.287 ms; seven
simulated fill events had median 2,124.501 ms. Those small samples do not support
p95/p99 and do not measure live fills or profitability. There were no bus drops.

The updated Windows application then restarted successfully on port 8001 with
13 registered plugins, demo mode, automation off, kill switch on, zero native
orders and failed live readiness. All 200 latest audit rows decoded successfully,
including 127 compressed records. Local evidence:
`data/portfolio-paused-restart.json`.

The full offline engine rehearsal covers a source event, exact native book,
pricing, central risk approval, durable intent, acknowledgement without an assumed
fill, partial fill, cancel race, audit persistence and restart. Fault tests cover:

- Fractional contracts, cent contracts and USD stakes with exact decimal cash.
- Wrong market/player/line/rules, unknown clocks, source staleness and sequence loss.
- Duplicate bookmaker observations and repeated fills with changed receipt times.
- Opposing exposure across venues and strategies, alternate lines, correlated
  limits, monetary limits, daily loss, duplicate intent and order-rate caps.
- Unknown placement outcomes, private fill watermarks and authoritative cash fees.
- Slow reconciliation reads that must not block emergency cancellation.
- Changed account/client/intent bindings during a network read.
- Cancel-pending and unknown cancellation states preserved across stale responses.
- Restart recovery, idempotent settlement, corrected fees, durable settlement
  invalidation and preventing a kill-switch reset before accounting repair.
- Invalid launch profiles, exhausted quotas, crashed workers, stale evidence,
  missing private sequences and account/environment mismatches.

## Windows and browser checks

The native sandbox launcher was actually started on port 8002 with its separate
`data/native-sandbox.db`. Health reported sandbox mode, automation off, failed
readiness and zero native orders. A production start on the occupied sandbox
port was rejected. The managed sandbox process then stopped cleanly and released
the port. All seven PowerShell scripts parsed without syntax errors.

The main app was restarted on port 8001. Existing monitor configurations and
selected monitor enablement were restored. It remains in demo mode with automation
off, the existing kill switch active, and zero native orders. No production
activation or financial transaction was performed.

Rendered browser inspection confirmed changing sample opportunities and player
props, the Trading desk launch-readiness panel, visible account/system blockers,
and paper-specific labels. Separate native ledger/statistics rendering and the
native Performance availability notice passed focused React render checks. The
disabled automation control and
active kill switch are visible. Screenshot: `artifacts/native-launch-readiness.png`.
The dashboard is available at http://127.0.0.1:8001/.

The final account-setup restart restored the enabled external feeds and native
monitors without further toggle requests. Kalshi and Polymarket US authenticated
and streamed again; Parlay Business streaming also resumed. The app remained in
demo mode with automation off, its kill switch active, and zero native orders.
The public dashboard state contained none of the configured API credentials.
Local evidence: `data/accounts-before-restart.json` and
`data/accounts-after-restart.json`. Startup preferences restore only inputs and
native monitors; they cannot restore strategy or execution authorization.

## Latest paused restart and authenticated read check

The final build restarted on port 8001 with the saved $5 including-fee limit,
`daily_loss_limit: null`, and event/player overlap guards enabled. Demo mode
retains its finite simulated bankroll; native account-cash limits are prepared
in `data/operator_launch_limits.json` and do not arm trading. Automation stayed
off, the persisted kill switch stayed on, and the native ledger contained zero
orders. The dashboard visibly showed these controls and unreviewed real-market
observations. Screenshot: `artifacts/operator-risk-policy.png`.

On October 21, 2026 at 04:13:21 UTC (October 20, 9:13:21 PM PDT), an independent
GET/subscription-only Kalshi production check confirmed `fill`, `user_orders`
and `market_positions` subscriptions. The authenticated account probe verified
identity, spendable cash, coherent empty order/position inventory, and private
connection/revision synchronization. Its order gate remained disabled. Local
evidence: `data/kalshi-private-readonly-check.json`. This validates read-side
account synchronization; it does not certify native placement, fills or payouts.

A Novig sandbox monitor briefly reconnected with a `ValueError` after restart
and resumed subscriptions. A separate bounded 35-second diagnostic then received
10 frames and three books with no parser exceptions or reconnects; seven saved
market IDs were excluded only after confirmed settlement. The intermittent
failure was not reproduced or claimed fixed. It remains an item for genuine
native lifecycle validation. Sanitized local evidence:
`data/novig-monitor-diagnostic.json`. No private payloads were exported.

## Operator policy rehearsal — October 20 PDT / October 21 UTC

An isolated 15-second synthetic run used the requested $5 including-fee cap,
no daily-loss stop, one active event/player, and a finite simulated bankroll.
It produced nine markets, 36 quotes, 18 positive and nine negative offers.
Three paper orders received fills. The overlap guard rejected 717 subsequent
same-event candidates; other price, freshness and duplicate checks also rejected
candidates. No native credentials or external connections were enabled.

Across 2,190 calculations, internal processing measured median **0.205 ms**,
p95 **0.379 ms**, p99 **0.559 ms**. Receipt-to-calculation measured median
**10.000 ms**, p95 **18.227 ms**, p99 **21.102 ms** (1,800 samples).
Seven simulated fill events had median **1,106.218 ms**; that is insufficient
for p95/p99. Three durable intents had median **3.498 ms** persistence delay.
This is synthetic execution behavior, not live queue position, live fills or
profitability evidence. Local evidence: `data/operator-policy-benchmark.json`.

The new checks also cover account reads racing with private updates or settlement,
complete and incomplete private-fill recovery, contradictory execution IDs,
queued dispatch after pause/expiry, cross-process execution locking, optional
daily-loss stops, and cash-only caps requiring verified available balance.

## Measured local latency

```powershell
.\.venv\Scripts\python.exe scripts\benchmark.py --seconds 15
```

This isolated synthetic run occurred after tests finished, with the main local
app also running. It generated nine markets, 36 quotes, 27 books, 18 positive and
nine negative opportunities. Nine paper orders were routed; four had fills.
There were no event-bus drops or safety halt.

| Measurement | Samples | Median | p95 | p99 |
|---|---:|---:|---:|---:|
| Internal event processing | 2,190 | 0.202 ms | 0.384 ms | 0.552 ms |
| Local receipt to calculation | 1,800 | 10.998 ms | 19.001 ms | 23.000 ms |
| Durable intent barrier | 9 | 3.476 ms | Insufficient samples | Insufficient samples |
| Paper acknowledgement | 9 | 1.000 ms | Insufficient samples | Insufficient samples |
| Simulated fill confirmation | 7 | 578.279 ms | Insufficient samples | Insufficient samples |

p95 requires at least 20 samples and p99 at least 100. These numbers do not measure
sportsbook-origin latency, native acknowledgement/fill speed, adverse selection,
or profitability. The simulator's fill delay comes from its deterministic schedule.
Zero realized P&L reflects unsettled sample markets.

Generated reports `artifacts/benchmark.json` and `data/coverage.json` are ignored
by Git, as are databases, account state and local credentials.

## Genuine connection evidence and remaining work

On October 21 at 04:42–04:43 UTC (October 20 PDT), the newly created production
Novig management key passed signed GET-only identity/signer and wallet checks.
The primary cash wallet returned $25.00; the account had zero trading subaccounts
and zero production trading keys. The web portfolio total was $75.00, which is
not assumed to be available API cash. Novig's portfolio definition can include
open positions and trade credits; the unexplained $50 was not classified.
Evidence: ignored `data/novig-production-management-diagnostic.json`. The key is
stored outside Git, with Windows permissions restricted to the current user.
The active sandbox trading credentials remain unchanged. Creating a management
key does not create/fund a trading wallet or certify order lifecycle behavior.
The browser separately reported blocked location permission; it was not bypassed.

Earlier read-only checks established Kalshi public REST discovery/books, Novig
sandbox signed discovery and private/book subscriptions, ProphetX production
Talaria authentication/subscriptions/market messages, and Parlay/Oddize REST
observations. Those facts do not prove current trading permissions or order
lifecycle correctness.

A new primary personal Kalshi production check succeeded on October 20, 2026
at 20:18:55 PDT (October 21, 03:18:55 UTC). Authenticated balance and API-key
identity/scopes reads succeeded. Read-only orders/positions reconciliation
completed with zero open orders and zero positions. The market WebSocket
returned `subscribed` and an authenticated `orderbook_snapshot`; a REST book
was also received. Evidence is saved locally in `data/kalshi-connection.json`.

The key reported `read` and `write::trade`, without transfer scope. Its observed
location attestation expires October 27, 2026 at 8:15:55 PM PDT. These are
expiring account-access facts, not application trading authorization. Only GET
requests and read-only WebSocket subscriptions were used: no native order or
cancellation was sent, and `trade_gate_enabled` remained false. That initial
monitor check did not certify private-state synchronization or settlement. The
later read-only private synchronization check above succeeded; actual settlement
and order lifecycle certification remain outstanding.

A separate personal Polymarket US retail production check succeeded on October
20, 2026 at 20:19:57 PDT (October 21, 03:19:57 UTC). Signed
`GET /v1/account/balances` authenticated and returned one row; unsigned public
discovery returned 25 markets and a REST book. Authenticated market and private
WebSockets delivered actual `marketData` and `orderSubscriptionSnapshot` frames,
with zero reported errors and zero unknown messages. Evidence is local in
`data/polymarket-us-connection.json`. This test used the personal US API, not the
international wallet/CLOB service.

The Polymarket US client remained monitor-only with `execution_supported: false`,
`reconciled: false`, and zero native orders sent. This confirms those read-only
requests and stream deliveries, not a reconciled account-risk inventory, order
lifecycle, settlement, trading permission or launch authorization.

Current Parlay paid WebSocket access was verified on October 20, 2026 local time
(October 21, 03:08:55–03:09:07 UTC). The server reported `tier: business`,
`push_mode: coalesced`, and `min_push_interval_s: 1.0`. Both checks showed
authenticated streaming, `basketball_nba` ready, no pending snapshot, and zero
reported connection errors. The received offer window changed and quarantine
entries increased from 49 to 75. Observed raw markets included moneylines, totals,
player points, assists, rebounds, and threes made. These were actual provider
observations, not synthetic test fixtures. The local evidence is
`data/parlay-paid-access.json`.

All unreviewed real markets remained quarantined: `mapped_quotes` was zero,
`auto_trade` false, `kill_switch` true, and native orders zero. The server's
one-second coalescing interval is not measured sportsbook-to-client latency,
execution delay, or a guarantee of every upstream price change. See
[current integration scope](IMPLEMENTATION_STATUS.md), [provider protocols](PROVIDER_PROTOCOLS.md),
[venue evidence](VENUE_PROTOCOLS.md) and [Oddize evidence](ODDIZE.md).

Native order placement, cancellation, fills and settled cash have not been tested
against a real account. Reviewed live mappings, fees, account permissions,
geographic eligibility and actual sandbox lifecycle evidence remain required.
Novig's narrow ordinary WIN/LOSS settlement path is implemented offline; pushes,
FMV, remediation and shared-position cash allocation remain unsupported. Kalshi private
synchronization is implemented and has a genuine read-only check recorded above.
ProphetX full-history/actual-fee readers and private fences are implemented with
offline tests; account-specific validation is outstanding. An explicitly reviewed narrow Novig
IOC settlement policy can satisfy capability admission; it still requires genuine
account-bound lifecycle and settlement evidence, and stops on exceptional results. Supplying keys alone cannot
satisfy these requirements. See [GO_LIVE.md](GO_LIVE.md).
