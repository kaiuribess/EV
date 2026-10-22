# Quote observation evidence

Reviewed against official documentation on 2026-10-20 and updated with a bounded authenticated ParlayAPI check on 2026-10-22. Protocol fixtures are offline tests, not proof of authenticated connectivity or current source coverage.

`source_time` preserves the existing source clock for compatibility. Strict reference profiles use the separate `source_observed_at` and require `observation_verified`, `observation_complete`, and `market_status_verified`. `source_price_changed_at` records price movement when its meaning is documented; it never refreshes an observation. Provider publication and local receipt remain separate.

## ParlayAPI

The documented `last_observed` plus `last_update_type: collector_observation` pair establishes a quote-bound collector observation. The current parser accepts timezone-qualified ISO values; numeric units are unspecified in the reviewed schema and remain unverified. `last_update` has legacy meanings and cannot universally establish verification or price movement. `line_changed_at_ms` is retained separately. Book/fixture heartbeat fields do not verify each displayed selection. The adapter preserves unknown market status; collector observation alone is insufficient for automatic execution. [Official reference](https://parlay-api.com/docs), [timing distinctions](https://parlay-api.com/docs/streaming).

Retained top-ups, partial/truncated snapshots, continuation pages and degraded sources prevent strict completeness. Separate outcomes must arrive together with matching observation clocks; an older cached opposing side remains research-only. Missing prices invalidate prior usable quotes, without treating unrelated null columns as withdrawals. Observation metadata never changes the source identity signature. These are deliberately conservative local eligibility rules, not claims of upstream guarantees. [Snapshot and pagination contract](https://parlay-api.com/docs).

### October 21 live parser repair

A bounded authenticated NBA capture exposed named spread rows with the same American price in both `price` and `over_price`, and a null `under_price`. The parser now recognizes that exact game/spreads shape, retaining the actual team and signed finite handicap. Unequal price aliases, extra opposing prices, missing/nonfinite handicaps, and conflicting prop/total fields remain invalid. A named spread side does not invent its opposing side, prove a settlement match, or acquire active-status evidence.

Flat Parlay streaming and REST prop batches now isolate malformed rows. The failed row's explicit source event/book/market scope is invalidated, its cached partial sides are cleared, and all rows in that affected scope are excluded from the current batch. Unrelated valid scopes still update. Missing identity broadens invalidation; an unidentifiable row invalidates the provider scope. Reconnect/replacement baselines retain their existing invalidation behavior. Recovery of previously verified observations still requires newer complete evidence.

Streaming `commence_time` and subscription sport now survive discovery as `scheduled_start` and `event_sport`. Missing values stay missing. These facts contribute to the source signature; previously authored mappings without this context require fresh review rather than silent migration. No existing real mappings were present when this repair was installed.

All 25 retained malformed real spread rows parsed successfully after the repair. A new 30-second authenticated NBA capture saw 68 FanDuel/DraftKings moneyline and spread offers with zero extraction errors. It still supplied no verified quote-bound observation or active-status evidence; no execution eligibility was inferred. The captures are bounded windows, not a coverage census. Local artifacts: `data/first-scope-bad-rows-20261021.json` (protected raw rows) and `data/first-scope-feed-fixed-20261021.json` (summary).

### October 22 FanDuel game-link status check

The current `/v1/sports/{sport_key}/odds` contract supports `includeLinks=true`.
It returns an individual FanDuel game-outcome link only for an eligible native
quote observed within five minutes whose market and runner were OPEN/ACTIVE.
The REST adapter now requests these links and marks the exact main market's
status verified only when every priced outcome has a valid link, the response
is not partial/truncated/degraded or topped up, and the market is not suspended.
It does not retain links in normalized offers or use them for order placement.
This status evidence does **not** provide a quote-bound observation clock: the
REST game `last_update` and book/fixture heartbeat fields still cannot establish
current price freshness. [Current OpenAPI](https://parlay-api.com/openapi.json),
[WebSocket and timestamp guidance](https://api.parlay-api.com/docs/websocket).

Two one-credit read-only NBA FanDuel moneyline requests on October 22 returned
14 complete two-sided game offers each. After the parser change, 14/14 had
link-based active-status evidence; 0/14 had verified quote-bound observation,
and 0/14 met both conditions. This is a narrow live sample, not proof that
other books, sports, markets, or future responses carry links. Strict reference
eligibility and native order submission remain blocked without the missing
freshness, settlement and market-mapping evidence.

### Withdrawal precedence repair, October 21

Offline regression reproduced two mapper defects: a `status: suspended` notice
with no price fields did not revoke its cached quote, and a usable-looking row
in the same batch could restore a quote marked removed, suspended, unavailable
or missing an explicit side price. The flat REST-prop and WebSocket paths now
treat those notices as adverse evidence for the entire affected event/book/market
scope in that delivery. They clear cached partial sides and exclude companion
rows in either order, while allowing unrelated scopes to continue.

Nullable fields belonging to another transport family remain harmless: unused
moneyline columns do not withdraw a complete prop, and unused over/under columns
do not withdraw a named-outcome price. A missing identity broadens invalidation
instead of guessing a player or threshold. These negative-evidence fixes do not
assert positive active-status verification or add an upstream withdrawal
guarantee that the provider contract does not supply.

## Oddize

`Price.ts` denotes observation time and `timestamp` is its ISO representation. Conflicting or missing clocks remain unverified. The older opposing-side observation is retained; unequal side timestamps do not receive synchronized completeness. The published flat schemas provide no authoritative current market status, so status verification remains false. [Official OpenAPI](https://oddize.com/api/v1/openapi.json).

`discover_prop_events(sport, prop_type=...)` calls the documented sport-wide route with an explicit prop type and preserves its raw grouped response. OpenAPI leaves nested `players`, `alt_lines` and `all_odds` objects untyped. They therefore remain discovery data, rather than guessed normalized prices. Fetch the typed event-props route for explicit per-book, per-side thresholds; different lines remain separate offers. This discovery call does not silently increase background polling or credit use. No sportsbook aliases, player matches or settlement equivalence are approved by this parser. [Official endpoint descriptions](https://oddize.com/odds-api).

## Remaining requirements

A new 30-second authenticated capture on October 21 observed **681 offers in 33
provider/book/market groups**, including Pinnacle PRA props through Parlay.
Parlay reported no transport errors; that does not certify its individual rows.
Oddize's 58 distinct moneyline observations had usable observation clocks; 51
also had synchronized complete opposing prices. All 58 lacked verified active
status. No sampled provider group established market-status verification, and
no TTL was approved. The single Oddize metered read consumed one credit.
Artifact: `data/freshness-final-audit-20261021.json`. This supersedes any inference
of absent Pinnacle coverage from an earlier shorter capture, without claiming
complete sport/prop coverage. The October 22 Parlay FanDuel REST status result
above is a later, narrower exception; it does not alter the October 21 sample.

The October 21 fields do not supply a validated provider/book/market freshness
limit, settlement match, active-status guarantee or native lifecycle evidence.
The narrow October 22 FanDuel status exception does not close those other gaps.
Missing evidence still blocks strict profiles. An unchanged price can have a new
verified observation; a newly received packet can contain an old price. Neither
means an execution target is liquid or profitable.
