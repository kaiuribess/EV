# Native venue implementation and validation status

Protocol documentation was checked on **2026-10-20**. Native modules are implemented under `backend/evplatform/venues/`. Kalshi public discovery/books passed on sandbox and production; the primary personal production account also passed signed balance/key/inventory reads and authenticated order-book streaming. Its server-reported location attestation was current at that check and expires October 27, 2026 at 8:15:55 PM PDT. Novig paper testing verified signed echo, key scopes, catalog discovery, and authenticated WebSocket market/private snapshots, public book deltas, and private heartbeats. An unfunded paper subaccount and separate monitoring key were provisioned with account-owner authorization. ProphetX production testing verified key authentication, catalog and market reads, Talaria sign-in, five channel acknowledgements, and real market messages. Personal Polymarket US signed balance, public discovery/book, and authenticated market/private streams also passed; that running monitor is read-only. A separate guarded retail IOC executor is now implemented and tested offline; account identity, spendable cash and settlement proof still block native launch. No native sandbox fill or production order has been validated. Creating a client opens no connection. Demo liquidity and paper fills remain explicitly synthetic. See [current connection evidence](PROVIDER_PROTOCOLS.md).

The dashboard does not arm a native venue just because credentials exist. The shipped risk engine blocks native execution until native market mappings, account permissions, fee schedules, unit conversions, settlement equivalence, and reconciliation have been validated. A transport implementation is not proof that an account may trade.

## Implemented protocols

| Venue | Implemented | Authenticated testing needed |
| --- | --- | --- |
| Kalshi | Public discovery; snapshots; signed WebSocket books and private fills; V2 limit placement/cancel; REST order/fill reconciliation; subscription sequence gap invalidation and reconnect | Demo account/key, current product fee metadata, native mapping and fractional-fill behavior |
| Novig | Signed v3 REST; discovery; native v3 WebSocket book/private subscriptions; increasing nonces; REST and WebSocket place/cancel; per-channel sequences and private heartbeat gaps; reconciliation | Order lifecycle and cent-contract execution; paper REST and authenticated subscriptions verified |
| ProphetX | Key/session exchange and token refresh; discovery; runtime Talaria host; event/event-subtype channels; channel-specific auth/sign-in; double-JSON/base64 decoding; v4 orders/cancels/reconciliation; partial-fill interpretation | Order lifecycle, sandbox account, account limits, settlement/fee configuration; production read-only transport verified |

Each client exposes `get_manifest()`, `get_capabilities()`, `health_check()`, `connect()`, `disconnect()`, discovery, subscriptions, snapshot, stream, submit, cancel, and reconciliation methods where supported. Method arguments remain venue-specific at the transport layer. Unsupported operations are not manufactured.

## Kalshi details

The client uses the dedicated external API hosts, with separate demo/production hosts. Signatures cover timestamp, method, and the full path without its query. RSA-PSS/SHA-256 and Ed25519 PEM keys are supported. See the official [environments](https://docs.kalshi.com/getting_started/api_environments) and [authentication](https://docs.kalshi.com/getting_started/quick_start_authenticated_requests) documentation.

Placement uses `/trade-api/v2/portfolio/events/orders`, with fixed-point price/count strings. Buying NO maps to an ask in the YES book at the complementary price. Cancellation uses the V2 route with `market_ticker` for routing. Responses acknowledge transmission; the bridge never interprets that as a fill. See [V2 placement](https://docs.kalshi.com/api-reference/orders/create-order-v2) and [V2 cancellation](https://docs.kalshi.com/api-reference/orders/cancel-order-v2).

Books retain opposing bids and derive executable asks by complement. Sequence continuity is tracked per subscription; a gap invalidates all books on that subscription and triggers a new connection/snapshot. Private fill events remain separate. See [order book messages](https://docs.kalshi.com/websockets/orderbook-updates).

## Novig details

`NOVIG_API_KEY` holds the v3 key UUID, not a bearer token. `NOVIG_PRIVATE_KEY_PATH` is also required. The signer implements the official six-line `NOVIG-V3` canonical representation, preserving literal `+`, repeated query parameters, path escaping, and the exact transmitted body. Offline tests include all 30 official canonicalization vectors, without copying their published private test keys. See [signing](https://docs.novig.com/api/signing).

Sandbox configuration uses Novig's separate paper exchange at `https://api.paper.novig.com`. Its credentials do not work in production. Production location checks must be satisfied through Novig's official process; API responses such as HTTP 451 are not bypassed. See [environments](https://docs.novig.com/api/environments).

A key created in the Novig Profile screen has `management` scope. It authenticates REST discovery but cannot open `/v3/ws`. Streaming requires a subaccount's `trading` or `trading::read` key; use the read-only scope for monitoring. A management key can inspect its scope with `GET /v3/keys/{id}` and list subaccounts with `GET /v3/account/subaccounts`. Subaccount/key creation is a separate account setup action; the application never performs it automatically. See [key scopes](https://docs.novig.com/api/api-keys) and [subaccounts](https://docs.novig.com/api/subaccounts/manage). Health reports REST authentication separately from successful streaming, preserves actionable access-denied messages, and stops reconnecting after HTTP 401, 403, or 451.

The default `legacy` credential profile reads `NOVIG_API_KEY` and
`NOVIG_PRIVATE_KEY_PATH`. Explicitly selecting the `environment` credential
profile reads `NOVIG_PAPER_TRADING_API_KEY` / `NOVIG_PAPER_TRADING_PRIVATE_KEY_PATH`
for sandbox or `NOVIG_PRODUCTION_TRADING_API_KEY` /
`NOVIG_PRODUCTION_TRADING_PRIVATE_KEY_PATH` for production. Missing or partial
selected pairs block instead of borrowing credentials from another profile.
Management credentials are never selected by that path. Private keys remain
outside the checkout. A paper key cannot authenticate production, and no
credential selection grants order authorization. The [setup tool](NOVIG_SETUP.md)
prepares owner-performed production wallet opening and funding with durable
unknown-outcome recovery; the application does not do those actions at startup.

Native streaming uses `/v3/ws`. Book sequences are per market/channel; private sequences are per subaccount/channel. Gaps invalidate state and reconnect with new subscription snapshots. `place` and `cancel` use request nonces and wait for an acknowledgement; private events are still required. The implementation requests uncompressed frames. See [streaming](https://docs.novig.com/api/streaming/connection) and [private updates](https://docs.novig.com/api/streaming/private).

Static requested IDs are checked on startup and reconnect. An ID is omitted only
when authenticated catalog `MARKET_NOT_FOUND` and market-history `SETTLED`
agree. Other failures block instead of silently changing scope. The requested
and excluded counts are reported in health.

The optional Novig monitor discovery fields (`discovery_leagues`,
`discovery_market_types`, `max_discovered_markets`, and
`discovery_interval_seconds`) use signed, read-only open-market catalog calls.
One bounded page per league lets a dense league schedule leave room for another:
the monitor chooses the nearest open markets in round-robin order. A `truncated`
health state reports that other open markets were left outside the configured
capacity. On a scope change, the monitor closes its socket, invalidates old
books/private state, and follows the ordinary reconnect/snapshot path. The
client revalidates every selected ID before subscription. Discovery changes
only monitored order books; it does not approve mappings, fees, source odds,
or native orders. The current paused production installation uses a four-market
cap across NBA, NFL, MLB, and NHL moneylines. A 52-market subscription did not
maintain a usable stream; 10 markets produced repeated public book sequence
gaps; the four-market scope has also required recovery. The client invalidates
all previously usable books and private state on a gap, then reconnects for a
new authoritative snapshot. Health reports gap counts by channel and whether
the last book failure was a skipped sequence or an unknown removal, without
exposing account details. The exact upstream/drop cause is not yet established.
The live-input paper workspace keeps only sportsbook comparison feeds, avoiding
a second production-key native subscription. See [Novig catalog](https://docs.novig.com/api-reference/catalog/list-markets)
and [streaming subscriptions](https://docs.novig.com/api/streaming/connection).

The paper feed was observed using `orderId` and `outcomeId` where the documentation's book examples use `order` and `outcome`. The parser accepts these specific aliases, validates their types, and rejects conflicting values. A live 46-second paper observation processed 30 books and 48 frames with zero invalidations or reconnects; this is transport evidence, not order-execution validation.

**One Novig contract pays $0.01**, with probability prices and whole-contract quantities. A 110-contract fill at `0.665` costs $0.73150 before fees. The price grid is checked. A client ID is a correlation label, not exchange deduplication; never resend an uncertain placement. See [monetary units](https://docs.novig.com/api/concepts/money) and [placement](https://docs.novig.com/api-reference/execution/place-an-order).

## ProphetX details

The Trading API uses an access-key/secret-key exchange for a Bearer session. The client caches the token, honors `access_expire_time`, and refreshes before expiration. It preserves `/partner` in every Trading API URL. It does not automate participant login or key generation. This is a local single-account client, not a multi-user Direct Link aggregator. See [integration](https://docs.prophetx.co/docs/integration).

Talaria connection details are discovered from `/websocket/connection-config`; no hostname is guessed. The client requires `ws_host` and TLS. Legacy Pusher-host fallback is intentionally unsupported. Registration uses `/v4/mm/websocket`, `X-CFTC-Terminology: true`, `event` and `event_subtype` selections, then waits for sign-in before subscribing. It uses each channel's own auth and returned `scope`; dynamic channel limits/rejections are checked. See [Talaria integration](https://docs.prophetx.co/docs/integrate-with-websockets).

The monitor exposes native `event_ids` and `prop_subtypes` settings. Streaming health requires sign-in and all channel acknowledgements; real market traffic is counted separately from system traffic. Refresh uses the existing refresh token, including on quiet sockets, and trusts the returned expiration. An authenticated production refresh on October 21 at 20:51 PDT returned `data.access_token` and `data.access_expire_time` without a new `refresh_token`. The client now retains its existing refresh token when that field is absent; an explicitly empty replacement still fails. This was verified with an actual read-only refresh response and offline regression tests. A fresh monitor login and streaming subscription succeeded afterward, but its next scheduled automatic refresh had not yet occurred at the time of this note. An ambiguous login outcome blocks automatic retries because another login consumes a session slot. Separate processes should use separate access-key pairs; revoking a key does not immediately release its session slots. See [session guidance](https://docs.prophetx.co/docs/integration) and [refresh endpoint](https://docs.prophetx.co/reference/post_auth-refresh).

On 2026-10-20, an owner-authorized production token returned 374 tournaments and 788 events; a three-event subscription confirmed five channels and delivered `v2.market_selections`. This verifies read access only. The application's native order gate stayed disabled, and no order, cancellation, deposit, or transfer was submitted. Token creation requires the account owner to acknowledge ProphetX's five-year activity-record requirement; the app never accepts that obligation automatically. See [self-service keys](https://docs.prophetx.co/docs/getting-api-keys).

`v2.market_selections` is decoded independently of legacy payloads. Native `price` is American odds. **Executable stake is `value`; legacy `quantity` must not be used as available liquidity.** Source-level `updated_at` remains separate from message publication/receipt. Sequence numbers are monotonic ordering tokens; the code does not assume adjacent integers or fabricate gap guarantees. See [WebSocket payloads](https://docs.prophetx.co/docs/websocket-events).

Orders use `strike_id`, not `outcome_id`, and USD stake quantities. A fetched, schema-validated native price ladder must be installed before submitting. `fillOrKill` does not mean all-or-none. Partial fills may survive while remaining stake is canceled; the final record can still say `open` and `fully_matched`. The helper uses original and cumulative quantities. HTTP 404 from the reporting read is not proof that an order failed. Cancel races preserve their error codes. See [order lifecycle](https://docs.prophetx.co/docs/direct-link-order-lifecycle) and [ID/unit reference](https://docs.prophetx.co/docs/direct-link-ids-and-field-names).

## Core execution bridge

`NativeExecutionVenue` implements the shared `ExecutionVenue` contract. `native_execution_factories(authority)` provides factories for all native clients without changing strategies or the OMS.

The bridge requires:

1. An exact `VerifiedVenueMapping` for canonical market/outcome, with native instrument, settlement fingerprint, mapping evidence, and verified fee schedule.
2. A current `NativePreflight` matching sandbox/production, authorized account evidence, explicit native enablement, and production geographic eligibility.
3. A one-use authorization from the central `RiskAuthority`. The OMS must have persisted the exact client order ID before issuing it.
4. A supported quantity/payout mapping.

The legacy paper `Order`/`Fill` contract represents whole $1 units. Native routing now uses a separate `NativeOrderIntent`/`NativeOrder` contract, exact decimal cash and fee accounting, and the same central risk lock and position limits. It supports fractional Kalshi contracts, whole Novig cent contracts, and ProphetX USD stakes without converting them into paper contracts. `native_positive_ev` is disabled by default and consumes verified, source-clocked native books. The guarded native router persists intent and rechecks original executable limits before transmission. Its end-to-end tests are entirely offline.

The bridge returns `acknowledged`, with reconciliation required, for accepted placement. Cancel acknowledgement returns `cancel_pending`; it never releases exposure before reconciling racing fills. Uncertain network outcomes remain unknown and are never retried automatically. Restart restores these reservations and requires account reconciliation. Authoritative cumulative quantities, cost, payout and actual fees must agree before a position can settle; later contradictory evidence invalidates settlement durably and stops automation.

Authenticated read-only account probes bind account identity and inventory for Novig and Kalshi. Private synchronization uses Novig sequences, Kalshi local connection/revision fences with authenticated REST watermarks, and ProphetX private revision checks. The new ProphetX inventory/fill/fee/settlement readers require an explicit funded-cash account review and complete authoritative records; genuine account-backed validation remains outstanding. Numeric ProphetX quote clocks require a reviewed unit declaration because the inspected official schema does not specify the unit.

Unsupported in this release: automatic account/geo certification, automatic fee-schedule verification, live cross-provider mapping verification, production preflight evidence acquisition, native replacement/amend orchestration, RFQs/FIX, and general settlement across arbitrary/shared positions. These are visible launch blockers, not conditions bypassed by supplying an API key. See [the launch guide](GO_LIVE.md) for required evidence and configuration.

### Restricted Novig settlement reconciliation

The native Novig bridge explicitly submits IOC orders. Its read-only settlement adapter accepts an ordinary WIN/LOSS only when one fully reconciled terminal order owns the entire native market position. It verifies the authenticated wallet, full market fill history, taker-only fills, actual position quantity/cost/payout/P&L, actual execution fees, and matching market/position settlement timestamps. It never allocates one shared position across several local orders. Tests use documented offline responses, not actual cash settlement.

PUSH, FMV, maker credits, external/netted positions, and remediation remain blocked for cash review. The [position history schema](https://docs.novig.com/api-reference/history/get-a-position-from-history) provides payout and cost but no separately attributed settlement fee/refund. The [transaction ledger](https://docs.novig.com/api-reference/accounts/list-transactions) identifies the other account in `ref`; it does not document an order/position link for allocating fee or adjustment rows. Therefore this restricted adapter does not satisfy the general unattended-settlement readiness check. A changed or unverifiable previously accepted settlement invalidates its accounting and halts automation.

Novig's [v3 fee contract](https://docs.novig.com/api/concepts/fees) explicitly omits `fee` from an uncharged REST fill, which is normalized to zero only for that documented schema. Private fill messages carry no fee evidence and cannot establish a zero fee. Actual native numeric JSON values are parsed as decimals before normalization.

## When native credentials arrive

Keep secrets in the local `.env` file or Windows environment; never paste them into dashboard configuration.

```dotenv
KALSHI_API_KEY_ID=
KALSHI_PRIVATE_KEY_PATH=C:/private/kalshi-demo.pem
NOVIG_API_KEY=
NOVIG_PRIVATE_KEY_PATH=C:/private/novig-paper.pem
PROPHETX_API_KEY=
PROPHETX_SECRET_KEY=
```

1. Obtain native demo/paper/sandbox credentials through the venue's official account process. Paid sportsbook WebSocket keys are separate from exchange trading keys.
2. Configure the corresponding `KalshiSettings`, `NovigSettings`, or `ProphetXSettings` using environment variables; leave `environment="sandbox"` and `gate.enabled=False`.
3. Run `connect()`, discovery, and read-only subscriptions. Inspect real native fixtures before approving aliases, player identity, lines, settlement rules, fees, and units.
4. Use `stream_updates()` concurrently with any native WebSocket commands. Preserve per-level source timestamps; do not replace them with the reconnect time.
5. Run account-authorized sandbox lifecycle checks for rejection, unknown transmission, cancel/fill races, partial fills, reconnect, and restart. Record actual exchange IDs and timestamps.
6. Build the reviewed `data/native_mappings.json` and account-bound `data/launch_profile.json` described in [GO_LIVE.md](GO_LIVE.md). Check the dashboard's current blockers. Production still requires venue eligibility, real lifecycle/settlement evidence, explicit application configuration, and a successful current preflight.

Neither adding a key nor starting demo mode sends a native order. The `.env` placeholders are not a claim that these clients have already connected.

## Offline checks

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_venues.py -q --basetemp "$PWD\data\pytest-venues"
```

Tests cover signature verification, official Novig canonicalization vectors, exact signed request bytes, native price units/grids, complementary asks, sequence gaps/reconnects, private heartbeat loss, Talaria parsing/scopes/sign-in, native guards, partial fills, cancel races, unknown outcomes, and shared bridge authority. Every HTTP/WebSocket exchange is mocked. No test calls production.

## Additional guarded lifecycle adapters

Kalshi now includes a primary-account, buy-only binary IOC settlement adapter
that checks complete actual fill accounting and paid settlement revenue. Shared
positions, scalar/FMV settlements, special subaccounts and absent cash history
remain blocked. Its fee-bound helper expires at the next scheduled fee change
and includes fractional-fill rounding. See [Kalshi lifecycle](KALSHI_LIFECYCLE.md).

ProphetX now joins matched details to actual wallet debits and commissions,
validates complete paginated account history, and supports narrowly reviewed
funded-cash normal win/loss payouts. It does not infer absent fees as zero or
assume that exposure credits are cash. The account inception window and lack of
GEC/LEC funding require explicit evidence. Pushes, voids, manual grades, credits
and shared positions remain unsupported. These paths have offline fault tests;
no real fill or payout has certified them.

The October 21 production recheck completed the seven-day order/fill/wallet
history after fixing timestamped empty-order null cursors. Get-order fee
omissions now use exact-revision order-history evidence plus actual wallet
commissions. This does not certify a genuine executed lifecycle or complete
cash-only account scope. See [ProphetX reconciliation](PROPHETX_RECONCILIATION.md).

Polymarket US has a separate risk-gated retail IOC executor and cancel/reconcile
interface; the public/account monitor itself remains GET-only. YES/NO mappings
retain exact decimal prices and USD accounting. No undocumented client-order-ID
or automatic resend is used after an unknown outcome. Exact private executions
are required to establish cost and commissions; average price is insufficient.
The monitor may now discover current moneylines from the documented public
[`/v2/leagues/{slug}/events`](https://docs.polymarket.us/api-reference/sports/get-events-by-league-slug)
route, balancing a bounded set across configured leagues. Scope changes restart
read-only sockets and require new market/private baselines. A catalog row is
never a reviewed cross-provider mapping or a verified profitable signal.
No authoritative retail endpoint has established the stable wallet identity,
spendable-cash semantics and order-attributed actual settlement needed for a
launch, so those checks remain false. Public settlement prices and unlinked
account activities cannot stand in for cash settlement.

See [operator limits](OPERATOR_LIMITS.md) for the $5/no-overlap policy and
per-environment controller lock. Protocol implementation and passing offline
tests do not establish permission or genuine execution certification.
