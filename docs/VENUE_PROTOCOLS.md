# Native venue implementation and validation status

Protocol documentation was checked on **2026-10-20**. Native modules are implemented under `backend/evplatform/venues/`. They have offline protocol tests plus successful unauthenticated Kalshi public REST discovery and order-book reads on sandbox and production hosts. No account authentication, authenticated WebSocket, native sandbox fill, production order, or geographic eligibility has been validated. Creating a client opens no connection. Demo liquidity and paper fills remain explicitly synthetic.

The dashboard does not arm a native venue just because credentials exist. The shipped risk engine blocks native execution until native market mappings, account permissions, fee schedules, unit conversions, settlement equivalence, and reconciliation have been validated. A transport implementation is not proof that an account may trade.

## Implemented protocols

| Venue | Implemented | Authenticated testing needed |
| --- | --- | --- |
| Kalshi | Public discovery; snapshots; signed WebSocket books and private fills; V2 limit placement/cancel; REST order/fill reconciliation; subscription sequence gap invalidation and reconnect | Demo account/key, current product fee metadata, native mapping and fractional-fill behavior |
| Novig | Signed v3 REST; discovery; native v3 WebSocket book/private subscriptions; increasing nonces; REST and WebSocket place/cancel; per-channel sequences and private heartbeat gaps; reconciliation | Paper account/subaccount trading key, signature echo, scopes, order lifecycle, cent-contract sizing |
| ProphetX | Key/session exchange and token refresh; discovery; runtime Talaria host; event/event-subtype channels; channel-specific auth/sign-in; double-JSON/base64 decoding; v4 orders/cancels/reconciliation; partial-fill interpretation | Sandbox access pair, current price ladder, private channel fixtures, account limits, settlement/fee configuration |

Each client exposes `get_manifest()`, `get_capabilities()`, `health_check()`, `connect()`, `disconnect()`, discovery, subscriptions, snapshot, stream, submit, cancel, and reconciliation methods where supported. Method arguments remain venue-specific at the transport layer. Unsupported operations are not manufactured.

## Kalshi details

The client uses the dedicated external API hosts, with separate demo/production hosts. Signatures cover timestamp, method, and the full path without its query. RSA-PSS/SHA-256 and Ed25519 PEM keys are supported. See the official [environments](https://docs.kalshi.com/getting_started/api_environments) and [authentication](https://docs.kalshi.com/getting_started/quick_start_authenticated_requests) documentation.

Placement uses `/trade-api/v2/portfolio/events/orders`, with fixed-point price/count strings. Buying NO maps to an ask in the YES book at the complementary price. Cancellation uses the V2 route with `market_ticker` for routing. Responses acknowledge transmission; the bridge never interprets that as a fill. See [V2 placement](https://docs.kalshi.com/api-reference/orders/create-order-v2) and [V2 cancellation](https://docs.kalshi.com/api-reference/orders/cancel-order-v2).

Books retain opposing bids and derive executable asks by complement. Sequence continuity is tracked per subscription; a gap invalidates all books on that subscription and triggers a new connection/snapshot. Private fill events remain separate. See [order book messages](https://docs.kalshi.com/websockets/orderbook-updates).

## Novig details

`NOVIG_API_KEY` holds the v3 key UUID, not a bearer token. `NOVIG_PRIVATE_KEY_PATH` is also required. The signer implements the official six-line `NOVIG-V3` canonical representation, preserving literal `+`, repeated query parameters, path escaping, and the exact transmitted body. Offline tests include all 30 official canonicalization vectors, without copying their published private test keys. See [signing](https://docs.novig.com/api/signing).

Sandbox configuration uses Novig's separate paper exchange at `https://api.paper.novig.com`. Its credentials do not work in production. Production location checks must be satisfied through Novig's official process; API responses such as HTTP 451 are not bypassed. See [environments](https://docs.novig.com/api/environments).

Native streaming uses `/v3/ws`. Book sequences are per market/channel; private sequences are per subaccount/channel. Gaps invalidate state and reconnect with new subscription snapshots. `place` and `cancel` use request nonces and wait for an acknowledgement; private events are still required. The implementation requests uncompressed frames. See [streaming](https://docs.novig.com/api/streaming/connection) and [private updates](https://docs.novig.com/api/streaming/private).

**One Novig contract pays $0.01**, with probability prices and whole-contract quantities. A 110-contract fill at `0.665` costs $0.73150 before fees. The price grid is checked. A client ID is a correlation label, not exchange deduplication; never resend an uncertain placement. See [monetary units](https://docs.novig.com/api/concepts/money) and [placement](https://docs.novig.com/api-reference/execution/place-an-order).

## ProphetX details

The Trading API uses an access-key/secret-key exchange for a Bearer session. The client caches the token, honors `access_expire_time`, and refreshes before expiration. It preserves `/partner` in every Trading API URL. It does not automate participant login or key generation. This is a local single-account client, not a multi-user Direct Link aggregator. See [integration](https://docs.prophetx.co/docs/integration).

Talaria connection details are discovered from `/websocket/connection-config`; no hostname is guessed. The client requires `ws_host` and TLS. Legacy Pusher-host fallback is intentionally unsupported. Registration uses `/v4/mm/websocket`, `X-CFTC-Terminology: true`, `event` and `event_subtype` selections, then waits for sign-in before subscribing. It uses each channel's own auth and returned `scope`; dynamic channel limits/rejections are checked. See [Talaria integration](https://docs.prophetx.co/docs/integrate-with-websockets).

`v2.market_selections` is decoded independently of legacy payloads. Native `price` is American odds. **Executable stake is `value`; legacy `quantity` must not be used as available liquidity.** Source-level `updated_at` remains separate from message publication/receipt. Sequence numbers are monotonic ordering tokens; the code does not assume adjacent integers or fabricate gap guarantees. See [WebSocket payloads](https://docs.prophetx.co/docs/websocket-events).

Orders use `strike_id`, not `outcome_id`, and USD stake quantities. A fetched, schema-validated native price ladder must be installed before submitting. `fillOrKill` does not mean all-or-none. Partial fills may survive while remaining stake is canceled; the final record can still say `open` and `fully_matched`. The helper uses original and cumulative quantities. HTTP 404 from the reporting read is not proof that an order failed. Cancel races preserve their error codes. See [order lifecycle](https://docs.prophetx.co/docs/direct-link-order-lifecycle) and [ID/unit reference](https://docs.prophetx.co/docs/direct-link-ids-and-field-names).

## Core execution bridge

`NativeExecutionVenue` implements the shared `ExecutionVenue` contract. `native_execution_factories(authority)` provides factories for all native clients without changing strategies or the OMS.

The bridge requires:

1. An exact `VerifiedVenueMapping` for canonical market/outcome, with native instrument, settlement fingerprint, mapping evidence, and verified fee schedule.
2. A current `NativePreflight` matching sandbox/production, authorized account evidence, explicit native enablement, and production geographic eligibility.
3. A one-use authorization from the central `RiskAuthority`. The OMS must have persisted the exact client order ID before issuing it.
4. A supported quantity/payout mapping.

The current core `Order`/`Fill` contract represents whole $1 units. The bridge therefore supports verified Kalshi $1 binary mappings, but **blocks Novig cent-contract and ProphetX USD-stake execution** rather than rounding their partial fills into the wrong unit. Fractional Kalshi fills remain `unknown` until a quantity-aware reconciliation extension is supplied. Raw transports support these native payloads for independent sandbox integration work. The application does not auto-route native trades.

The bridge returns `acknowledged`, with reconciliation required, for accepted placement. Cancel acknowledgement returns `cancel_pending`; it never releases exposure before reconciling racing fills. Uncertain network outcomes remain unknown and are never retried automatically. Core/native acknowledgement and fill consumers must be integrated and tested before routing is enabled.

Unsupported in this release: automatic account/geo certification, automatic fee-schedule verification, live cross-provider mapping verification, production preflight evidence acquisition, arbitrary fractional core fills, Novig/ProphetX automatic execution through the $1 strategy, native replacement/amend orchestration, RFQs/FIX, and autonomous production settlement. These require explicit extension work, not merely an API key.

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
6. Complete the missing bridge/OMS/unit/fee extensions described above. Production still requires venue eligibility, explicit application configuration, and a successful current preflight.

Neither adding a key nor starting demo mode sends a native order. The `.env` placeholders are not a claim that these clients have already connected.

## Offline checks

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_venues.py -q --basetemp "$PWD\data\pytest-venues"
```

Tests cover signature verification, official Novig canonicalization vectors, exact signed request bytes, native price units/grids, complementary asks, sequence gaps/reconnects, private heartbeat loss, Talaria parsing/scopes/sign-in, native guards, partial fills, cancel races, unknown outcomes, and shared bridge authority. Every HTTP/WebSocket exchange is mocked. No test calls production.
