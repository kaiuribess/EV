# ProphetX NBA recurring scope for owner review

Status: **owner approved and saved on 2026-10-28; execution pending live gates.** The account-bound profile and recurring authorization expire on 2026-10-29 at 20:14:49 UTC. Production remains disarmed until the existing release gate passes.

## Contract rule

The [certified ProphetX NBA contract filing](https://www.cftc.gov/filings/ptc/ptc0710269372.pdf), Appendix A, identifies a moneyline as the designated club winning the specified event. NBA contracts include overtime unless the contract specifies a shorter period. The approved family token proposed here is `moneyline|moneyline|default_full_game`, limited to an active NBA event whose unqualified native market has `name=Moneyline`, `type=moneyline`, `sub_type=moneyline`, and no period field. First-half, qualified, ambiguous, or changed questions do not match that token. Each game still needs exact event, teams, start time, outcome IDs, strike IDs, active status, fresh book, and current price ladder.

The [Parlay WebSocket guide](https://parlay-api.com/docs/websocket) describes `h2h` as a full-game moneyline and lists period markets separately. This supports the ordinary game-winner question; exceptional settlements remain excluded from the `winner_only_binary` estimate and require actual venue accounting.

## Proposed 24-hour execution scope

ProphetX production account fingerprint: `183b7b16…cc2e57a` (full fingerprint is read from the authenticated runtime when the profile is saved). Prematch NBA full-game two-way moneylines only. Parlay FanDuel, DraftKings, Caesars, and bet365, provisional equal 25% weights; at least three complete independent two-sided books. Unknown selection observation age and active status are disclosed assumptions. Known stale, degraded, topped-up, truncated, suspended, withdrawn, or incomplete records are rejected. Each available stored-price or polling indicator and local delivery must be at most 30 seconds old. Polling indicators are not presented as quote observations.

Price with the owner-assumed ProphetX fee of **2% of winning profit**, explicitly *not an exchange-verified account fee*. Deduct 1% of normal payout for source uncertainty. Require at least 1.5% estimated EV after both deductions. Use cash-only quarter-Kelly, $25 per-order debit, $25 total ProphetX exposure, $25 daily new-order debit, $25 daily realized-loss limit, at most one open order, and at most one new order per minute. `unit_size_usd=5` is a reporting unit. Use `external_exposure_scope=profile_account`; other venues remain disabled. Authorization expires after 24 hours and never renews itself.

## Current production state

An authenticated read reported $49.10 available cash and subsequently reconciled zero open orders and positions when the private heartbeat was current. This result is transient and is rechecked by the running service. Trading permission and order-specific location eligibility were not established by that read. At the deployment check, there were no current NBA moneyline fair-price estimates. The saved authorization does not release the kill switch until current account, market, liquidity, lifecycle, settlement, and risk checks pass. No price, historical smoke trade, or contract filing alone establishes those live facts.
