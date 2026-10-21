# Native Novig paper validation scope

Reviewed October 20, 2026 (America/Los_Angeles). Research only: this review made no account requests, transfers, or orders.

Novig explicitly identifies `https://api.paper.novig.com` as its play-money exchange, using the same API as production with no location check. The corresponding WebSocket host is `wss://api.paper.novig.com`. Paper and production keys are separate. Initial web signup grants $10,000 play money; this does not establish an ongoing refill mechanism. [Official environments documentation](https://docs.novig.com/api/environments).

For an **existing paper trading subaccount**, the documented route can transfer available **paper cash** from its parent account:

- Method and path: `POST /v3/account/subaccounts/{keyId}/transfer`.
- Sign using the **paper management key**; `{keyId}` identifies the existing paper trading subaccount.
- Body: `direction: "fund"`, a positive decimal-string `amount` with at most five fractional places, and a unique durable `clientTransferId`.
- A `202` acknowledgement is pending. Verify `Applied` using `GET /v3/account/subaccounts/{keyId}/transfers/{transferId}` before treating the play funds as available. Reuse the exact same request/id after an uncertain response; do not queue a new transfer.

This is a transfer from existing funds, **not a cash-wallet faucet**. [Official funding guide](https://docs.novig.com/api/subaccounts/funding).

The [official Quickstart](https://docs.novig.com/api/quickstart) explicitly performs its account, transfer, catalog, and order sequence on Paper. It distinguishes an accepted order from the private-stream event proving that the order rests. Native paper tests can therefore validate signing, protocol, acceptance, reconciliation, cancellation, and observed fills when the play exchange supplies matching liquidity. They cannot prove production account/location eligibility, production fills, or strategy profitability.

No paper cash-wallet refill, reset, or deposit API was found in the current official documentation index, environments page, Quickstart, funding guide, or targeted official-site search. If the existing paper cash wallet is depleted, this review supplies no supported API to replenish it. Do not invent a route, reuse production funding, or claim initial signup funds guarantee the current balance.

A validation runner must pin the exact paper hostname, disable cross-host redirects, load only paper credentials, keep its journal and outcomes separate from production evidence, and reject any production host before dispatch. Settlement coverage still requires a real observed paper settlement; placement or a fill alone does not certify that stage.
