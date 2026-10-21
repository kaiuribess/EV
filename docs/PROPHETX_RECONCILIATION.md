# ProphetX reporting repair — October 21, 2026

The production read-only check at **15:45:52 UTC** authenticated and confirmed
the signed account identity. The seven-day order, fill and transaction requests
completed without errors: zero orders, zero fills and one wallet transaction.
No order, cancellation, transfer or trading activation was performed.

## Empty history cursor

The production service returned `data.orders: []`, `data.next_cursor: null`
and an ISO `last_synced_at`. Our reader previously rejected the cursor. Both
the financial reader and diagnostic capture now accept this observed empty
order-history shape, retaining the `observed_empty_order_null` marker.

This is a narrow compatibility rule based on the authenticated response, not
a claim that the published OpenAPI declares null cursors. The
[order-history schema](https://docs.prophetx.co/reference/get_v4-mm-get-order-history)
declares a string cursor; the [wallet guide](https://docs.prophetx.co/docs/wallets)
documents an absent final transaction cursor. Null on a nonempty order page or
another collection remains invalid. Stale, future or absent reporting clocks,
cursor loops and truncated pagination remain blocked.

Completing a requested window does not certify account inception coverage,
replication completeness, current private state or absence of older positions.
Runtime inventory verification still requires the account-specific inception
and funded-cash review, private synchronization and matching wallet aggregates.

## Recorded fees and settlement

The [individual-order schema](https://docs.prophetx.co/reference/get_v4-mm-get-order-id)
omits `fee`. Section 7 of the
[order lifecycle guide](https://docs.prophetx.co/docs/direct-link-order-lifecycle)
lists fees in order-history rows, although that reference schema omits the
field too. When get-order omits the field, the financial and settlement readers
now query history for the actual recorded fee. They require:

- The owned native order, client order, account and strike identities.
- The exact same order revision and agreement on every supplied order field.
- A current reporting watermark covering the revision.
- An explicit nonnegative fee, reconciled against actual wallet commissions.

Missing fees never become zero. Explicit invalid fees are not overwritten by
fallback data. Newer, older or contradictory history must be reconciled later.
Actual matched quantities and profit, cash debits and posted payouts remain
required. An acknowledgement is not a fill; `settled` is not paid `closed` cash.

The existing native request validator enforces the documented $1 minimum USD
stake and cent precision. The $5 reporting unit is not that minimum. Existing
partial-fill handling preserves the guide's fill-then-kill behavior rather than
assuming all-or-nothing execution.

## Evidence limits

The genuine account has supplied no order or fill in these captures. Fee fallback
and paid settlement are validated with offline fixtures, not a real lifecycle.
The balance endpoint does not expose LEC; zero GEC alone does not prove cash-only
funding. Pushes, voids, manual settlements, resettlement and exposure-credit
accounting remain outside the reviewed execution scope. Launch gates remain on.

Protected, Git-ignored evidence is stored in
`data/prophetx-fix-before-20261021/` and `data/prophetx-fix-after-20261021/`.
The public source snapshots are in `data/prophet-api-review-20261021/`.
Regression tests cover empty histories, malformed cursors and clocks, fee
omissions, ownership mismatches, conflicting revisions, wallet disagreement and
normal paid settlement using the history fee.

## Captured owned-order replay

The read-only diagnostic now preserves the documented individual order,
matched-detail, history and wallet responses for application-owned native IDs.
The offline verifier previously always reported ProphetX finances as missing;
it can now compare their exact financial totals. Raw pages, pagination endings,
receipt order, reporting clocks and account bindings are validated again during
replay. The fee-enrichment routine is shared with the runtime.

Offline tests include a $1 submitted stake with a $0.40 partial fill and a killed
remainder, plus a closed winning fixture, missing fees, foreign ledger entries,
cursor loops, altered summary rows, duplicate bundles and wrong account scopes.
These are simulated fixtures with no exchange submission. A closed financial
match does not clear the separate paid-settlement or restart evidence gates.

Capture commands and environment-specific ledger selection are described in
[native lifecycle evidence](NATIVE_LIFECYCLE_EVIDENCE.md).
