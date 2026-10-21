# Validation - 2026-10-20

## Latest automated checks

Executed on this Windows PC with Python 3.12.0:

```powershell
.\run_tests.ps1
```

**755 tests passed in 21.73 seconds with coverage enabled.** Backend statement
coverage is **85.74%** (6,735 of 7,855 statements). TypeScript checking and the Vite
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
lifecycle correctness. Parlay's replacement key still lacks verified streaming
entitlement. See [current integration scope](IMPLEMENTATION_STATUS.md),
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
