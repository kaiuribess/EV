# Live readiness audit — October 21, 2026

## October 22, 11:17 PDT Kalshi NBA outcome and quote review

The read-only Kalshi NBA crosswalk now checks both sides of each native game
winner listing against its public `rules_primary` winner, opponent, and original
game-date wording, including the venue-specific Los Angeles team labels. It pairs that explicit winner with a
two-sided source moneyline outcome only when the source event, participants,
market family, period and outcome set pass the narrow review checks. The
OddsPapi path additionally requires its NBA full-game market 111, the actual
zero-handicap/overtime-inclusive catalog entry, and documented home/away
outcome IDs 111/112. This is an outcome-label lead, **not** settlement
equivalence or an executable mapping. [Kalshi market listing](https://external-api.kalshi.com/trade-api/v2/markets/KXNBAGAME-26OCT22CLEPHI-PHI),
[OddsPapi NBA market identifiers](https://oddspapi.io/blog/genius-sports-alternative/).

The bounded production read-only run at `data/kalshi-nba-crosswalk-20261022-v8.json`
found 39 fixture leads with both native winner rules verified and 75 exact
two-sided source moneyline leads: 61 Oddize, 12 Parlay game WebSocket and 2
OddsPapi. It found **zero** leads with complete current quote evidence. All 75
failed the 120-second quote-observation limit; 73 lacked verified current
market status. The two with affirmative status were OddsPapi FanDuel (no
complete bookmaker timestamp pair) and Pinnacle (last bookmaker change more
than an hour before this review). The source quarantine reached its bounded
capacity, and 12 native events had conflicting provider start times. These
counts describe the captured review window, not universal provider quality or
future opportunities. No mapping, profile, order or live permission was issued.

OddsPapi documents `bookmakerChangedAt` as the last bookmaker-reported
**change** and `changedAt` as its system's last recorded change. Its active
fixture/market/selection flags can establish reported availability, but an
unchanged price's old change clock does not become a recent sportsbook
observation merely because a REST response arrived. The review keeps those
facts separate. [OddsPapi odds response fields](https://oddspapi.io/en/docs/get-odds).
Parlay likewise says its retained `last_update` does not advance on unchanged
upstream polls and its heartbeat does not verify each quote.
[Parlay WebSocket timing and snapshot semantics](https://www.parlay-api.com/docs/websocket).
The documented Kalshi fair-price cancellation contingency still differs from
ordinary sportsbook refunds; matching winner labels do not remove that payout
difference.

Reproduce the review without sending an order:

```powershell
.\.venv\Scripts\python.exe scripts\review_kalshi_nba.py --output data\kalshi-nba-crosswalk-new.json
```

## October 22, 09:56 PDT Kalshi NBA fixture and contract review

The public Kalshi `KXNBAGAME` catalog exposed both sides of tonight's
Cleveland–Philadelphia and Denver–Oklahoma City game-winner events. For both,
`occurrence_datetime` equaled `expected_expiration_time` and was three hours
**after** the [official NBA tipoff](https://www.nba.com/schedule?cal=October&region=1):
23:00 versus 02:00 UTC for Cleveland–Philadelphia, and 01:30 versus 04:30 UTC
for Denver–Oklahoma City. It is therefore not used as kickoff or as evidence
that a sportsbook event's scheduled-start clock disagrees with the exchange.
The exact [Philadelphia listing](https://external-api.kalshi.com/trade-api/v2/markets/KXNBAGAME-26OCT22CLEPHI-PHI)
also says a postponed game that fails to start within 48 hours resolves to a
**fair price**, rather than an ordinary automatic refund. That contingency
must be compared against each reference book's actual rules before pricing.
For example, [bet365's Iowa basketball rules](https://help.ia.bet365.com/s/en-us/sportsrules/basketball)
say an abandoned game is generally *no action* if it does not finish within
48 hours. That is a concrete rules difference; the book's applicable
jurisdiction and exact wager terms still need separate review.

The new read-only `scripts/review_kalshi_nba.py` crosswalk uses known NBA team
codes and the Eastern game date to find candidate fixtures while retaining
the distinct source tipoff and Kalshi occurrence clocks. It records exact
listing URLs and hashes of the public rule text. The code handles Kalshi's
observed `PHX`, `BKN` and `CHA` spellings separately from Oddize's aliases.
A bounded run found 38 provider/event candidate pairs, 4 conflicting
source-event identities and 12 native events with more than two minutes of
cross-provider start-time disagreement; it issued **zero** mappings,
settlement certificates or order permissions.
The local report is `data/kalshi-nba-crosswalk-20261022-v3.json`. Windows setup
now installs `tzdata` so the Eastern date comparison works across DST.

## October 22, 09:52 PDT live-input review

The running paper monitor had synthetic data off, both Parlay and Oddize
inputs connected, 95 markets and 277 quotes, and zero eligible opportunities.
A bounded review of 2,998 quarantined observations found 55 within the
five-second source-age limit but **zero** with the combined quote-observation,
active-status, exact-settlement and identity evidence needed to authorize a
native market mapping. The review counted 0 authorized mappings. It is a
sample of the most recent 3,000 rows, not proof every future source row will
fail. The production strategy was enabled for monitoring; automatic orders
remain off and the emergency stop remains on. No real trade was submitted.
The local review artifact is `data/review-market-20261022-0952.json`.

Parlay's current [WebSocket contract](https://www.parlay-api.com/docs/websocket)
explicitly says recent-row snapshots, heartbeats and per-book verification do
not certify each DraftKings/FanDuel outcome's current status or reliable
withdrawal. This is a provider-contract limit, not a missing API key or a
reason to mark a retained price as actively offered.

## October 22, 09:43–09:45 PDT ProphetX history backfill

The authenticated ProphetX production collector now requests the documented
maximum 1,000 order-history rows per page and a 60-day created-at window.
Runtime full-history reconciliation uses contiguous 60-day windows rather
than seven-day windows; the transport already enforces the published
one-request/second account-key pace. The offline pagination/window tests pass.
This improves backfill completeness and time to account verification, without
turning a bounded report into a claim about account inception.

A new protected, read-only 60-day capture at 16:44 UTC confirmed the signed
production account identity, zero returned order and trade rows, one completed
$50 deposit, and zero durable native orders in the production SQLite ledger.
The deposit's own wallet fields show a $0.10 balance **before** that deposit;
the sampled period does not explain that earlier amount or establish whether
the account has any loss exposure credit, which ProphetX says is not reported
by `get_balance`. The account's funded-cash-only and complete-history review
therefore remains incomplete. No order, cancellation or transfer was sent.
Protected capture: `data/prophetx-history-20261022-0948/`.
[Official order-history guidance](https://docs.prophetx.co/docs/direct-link-order-lifecycle),
[wallet and credit definitions](https://docs.prophetx.co/docs/wallets).

An authenticated wallet-query diagnostic at 16:48 UTC confirmed that the
60-day bounded transaction request accepts the documented 1,000-row page
size and returns the same single deposit. An unfiltered request with a
100-row page size returned HTTP 400; other rapid exploratory requests hit
HTTP 429, so this does **not** establish that full wallet history is
available unfiltered to this account. The protected capture remains bounded
and does not certify the unexplained earlier $0.10.

## October 22, 09:35–09:39 PDT production recheck

The running production monitor's authenticated, read-only account probes now
agree with the corrected local diagnostic for Novig: its separate management
key verified the trading key and signer, the $25.00 wallet, and zero open
orders/positions. A fresh Kalshi probe also succeeded after the earlier
rate-limited attempts: it verified $50.00 available cash, trading-key scope,
zero open orders/positions, and an account location attestation expiring
October 27, 2026 PDT. ProphetX still lacks its complete-history funded-cash
review; Polymarket US still lacks cash-only account and paid-settlement proof.
All four probe results explicitly report `usable_for_execution=false`.

The paused Kalshi pilot helper was corrected to require the emergency stop to
remain **on** while it prepares limits. It then completed against the running
production workspace: $1 per-order and total exposure caps, one lifetime
native attempt, quarter-Kelly sizing, $5 units, and overlapping event/player
blocks. The limits are a saved operator draft and current risk settings, not a
reviewed launch profile. A direct operator-requested activation call returned
HTTP 409 (`Native execution requires explicit enablement and all launch checks
passing`). Afterward, `auto_trade=false`, `kill_switch=true`, zero native
orders, zero approved native market mappings, zero real reference markets and
`safe_to_arm=false` were confirmed. No real order was placed.

A live read-only NBA event crosswalk found three fixture candidates, including
Oddize and Parlay observations pointing to the same scheduled
Oklahoma City–Denver ProphetX event. Its bounded moneyline inspection found
six Oddize bookmaker labels for that event and exact native strike IDs. Each
sampled Oddize quote lacked verified active-market status; the displayed
source observations ranged from roughly 20 minutes to 16 hours old when reviewed.
The native catalog was truncated at the bounded review limit. These are useful
discovery leads, but neither an approved moneyline mapping nor a current
three-book fair value. The current [Parlay API contract](https://parlay-api.com/docs)
distinguishes source observation from price-change and delivery clocks; an
API key does not supply a missing quote timestamp or market status.

## October 22, 09:28–09:33 PDT follow-up

The workspace is now 2.53 GiB after guarded removal of old pytest and
benchmark artifacts; operational databases and account evidence were retained.
Lossless signal-snapshot compression, six-hour raw-detail retention and the
3 GiB per-database SQLite cap remain in force. The production workspace still
reported `safe_to_arm=false`, `auto_trade=false`, `kill_switch=true`, zero
reviewed launch profiles and zero verified native/source market mappings.

The read-only production account diagnostic now uses the separately configured
Novig management key as the runtime already does: the documented
`GET /v3/keys/{id}` confirms the trading key's scope and the actual local
signer's public-key fingerprint. At 16:28 UTC, the wallet reported $25.00,
zero resting orders/positions, reconciled inventory, and verified cash for
that wallet. This is neither geographic eligibility nor a launch profile or
an order-to-settlement test. Kalshi's $50 balance read succeeded, but its
inventory/key-metadata probe returned HTTP 429 twice; no current Kalshi cash
or permission verification is claimed from those attempts. ProphetX reported
$50.10 with zero global exposure credit and zero matched/unmatched balances,
but its full financial history/identity review remains incomplete. Polymarket
US reported a $50 balance and $50 reservation; cash-only availability and
authoritative settlement remain unverified. These values are point-in-time
reads, not spendable pooled funds.

Parlay's credit-free coverage endpoints reported 14 NBA main-line games for
FanDuel and Pinnacle, plus NBA points rows from FanDuel and Pinnacle. NFL
receiving-yards coverage listed FanDuel, DraftKings and Pinnacle. A bounded
three-credit NFL props request immediately afterward actually returned 137
FanDuel and 125 DraftKings rows, with no Pinnacle row in that response, no
recent validated collector-observation clock and no bilateral FanDuel active
selection links. Coverage is not a quote, and these sampled rows cannot form
a strict three-book NFL fair value or an executable opportunity. The diagnostic
is repeatable with `scripts/probe_parlay_prop_status.py --sport
americanfootball_nfl --market player_rec_yds --books
pinnacle,fanduel,draftkings,betonline`. It reports counts only. No real order
or transfer was made. [Novig key contract](https://docs.novig.com/api/api-keys),
[Parlay coverage/prop contract](https://parlay-api.com/openapi.json),
[Kalshi 429 guidance](https://docs.kalshi.com/getting_started/rate_limits).

## October 22 current-state check

The production monitor was restarted on the tested build and remained paused:
`auto_trade=false`, `kill_switch=true`, with no reviewed launch profile or
source/native market mapping. The live-input paper workspace restarted with
synthetic odds off and simulated trading on. Its real-signal ledger had zero
eligible episodes; repeated `NO SIGNAL` evaluations are tracked but are not
profitable opportunities or placed orders. The latest full offline check passed
2,115 backend tests, 104 dashboard tests and the frontend build.

Four authenticated, read-only account diagnostics at 08:44 PDT returned:

| Venue | Available cash evidence | Inventory | Trading permission | Account/location limits |
| --- | ---: | --- | --- | --- |
| Kalshi | Verified in the read-only probe | 0 open orders, 0 positions; reconciled | Verified | A time-limited location attestation exists; no execution profile |
| Novig | Verified in the read-only probe | 0 open orders, 0 positions; reconciled | Verified | No location attestation in this probe; no execution profile |
| ProphetX | Unverified | Counts and reconciliation unverified | Unverified | Signed identity verified, funded-cash/inception review missing |
| Polymarket US | Unverified | 0 reported orders/positions, reconciliation unverified | Unverified | Retail account identity/cash and paid settlement join unresolved |

Each diagnostic returned `usable_for_execution=false`; the values are a
momentary account read, not permission to submit an order. Kalshi and Novig
cash balances are not pooled. The [quote evidence review](QUOTE_OBSERVATION_CONTRACT.md)
also now records a narrow FanDuel REST game-status improvement: 14 sampled
NBA moneylines had both active-status links, but none had a verified
quote-bound observation clock. This did not produce an eligible live reference
or remove the mapping, settlement and genuine lifecycle gates. The existing
database retention/compaction had reduced the workspace to about 8.6 GB,
below the requested 10 GB target.

The provider review now counts complete quote-clock evidence and verified
active status **on the same observation**, separately from the two individual
counts. In a bounded October 22 production review window, Parlay REST had
236 clock-verified observations and 46 status-verified observations among its
last 1,000 quarantined rows, but zero carried both complete clock and status
evidence. The window hit its cap and is not a census of all markets. Parlay's
streaming feeds and Oddize also had zero joint observations in their current
windows. This is a measured reason no quote was promoted to a strict live
reference, not a claim that either provider never supplies useful odds.

## Existing-key follow-up (02:42 UTC October 22)

Only the already configured ParlayAPI and Oddize credentials were used. Parlay's
documented credit-free `/props/coverage` and `/odds/coverage` checks now have
adapter methods and a local `scripts/check_parlay_coverage.py` diagnostic. The
NBA check reported five pregame games on the requested main-line board:
FanDuel at five, Pinnacle at three, Caesars and DraftKings at one each. Its
60-minute `player_points` coverage view listed 148 API-visible FanDuel rows,
five DraftKings rows and two Pinnacle rows. These are coverage counts, **not**
fresh active quote sets; `latest_update` is a price-change clock. A separate
metered two-minute-age props request immediately afterward returned zero rows.
Neither result supplied the three independent, complete, exact-market,
active-status-verified references needed by the proposed NBA prop profile.
The coverage script reserved zero credits. Coverage will change with the game
schedule; rerun the script for a current diagnostic. The full backend suite
passed **2,060 tests** after these changes.

Parlay's current contract can return a previously built props board with
`X-Parlay-Stale: props_scan_busy`. The HTTP transport now retains that header,
and normalization marks the resulting offer `stale_cached_board`, refusing to
mark its paired outcomes complete. This avoids treating a new HTTP receipt as
proof that a cached board is a current sportsbook offer. Focused offline tests
cover the header and the zero-credit discovery endpoints. The production
workspace still has no reviewed launch profile or eligible native candidate;
automatic real orders remain disabled.

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

One authenticated, metered `GET /v1/sports/basketball_nba/live` with
`include=normalized,verification` returned eight in-play events. In the
sampled response, the `is_current`/`verified_at` fields were on a bookmaker
block; the market had only `key`, `last_update`, and `outcomes`, and an outcome
had only `name` and `price`. No active/suspended field bound to an individual
outcome appeared. This matches Parlay's [specific verification caveat](https://parlay-api.com/docs):
the bookmaker heartbeat does not prove each retained outcome is available.
An in-play event filter therefore still cannot be promoted to a current quote
or live execution permission.

The read-only ProphetX market catalog review now exposes bounded public
selection labels and strike IDs, without executable prices or account data.
For the October 21 Lakers–Warriors listing, its full-game moneyline contained
two distinct named strikes. An exact-label candidate crosswalk found **12**
source-book rows in the retained review window: seven Oddize and five Parlay
REST. None supplied verified active quote status. The source quarantine was
capped and the 50-market native catalog response was truncated, so 12 is not
an exhaustive feed count. Selection `updated_at` values in this bounded live
sample were 19-digit numbers consistent with nanosecond Unix times, but the
published contract still does not specify a unit; the engine does not promote
that empirical observation into a verified execution clock. The report preserves
`market_mapping_verified=false`, `settlement_equivalence_verified=false`, and
`execution_authorized=false`. Re-run the current read-only review with:

```powershell
$env:PYTHONPATH = 'backend'
.\.venv\Scripts\python.exe scripts\review_event_crosswalk.py --url http://127.0.0.1:8002 --tournament-id 132 --event-id 1700099993
```

Parlay's current [OpenAPI contract](https://parlay-api.com/openapi.json) adds
`includeLinks=true` to `/v1/sports/{sport_key}/props`. It documents FanDuel
per-side links only when the stored native market was OPEN and runner ACTIVE.
The REST adapter now requests that metadata and treats **both** HTTPS side
links as active-status evidence only when the same two-sided prop row also has
an unambiguous, complete `last_observed` / `collector_observation` clock. The
links are not exposed in normalized quotes or sent to an order venue.
An authenticated NBA FanDuel sample (three credits) returned two Stephen
Curry rows, each with an Over link but no Under link and no collector clock;
a second bounded NBA query (three credits) returned no rows. Neither response
made a quote live-eligible. This rule applies only to the documented FanDuel
REST prop shape. Other books and main lines still lack equivalent verified
status evidence in the current feeds.

A further pregame NBA `player_points` probe for the exact Lakers–Warriors
fixture requested Pinnacle, FanDuel and DraftKings from Parlay (three credits).
It returned six FanDuel rows, no Pinnacle or DraftKings rows, zero complete
Over/Under pairs, zero bilateral links and zero collector clocks. This is
observed coverage for that fixture at that time, not a claim about every NBA
game. Oddize's documented event-props endpoint (two credits) returned 758
`player_pts` price rows from Caesars, FanDuel, BetRivers, DraftKings and BetMGM
for the same event, but its flat price records still lack an authoritative
active-status field. The configured Oddize key reported 1,407 of 1,500 monthly
credits before that probe; automatic all-event, high-frequency prop polling
would exhaust that allowance. No sampled row was promoted to a live reference.

At **02:00:27 UTC October 22**, a fresh bounded `player_points` Parlay REST
request with `includeLinks=true` returned 123 FanDuel rows and no Pinnacle,
DraftKings or Caesars rows in that request. None had both priced sides, a
recent documented collector-observation pair, or bilateral native links.
This is a time- and filter-specific observation, not a general coverage claim.
The diagnostic `python scripts/probe_parlay_prop_status.py` prints counts only
and consumes the documented three credits. The new Oddize event-context probe
above found actual bilateral NBA props, but did not supply the missing active
status; combining the feeds does not turn either source into an eligible live
reference.

After restarting both local workspaces on the tested build, the production
quarantine review examined 3,104 distinct valid observations from 3,110 rows.
**Zero** passed quote evidence and **zero** had verified market status; the
five-second diagnostic found 17 within-limit legacy source clocks, which are
not a quote-validity or provider-specific TTL result. No source/native mapping
or launch profile was approved. The local report is
`data/readiness-review-20261021-evening.json` (ignored by Git).

### Paper audit write-rate repair

The live-paper SQLite file had reached about **7.3 GB**. A read-only count
found 2,559,544 raw audit events; 1,314,208 were `signal.observation` records
whose stored payloads occupied about 4.4 GB. A sampled no-candidate record
repeated roughly 12 KB of pricing policy plus the current reference universe.
This was repeated evaluation, not 1.3 million independent trading signals.
The engine now records the initial `NO SIGNAL`, the first arrival or loss of
reference quotes or target order books, and a periodic no-candidate state at
most once per market per minute. A candidate episode and its subsequent
withdrawal are always recorded immediately; a pricing-policy replacement also
produces an immediate new snapshot. Every underlying quote/order-book input remains in the
audit log for replay. The coalesced evaluation count is exposed in metrics.
An integration test verifies 100 repeated no-candidate evaluations create one
durable signal observation and that a later candidate withdrawal closes its
episode. This change bounds future write rate; it does not delete or rewrite
historical audit data, so the existing database size remains large.

The live-paper latency panel also mixed retained quote age into its
`receipt_to_calculation_ms` series: a heartbeat can republish an unchanged
paper quote while preserving the original source receipt. That series now
excludes those derived paper events. A separate
`bus_publish_to_calculation_ms` series measures local queue/processing delay
for every market update. Source observation age stays on the quote and must
not be described as sportsbook-to-client or order-execution latency.

The paused production Kalshi monitor completed a fresh read-only inventory:
identity, $50.00 available cash, zero open orders/positions, trading permission
and the current location attestation were reported verified. The local pilot
still limits a first attempt to $1, one native order, quarter-Kelly sizing and
a $5 reporting unit. Novig's read-only probe verified identity/inventory but
not spendable cash or permission; ProphetX verified identity but not funded
cash/inventory; Polymarket US did not verify retail account identity or cash.
These account facts are time-limited and do not fix missing market mappings,
source quote status or actual order-to-settlement evidence.

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

### Oddize flat-prop event binding, October 21 evening PDT

Oddize's [current OpenAPI event schema](https://oddize.com/api/v1/openapi.json)
supplies `event_id`, away/home teams and scheduled start in `/odds/latest`, but
its flat `/events/{sport}/{event_id}/props` response supplies only the event ID,
sport and prop rows. The adapter now selects a bounded future event directly
from the same odds snapshot, rejects conflicting IDs, and carries that exact
event reference into flat-prop normalization. No cached team or start-time
guess is used. Its automated discovery default remains off and the configured
credit budget accounts for each enabled 2-credit prop request.

A read-only, three-credit maximum diagnostic at **2026-10-22 01:56:33 UTC**
used event `202610210LAL`: 2,245 in-scope flat rows from five observed books
formed 311 two-sided normalized offers; 252 had complete same-observation
clocks. The event teams/start matched the source snapshot. None carried a
verified active-market status, and the observed books included no Pinnacle or
Circa in that event's sampled props. This proves the event-context join and
some real two-sided feed coverage, not a current tradable quote, contract
equivalence, a profitable edge, or order readiness. Re-run the bounded probe
with `python scripts/probe_oddize_prop_event.py` when monitoring coverage.

### Optional status and observation feeds, October 21 evening PDT

The [OddsPapi v4 odds contract](https://oddspapi.io/en/docs/get-odds) provides
explicit fixture, bookmaker, market and selection availability flags. The
adapter now requires all positive flags before marking market status verified;
missing fields fail closed. Its bookmaker and provider change timestamps still
do not verify when an unchanged individual price was last observed. No
OddsPapi credential is configured, so this was validated with controlled
responses only.

The [Owls Pinnacle realtime contract](https://owlsinsight.com/docs) separately
documents `markets[].last_seen` as a confirmation of a specific main market.
An optional `owls_pinnacle_realtime` REST plugin now preserves that observation
apart from `last_update` price changes. It accepts only an actual Pinnacle
response with a healthy feed, a future scheduled event, an unsuspended main
market, and a nonfuture market confirmation; a PS3838 fallback or failed poll
invalidates cached quotes immediately. The new plugin is independently
registered and disabled by default. There is no `OWLS_API_KEY` in this local
installation, no paid entitlement was obtained, and no genuine quote or market
was certified through this path. It also does not resolve cross-venue
settlement equivalence or the first real order-to-settlement test.

One additional three-credit, read-only Parlay NBA props request at
**2026-10-22 02:26:58 UTC** omitted the market filter and asked for the four
priority books. It returned zero rows at that instant. Thus the earlier
`player_points` sample cannot be extrapolated to current availability, and
there is still no complete current FanDuel pair with both native links and a
recent collector observation. The diagnostic is repeatable with
`python scripts/probe_parlay_prop_status.py --all-markets`; a zero-row result
does not prove future or other-sport coverage is absent.

The optional Owls NBA realtime plugin is now preconfigured in the paper and
production workspaces for 30-second REST polls, but remains disabled in both.
The preparation script made only local control requests: it read no credential,
spent no provider quota and armed no native orders. An MVP-or-higher Owls key,
actual response sampling, a validated exact-source TTL and additional
independent active references would still be needed before a strict live
profile can be reviewed.

After the current build restarted, the paper workspace remained sample-free,
with authenticated Parlay raw streaming and Oddize REST connected. Across its
bounded 10,000-observation in-process sample at about **02:34 UTC**,
bus-publication-to-calculation measured **11.001 ms median, 39.422 ms p95,
50.024 ms p99**. These are internal processing times, not source-to-client,
submission, acknowledgment or fill delay; there were zero paper/native orders
and zero fills in this restart window. Production still had zero launch
profiles, verified native markets and qualifying real reference markets, so
automatic native trading remained paused.

### Fresh launch check, October 22 at 17:04 UTC

The production control API still reported `safe_to_arm=false`, with no reviewed
launch profiles, verified native market mappings, qualifying real reference
markets or native candidates. Live execution was configured but automatic
trading remained off and the kill switch remained on. No real order was sent.

A read-only account diagnostic confirmed a current Novig trading identity,
reconciled inventory and cash-available evidence. Kalshi's balance read worked,
but its follow-up inventory call returned HTTP 429; ProphetX returned only a
balance read, and the Polymarket US inventory did not verify trading identity or
cash availability. These observations do not authorize an order on any venue.

Two authenticated, three-credit Parlay NBA props requests used the documented
`includeLinks=true` parameter. The `player_points` request returned 103 rows
from FanDuel and Pinnacle, including 22 two-sided rows. The broader props
request returned 6,848 rows from DraftKings, FanDuel and Pinnacle across 67
market types, including 489 two-sided rows. **Neither request returned a recent
`last_observed`/`collector_observation` pair or a FanDuel row with both native
side links.** Thus none met the application's quote-level status and freshness
requirements. These counts are snapshots of a particular time and request scope,
not a claim that those books never supply valid quotes. The current
[Parlay WebSocket contract](https://api.parlay-api.com/docs/websocket) also says
complete board snapshots and book heartbeats do not verify each retained
outcome; there is no defensible status shortcut from a recent message alone.

### Kalshi account diagnostic repair, October 22

The first read-only Kalshi account check above hit HTTP 429 at its second
`/portfolio/balance` read. The diagnostic had fetched a balance independently
and then called the complete inventory probe, which fetched it again. Kalshi's
[current rate-limit contract](https://docs.kalshi.com/getting_started/rate_limits)
puts this endpoint in a separate no-burst bucket with a one-per-second Basic
limit. The diagnostic now derives its balance display from the inventory probe's
single authenticated balance read, retaining an allowlisted route label on an
HTTP error. It never retries a write.

After this change, an authenticated production read-only check completed with
verified Kalshi key/account identity, trading permission, available cash and
reconciled empty inventory. The diagnostic explicitly remains **not** a launch
certificate. There are still no approved real market mappings, qualifying
reference quotes or reviewed launch profiles, so automatic orders remain off.

### Kalshi pilot limits and official NBA fixture check, October 22

The paused production pilot preparation completed after the Kalshi diagnostic
repair. The running risk configuration now has a $1 maximum order stake, one
lifetime native attempt, quarter-Kelly sizing, a $5 unit and overlapping-event
blocks. The limits also exist as a saved operator draft. They are **not** bound
to a launch profile, and the production switch remains `auto_trade=false` with
the kill switch on. The native order ledger remained empty.

The [NBA's current Cleveland schedule](https://www.nba.com/team/1610612739/schedule)
lists Cleveland at Philadelphia on October 22 at 7:00 p.m. ET; the
[Oklahoma City schedule](https://www.nba.com/team/1610612760/schedule)
lists Denver at Oklahoma City at 9:30 p.m. ET. Those independent league listings
agree with both the Oddize and Parlay game-stream starts in a fresh bounded
Kalshi crosswalk: 23:00 UTC on October 22 and 01:30 UTC on October 23. Each
fixture has one candidate source event per provider and no start conflict.
The corresponding Kalshi market
`occurrence_datetime` values are three hours later, matching expected market
expiration rather than tipoff. This corroborates two fixture leads only;
there is still no approved exact market/settlement mapping, current active
reference quote or profitable executable offer for a pilot order.
The local read-only artifact is `data/kalshi-nba-crosswalk-20261022-v4.json`;
its 40 candidates are not executable mappings.

### Live Parlay NBA game-line status probe, October 22 at 17:18 UTC

A bounded, authenticated REST probe requested NBA full-game moneylines from
Pinnacle, FanDuel and DraftKings, using `includeLinks=true` and the production
normalizer's existing evidence rules. It returned 30 two-sided offers: 14
FanDuel, 14 Pinnacle and 2 DraftKings. All 14 FanDuel offers had the documented
per-side native links that indicate an OPEN market and ACTIVE runners at the
stored observation. **Zero** offers carried a quote-bound collector observation
inside the 120-second limit, so **zero** had both active-status and current
observation evidence. The diagnostic spent at most one requested REST credit
and printed no odds, event IDs, links or credentials. Repeat with
`python scripts/probe_parlay_game_status.py --sport basketball_nba --books pinnacle,fanduel,draftkings --markets h2h`.

The [Parlay game-line documentation](https://api.parlay-api.com/docs) describes
`verified_at` as a book/fixture poll heartbeat rather than a market or outcome
verification. It also says a complete REST/stream baseline and recent frame do
not prove that unchanged or withdrawn offers remain active. The FanDuel links
therefore close only the status half of the evidence check, not the independent
quote-clock half. Production still reports zero approved native markets, zero
qualifying real reference markets and no launch profile; automatic real orders
remain off with the kill switch on.

### NBA settlement and alternate feed review, October 22

The current [Kalshi basketball winner terms](https://assets.kalshi.com/contract_terms/BASKETBALLGAMEWIN.pdf)
count overtime for an unspecified full-game period, but can resolve a canceled,
abandoned or substantially delayed game to an exchange-determined last fair
price. The [Pinnacle basketball rules](https://www.pinnacle.com/en/future/betting-rules/)
also count overtime on the game period, yet void an NBA game-period bet if fewer
than 43 minutes are completed; its general rules additionally void a fixture
not started within 12 hours and handle forfeits differently. Thus a normal
final-winner probability is a useful comparison, but the two instruments do
**not** have identical cash outcomes in those contingencies. No reviewed
settlement transformation or tail-risk estimate exists for this series.

The current [Oddize OpenAPI](https://oddize.com/api/v1/openapi.json) lists
per-price timestamps but no per-market or per-selection active-status field.
The existing Oddize feed cannot by itself close the source-status gate.
[OddsPapi v4](https://oddspapi.io/en/docs/get-odds) documents the missing
market/selection flags and separate bookmaker/provider change clocks. Its
adapter now admits a recently **changed**, fully timed and active two-sided
quote; it never renews an unchanged price from REST arrival. Offline tests
passed, but this installation has no OddsPapi key, so there is no authenticated
live response proving its exact NBA coverage or whether its free account
includes the required books. The available in-app browser reached the public
sign-in form, with no signed-in account to use.

### Authenticated free-tier OddsPapi check, October 22 at 17:30 UTC

After the owner supplied a key, the authenticated, unmetered
[`/v4/account`](https://oddspapi.io/en/docs/get-account) read reported 250
monthly requests available, NBA sport 11 and access to Pinnacle, FanDuel,
DraftKings, Circa and other named reference books. It reported
`has_live_odds=false` for those books; this free account is not an in-play
reference source. Its prop entitlement flags do not promise actual player or
statistic coverage.

A bounded, metered NBA pregame sample from tournament 132 returned two future
fixtures. One fixture contained 1,459 normalized offers across the four named
books. The actual response contained affirmative active-market fields for
hundreds of two-sided prices, but **zero** offers met the current 120-second
bookmaker-change and active-status test. FanDuel provided no complete
`bookmakerChangedAt` pair in this fixture. DraftKings and Pinnacle had some
bookmaker clocks, but their youngest complete pair was older than 120 seconds.
These are observations of this fixture and time, not universal coverage facts.
The provider's `changedAt` may be recent even when a bookmaker clock is absent;
the adapter retains that distinction and does not relabel REST receipt as a new
source observation. [Odds timestamp definitions](https://oddspapi.io/en/docs/get-odds)

Both local workspaces now authenticate the OddsPapi feed with pregame NBA-only
discovery, at most two fixture requests per daily cycle, a 100-request local
session budget, and a stop when 150 provider requests remain. The production
workspace remains `auto_trade=false`, kill switch on, zero native orders, zero
reviewed launch profiles, zero verified native market mappings and zero
qualifying real reference markets. The free source adds real research data and
reviewable status evidence; it does not authorize a real bet.

The next read-only Kalshi crosswalk joined OddsPapi's NBA sport 11/tournament
132 discovery names and scheduled time to the same fixture ID in its odds
response. It found one additional Denver-at-Oklahoma City event lead from
OddsPapi. The latest review report has 29 provider/event candidates, including
14 source-start conflicts: Parlay game streams list that matchup at 9:40 p.m.
ET while OddsPapi and Oddize list 9:30 p.m. ET, matching the
[NBA's published Oklahoma City schedule](https://www.nba.com/team/1610612760/schedule).
Those are review leads, not approved market mappings; the report flags the
conflicts and retains the distinct Kalshi expiration time. Its local artifact
is `data/kalshi-nba-crosswalk-20261022-v5.json`.

### ProphetX production NBA market review, October 22 at 17:55 UTC

The existing authenticated ProphetX monitor was receiving native market frames.
Its local read-only discovery endpoint returned two current NBA fixtures and,
after filtering the complete market response to subtype `moneyline`, one active
full-game moneyline market per event. Each had two distinct team strike IDs.
The new `scripts/review_prophetx_nba.py` compares those public catalog identities
with quarantined sportsbook event records through that existing session. It does
not open another ProphetX login session or produce executable mappings.

The review now uses ProphetX's documented batched `get_multiple_markets`
request through the existing authenticated monitor. The bounded latest report
`data/prophetx-nba-crosswalk-20261022-v6.json` has three fixture/strike
leads and fourteen exact two-sided sportsbook moneyline leads. It rejects
partial batched responses and marks any unread catalog incomplete. The
OddsPapi-specific review accepts only its [documented NBA full-game market
111 and home/away outcomes 111/112](https://oddspapi.io/blog/genius-sports-alternative/)
along with the actual catalog's `result`, zero-handicap, overtime-inclusive
fields. This mapping remains a review lead, never an execution mapping.

Of those fourteen source leads, only two carried affirmative market-status
evidence (OddsPapi FanDuel and Pinnacle). FanDuel had no verified complete
quote observation; Pinnacle's last bookmaker change was about 65 minutes old
at the check. The other twelve came through Oddize, which supplies a price
observation clock but no verified active-market status; their observations
were also old. There was no qualifying current independent reference set.
The source quarantine filled to capacity for at least one provider, and a
Parlay schedule conflict remains in the report, so the lead inventory is not
a complete, approved game mapping.

The [ProphetX NBA moneyline contract filing](https://www.cftc.gov/filings/ptc/ptc0710269372.pdf)
provides a primary rule source: the ordinary full-event winner includes
overtime, a tied result after overtime is voided, official NBA results are the
primary settlement reference, and the exchange retains discretion when official
results are unavailable. This family-level filing is not a market-specific
settlement certificate. The catalog does not provide reviewed exception-rule
equivalence to the sportsbook references, a validated source observation clock
for every executable level, or funded-cash-only account history coverage.
Read-only production account probes confirmed spendable cash and zero reported
open orders/positions on Kalshi and Novig, but no reviewed launch profile or
native market mapping exists. ProphetX authenticated without proving full
funded-cash inventory; Polymarket US still lacks a documented stable cash
account identity/scope for the current retail API. The live order switch
remains off; zero real orders were submitted.
