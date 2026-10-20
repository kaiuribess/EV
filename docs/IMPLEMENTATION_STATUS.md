# Implementation status

This is a working demo/paper platform and an extensible integration foundation.
It is **not a completed production autonomous trading release**.

| Area | Shipped behavior | Remaining work |
|---|---|---|
| Core | Python 3.12 asyncio, typed immutable Pydantic contracts, versioned bus, plugin registry/DI, entry points, schema/config migrations | Process isolation for untrusted plugins; deployment-grade operational supervision |
| Inputs | Parlay REST/WS, Owls REST, OddsPapi v4 REST, generic REST/SSE template, deterministic simulation/replay | Paid-account connectivity; exhaustive catalog/page coverage; provider-specific new shapes |
| Identity | Exact canonical identities, verified aliases and source signatures, quarantine, player/line/rule rejection | Reviewed mappings for real markets and official athlete/team IDs; no guessed alias dataset |
| Pricing | Proportional and power devig, weights/requirements/age/disagreement, bookmaker dedup, best-offer EV and size curves | Calibrated real-market weights; validated push/different-line models; native live fee feeds |
| Strategies | Positive EV, independent strategy interface, required input and market metadata | Arbitrage, cross-exchange multi-leg execution, middles, steam and ML strategies |
| Paper OMS | Central risk, durable intent, limit price, partial fills, cancel, unknown-state freeze, duplicate protection, restart, simulated settlement | Real queue position, native private fills, exchange-specific fee settlement and fractional units |
| Kalshi | Current documented native clients and guarded common-interface bridge | Account/sandbox testing, verified mappings/fees, fractional fill handling, central native router |
| Novig | v3 signed REST/WS, native order operations/private state, cent units | Account/paper testing, cent-contract accounting and standardized private fills in common OMS |
| ProphetX | Native auth, Talaria event/prop subscriptions, v4 orders/cancel/private state helpers | Sandbox testing, USD-stake/odds accounting and asynchronous matching integration in common OMS |
| Dashboard | Six responsive screens, market detail, references/age/depth/history, metadata config, paper controls, events | Browser screenshot QA unavailable on this host; no native-account order controls |
| Analytics | Processing/receipt/durable-intent/ack/fill timings, count/fill rate, opportunity duration, paper positions/settlements | Live slippage calibration, CLV from verified closing data, production returns and richer group reports |
| Storage | Async batched WAL SQLite audit, configurations, orders and fill IDs | Retention/compaction policy for long unattended runs; database upgrade migrations beyond version 1 |
| Windows | Setup/demo/paper/test/stop PowerShell scripts, local built dashboard | Always-on operation requires an awake powered PC or a separately deployed host |

## Connection truth

No paid provider, native sandbox, or live trading account was authenticated during
implementation. No real order was submitted. Offline tests validate documented
request construction, parsing, authentication signing, failure handling and
synthetic end-to-end behavior; they are not real exchange certification.

Public, unauthenticated Kalshi event/market discovery and book snapshots were
successfully fetched from both sandbox and production REST hosts. This verifies
only those public read operations; it does not validate private trading access.

Missing credentials never silently trigger mock data. The separately named
synthetic plugin runs in demo/paper mode and all its prices/fills are labeled.

## Deliberate safeguards

Live account permissions and geographic eligibility cannot be verified offline.
The current native common-interface bridges require explicit account evidence,
verified mapping/fees and one-use risk authorization. The shipped central router
uses the paper execution plugin only. `EV_LIVE_ENABLED` is reserved configuration,
not a bypass; the production route rejects attempts.

The built-in reference model never treats unknown timestamps as current. A
required stale reference produces NO SIGNAL. Execution rejects sequence gaps,
stale books, changed identities, unverified fees, mismatched settlement and
incompatible contract payout/quantity conventions.

Future plugins require adapters, authentication and tests; “universal” describes
stable extension seams, not automatic compatibility with every unknown protocol.

