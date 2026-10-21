# Genuine native lifecycle evidence

The read-only collector records authenticated account histories and an SQLite read-transaction snapshot. The verifier cross-checks exact order identities, account/environment scope, individual fill accounting and historical financial totals. Neither tool creates orders, cancels orders, transfers money, enables trading or issues launch approval.

## Capture and review on Windows

Run from the project directory. Use a new output directory for every capture; existing evidence is never overwritten.

```powershell
.\.venv\Scripts\python.exe scripts\capture_native_lifecycle.py --venue kalshi novig prophetx --environment production --output data\native-history-review-1
.\.venv\Scripts\python.exe scripts\verify_native_lifecycle.py --capture data\native-history-review-1\account_history.json --output data\native-history-review-1\lifecycle_review.json
```

Use `--environment sandbox` only with that environment's separately configured credentials. Production credentials are never substituted into a sandbox. `--max-pages` bounds each collection; reaching the bound means incomplete history. Optional `--previous-capture` compares immutable identity continuity, not whether an actual process restart occurred.

Files are private account records protected with the existing owner-only storage helper. They remain under ignored `data/`, outside source control. Raw JSON responses retain exact decimal amounts and order/trade IDs, with authentication material redacted. Coverage summaries contain counts and account fingerprints, never secrets. The artifacts include current source-build and content hashes for reproducibility; these hashes are not an independent signature by the exchange and do not make manually modified evidence trustworthy.

## Actual read-only result: 2026-10-21 UTC

Protected evidence is stored in `data/native-lifecycle-20261021-readonly/` as `account_history.json`, `coverage.json` and `lifecycle_review.json`.

| Account | Authenticated scope | Served history result |
| --- | --- | --- |
| Kalshi production | Verified native identity | Current orders 0, fills 0, settlements 0; returned pagination exhausted. Archived history and retention are outside this capture. |
| Novig production trading wallet | Verified native identity | Orders across all five statuses 0; fills 0; settled positions 0; one funding transfer. Funding is not evidence of execution or settlement. |
| ProphetX production | Verified private sign-in identity | Last seven days: fills 0; one wallet transaction; order history returned `next_cursor: null`. Order pagination remains incomplete because documented exhaustion semantics for that value were not established. Reporting history is eventually consistent. |
| Local native OMS | Database read transaction | No native orders, fills, reconciliations or settlements recorded. |

The verifier reports `no_native_oms_orders_to_validate`. These are genuine read responses, **not successful order-to-settlement tests**. No real orders or funds were changed by this capture.

## What must still be observed

For each intended account, environment, market-rule family and application build, genuine event-time evidence must link the durable client intent to native acknowledgement, individual fills, final state and actual settlement cash/fees. A complete validation also needs position release, restart/recovery with a durable order, settlement idempotency and partial-fill/cancellation races. Current table rows cannot retrospectively prove that a commit preceded transmission. A historical manually entered trade cannot certify this application's OMS. Two read snapshots cannot prove a restart happened.

The verifier intentionally marks those event-time checks missing, even when historical money totals match. Native order/fill history must be complete and agree exactly with durable quantities, costs, payouts and actual fees. Missing collections, scope differences, inconsistent identities, incomplete pagination and invalidated evidence cannot count as a match. ProphetX needs synchronized fill and wallet-ledger fee attribution; a reporting summary alone does not establish its final financial state.

Offline fixtures exercise parser/accounting invariants and no-mutation boundaries. They are useful regression coverage, not certificates of real exchange connectivity or production behavior. The report never produces a `LaunchEvidence` object, changes launch profiles, or alters risk controls.

## Official contracts consulted

- Kalshi: [orders](https://docs.kalshi.com/api-reference/orders/get-orders), [fills](https://docs.kalshi.com/api-reference/portfolio/get-fills), [settlements](https://docs.kalshi.com/api-reference/portfolio/get-settlements).
- Novig: [official documentation index](https://docs.novig.com/llms.txt) and its [v3 OpenAPI specification](https://docs.novig.com/api-reference/spec-files/openapi-v3.json) for status-filtered orders, fills, settled position history and personal wallet transactions.
- ProphetX: [order lifecycle](https://docs.prophetx.co/docs/direct-link-order-lifecycle), [wallets](https://docs.prophetx.co/docs/wallets), [field identities](https://docs.prophetx.co/docs/direct-link-ids-and-field-names) and [sandbox notes](https://docs.prophetx.co/docs/direct-link-sandbox-notes-and-faq).
