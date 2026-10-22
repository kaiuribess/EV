# Novig Paper connector evidence (2026-10-21)

The configured Novig Paper trading key authenticated against
`api.paper.novig.com`. A separate Paper management key verified its `trading`
scope and matching local signing-key fingerprint. The Paper cash wallet had
$10,000 in play money. The one-time funding journal transferred $2.00000 of
play money into the existing trading subaccount; an independent transfer-status
read returned `Applied`. Repeating the funding command read the same transfer
and did not send another one.

One Paper-only, price-limited IOC went through the central native order manager:
durable intent, shared risk reservation, one-use dispatch authority, native
Novig request, independent order/fill history, and local SQLite accounting.
Novig reported 100 whole cent-contracts filled for $0.46500 with a $0 fee.
The pregame purchase has a $1 gross payout if its outcome wins. A subsequent
authenticated wallet read showed $1.53500 play cash, one position, no open
orders, and inventory matching the local ledger. Repeating `--place` only
reconciled the existing order; it did not send another.

The first reconciliation quarantined the fill because the exchange match clock
was 143 ms ahead of local receipt. The source and local timestamps were both
retained. The code now accepts at most one second of source clock skew while
rejecting larger future timestamps. A narrow, audited repair replayed all
retained reports, compared a fresh Novig order/fill read, preserved the
original conflict and reports, and let the ordinary OMS reconcile the same
order. The running process remained risk-halted after the conflict; a fresh
process recovered the financially reconciled order without a second attempt.

These commands are local and pinned to Paper credentials and the Paper host:

```powershell
& '.\.venv\Scripts\python.exe' 'scripts\fund_novig_paper_probe.py'
& '.\.venv\Scripts\python.exe' 'scripts\probe_novig_paper_order.py'
```

The funding command is idempotent through `data/novig_paper_probe_transfer.json`.
The order command defaults to read-only. Its `--place` flag has already used the
one allowed attempt in `data/novig_paper_order_probe.db` and will only reconcile
that order. The `--repair-clock-skew` path was used once for the recorded
false-positive conflict; it does not place orders or alter financial totals.
Do not delete these local journals or database to repeat the test.

This proves Paper transport and one order-to-fill path. It does **not** prove
production placement, an EV signal, source-to-exchange settlement equivalence,
Novig production location eligibility at placement time, or final settlement.
The native-only test market is deliberately excluded from sportsbook fair
pricing and live launch profiles. The position remains open until Novig grades
that Paper event; final cash settlement can be checked afterward.

An authenticated, read-only recheck on October 22 found the existing Paper
market still `OPEN`. The saved fixture starts on December 26, 2026 UTC, so this
order cannot yet provide genuine paid-settlement evidence. The already used
one-attempt probe was not reset and no further Paper order was placed.

The configured Novig production trading key and production management key also
passed separate read-only authentication and permission checks. At the time
of the probe, the production subaccount reported $25.00000 available, zero open
orders, and zero positions. No production order was sent.

Official contracts consulted:
[Novig environments](https://docs.novig.com/api/environments),
[account model](https://docs.novig.com/api/concepts/account-model),
[funding](https://docs.novig.com/api/subaccounts/funding),
[execution](https://docs.novig.com/api/execution/orders),
[money units](https://docs.novig.com/api/concepts/money), and
[fees](https://docs.novig.com/api/concepts/fees).
