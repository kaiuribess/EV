# Validation - 2026-10-20

October 21 at about 21:20 PDT: **2,110 backend tests passed** in 42.57
seconds, with one third-party Starlette warning. New Kalshi tests cover
bounded series selection across sports, wrong-series/clock rejection,
read-failure retention and reconnect-based scope rotation. Public production
catalog reads returned current NBA/NFL/MLB/NHL game markets; authenticated
production monitoring then streamed eight selected books while automatic
orders remained off. These checks placed no orders and do not verify a
sportsbook/contract settlement match, live quote status, fees, or profitability.

October 21 at 21:15 PDT: **2,099 backend tests passed** in 42.76 seconds,
with one third-party Starlette warning. New Polymarket US tests cover public
league discovery, bounded multi-league moneyline selection, wrong-status and
wrong-market rejection, read-failure retention, and clean stream-baseline
rotation. A real public catalog read found current NBA/NFL/MLB/NHL moneylines
without a credential or order call. The catalog result was truncated, so it is
monitoring coverage evidence, not exhaustive coverage, verified sportsbook
matching, settlement equivalence, or production order certification.

October 21 at 21:00–21:05 PDT: a 20-second Parlay-only, four-book NBA game
capture authenticated on Scale and delivered 20 offers across four book/market
groups. Every sampled row lacked `last_observed`, `last_update_type`, `status`,
and `available`; all 20 retained `last_update`, which is not sufficient proof
of current tradability. No Oddize credit was used. Account-only diagnostics
read $50 Kalshi and $25 Novig available cash with empty inventories; ProphetX
identity was authenticated but complete funded-cash scope remained unverified.
Ten Novig markets triggered public book sequence gaps in both local workspaces;
the paused production monitor now samples four, while duplicate paper-side
native production monitors are stopped. Gap-channel diagnostics and the paper
launcher policy have focused tests/PowerShell parse checks. No real-money order
or live activation occurred in this follow-up.

October 21, 2026 evening follow-up: **2,088 backend tests passed** in 42.23
seconds, with one third-party Starlette deprecation warning. The new Novig
catalog tests cover bounded multi-league selection, conflicting identities,
read-failure retention, and reconnect-based scope rotation. ProphetX tests
cover consecutive access-only refreshes without another login. Actual signed
catalog reads found 52 open production NBA/NFL/MLB/NHL moneylines; both local
workspaces streamed 10 selected Novig books. An authenticated ProphetX refresh
response confirmed that production may omit a replacement refresh token. The
paused production monitor authenticated and streamed after the parser repair;
its next automatic renewal remains to be observed. No native order, account
transfer, or live activation was made in this follow-up.

October 21, 2026 follow-up: **2,076 backend tests passed** in 40.41 seconds
with one third-party Starlette deprecation warning. Focused Parlay/Oddize and
ProphetX tests passed. PowerShell parsed `start_live_paper.ps1` and
`setup_live_inputs.ps1` without syntax errors. A paused production restart
restored the single ProphetX monitor to authenticated Talaria streaming;
paper retained live Parlay/Oddize inputs and native execution stayed off.
The production workspace still has zero reviewed market mappings and zero
native orders. These checks did not validate real-money order or settlement
behavior.

## Latest automated checks

Sport/market profile integration: **1,531 backend tests passed in 40.06 seconds**,
with **88.53% statement coverage** (10,589 of 11,961 statements). All **88 frontend
tests** passed, followed by TypeScript checking and the production build. One
upstream Starlette/httpx TestClient deprecation warning remains. These are offline
tests, not native order or settlement certifications.

The added checks cover all 11 imported profiles, exact pregame selectors,
independent-book counts/anchors, unchanged-price observation clocks, source TTL
policies, incomplete/status-unknown quotes, duplicate conflicts, degraded-source
recovery watermarks, dual-method EV at original limits/fee caps, native production
permissions, full rejected-reference audit records, and research/negative episode
separation. The offline evaluator additionally tests chronological event grouping,
opposing-side deduplication, equal event weights, result-availability timing,
regularized simplex fitting, common-fold comparisons, log loss/Brier/calibration,
synthetic provenance and exclusive local output.

A separate **15-second strict-profile synthetic run** produced nine markets,
63 source quotes and nine research-only opportunities. Internal processing across
3,000 observations: median **0.125 ms**, p95 **1.552 ms**, p99 **1.816 ms**.
Receipt-to-calculation across 2,610 observations: median **8.999 ms**, p95
**51.419 ms**, p99 **58.001 ms**. There were **zero dropped bus events and zero
orders**, despite that isolated benchmark's paper automation being on: empty
verified TTL policies correctly prevented execution. This sample is not live
feed latency, exchange fill latency or profitability evidence. Output:
`artifacts/benchmark-reference-priors.json`.

The dashboard has all 11 profiles saved, with the first four core prop profiles
selected and the original empty TTL table/live-disabled flags retained. Browser
inspection confirmed the NBA 50/20/20/10 weights, FanDuel anchor, minimum three
books, power/proportional comparison, research-only status, missing TTL coverage,
pending launch checks and disabled real-trading button. The app remains paused
with its emergency stop on and zero native orders. Native monitors authenticate
and stream, but no reviewed real source/native market mappings or native
candidates exist. No source results have been fitted into profitable weights.

## Prior launch-preparation checks

Launch-preparation and reference-policy follow-up: **1,395 tests passed in
38.60 seconds**, with **88.03% backend statement coverage** (9,874 of 11,216
statements). These are offline tests, including the new preparation report,
account/secret boundaries, failed-activation stop preservation, reviewed-scope
digest checks, bookmaker eligibility, changed pricing policy, stale references,
independent sport overrides, and missing Parlay market timestamps. One upstream
Starlette/httpx TestClient deprecation warning remains.

All **57 frontend control tests** passed, followed by strict TypeScript checking
and the Vite production build. Browser inspection confirmed the source-origin
banner, all four account cards, unapplied $5 policy, uncalibrated equal-weight
description, pending requirements and disabled real-trading control. Final
confirmation renders the profile's exact market whitelist and account hashes,
not the global market count. No activation was attempted during browser checks.

A separate 15-second synthetic run produced nine markets, 18 positive and nine
negative opportunities, nine paper orders, and four orders with simulated fills.
Across 2,190 calculations, internal processing was median **0.189 ms**, p95
**0.397 ms**, p99 **0.566 ms**. Receipt-to-calculation was median **9.999 ms**,
p95 **18.995 ms**, p99 **22.001 ms** across 1,800 samples. Seven simulated fill
events had a 510.787 ms median; this sample does not support tail percentiles or
any claim about real fills. There were no bus drops. The separate benchmark
database does not change the dashboard's paused state. Local output:
`artifacts/benchmark.json`.

The updated backend restarted on port 8001 with automation off, the emergency
stop on and zero native orders. The new preparation endpoint confirmed all four
configured production monitors authenticated and streaming, but zero reviewed
native markets, zero real reference markets and zero native candidates. All 27
opportunities in that snapshot were synthetic. The $5 cash-only/no-overlap
operator limits remained a saved, unapplied production draft. Polymarket's
unsupported settlement accounting is now explicit. Local snapshot:
`data/launch-setup-paused-restart.json`.

An isolated GET-only check of the existing Novig **paper** management wallet
returned 10,000 play dollars. Its separate paper trading wallet had zero.
No transfer or order was submitted. Official documentation confirms paper uses
play money and separate credentials; that observation is not production
lifecycle evidence. See [paper validation scope](NOVIG_PAPER_VALIDATION.md).

Latest storage-diagnostic follow-up: **59 focused tests passed in 2.63 seconds**.
Windows permission failures now retain the fixed failing operation and the
numeric Win32 code while excluding private paths and credentials. Tests cover
direct Win32 return codes (rather than a stale last-error value), local-only
diagnosis, unchanged setup journals, and redacted failure reports. The original
terminal failure is still not reproduced in the agent process. A newly opened
interactive owner setup window passed all six local checks and waited at the
owner confirmation; no confirmation was supplied by the agent.

The owner then completed both confirmations. A GET-only transfer check proved
`Applied` for $25.00000; the production trading key passed signed account reads
with a $25.00000 wallet balance, identity verification and no orders/positions.
Its credential pair was exported locally without replacing legacy paper keys.
At October 20 10:24 PM PDT, the restarted app's Novig production monitor reported
authenticated streaming, ten book markets, zero errors/reconnects and no invalid
book subjects. Automation remained off, the kill switch on, native orders zero,
and live readiness false. Local evidence:
`data/novig-production-trading-diagnostic.json` and
`data/novig-production-monitor-connected.json`. No live order was submitted.

Follow-up Windows storage repair: **55 focused tests passed in 2.54 seconds**
(52 setup cases plus three real Windows permissions/error-handling cases).
The permissions helper now uses Win32 directly instead of launching PowerShell;
tests block subprocess launches and verify the resulting owner/DACL independently
through .NET. Actual protected file creation/replacement and refusal to write
after a permissions failure passed. The saved production journal and signer also
passed the real local lock/load path from Windows PowerShell with Restricted
execution policy. That check made no network requests, preserved the journal,
and confirmed no opening attempt or transfer was recorded. The earlier reported
failure was not reproducible in the agent's environment; its shell dependency
has been removed rather than treating the earlier passing check as sufficient.

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

## October 21 live-input paper restart check

The existing Parlay Scale and Oddize Developer credentials authenticated after
`start_live_paper.ps1 -NoBrowser` started port 8001 against the dedicated
`data/live-paper.db`. Sample odds were off and paper automation on. The live
state contained 47 paper markets, 132 real-source quotes, 47 recorded signal
states and zero paper orders at the check; no qualifying opportunity was
present. This is a real-feed paper observation, not a native execution test.

The launcher now records its database path and refuses to treat an unrelated
paper database as the already-running live-paper workspace. The previous
default `evplatform.db` and its history were preserved. An unchanged Parlay
book heartbeat no longer republishes a quote plus two hypothetical book sides;
eligibility transitions still publish them. A 15-second sample after restart
recorded 401 total audit events, including 90 provider frames and 3 signal
observations. That sample measures local logging volume, not feed latency or
trading performance. The full backend suite passed **2,074 tests** (one
third-party deprecation warning), and both edited PowerShell launchers parsed.
