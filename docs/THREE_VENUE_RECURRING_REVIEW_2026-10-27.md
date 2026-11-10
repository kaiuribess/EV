# Kalshi, ProphetX and Polymarket US recurring review — pending

This is a read-only evidence and owner-review record. It grants no trading
authority. `unit_size_usd=5` remains a reporting unit. Production stays
`auto_trade=false`, `kill_switch=true`.

## External inventory and account facts, October 27

- Kalshi: authenticated primary-account reads show no resting orders. The raw
  NHL YES order `01a17983-3b70-7ecd-863e-7847f26d9cf0` settled NO on
  October 27; its $0.336000 purchase and $0.010400 fee are historical loss,
  not current exposure. The older streamer-award YES fill
  `01a16a2a-1810-745c-ba45-43790063ecc2` remains 19.70 contracts on exchange
  index 0. Its $9.653000 purchase plus $0.344700 fee reserves $9.997700 of
  possible loss. Index-0 available cash was $39.6559; indices 1–3 reported
  zero. These debits are already reflected in venue cash and are not deducted
  a second time. The external orders remain outside OMS history. A fresh
  private-state fence and proposed-market index cash are still required at
  submission.
- ProphetX: the raw $1 stake order
  `partner_f951c27d-3ad4-4131-9b39-25e2688cebfe` is settled LOSS. The current
  authenticated wallet reported $49.10 available cash, zero GEC, zero matched
  and unmatched stake; the historical TRADE debit is $1. The external order
  stays in resolved history, not OMS. A complete account-specific current
  inventory, funded-cash/LEC scope, trading permission and order-specific
  location check remain necessary for execution.
- Polymarket US: raw order `DB9JPZX011MQ` is filled and its position-resolution
  activity shows one long contract reduced to zero, with $0.37 realized loss
  and $0.02 fee. No open order or position for that slug remained. The balance
  read still showed `balanceReservation=25`, no `lastUpdated` clock and no
  documented stable cash-account/trading-scope attestation. The $25 is treated
  as unexplained account state, not free buying power or a certified exposure
  cap. It blocks global execution until its meaning and any other exposure are
  established. Polymarket US remains research-only.

The authenticated captures and original receipts are retained under `data/`;
`data/external_native_positions.json` binds their hashes. The captured account
facts expire for execution and must be refreshed through the normal runtime.

## Current market review

The bounded Kalshi NBA crosswalk produced 20 structural event matches and 43
moneyline leads. None had execution-grade quote/status evidence, and no
source/Kalshi exceptional-settlement equivalence was verified. The ProphetX
NBA crosswalk produced four structural event matches and 20 moneyline leads;
none verified sportsbook market status. These reports are local review leads,
not executable mappings or current price claims. No native execution mapping
or launch profile was approved by this review.

## Recurring approval to prepare after factual gates pass

One owner approval must bind the actual account, venue, reviewed market-family
and exact mapping rules, source books/status/age policy, winner-only payout
assumption or strict settlement policy as applicable, 1% payout uncertainty
deduction, 1.5% minimum net EV, quarter-Kelly, fees, build/pricing/mapping
digests, and an expiry. Separately state numerical maximum per-order debit and
loss, total open exposure, daily debit, daily realized loss, and order-frequency
limits. `unit_size_usd=5` supplies none of those limits. The existing Novig
draft and previous raw smoke trades do not approve this scope. Until the
specific values and source uncertainties are accepted in the application,
there is no recurring authorization to load or arm.

Official references: Kalshi portfolio positions, fills and settlements
(https://docs.kalshi.com/api-reference/portfolio/get-positions), ProphetX wallet
(https://docs.prophetx.co/docs/wallets), Polymarket US balances and portfolio
activities (https://docs.polymarket.us/api-reference/account/get-account-balances,
https://docs.polymarket.us/api-reference/portfolio/get-activities).
