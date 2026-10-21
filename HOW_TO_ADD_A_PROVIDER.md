# How to add an input, strategy, or execution plugin

The engine does not import API endpoints or provider-specific payload fields.
Transport adapters live in `backend/evplatform/integrations`; canonical contracts,
pricing, risk and execution lifecycle live separately.

## 1. Register a package without changing the trading core

Implement a `Plugin` subclass, or the more specific `InputPlugin`,
`StrategyPlugin`, or `ExecutionVenue`. In a separate Python package:

```toml
[project.entry-points."evplatform.plugins"]
my_feed = "my_feed.plugin:MyFeed"
```

Install it in the local environment:

```powershell
.\.venv\Scripts\python.exe -m pip install -e C:\path\to\my-feed
```

Restart the application. Entry-point imports that fail are isolated in
`registry.discovery_errors`. Incompatible contract versions are rejected.
Dependency declarations are checked for missing plugins/cycles; valid plugins
start in dependency order. The new manifest automatically appears in Provider
Center or the relevant strategy/execution section.

Plugin metadata must include a stable name/version/kind/contract version,
actual supported sports/markets or discovery capability, input/output schema
versions, transport/authentication needs, streaming/polling support, quotas,
configuration JSON schema, health checks, permissions, dependencies, and official
documentation. Do not declare an unsupported operation as implemented.

The dependency-injection context supplies an event bus, schema registry, validated
configuration and explicitly provisioned services. Strategy context deliberately
does not contain an order router or credentials. Python plugins are trusted code;
permissions are declared capabilities, not OS process isolation.

## 2. Add a sportsbook REST feed

Read its current official docs first. `integrations/templates.py` includes a
`GenericRestTemplate`; [the transport guide](docs/PROVIDER_TEMPLATE_GUIDE.md)
shows field mapping and SSE framing.

Keep the following inside the new package:

- Authentication and credential lookup from backend environment/secret store.
- URLs, pagination, rate limits and billing units.
- Status/coverage restrictions, exact native odds format and timestamp units.
- Native snapshot completeness and delta/removal semantics.
- Source IDs and verified translation rules.

Implement only supported lifecycle/discovery/snapshot methods. Unsupported
operations raise `UnsupportedOperation`. Use HTTPX with a bounded timeout and
injected `MockTransport` for offline tests. Honor server throttling as well as
local minute/session budgets. Never log full credential-bearing request URLs.

Use exact explicit mappings into `Market` and `Quote`. For example, these fields
are separate concepts:

```json
{
  "native_event_id": "provider_event_123",
  "canonical_event_id": "verified:event_abc",
  "native_player_id": "athlete_456",
  "canonical_player_id": "verified:player_xyz",
  "source_price_time_field": "selection.bookmakerChangedAt",
  "provider_publish_time_field": "selection.changedAt",
  "timestamp_unit": "ISO-8601",
  "native_decimal_odds_field": "selection.price"
}
```

This example describes a mapping; it is not a generic claim about any provider's
actual fields. `VerifiedProviderMapper` supports reviewed signatures through
`data/verified_mappings.json`. Do not guess mappings from display names.

Publish:

```python
await context.bus.publish(Envelope(
    schema_name="odds.quote", schema_version=1,
    source=self.manifest.name, payload=canonical_quote,
))
```

Register a canonical `sports.market` first. A future timestamp, unknown market,
unverified alias, wrong threshold or incompatible settlement rule stays quarantined.

## 3. Add WebSocket feeds

Use the maintained asynchronous `websockets` package. Implement upgrade
authentication, entitlement errors, subscriptions, heartbeat/watchdog handling,
reconnect/backoff, snapshot/delta ordering and sequence semantics exactly as
documented. Do not invent a sequence guarantee if the feed has none.

A reconnect invalidates affected quotes/books immediately. Rebuild native books
from a fresh snapshot before publishing executable liquidity. A heartbeat proves
connection liveness, not individual quote freshness. Do not overwrite a quote's
source observation timestamp with the frame time or reconnect time.

Preserve partial-snapshot limits. A missing row in a truncated snapshot is not
proof that the market was removed. Parlay's adapter and tests illustrate these
distinctions. New native exchange streams must use their own order-book state
implementation before publishing a complete `exchange.book`.

## 4. Add Server-Sent Events

`integrations/templates.py` provides HTTPX SSE framing with multiline `data`,
event IDs and `Last-Event-ID`. Supply the real URL and documented authentication.
Provider-specific resumability and heartbeat semantics remain the adapter's
responsibility. Invalidate state on gaps where resumability is unproven.

The rest of the application consumes the normalized events, so changing from
polling to SSE does not change pricing or dashboard code.

## 5. Add scores, projections, injuries, or unusual markets

Do not put non-odds data into `Quote`.

```python
class InjuryReport(Contract):
    event_id: str
    player_id: str
    availability: str
    observed_at: datetime
    source: str

context.schemas.register("player.injury", 1, InjuryReport)
```

Then publish `Envelope(schema_name="player.injury", payload=report, ...)`.
Strategies/observers subscribe to that schema independently. Existing schemas
include `sports.game_state` and `player.projection`.

Use a type-specific schema or a namespaced `Market.extensions` object for new
information. Do not weaken existing binary-contract invariants. Different
thresholds, different participation requirements, overtime treatment, periods,
currencies, and payout units are different contracts.

The built-in positive-EV and paper sizing paths accept validated $1 USD binary
contracts without push probability. A new unusual contract needs its own pricing,
sizing, fee and settlement policies, not a flag that bypasses validation.

## 6. Add a strategy or pricing model

Implement `StrategyPlugin.evaluate(event, view) -> list[Candidate]` and
`revalidate(candidate, view) -> Candidate`. The second method must be synchronous,
memory-only, and safe immediately before transmission. Strategy inputs and
market/sport types are declared in the manifest.

The view exposes current canonical markets, quotes/books and typed inputs.
Subscribe directly to the bus for new non-odds categories in `start()`. Do
statistical/model-service IO in an input plugin; publish its validated outputs
for the strategy to consume.

Emit standardized candidates. The central engine checks identity, current
executable price, source requirements, sequence, freshness, fee-adjusted edge,
positions, duplicate protection and kill-switch state. Strategies do not submit
orders. A one-use risk authorization is required by execution adapters.

`positive_ev` is implemented. Arbitrage, cross-exchange legs, middles, steam and
AI models are extension work. Multi-leg strategies additionally need coordinated
risk reservations and explicit partial-leg exposure handling before execution.
Incompatible settlement contracts cannot create executable arbitrage.

## 7. Add an execution venue

Implement the common `ExecutionVenue.submit`, `cancel`, `reconcile`, health and
capability contract. Native transport clients may retain venue-specific methods
behind this bridge. See `venues/bridge.py` and the
[native protocol guide](docs/VENUE_PROTOCOLS.md).

Declare actual order types, price/quantity units, tick precision, minimums, maker
and taker fees, cancellation/replacement behavior, private state streams,
client-ID semantics and sandbox support. No engine code should assume all
venues support replacement or exchange deduplication.

The common OMS owns durable intent, central risk approval, order state and
exposure. Consume the authority token once. Do not retry an ambiguous request.
An acknowledgement is not a fill; private fills and account reconciliation must
establish actual filled quantities. Cancellation can race with a fill.

Verify account permissions/eligibility through official mechanisms. A preflight
boolean is not evidence by itself. Never bypass geographical access controls.
Populate fee rules from verified venue-specific metadata, with validity times.

Current native bridges are intentionally not wired into unattended routing:
the shipped integer $1 OMS cannot faithfully represent every fractional Kalshi,
Novig cent-contract, or ProphetX USD-stake execution. Extend the quantity and
settlement domain with new schema versions, migrate persisted data, and prove
reconciliation before enabling that route. Raw clients implement the documented
native operations for account-backed integration work.

## 8. Version and migrate

Schema registration keys are `(name, version)`. Existing versions are immutable.
Register a new model and explicit transform:

```python
schemas.register("player.injury", 2, InjuryReportV2)
schemas.add_migration("player.injury", 1, migrate_v1_to_v2)
```

`schemas.migrate(name, source_version, target_version, data)` validates the output.
Do not implicitly downgrade. Configuration migrations are keyed by plugin name
and original version in `registry.config_migrations`; validate the final Pydantic
config model. SQLite currently has storage schema version 1 and refuses unknown
versions rather than guessing at a migration.

Retain old fixture tests and add new version fixtures. A new plugin must not
change previously normalized identities or reinterpret persisted stake units.

## 9. Test every adapter offline

Use HTTPX MockTransport, injectable WebSocket transports and immutable fixtures.
Test:

- Authentication shape and redaction, missing/invalid credentials, permissions.
- All real supported market types, absent fields, complete outcome arity.
- Source vs publication vs local clocks; no source time means no fresh quote.
- Heartbeats without price updates, old/future/out-of-order values.
- Suspensions/removals, incomplete snapshots, disconnects, sequence gaps.
- Local and provider quotas, 429 cooldown, reconnection limits.
- Exact team/player/event/threshold/period/settlement equivalence.
- Contract quantities, partial fills, cancellation races and unknown outcomes.
- Durable deduplication, restart, risk limits and kill-switch races.
- Replay determinism and unrelated-plugin failure isolation.

Run `.\run_tests.ps1`. Report mocked tests as offline protocol tests. Only a
successful authorized account-backed operation establishes real connectivity.

## 10. Review live identity and settlement evidence

Run the read-only review against the local app before authoring mappings:

```powershell
.\.venv\Scripts\python.exe scripts\review_market_readiness.py --url http://127.0.0.1:8001 --output data\market-review.json
```

The output file must be new. The command only issues local GET requests, reports
original source timestamps, suspensions, missing opposing prices and missing
identity/settlement facts, and never changes mappings or trading permissions.
Use `--snapshot saved-quarantine.json` for offline review, `--max-source-age 5`
for the review threshold, and `--max-rows 3000` to bound collection. The quarantine
is a bounded, changing window; a multi-page collection is explicitly non-atomic.
Old quotes do not become current because the report or provider response is new.

Optional `--comparisons contracts.json` accepts `{"version":1,"comparisons":[
{"left":{...},"right":{...}}]}` with explicit `ContractReview` records from
`core/market_review.py`. Unknown facts stay null. Compare period, line, overtime,
postponement, voids, and payout conditions using current official contract
documents. A structurally equal comparison is still not external verification.
The report never infers team aliases, independent bookmaker counts or approval.

REST discovery preserves a supplied `commence_time` in observation context as
`scheduled_start`; a changed supplied start time changes the review signature.
Do not reuse an old signature or silently treat a postponed event as equivalent.

## 11. Read audit history through the storage decoder

New audit JSON records of at least 4 KiB use lossless zlib compression when it
reduces their size. Small records and existing records stay plain JSON TEXT.
Compressed records are SQLite BLOBs with an `EVPAUDIT` version marker, original
byte length and SHA-256 checksum. Every event ID, timestamp and payload remains
intact. Orders, fills and configuration tables retain their existing encoding.

Use `await storage.decode_event_data(row[0])` before parsing `events.data`.
This reads old TEXT and new BLOB records; it rejects unknown versions, corrupt
checksums, oversized expansion and trailing compressed streams. Compression and
decompression run in worker threads. The maximum compressed source size is
16 MiB; larger records remain unmodified TEXT rather than being truncated.
Existing database content is not rewritten or removed. Compression reduces
future growth but does not impose retention or reclaim existing disk space.

