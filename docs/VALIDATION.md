# Validation — 2026-10-20

## Automated checks

Executed on this Windows PC with Python 3.12.0 and Node 24.13.1:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\run_tests.ps1
```

Result: **190 tests passed**, **81% Python statement coverage** (3,240 of 4,003
statements). Python test duration: 7.90 seconds with coverage enabled.
TypeScript checking and Vite production build also passed. The single warning
is an upstream Starlette/httpx TestClient deprecation warning.

The provider-specific suite contains 45 cases, and the native venue suite
contains 69. The remainder exercise normalization, pricing, event schemas,
replay, risk, orders, recovery, API controls, independent strategies and
settlement. Twenty tests were written in an independent review pass.

Coverage is line/statement coverage, not a claim of exhaustive behavior or live
correctness. Native transports are primarily validated with mocked responses.

Specific safety regressions verified:

- Separate sportsbook observations never become duplicate bookmaker evidence.
- Missing/stale/future source times cannot become current by receipt or heartbeat.
- Wrong player/threshold/settlement or unrelated fair-value market is rejected.
- Gaps invalidate books until a snapshot; stale signals expire when updates stop.
- Independent strategies still pass centralized executable-data and risk gates.
- A disabled execution plugin blocks submission.
- Concurrent duplicate candidates reserve and transmit only once.
- A kill switch activated during durable intent prevents transmission.
- Unknown outcomes retain exposure; they are never blindly retried.
- Malformed/wrong-venue/above-limit fills are not booked.
- Durable fill IDs prevent duplicate accounting after restart.
- Cancellation races preserve filled exposure.
- Settlement is idempotent, durable, and rejects unknown outcomes.
- A post-settlement fill requires reconciliation.
- Automatic safety halts persist through restart.

## Windows launch tests

Executed `setup_windows.ps1`, `start_demo.ps1`, `start_paper.ps1 -AutoTrade`,
`stop_local.ps1`, and `run_tests.ps1` on this PC.

Demo smoke test: HTTP ready, 9 markets, 10 plugins, automation disabled.
Paper smoke test: 18 orders, 8 orders with fills, kill switch false.
Graceful shutdown completed. Test instances used isolated SQLite files and port
8002. The final local demo uses port 8001 with automatic trading disabled.

Actual HTTP/WebSocket checks established:

- Dashboard HTML and bundled assets returned 200.
- Two state frames changed over approximately 513 ms.
- Nine markets included three distinct player props and 36 source observations.
- Plugin configuration, settings, paper controls and emergency stop round-tripped.

Visual browser screenshot/click QA was unavailable: the supported browser control
tool reported no available surfaces. Responsive CSS, TypeScript and built assets
were inspected, but a pixel-perfect rendered result is not claimed.

## Measured internal latency

Command:

```powershell
.\.venv\Scripts\python.exe scripts\benchmark.py --seconds 15
```

Final standalone benchmark (no concurrent pytest run; another local demo process
was active). Synthetic dataset: 9 markets, 36 quotes, 27 books.

| Measurement | Samples | Median | p95 | p99 |
|---|---:|---:|---:|---:|
| Internal event processing | 2,190 | 0.163 ms | 0.342 ms | 0.472 ms |
| Local receipt to calculation | 1,800 | 8.003 ms | 15.002 ms | 19.998 ms |
| Durable order-intent barrier | 18 | 0.755 ms | Insufficient samples | Insufficient samples |
| Paper acknowledgement | 18 | 1.000 ms | Insufficient samples | Insufficient samples |
| Simulated fill confirmation | 19 | 1,019.917 ms | Insufficient samples | Insufficient samples |

The run produced 18 orders, 8 orders with fills, and 146 filled synthetic
contracts. At the final snapshot, 18 offers had positive EV and 9 negative EV.
No event-bus drops or safety halt occurred. Zero realized P&L reflects
unsettled demo markets, not a break-even live trading result.

p95 requires at least 20 samples and p99 at least 100. Processing metrics exclude
provider transport latency; receipt-to-calculation includes local queueing.
Simulated fill delay is largely the deterministic simulator schedule, not a
prediction of exchange matching latency.

The full report is `artifacts/benchmark.json`; coverage details are
`data/coverage.json`. These generated runtime artifacts are Git-ignored.

## Genuine network checks

Unauthenticated **Kalshi public REST** checks succeeded against both documented
sandbox and production hosts: event discovery (one item), market discovery
(one item), and an order-book snapshot for the returned market.
Results are recorded in `artifacts/public_connectivity.json`.

These were read-only public requests. No authenticated WebSocket, account
balance, private orders/fills, sandbox order, or production order was tested.
Parlay, Owls, OddsPapi, Novig and ProphetX await credentials/account access.
Owls public metadata was blocked by provider access controls; no bypass was used.

## Remaining verification

Account-backed sandbox tests, real mapping review, native fee/payout/quantity
validation and private fill reconciliation are required before any production
automation. See [implementation status](IMPLEMENTATION_STATUS.md).

An automatic approval review blocked deletion of temporary downloaded research
files. That folder remains local and Git-ignored; it is not imported at runtime.

