# Oddize REST integration

This independent input plugin uses Oddize's official v1 REST API at
`https://oddize.com`. It is discovered as `oddize_rest` in Provider Center.
Importing the plugin does not contact Oddize. It is disabled by default and
missing credentials do not affect other providers.

Official references checked October 20, 2026:

- [API overview, credit costs and pricing](https://oddize.com/odds-api)
- [Interactive API documentation](https://oddize.com/api/v1/docs)
- [OpenAPI 3 contract](https://oddize.com/api/v1/openapi.json)

Authenticated validation on October 20, 2026 successfully read the Developer API
account and catalogs: six sports, 20 books and seven main/alternate offer types.
One real NBA snapshot consumed one credit and produced 310 extracted source
offers across 17 returned books, including `pin` and `circa`; all 310 had valid
observation clocks. The reported balance was 1,499 credits after that request.
This confirms those particular REST requests and parsing. It does not establish
freshness of every price, live player props, executable liquidity, canonical
settlement equivalence, or profitable trades. Real observations remain subject
to verified mapping and freshness checks.

## Credentials and account access

The Oddize **site subscription and API plan are separate**. Confirm the account
email, then create an API key in **Settings > API**. The full key is shown once;
store it in the ignored local `.env` file as `ODDIZE_API_KEY`. Never paste it in
Provider Center, source code, or a Git commit. Only the backend sends it, using
the documented `X-API-Key` header. No account purchase or plan upgrade is made.

`GET /api/v1/account/usage` establishes the actual account allowance. The plugin
only exposes allowlisted plan, quota and period fields; it discards the key
label/prefix and other account fields from monitoring output.

## Supported transport and data

The published API documents REST polling. It does not document a WebSocket or
SSE protocol; the plugin's capability declaration therefore reports polling
true and streaming false. It supports:

- Free catalog discovery through `/api/v1/sports`, `/books`, `/offer-types`, and
  `/sports/{sport}/teams`.
- Current snapshots through `/api/v1/odds/latest` at **1 credit per request**.
- Individual event snapshots through `/api/v1/events/{sport}/{event_id}` at
  **1 credit**. Event IDs containing slashes are preserved exactly after path
  segment validation.
- Flat event player/team props through the event `/props` route at **2 credits**.
  Explicit discovered `prop_types` are required because the endpoint otherwise
  defaults to team totals.
- Optional `updated_since` delta polling for the documented Starter/Pro/Elite
  API tiers. A Developer account uses full snapshots even when the option is set.

The documented latest-props endpoint has an open-ended nested response schema.
This adapter uses the fully specified **flat event-props** schema instead.
Historical movement, alternate-line, opening, closing and bulk-range APIs are
documented by Oddize but are not polled by this plugin. No order placement,
exchange order-book depth, or automatic settlement API is implemented here.

The catalog currently documents six league codes: `mlb`, `nba`, `nfl`, `ncaabm`,
`ncaaf`, and `nhl`. Runtime catalog data is the authority for actual coverage.
`has_odds` means scheduled games exist, not that every book currently has a price
or that markets are live. The book catalog is not a book-by-market coverage
guarantee. Main-line codes are `ml`, `s`, `t`, `rl`, and `tt`; property codes must
be copied from the league's `prop_types` metadata.

## Conservative free-tier configuration

The published Developer API tier supplies 1,500 credits per month and 30 requests
per minute. The free site plan does not imply an API streaming entitlement.
This configuration is suitable for an additional background reference feed:

```json
{
  "sports": ["nba"],
  "bookmakers": ["pin", "circa", "dk", "fd", "czr", "b365", "mgm"],
  "markets": ["ml"],
  "poll_interval_seconds": 3600,
  "session_request_budget": 30,
  "monthly_credit_budget": 1200,
  "include_past": false,
  "use_delta_polling": false,
  "include_props": false
}
```

Book and market filters are sent as repeated query parameters, matching the
OpenAPI `form`/`explode` array contract. Filtering to fewer books does not make
the documented one-credit current-odds call free.

The effective interval is the maximum of the configured interval, the 31-day
monthly scheduling budget divided across configured sport/prop requests, and a
pace that preserves the server's remaining credits until its reset. More sports
and per-event props therefore make the interval longer. The monthly setting is
a **scheduling budget**, not a persistent accounting ledger across restarts or
manual diagnostic calls. The server's actual remaining balance and the local
session credit cap are independently enforced before requests.

## Live-paper comparison with Parlay

`setup_live_inputs.ps1` enables `oddize_rest` for NBA moneylines and the
`oddize_paper_input` consumer in sample-free paper mode. The consumer reuses
authenticated REST snapshots already fetched by `oddize_rest`; it makes no
independent Oddize requests. Its NBA team codes were checked against the
account's free `/api/v1/sports/nba/teams` catalog. A quote is attached only
to one Parlay paper game with the same home/away teams and a start time no
more than 15 minutes apart. Unknown teams, ambiguous games, incomplete or
timestamp-mismatched sides, and non-moneyline prices are not paper-priced.

The paper strategy uses Oddize's source **observation** timestamp, not HTTP
receipt, and requires it to be at most 10 minutes old. Both API copies of
one book count as one independent reference; equally timed conflicting copies
block the comparison. A matched stale Circa quote can be inspected but has
zero paper pricing weight. The Developer plan's hourly REST cadence means
many returned quotes will be stale even when the API is healthy. This scope
currently covers NBA pregame moneylines only. No Oddize player props or other
sports are paper-matched, and no Oddize data is treated as verified native
execution evidence. Paper offers, depth, and fills are simulated.

On October 21, 2026 an extra read-only NBA query showed 25 complete pairs
younger than 10 minutes across the full book roster, mostly books outside the
initial four-book filter. We expanded the same one-credit NBA poll to include
Caesars, Bet365 and BetMGM so their timestamps are visible. This does not
silently add them to the NBA mainline profile: that user-supplied profile
still requires Pinnacle or Circa as an anchor, with its own configured weights.
The filtered sharp books in that query remained old, so a paid tier alone is
not evidence that their source observations would become current.

All metadata reads cost zero credits but still count toward the request rate.
The plugin observes `X-Credits-Cost`, `X-Credits-Remaining`,
`X-RateLimit-Limit`, and `X-RateLimit-Remaining`; a cache hit still costs credits.
HTTP 402 or an exhausted local/remote quota stops polling and leaves a blocked
health state. HTTP 429 respects `Retry-After`. Review quota and re-enable the
plugin explicitly after exhaustion; it does not buy additional credits.

## Normalization and safety

Oddize `ts` is an epoch-seconds **observation** timestamp; `timestamp` is its UTC
ISO representation. The backend retains that clock, uses the oldest observation
when opposing prices are combined, and leaves the clock unknown when missing or
contradictory. An HTTP response time or cache hit never refreshes the source odds.
It cannot measure sportsbook-to-client latency from this schema.

The extraction layer preserves provider IDs, book codes, exact player subjects,
thresholds, and opposing spread points. Player, team, book, event and threshold
identities must match before rows are paired. Different lines remain separate.
The flat event-props endpoint can return unrelated offer types despite an
explicit `prop_types` filter. The adapter counts and removes those rows before
normalization; a malformed row within the requested type still fails closed.
An observed October 21, 2026 MLB `pitcher_k` request returned 54 requested
rows among 1,381 total, with 1,327 unrelated rows excluded. Five requested
rows had both sides, from BetRivers and FanDuel only. This is one event at one
time, not proof of general MLB strikeout coverage or a usable Circa quote.
`team1` is the **away** team and `team2` is the **home** team in the official
contract. Unknown identities, duplicate outcomes, invalid prices and uncertain
settlement mappings are quarantined by `VerifiedProviderMapper`.

This odds schema does not supply complete overtime, participation, void or
market-suspension rules. Those cannot be inferred from a shared market name.
Canonical mapping still requires independent evidence of settlement equivalence.
The parser does not turn Oddize's aggregated Novig, ProphetX or other exchange
prices into executable liquidity. Only a native verified execution venue can
supply executable prices and depth. Adding this feed does not enable trading.

## Offline and authenticated checks

Run the offline contract tests without credentials:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_oddize.py tests/test_providers.py
```

After storing the key locally, this explicit diagnostic loads `.env`, verifies
the account and three catalogs through **four zero-credit requests**, and prints
only a safe summary:

```powershell
@'
import asyncio, json
from dotenv import load_dotenv
from evplatform.integrations.oddize import live_connection_check
load_dotenv('.env')
print(json.dumps(asyncio.run(live_connection_check()), indent=2))
'@ | .\.venv\Scripts\python.exe -
```

To include one real NBA snapshot, change the function call to
`live_connection_check(sample_sport="nba")`. This adds one metered request.
The helper never submits an order. A successful mocked test proves the local
contract, not remote connectivity; only the actual authenticated response proves
that a specific live request succeeded. A successful snapshot does not establish
all-book, player-prop, in-play, or settlement support.
