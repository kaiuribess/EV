# Extending provider transports

The stable boundary is `ProviderEnvelope`, not a forced odds schema. A score,
projection, injury update, or order-book event can define its own registered
schema and publish independently. A provider never receives an order router.

## REST adapter

Subclass `RestProvider`, declare `provider_id`, fixed official `base_url`,
`credential_env`, and a Pydantic `config_class`. Implement `auth()` and the
supported discovery/snapshot methods. Unsupported methods inherit an explicit
`UnsupportedOperation`; do not return fabricated catalogs.

```python
from evplatform.integrations.base import RestProvider

class DocumentedProvider(RestProvider):
    provider_id = "my_provider"
    # Set base_url and paths only after verifying the provider's documentation.
    base_url = ""
    credential_env = "MY_PROVIDER_API_KEY"

    def auth(self):
        raise NotImplementedError("Implement the provider's official auth scheme")

    async def fetch_snapshot(self, sport, **filters):
        raise NotImplementedError("Use the documented route and response schema")
```

The empty URL and unimplemented protocol are intentional template boundaries,
not a claim that a future provider shares another vendor's API.

Use `request()` for credential-safe errors, HTTPX transport injection, quota
reservation, response usage headers, and circuit breaking. Never write query
credentials or raw account responses to logs. Secrets belong in the backend
environment, not in a public configuration schema.

## Strict field mapping

`map_json_fields` demonstrates field projection without guessing defaults:

```python
from evplatform.integrations.templates import map_json_fields

# Illustrative local fixture, not a live provider response.
fixture = {"event": {"id": "fixture-1"}, "quote": {
    "observedAt": "2026-10-20T18:00:00Z", "price": 1.91
}}
mapped = map_json_fields(fixture, {
    "native_event_id": "event.id",
    "source_time": "quote.observedAt",
    "decimal_odds": "quote.price"
})
```

Missing fields raise an error. Projection cannot verify team/player identity,
opposing outcome completeness, settlement equivalence, or threshold semantics.
Keep those checks in a dedicated normalizer and verified mapping registry.
Keep actual source, provider, and local clocks separate. Never fill a missing
source clock with the current time.

## WebSocket

`ParlayWebSocket` is a worked example of the connection lifecycle. For another
vendor, implement its actual authentication, commands, heartbeat, and frame
protocol. Reuse `Backoff`; use `SequenceTracker` only if the vendor documents
consecutive sequence numbers for the relevant channel or subscription.

On a missed sequence, reconnect, timeout, malformed frame, or subscription
failure, emit an invalidation before using another quote. Acquire the documented
replacement snapshot before applying deltas. Do not infer that a subscription
acknowledgement or heartbeat verifies retained market prices.

## SSE

`templates.parse_sse` supports comments, multiline data, event names, and IDs;
`templates.sse_updates` wraps an HTTPX stream. A plugin supplies the documented
URL, authentication headers, and decoder. Last-Event-ID is used only if the
provider documents resumability. A truncated event is discarded.

Do not treat an SSE score feed as odds. Register a game-state schema and let
interested strategies declare it as a required input.

## Replay and isolated tests

Construct a provider with `httpx.AsyncClient(transport=httpx.MockTransport(...))`
for exact URL/auth/status tests. `ParlayWebSocket` also accepts `ws_factory`
for deterministic protocol fixtures. `replay_envelopes` preserves original
timestamps and event order; replay speed zero removes wall-clock sleeps.

Each adapter should prove:

1. No configured key means no network request and no synthetic fallback.
2. Authentication failures stop retries and expose no secret.
3. Discovery uses documented catalogs rather than hardcoded coverage claims.
4. Missing source clocks stay unknown; response arrival cannot refresh them.
5. Suspensions, removed selections, stale/missing deltas, and failed snapshots
   invalidate any previously accepted reference.
6. Threshold, player, period, and settlement changes cannot reuse a mapping.
7. Local quota limits and observed provider limits block excess requests.
8. The same fixture replay produces the same normalized observations.

Register the adapter through the core plugin entry-point mechanism, or use a
thin `InputPlugin` bridge around the transport. Export a manifest with its
configuration schema, permissions, supported operations, schema versions,
official documentation, and limitations. The registry and dashboard can then
present it without changing the pricing engine.

An execution venue is a separate plugin implementing the central
`ExecutionVenue` contract. It must declare order capabilities, fees, venue
states, reconciliation behavior, and eligibility requirements. Input adapters
and strategies cannot call it directly; only the central risk and order
management path may authorize a transmission.
