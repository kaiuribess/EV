# Provider protocols and verification status

Checked against official documentation on 2026-10-20. The installed transports
are real HTTPX/WebSocket implementations; automated tests use controlled responses.
Parlay REST was separately authenticated against the real service: sport discovery
and NBA moneylines from DraftKings, FanDuel, and Pinnacle succeeded. The current
paid WebSocket connection was verified on October 20, 2026 local time: the server
reported Business access, NBA became ready, and real game-line and player-prop
offers continued arriving. This replaces the earlier entitlement-denied status.
Synthetic demo data comes from the separate demo plugin, never these adapters.

## Implemented clients

| Plugin ID | Transport | Authentication | Native sport selector |
|---|---|---|---|
| `parlay_rest` | REST polling | Backend `PARLAY_API_KEY`, `X-API-Key` header | e.g. `basketball_nba` |
| `parlay_ws` | WebSocket, REST discovery methods | Same key, header on upgrade | One sport per socket |
| `owls_rest` | REST polling | Backend `OWLS_API_KEY`, Bearer header | e.g. `nba` |
| `oddspapi_rest` | v4 REST polling | Backend `ODDSPAPI_API_KEY`, documented query auth | Numeric sport ID as a string |

Plugins are disabled by default. A missing credential returns `missing_credentials`
without opening a network connection. No credential is part of a public
configuration model. Authentication refusals stop automatic requests; local
budgets, observed quotas, bounded reconnect delay, and HTTP circuit breakers
limit repeated failures. HTTP request duration is measured as HTTP roundtrip
time, not sportsbook-to-client latency.

## ParlayAPI

REST implements `/v1/sports`, per-sport `/events`, `/odds`, `/props`, and
`/props/markets`, plus the market and bookmaker catalogs. Price requests specify
decimal odds; the prop parser uses the documented American fields. Completeness
and pagination headers are preserved. [Official REST reference](https://parlay-api.com/docs)

The WebSocket connects to `/v1/ws/odds/{sport_key}` and consumes `connected`,
`initial_state`, `odds_update`, and `heartbeat`. It uses full rows (`diff=false`).
Business, Enterprise, or Scale access is required. Close codes for invalid
authentication, missing entitlement, exhausted credits, or rejected filters
stop reconnection. Transient failures invalidate cached quotes and restart from
a new initial state. [Official WebSocket reference](https://parlay-api.com/docs/websocket)

The local `account_tier` setting never grants entitlement. Health separately
reports the current server-provided tier and push settings, and clears them
when that connection ends. The observed current connection reported `tier: business`,
`push_mode: coalesced`, and `min_push_interval_s: 1.0`, with authenticated streaming,
`basketball_nba` ready, no pending snapshot, and zero reported errors. Successive
checks received changing game and prop offers; source mappings remained
unreviewed, so all such markets stayed quarantined and `mapped_quotes` stayed zero.
Automation remained off, the kill switch active, and native orders zero. Local
evidence: `data/parlay-paid-access.json`, October 20 local / October 21 UTC.
The advertised push interval is not a measurement of source-to-client latency.

The documented row limit is 1–1000. A support-enabled trial may differ from the dashboard's base plan; the server handshake is the
authority. A trial ending in a streaming refusal does not cause an automatic
plan purchase or a silent change to synthetic data.

Keys are shown once. Parlay's documented **Reveal & rotate** action immediately
mints a replacement and leaves the old secret usable for 14 days. Preserve a
saved key when a trial was enabled specifically on it; trial inheritance after
rotation is not documented. [Authentication](https://parlay-api.com/docs#authentication),
[streaming trial](https://parlay-api.com/docs/websocket/quickstart)

Only an explicit complete, untruncated current-board flag establishes a
replacement baseline. A bounded recent-rows snapshot is not a full inventory.
Frame send time, retained row observation time, and local receipt time stay
separate. Numeric row `last_update` uses milliseconds; numeric envelope
`timestamp` uses seconds. [Machine-readable AsyncAPI](https://parlay-api.com/v1/asyncapi.json)

The extractor accepts the documented legacy American `price` as well as explicit
`price_american` / `price_decimal` fields and preserves explicit outcome labels.
Contradictory price representations are rejected. Source timestamps can be ISO
strings or documented epoch milliseconds; receipt time never replaces them.
Exact same-source player/line Over/Under rows can be structurally paired, but
still require a verified player, event, period, and settlement mapping before
becoming canonical quotes. [Player-prop formats](https://parlay-api.com/docs/websocket/player-props)

Implementation limits:

- Broad initial results may omit markets. No lossless removal history or exactly-once
  cursor is assumed.
- The REST prop method supports explicit `offset` and preserves continuation
  headers; the default polling loop samples its configured page rather than
  pretending to exhaust all pages.
- The strict mapper understands paired props, paired moneylines, totals with
  paired over/under prices, and line-free individual moneyline outcomes. A
  provider-specific extension is still needed for unfamiliar raw spread layouts
  or side-dependent partial line messages.
- It never guesses a missing draw outcome or missing opposing prop price.

## Owls Insight

The client uses `/api/v1/{sport}/odds`, `/events`, `/props`, and
`/api/v1/docs/meta`. Available books and ignored query parameters remain in
response metadata. The source catalog distinguishes game odds from props;
Circa game-line access does not imply Circa props. Monthly/minute quota headers
are consumed, including the millisecond minute reset. [Official API reference](https://owlsinsight.com/docs)

Per-book or response publication time does not establish an individual quote's
age. The mapper prefers a market's own timestamp; when absent it leaves source
time unknown and the pricing engine must produce no signal. The per-prop
`lastUpdate` is retained separately from board metadata. Unknown market
structure is not silently treated as over/under. [Official OpenAPI schemas](https://owlsinsight.com/openapi.json)

Implementation limits: REST only; native Owls WebSocket and v2 source-specific
endpoints are extension work. The published metadata API was not accessible
from the development network without passing the provider's access controls;
no bypass was attempted. Entitled account coverage remains unverified.

## OddsPapi

The v4 client implements sports, bookmaker and market catalogs, scheduled
fixture discovery, account capability discovery, and fixture odds. Fixture
discovery is slower than quote polling. It respects a conservative two-second
per-endpoint cooldown. [Fixture discovery](https://oddspapi.io/en/docs/get-fixtures),
[odds protocol](https://oddspapi.io/en/docs/get-odds)

An account response contains an API key, so only a whitelist of capability and
usage information leaves `fetch_account()`. Live data is accepted only when
explicitly configured and each requested bookmaker has verified account and
catalog coverage. Null coverage stays unknown. [Account capabilities](https://oddspapi.io/en/docs/get-account),
[bookmaker coverage](https://oddspapi.io/en/docs/get-bookmakers)

`bookmakerChangedAt` is distinct from the provider's `changedAt`; fixture
`updatedAt` never dates a quote. Missing bookmaker clocks produce no current
reference. The market catalog is needed to verify period, threshold, player and
outcome IDs. [Market catalog](https://oddspapi.io/en/docs/get-markets)

Quota accounting reserves requests before transmission, because failure
responses can consume the provider allowance. Account usage refresh is
unmetered locally, while a local minute limit still protects against runaway
loops. [Quota policy](https://oddspapi.io/en/docs/requests-and-quota)

Implementation limits: v5 B2B and native OddsPapi WebSocket are not implemented.
Every returned bookmaker must be verified independently for live/prop coverage.
This feed is a reference-data adapter, not an execution connector.

## Authenticated Kalshi monitor check

On October 20, 2026 PDT, the primary personal production account passed genuine
read-only balance, API-key identity/scopes, orders and positions checks. Inventory
reconciliation found no open orders or positions. The authenticated WebSocket
then delivered `subscribed` and `orderbook_snapshot` messages. This extends the
earlier public REST discovery/book checks; local evidence is
`data/kalshi-connection.json` (October 21, 03:18:55 UTC).

Observed scopes were `read` and `write::trade`, without transfer scope. The
location attestation expires October 27, 2026 at 8:15:55 PM PDT; it must be
rechecked when needed and does not authorize application automation. The trade
gate remained false, and no native order or cancellation was submitted. A market
book snapshot does not establish private order/fill synchronization, a tested
order lifecycle, reviewed fee/rule mappings, or settlement readiness. Those
launch requirements remain blocked. See [venue protocols](VENUE_PROTOCOLS.md)
and [launch checks](GO_LIVE.md).

## Polymarket US personal retail monitor

The separate read-only client targets the personal US retail API. Public market
discovery and books use `https://gateway.polymarket.us`; signed account reads
and both WebSockets use `api.polymarket.us`. Authentication uses the personal
US API key ID and Ed25519 signing secret in protected backend environment
variables `POLYMARKET_US_API_KEY_ID` and `POLYMARKET_US_SECRET_KEY`. These are API
credentials, not an international wallet private key. No international CLOB or
institutional API compatibility is implied. [Official authentication](https://docs.polymarket.us/api-reference/authentication)

Actual production checks succeeded on October 20, 2026 at 20:19:57 PDT. Signed
`GET /v1/account/balances` returned one balance row; unsigned discovery returned
25 markets and a REST book. Authenticated `/v1/ws/markets` delivered `marketData`,
and `/v1/ws/private` delivered `orderSubscriptionSnapshot`. The inspected
connection reported zero errors and zero unknown frames. Evidence is local in
`data/polymarket-us-connection.json` (October 21, 03:19:57 UTC).

Subscriptions use the official SDK/detail-page camelCase keys and string enums;
actual returned envelopes used `requestId` and `subscriptionType`. The adapter
also recognizes documented snake_case aliases but rejects conflicting aliases.
A market subscription needs explicit discovered slugs; local configuration is
limited to 50. Discovery is bounded and does not claim full catalog coverage.
A market heartbeat never refreshes cached book observation times.
[Market stream](https://docs.polymarket.us/api-reference/websocket/markets),
[private stream](https://docs.polymarket.us/api-reference/websocket/private)

This is an observer, not an execution venue. Its capability report explicitly
sets `execution_supported: false` and `reconciled: false`. No order or
cancellation was submitted. It does not publish canonical pricing signals or
support central account-risk reconciliation, fee validation, settlement or
native execution. A received private snapshot is delivery evidence only; it
must not satisfy the launch gate's complete order/position reconciliation.
No personal retail sandbox is assumed. Successful connection checks do not
authorize automation, establish trading permissions, or demonstrate profit.

## Verified mapping files

`VerifiedProviderMapper.from_file("data/verified_mappings.json")` loads:

```json
{
  "version": 1,
  "mappings": []
}
```

With an empty file, real observations are collected and quarantined, not
traded. Quarantine entries include a stable source signature and inspectable
identity fields. A mapping records:

- The exact source signature, including provider, bookmaker, native event,
  market, player, threshold, period, outcome points, and settlement context.
- A complete canonical `Market`, including a verified `Settlement`.
- A one-to-one map from every actual source outcome to every canonical outcome.
- The canonical bookmaker identity, `verified: true`, and verification evidence.

Use the returned signature; do not construct one from a display label.
Market metadata and book rules must be checked against the venue's equivalent
contract before registering a rule. A name suffix, source timestamp, or
provider-supplied event hash does not establish athlete or settlement identity.

Rules are validated as `VerifiedOfferMapping`. A threshold, period, player,
event, or settlement-context change produces a different signature. Incomplete
sets, midpoint-based quotes, invalid prices, and unknown rules cannot become
active references. Disconnects and removal events suspend cached mapped quotes
without advancing their original source timestamps.

The mapper is callable:

```python
from evplatform.integrations import PROVIDER_FACTORIES
from evplatform.integrations.normalization import VerifiedProviderMapper

mapper = VerifiedProviderMapper.from_file("data/verified_mappings.json")
provider = PROVIDER_FACTORIES["parlay_ws"]({
    "enabled": True,
    "sports": ["basketball_nba"],
    "bookmakers": ["pinnacle"],
    "markets": ["h2h", "player_points"],
    "stream_kinds": ["game", "prop"],
    "account_tier": "business"
})
# Only the backend reads PARLAY_API_KEY from its environment.
# The application's input bridge owns lifecycle and publishes the resulting
# (Market, Quote) pairs to the normalized event bus.
```

For a new or replacement WebSocket key, place it in backend `.env` as
`PARLAY_API_KEY=...`, restart the backend so it reads the environment, and
configure/enable `parlay_ws` in Provider Center. Select actual sport and market
keys from discovery. For game lines plus props, include both `game` and `prop`
in `stream_kinds` and explicitly include the desired prop market keys in
`markets`; leaving the default game-market filter excludes props. Set
`account_tier` to the entitled `business`, `enterprise`, or `scale` value.
`unknown` attempts authentication and lets the server verify entitlement;
`free`, `starter`, and `pro` disable live streaming locally. Tier selection
never bypasses the provider's handshake checks.

Initially keep automatic trading off, inspect source timestamps and quarantine
identities, and install only reviewed mappings in the local mapping file.
Disable/re-enable the provider after editing that file to reload it. Enabling
a data source does not enable live execution.

See [provider extension guide](PROVIDER_TEMPLATE_GUIDE.md) for transport seams
and offline conformance checks.

## Tests

Run from the repository root:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_providers.py -q --basetemp=.pytest-providers-temp
```

Tests exercise documented URLs/authentication, key redaction, absent credentials,
quota enforcement, access denial, stream framing, incomplete snapshots,
heartbeat separation, unknown outcome sets, numeric timestamp units, source
age, bad payloads, mismatched player/line/period, suspension/removal, per-outcome
aggregation, disconnect recovery, and replay repeatability. The fixture values
are synthetic; successful tests are not evidence of live connectivity.
