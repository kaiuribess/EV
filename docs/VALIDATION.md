# Validation - 2026-10-20

## Latest automated checks

Executed on this Windows PC with Python 3.12.0:

```powershell
.\run_tests.ps1
```

**839 tests passed in 29.01 seconds with coverage enabled.** Backend statement
coverage is **86.49%** (7,142 of 8,258 statements). TypeScript checking and the Vite
production build passed. One upstream Starlette/httpx TestClient deprecation
warning remains. Coverage measures executed statements, not exchange certification.

All order, cancellation, fill, account and settlement exchanges in automated tests
use controlled offline responses. No test submitted a real native order.

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
cancellation was sent, and `trade_gate_enabled` remained false. Private-state
synchronization and settlement certification remain blocked; this successful
monitor check does not certify either capability.

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
FMV, remediation and shared-position cash allocation remain unsupported. Full settlement coverage, Kalshi private synchronization and ProphetX complete
inventory/actual fees remain unsupported. An explicitly reviewed narrow Novig
IOC settlement policy can satisfy capability admission; it still requires genuine
account-bound lifecycle and settlement evidence, and stops on exceptional results. Supplying keys alone cannot
satisfy these requirements. See [GO_LIVE.md](GO_LIVE.md).
