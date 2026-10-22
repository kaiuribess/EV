# Implementation status

This is a working demo/paper platform and an extensible integration foundation.
It is **not a completed production autonomous trading release**.

| Area | Shipped behavior | Remaining work |
|---|---|---|
| Core | Python 3.12 asyncio, typed immutable Pydantic contracts, versioned bus, plugin registry/DI, entry points, schema/config migrations | Process isolation for untrusted plugins; deployment-grade operational supervision |
| Inputs | Parlay REST/WS, Oddize REST, Owls REST, OddsPapi v4 REST, generic REST/SSE template, deterministic simulation/replay; real Parlay/Oddize REST observations and Parlay Business NBA WebSocket game/prop updates verified | Reviewed real-market mappings; Owls/OddsPapi account connectivity; exhaustive catalog/page coverage; provider-specific new shapes |
| Identity | Exact canonical identities, verified aliases and source signatures, quarantine, player/line/rule rejection | Reviewed mappings for real markets and official athlete/team IDs; no guessed alias dataset |
| Pricing | 11 typed sport/market priors; exact pregame selectors, anchor/quorum gates, canonical-book dedup, documented observation/TTL/status gates, power plus proportional sensitivity at size/revalidation, research-only diagnostics and profile-bound live permission | Verified source TTL/status policies; calibrated real-market weights; validated push/different-line models; native live fee feeds |
| Strategies | Positive EV, independent strategy interface, required input and market metadata | Arbitrage, cross-exchange multi-leg execution, middles, steam and ML strategies |
| Paper OMS | Central risk, durable intent, original-limit/fee rechecks, partial fills, cancel, unknown-state freeze, global position-family locks, restart, simulated settlement, burst/open-order caps | Real queue position and coordinated multi-leg policies |
| Native OMS | Separate exact-decimal native contract, central router and shared risk, durable intent, account binding, original-price revalidation, partial/cumulative cash reconciliation, conservative unknown states, cancellation races, restart and settlement ledger; one Novig Paper play-money IOC fill reconciled | Production lifecycle and paid settlement proof; a Paper fill is not production certification |
| Kalshi | Current native clients, fractional-contract accounting, normalized books, private generation/revision fencing, narrow actual-cash settlement and expiring conservative fee bounds; real primary production balance/key identity/scopes, empty order/position inventory reconciliation and authenticated WebSocket book snapshot verified | Genuine sandbox order lifecycle testing, reviewed mappings/fees, genuine order lifecycle and settlement certification |
| Novig | v3 signed REST/WS, native operations/private state, cent-contract accounting and reconciliation; authenticated paper subscriptions and production account/book reads verified; protected setup and durable recovery; one play-money Paper IOC order filled and reconciled | Actual production order lifecycle, location preflight at placement, paid settlement and complete launch evidence; unusual/shared-position settlement remains blocked |
| ProphetX | Native auth, Talaria event/prop subscriptions, v4 orders/cancel, USD-stake accounting, matched-fill/wallet accounting and restricted funded-cash win/loss settlement; production read-only subscriptions and market messages verified | Genuine complete history, funded-cash and actual fee evidence; reviewed source clock units; sandbox lifecycle and settlement certification |
| Polymarket US | Personal US read-only monitor plus separate guarded IOC executor/cancel, canonical book mapping, exact-fill reconciliation and inventory diagnostics; earlier actual signed production read/stream checks passed | Stable account identity/permissions, verified available cash and actual account-attributed settlement remain blocked; guarded executor tested offline only; no retail sandbox assumed |
| Dashboard | Six responsive screens, market detail, references/age/depth/history, metadata config, paper controls, events, account-aware live setup, actionable launch blockers and separate native order ledger; final owner enable control bound to current launch scope | Broader browser/device QA; no current production scope passes the gate |
| Analytics | Durable all-signal episodes and full normalized reference/policy audit even while paused; first-entry hypothetical grading; offline equal/anchor/prior/learned model comparison with event-grouped chronological splits, result-availability gating and log loss/Brier/calibration; processing/receipt/intent/ack/fill timings | Verified real forecast/result dataset; automatic verified result ingestion, live slippage calibration, CLV from verified closing data, production returns and independently clustered uncertainty estimates |
| Storage | Async batched WAL SQLite audit, lossless large-event compression off the event loop, configurations, orders and fill IDs; legacy TEXT and compressed events both readable | Retention/compaction policy for long unattended runs; existing history is not rewritten or deleted |
| Windows | Setup/demo/paper/native/test/stop PowerShell scripts, separate native environment databases, local built dashboard | Always-on operation requires an awake powered PC or a separately deployed host |

## Connection truth

Parlay REST authentication, sport discovery, and NBA moneylines were verified
against the real service. On October 20, 2026 local time, the current paid
WebSocket connection authenticated and the server reported `tier: business`,
`push_mode: coalesced`, and `min_push_interval_s: 1.0`. NBA (`basketball_nba`)
was ready with no pending snapshot and zero reported connection errors. Real
game-line and player-prop offers continued arriving across successive checks.
This supersedes the earlier entitlement refusal; current Business access is verified.

On October 21 at 20:13 PDT, the currently configured Parlay key authenticated
four requested game-board sports and the server reported Scale/raw streaming.
The live-feed paper workspace on port 8001 also authenticated Oddize's existing
Developer key and began receiving real source updates with sample odds off.
The latest scoped Parlay NBA player-points probe found 48 FanDuel rows, one
two-sided row with both native links, and **zero rows** with a recent validated
collector-observation pair. A fresh Parlay NBA h2h REST read returned FanDuel
and Pinnacle prices, but no book or market had that observation pair. These are
working data connections, not verified active sportsbook offers. Production
pricing still has zero reviewed real-market mappings and sends no orders.
The running paper workspace records NO SIGNAL where its profile lacks a fresh
anchor or quorum; it does not create an edge by removing that requirement.

On October 21 at 20:32 PDT, the paper workspace had 65 real-input comparison
markets and 170 visible quotes, with sample odds off and zero orders. Parlay
and Oddize remained authenticated. The production workspace had both feeds
authenticated and native automation off, with zero reviewed canonical markets
or native orders. Authenticated production account probes read $50 Kalshi and
$25 Novig spendable cash, empty order/position inventories and trading-key
scope; these are read-only observations, not launch permission. ProphetX
Talaria authenticated and resumed streaming after its redundant paper-side
production monitor was stopped. That duplicate session used the same key and
could compete for ProphetX's per-key session pool. The paper launcher now
keeps the production ProphetX session on the paused native workspace only.

The live Oddize Developer NBA feed still polls hourly within its 1,500-credit
monthly allowance. Its prior 30-credit process cap would stop an otherwise
healthy hourly feed after 30 paid snapshots; the session ceiling is now 1,200
while provider remaining credits and the monthly pacing rule remain enforced.
Equivalent Parlay schedule timestamp spellings now yield one source signature;
an actual schedule change still requires renewed mapping review. No existing
reviewed production mapping needed migration because none is installed.
An additional read-only Parlay prop check at 03:35 UTC on October 22 returned
1,022 NFL rows (609 two-sided) and 36 MLB rows, but **zero** rows in either
response had a collector-observation timestamp within two minutes. This shows
the paid feed is returning real odds, not that these rows pass the current
quote-bound live-freshness gate. The requests did not place bets.

The evidence is saved locally in `data/parlay-paid-access.json` (03:08:55–03:09:07
UTC on October 21). All unreviewed real markets remained quarantined, with
`mapped_quotes: 0`; automation was off, the kill switch was active, and native
orders remained zero. The one-second setting describes the server's coalesced
push cadence, not measured sportsbook-to-client latency or a fill-speed guarantee.

Oddize's free Developer API authenticated account and catalog reads, and one
real NBA snapshot returned 310 source offers across 17 books, including Pinnacle
and Circa. The running adapter uses background polling within explicit quotas.
Observation clocks do not establish sportsbook-origin latency, executable depth,
or reviewed settlement mappings. See [Oddize evidence](ODDIZE.md).

Novig paper testing verified signed discovery and authenticated book/private
subscriptions. ProphetX production testing verified authentication, Talaria
channel acknowledgements, and real market messages. These earlier read-only
checks sent no native orders. A later Novig Paper play-money order filled and
reconciled, as described in [the Paper probe](NOVIG_PAPER_PROBE.md). See
[venue evidence and limits](VENUE_PROTOCOLS.md).

Offline tests validate documented request construction, parsing, authentication
signing, failure handling, and synthetic end-to-end behavior; they are not real
exchange certification. Real observations without reviewed identity/settlement
mappings remain quarantined and cannot become tradable reference signals.

Public Kalshi discovery/books were verified on both sandbox and production REST
hosts. A separate check of the primary personal production account succeeded on
October 20, 2026 PDT: authenticated balance and API-key identity/scopes reads,
orders/positions inventory reconciliation (both empty), and authenticated
WebSocket `subscribed` plus `orderbook_snapshot` messages. Local evidence:
`data/kalshi-connection.json`, checked at 20:18:55 PDT / October 21 03:18:55 UTC.

The observed key scopes were `read` and `write::trade`, without transfer scope.
The returned location attestation expires October 27, 2026 at 8:15:55 PM PDT;
this is an expiring account observation, not an authorization to start this
application's trading. No native orders or cancellations were sent, and the
trade gate remained false. A later GET/subscription-only check at 04:13:21 UTC
confirmed all three private channels, reconciled spendable cash and private-state
synchronization (see VALIDATION.md). Genuine order lifecycle testing, reviewed
mappings/fees and settlement certification still block a Kalshi launch.

The personal Polymarket US retail connection also passed read-only production
checks on October 20, 2026 at 20:19:57 PDT: signed balance access returned one
row, public discovery returned 25 markets and a book, and authenticated market
and private sockets delivered `marketData` and `orderSubscriptionSnapshot`.
The observed stream reported zero errors and zero unknown messages. Local
evidence is `data/polymarket-us-connection.json` (October 21, 03:19:57 UTC).
This is the personal US API, not the international wallet/CLOB service. The
monitor client is read-only: `execution_supported: false`, `reconciled: false`, and
zero native orders sent. Private stream delivery does not establish complete
account-risk reconciliation or permission to trade through this application.

Missing credentials never silently trigger mock data. The separately named
synthetic plugin runs in demo/paper mode and all its prices/fills are labeled.

## Requested operator limits

The saved operator policy is quarter Kelly with $5 as one reporting unit, no
fixed dollar stake cap, no daily-loss stop, and one active/unsettled event or
player across accounts. The earlier $5-per-bet policy is superseded. Native cash-only caps
require verified spendable cash for that exact account. A reviewed launch portfolio
now supports one account per venue, with a cross-venue process lock and one durable
risk ledger. Every selected account must pass its own release checks, while cash
cannot be pooled between accounts. See [operator limits](OPERATOR_LIMITS.md).

The October 20 21:22 PDT read-only account check confirmed production Kalshi cash
and ProphetX's reported balance. Polymarket US returned a balance but omitted its
source-change clock; available-cash/account certification remains incomplete.
The saved Novig key returned production HTTP401 and sandbox success. Its funded
production account was subsequently accessed with a newly created management
key: signed production reads verified $25.00 primary-wallet cash, zero trading
subaccounts and zero production trading keys. The website's $75.00 portfolio
total is not assumed to be spendable API cash. Production trading still needs a
separate trading wallet/key and account-bound validation; the sandbox runtime
credentials remain unchanged. No account is labelled unfunded merely because
its sandbox is empty. `scripts/check_accounts.py` repeats
these explicit environment checks without sending orders or transferring money.

The owner subsequently completed wallet-opening and $25 funding confirmations
in the [Novig setup helper](NOVIG_SETUP.md). A scoped transfer GET confirmed
`Applied`; signed production trading-account reads confirmed the $25 balance
and an empty order/position inventory. The private signer remains protected
outside the repository, and the separate production pair is now saved locally.
The Novig monitor switched explicitly to production and streamed ten discovered
markets with no reported errors. Paper credentials remain preserved; trading
automation remains off and live release readiness remains false. Read access
and a completed wallet transfer do not certify order/fill/settlement behavior.

ProphetX now has local private connection/revision fencing, heartbeat-gap
invalidation and exact comparison with complete REST history. Its reporting
watermark must cover the start of the account probe; a heartbeat alone cannot
establish inventory. This path has offline tests, not a native lifecycle certificate.

## Measured local validation

Current Windows test totals and coverage are recorded in [VALIDATION.md](VALIDATION.md).
The TypeScript/Vite build and actual paused native Windows startup have also been tested. All automated provider/venue protocol exchanges use controlled
responses; real connection checks above were separate read-only observations.

An isolated 15-second synthetic run measured 2,190 internal calculations:
median **0.232 ms**, p95 **0.529 ms**, p99 **0.720 ms**. Local
receipt-to-calculation measured median **11.187 ms**, p95 **25.998 ms**,
p99 **36.001 ms**. Nine paper orders were routed; four had fills. Seven
simulated fill events had median submission-to-fill delay **2,124.501 ms**;
that sample is too small for p95/p99. These are local synthetic measurements,
not sportsbook-to-client latency, native fill speed or evidence of profit.
See [the validation record](VALIDATION.md).

Signal history starts with this release. Past displayed decisions are not
retroactively represented as a complete saved track record. No real verified
settlements or real profitable trading history have been established. See
[grading assumptions and safeguards](SIGNAL_TRACKING.md).

## Deliberate safeguards

Live account permissions and geographic eligibility cannot be verified offline.
The current native common-interface bridges require explicit account evidence,
verified mapping/fees and one-use risk authorization. The native strategy and
router use a separate exact-decimal ledger and share the central position guards.
`EV_LIVE_ENABLED` cannot bypass the launch profile, synchronized private account,
explicit monetary limits or genuine account-bound lifecycle evidence. Native
automation remains off at startup. See [GO_LIVE.md](GO_LIVE.md).

The built-in reference model never treats unknown timestamps as current. A
required stale reference produces NO SIGNAL. Execution rejects sequence gaps,
stale books, changed identities, unverified fees, mismatched settlement and
incompatible contract payout/quantity conventions.

Future plugins require adapters, authentication and tests; “universal” describes
stable extension seams, not automatic compatibility with every unknown protocol.

