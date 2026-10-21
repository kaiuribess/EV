# Actual provider freshness evidence

The read-only capture in `data/freshness-evidence-20261021-final.json` ran from **2026-10-21 06:40:00 to 06:40:19 UTC** (October 20 locally). Both configured sources authenticated. It observed 495 offer deliveries across 20 provider/book/market groups. This is a bounded sample, not a coverage or long-run availability certification.

The final report digest, excluding its own digest field, is `fe90bbb8777057d865e5cb65a9066df8ec87849a2006f7d9d3966a43a663413f`.

## What the capture established

Parlay's Business socket delivered a baseline, three updates and two heartbeats. The server declared one-second coalescing. No sampled row supplied the `last_observed`/`last_update_type` pair needed by the strict observation contract. Initial prop data carried truncation evidence. Some rows contained a boolean `available`, but the published schema does not define it as an authoritative per-outcome active-status guarantee. Unknown status remains unknown; no parser shortcut was introduced. [Parlay AsyncAPI](https://parlay-api.com/v1/asyncapi.json), [observation and completeness documentation](https://parlay-api.com/docs).

Oddize returned 57 grouped NBA moneyline observations with per-side `ts` and `timestamp` clocks, and no current-status fields. Its HTTP round trip was **237 ms**, but the delivered observations were much older:

| Book code | Grouped observations | Median delivered observation age |
| --- | ---: | ---: |
| circa | 10 | 8.49 hours |
| dk | 25 | 10.00 hours |
| fd | 13 | 41.81 hours |
| pin | 9 | 91.17 hours |

These clocks describe provider observations, not the time a sportsbook first published a price. Old observation evidence cannot certify that a price is still offered. The final capture used one metered snapshot and a free usage read, leaving 1,480 credits. Three short captures were used while correcting collector diagnostics: three total metered Oddize snapshots, with no account changes or trade calls. [Oddize field definitions](https://oddize.com/api/v1/openapi.json).

No sampled market had verified active status. No validated freshness rule was created. All TTL proposal values in the final capture are null because it lacks a sufficient series of independently verified observations.

## Repeating the measurement

From the repository in PowerShell:

```powershell
.\.venv\Scripts\python.exe scripts\collect_freshness_evidence.py --seconds 20 --output data\freshness-next.json
```

Use a new output path. Existing environment credentials stay in the backend. This opens one temporary read-only Parlay socket and performs a free Oddize usage check followed by at most one 1-credit NBA moneyline snapshot, only when remaining credits are confirmed. It does not modify the running program's subscriptions or settings. Missing credentials, transport errors and incomplete captures are reported explicitly.

The report stores field-type counts and timing summaries, not raw credential-bearing payloads or player identities. Duplicate deliveries do not become independent observations; provider-send delay counts frames rather than every price inside a frame. Median is available for small samples; p95 needs 20 samples and p99 needs 100. These are reporting thresholds, not statistical guarantees.

With at least 100 verified observations and 20 same-offer intervals, the collector can show a **measurement-only** candidate of `ceil((observation-age p99 + observation-interval p95) / 1000)` seconds. It always marks that candidate unvalidated and non-installable. Cadence measurements cannot determine the maximum safe quote age: quote validity, suspensions, market phase and price movement still require independent evidence.

## Provider requirements still needed

For Parlay, request a documented quote-bound observation clock and units for each intended sportsbook/market, including unchanged observations; exact semantics of `available`; reliable suspension/removal handling; and the supported complete baseline/reconnect contract for those selections. A source/fixture heartbeat or a non-truncated response alone is insufficient. Narrowing the requested books/markets may reduce truncation, but does not repair absent observation/status semantics.

For Oddize, request the supported current-market-status/withdrawal contract and whether a distinct quote-verification clock exists beyond retained `ts` observations. Confirm actual per-book prop coverage and source cadence before setting any fast execution policy. The flat event-props route preserves thresholds; the grouped endpoint's untyped alternate-line data remains discovery-only.

After these contracts are supported, collect representative sessions, bind source identities to reviewed canonical markets and settlement rules, and independently assess candidate age limits. This report cannot substitute for those steps or native order lifecycle validation.

## Follow-up captures, October 21 local time

The 20-second single-book capture `data/freshness-single-book-20261021.json` received 272 offers across five groups: 263 Parlay Pinnacle prop rows, all carrying truncation evidence, and nine Oddize Pinnacle moneyline observations. Six Oddize observations had complete opposing-side clocks; three had unsynchronized outcomes. None established current market status. One Oddize credit was used (1,478 remaining afterward). These samples do not establish a verified +EV candidate.

A second 20-second Parlay-only check explicitly requested NBA `h2h`, `kinds=game`, and Pinnacle/FanDuel/DraftKings. It authenticated and received an initial frame, two update frames and three heartbeats, but yielded zero extracted offers in that bounded window. It made no Oddize calls. This is not evidence that no such market exists outside the sampled scope/window. Capture: `data/freshness-game-moneyline-20261021.json`.

The collector now accepts `--parlay-markets`, `--parlay-kinds game` (or `prop`) and `--skip-oddize`, so narrowing a socket diagnosis does not consume another provider's credits. Example:

```powershell
.\.venv\Scripts\python.exe scripts\collect_freshness_evidence.py --seconds 20 --parlay-books pinnacle,fanduel,draftkings --parlay-markets h2h --parlay-kinds game --skip-oddize --output data\freshness-moneyline-next.json
```

### Questions requiring provider answers

Parlay: For Business NBA Pinnacle/FanDuel/DraftKings game moneylines and ordinary points props, which documented response fields bind a collector observation and an active/suspended/withdrawn state to the exact outcome, threshold and displayed price? Are unchanged offers reverified? Which request yields a complete baseline for this scope, and how are removals and reconnects represented? A narrowed Pinnacle prop sample remained truncated; the game-moneyline window yielded no extracted offers. Please specify exact supported parameters and whether additional entitlement is required, without sending credentials.

Oddize: For NBA Pinnacle moneylines, what establishes that each retained price is still available now, rather than merely the last stored `ts`/`timestamp` observation? Which field or endpoint reports suspensions/withdrawals? How are unchanged quotes reverified and opposing-side observations synchronized? Please document source cadence and actual status semantics; delivery latency cannot substitute for observation age.

These questions are prepared for review and have not been sent to either provider.

## Moneyline retest and parser correction, October 21 at 00:23–00:26 Pacific

The repeated NBA `h2h` test did receive moneyline rows. Two initial 30-second windows extracted 14 and 13 FanDuel observations, respectively, but rejected batches containing a named-outcome game row with the same American price in both `price` and `over_price`. The generic mixed-shape check in our adapter was too strict for this observed source container. This was an application parsing issue; the earlier empty bounded sample was not proof that Parlay lacked moneylines.

The parser now accepts that duplicate only for an explicit `kind=game`, `market_key=h2h`, named team, matching exact price, absent opposing `under_price`, absent line/total/player, and no conflicting moneyline/outcome fields. It preserves one named outcome, never invents the other side, and leaves observation/status verification false. Other mixed shapes or conflicting prices are still rejected. The documented named-outcome `team`/American-`price` representation is described in [Parlay's OddsRow schema](https://parlay-api.com/v1/asyncapi.json); the duplicate field was observed directly in the authenticated feed.

The post-fix 30-second capture (`data/parlay-retest-game-30-20261021T072607.json`) extracted **45 observations: 23 FanDuel and 22 DraftKings, zero extraction errors**. These are delivered observations, not 45 independent markets or bets. No Pinnacle row was observed in that window. The capture retained the same 30-second server age filter and made no Oddize calls. No validated quote-status guarantee, canonical mapping or trading authorization was added. 197 relevant offline regression tests passed. Any support draft describing moneylines as wholly unavailable should be updated with this finding.
