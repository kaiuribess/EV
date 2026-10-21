# Genuine native lifecycle evidence

The read-only collector records authenticated account histories and an SQLite read-transaction snapshot. The verifier cross-checks exact order identities, account/environment scope, individual fill accounting and historical financial totals. Neither tool creates orders, cancels orders, transfers money, enables trading or issues launch approval.

## Capture and review on Windows

Run from the project directory. Use a new output directory for every capture; existing evidence is never overwritten.

```powershell
.\.venv\Scripts\python.exe scripts\capture_native_lifecycle.py --venue kalshi novig prophetx --environment production --output data\native-history-review-1
.\.venv\Scripts\python.exe scripts\verify_native_lifecycle.py --capture data\native-history-review-1\account_history.json --output data\native-history-review-1\lifecycle_review.json
```

Use `--environment sandbox` only with that environment's separately configured credentials. Production credentials are never substituted into a sandbox. `--max-pages` bounds each collection; reaching the bound means incomplete history. Optional `--previous-capture` compares immutable identity continuity, not whether an actual process restart occurred.

The default local ledger is `data/native-production.db` for production and
`data/native-sandbox.db` for sandbox, matching the Windows native startup scripts.
Use `--database <path>` only for an explicit alternative ledger. A missing native
database remains unavailable; the collector never substitutes `data/evplatform.db`
or interprets the demo inventory as a production inventory check. Earlier captures
used the demo ledger by default and must be interpreted with that limitation.

For ProphetX, the collector additionally reads exact order, paginated matching
detail, order-history and wallet responses for locally managed native order IDs.
Account/environment identity must match before those reads. A maximum of 20
owned orders is allowed per capture; larger inventories are reported as bounded,
not silently truncated. The offline verifier replays raw pages and source clocks
through the same financial normalizer as the runtime. It checks fee fallback,
filled stake, matched profit and wallet commissions without allocating aggregate
fees to invented individual fills. Pagination exhaustion is checked again; a
saved `complete` flag is insufficient. A matching financial report still does
not certify submission, private synchronization, restart, cash settlement or
position release.

Files are private account records protected with the existing owner-only storage helper. They remain under ignored `data/`, outside source control. Raw JSON responses retain exact decimal amounts and order/trade IDs, with authentication material redacted. Coverage summaries contain counts and account fingerprints, never secrets. The artifacts include current source-build and content hashes for reproducibility; these hashes are not an independent signature by the exchange and do not make manually modified evidence trustworthy.

## Actual read-only result: 2026-10-21 UTC

Protected evidence is stored in `data/native-lifecycle-20261021-readonly/` as `account_history.json`, `coverage.json` and `lifecycle_review.json`.

| Account | Authenticated scope | Served history result |
| --- | --- | --- |
| Kalshi production | Verified native identity | Current orders 0, fills 0, settlements 0; returned pagination exhausted. Archived history and retention are outside this capture. |
| Novig production trading wallet | Verified native identity | Orders across all five statuses 0; fills 0; settled positions 0; one funding transfer. Funding is not evidence of execution or settlement. |
| ProphetX production | Verified private sign-in identity | Original capture: last seven days had fills 0, one wallet transaction and an unverified empty-order null cursor. A later narrow compatibility repair completed that requested window; details are in [ProphetX reconciliation](PROPHETX_RECONCILIATION.md). Reporting remains eventually consistent. |
| Local native OMS | Database read transaction | No native orders, fills, reconciliations or settlements recorded. |

The verifier reports `no_native_oms_orders_to_validate`. These are genuine read responses, **not successful order-to-settlement tests**. No real orders or funds were changed by this capture.

## What must still be observed

For each intended account, environment, market-rule family and application build, genuine event-time evidence must link the durable client intent to native acknowledgement, individual fills, final state and actual settlement cash/fees. A complete validation also needs position release, restart/recovery with a durable order, settlement idempotency and partial-fill/cancellation races. Current table rows cannot retrospectively prove that a commit preceded transmission. A historical manually entered trade cannot certify this application's OMS. Two read snapshots cannot prove a restart happened.

The verifier intentionally marks those event-time checks missing, even when historical money totals match. Native order/fill history must be complete and agree exactly with durable quantities, costs, payouts and actual fees. Missing collections, scope differences, inconsistent identities, incomplete pagination and invalidated evidence cannot count as a match. ProphetX needs synchronized fill and wallet-ledger fee attribution; a reporting summary alone does not establish its final financial state.

## Event-time instrumentation added October 21

New native orders now retain `native_lifecycle_trace` records with the exact intent hash, account, environment, market, software build, manager run and process instance. The application records the return from the durable intent commit before entering the transport. That commit receipt uses the existing submitting-state transaction. The dispatch-entry observation is persisted with the subsequent acknowledgement or unknown-outcome transaction; a process crash before that write deliberately leaves dispatch unproven. No additional disk barrier was added before transmission.

Recovery, complete financial reconciliation, accepted settlement and duplicate settlement calls also leave observations. Reloading a manager in the same process does not count as a new process. The read-only history capture includes these records; its `application_trace` section rejects conflicting identities, duplicate sequence numbers and regressing process clocks. Monotonic clocks are never compared across processes. These are locally recorded application facts, not exchange-signed proof. A settlement callback being repeated does not establish where that callback originated.

Existing orders cannot gain missing historical observations retroactively. No test fixture or application trace alone issues launch evidence. Native histories, exact paid cash/fees, release of actual positions, and independently reviewed runtime scope still need to agree. One ordinary filled bet also cannot exercise partial-fill cancellation races, interruption handling or every settlement exception. Keep those scenario checks distinct and use an authorized sandbox where available.

Offline fixtures exercise parser/accounting invariants and no-mutation boundaries. They are useful regression coverage, not certificates of real exchange connectivity or production behavior. The report never produces a `LaunchEvidence` object, changes launch profiles, or alters risk controls.

## Official contracts consulted

- Kalshi: [orders](https://docs.kalshi.com/api-reference/orders/get-orders), [fills](https://docs.kalshi.com/api-reference/portfolio/get-fills), [settlements](https://docs.kalshi.com/api-reference/portfolio/get-settlements).
- Novig: [official documentation index](https://docs.novig.com/llms.txt) and its [v3 OpenAPI specification](https://docs.novig.com/api-reference/spec-files/openapi-v3.json) for status-filtered orders, fills, settled position history and personal wallet transactions.
- ProphetX: [order lifecycle](https://docs.prophetx.co/docs/direct-link-order-lifecycle), [wallets](https://docs.prophetx.co/docs/wallets), [field identities](https://docs.prophetx.co/docs/direct-link-ids-and-field-names) and [sandbox notes](https://docs.prophetx.co/docs/direct-link-sandbox-notes-and-faq).
