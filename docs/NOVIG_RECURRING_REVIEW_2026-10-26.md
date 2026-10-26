# Novig recurring execution review — pending

This is an owner review record, not a launch profile, source-policy approval,
location attestation, or trading authorization. Production remains disarmed.

## Scope and limits supplied by the owner

- Venue: Novig production only. Other venues remain outside this proposed scope.
- Strategy: multi-book sportsbook EV, winner-only normal-result estimate, quarter Kelly.
- Minimum estimated EV: 1.5% after actual fees and a 1% normal-payout uncertainty deduction.
- The owner's later instruction supersedes the proposed fixed $25 ceilings:
  size from quarter Kelly and verified available cash rather than treating $5
  as a stake cap. No revised recurring loss, exposure or debit policy has been
  applied to production. A future review must bind the resulting cash-based
  limits to the actual account and explain turnover across repeated trades.
- Frequency: no daily attempt-count cap requested; the existing one-open-order and one-order-per-minute limits remain. Rejected and uncertain intents still consume daily debit budget.
- `unit_size_usd=5` is for reporting only. It never sets a stake or minimum order.
- The saved `data/operator_launch_limits.json` is an unapplied, now superseded
  $25 draft; do not use it as evidence of the owner's latest risk scope.

## Source policy awaiting recurring approval

The earlier approval covered a *single* narrow pilot, not recurring execution.
For recurring review, the proposed scope is pregame NFL receptions and receiving
yards from ParlayAPI, with at least three distinct books among FanDuel,
DraftKings, Caesars and bet365. Stored-row and delivery indicators would need to
be within 30 seconds. Explicitly truncated, degraded, topped-up, stale-cache,
suspended or withdrawn rows would be rejected. Per-selection observation time
and active status may remain unknown only if the owner explicitly accepts that
uncertainty for this recurring scope. A bookmaker heartbeat is not a quote
observation. No source policy is enabled or approved by this document.

## Exact market review in progress

The unapproved Brevyn Spann-Ford 1.5 receptions draft is saved at
`data/native_review_drafts/native-research-188dcc5e02ec81b7668e.json`.
It binds the Oct 26 Dallas–Philadelphia game, Parlay source identity, native
Novig market and both outcome IDs. It explicitly leaves participation and
overtime scope unreviewed. Its captured Over comparison was negative after
fees, and its captured prices expire normally. The draft cannot be turned into
an executable mapping by copying its IDs or extending its quote lifetime.

## Facts still required before a launch profile or authorization

1. Resolve the three other venues' raw orders against authoritative current
   positions and cash, or establish a conservative, documented aggregate bound.
   Novig's separate Charlotte position is reconciled at $0.97175 exposure;
   its debit is already reflected in Novig cash.
2. Obtain listing-bound Novig and sportsbook evidence for the normal NFL prop
   condition, period, overtime and participation. Review one exact current
   native mapping; do not infer these facts from a player-and-line label.
3. Obtain a current Novig account-bound location result using a supported venue
   method. Authenticated trading-key scope and cash alone do not establish it.
4. Bind an approved recurring source policy and explicitly reviewed current
   risk limits to the
   actual account, mapping, pricing and build digests in the existing launch
   profile. Then record a separate, expiring recurring authorization through
   the supported controller. The old one-shot authorization is not reusable.

Current production state: no launch profile, no executable native mapping,
no recurring authorization, `auto_trade=false`, `kill_switch=true`.

## Bounded read-only follow-up on October 26

Authoritative account reads found the three known raw-API orders as external
inventory, not OMS orders. Kalshi's NHL order
`01a17983-3b70-7ecd-863e-7847f26d9cf0` is executed for 0.60 YES
contracts with no remainder and remains an unsettled position. The second
Kalshi position is a separate, earlier executed order
`01a16a2a-1810-745c-ba45-43790063ecc2` for 19.70 YES contracts in
`KXSTREAMERAWARDS-GAMER26-JYN`; its fill was at $0.49 per contract. The
complete unsettled position page reports $0.336000 and $9.653000 of cost,
respectively, plus $0.010400 and $0.344700 of fees. Both are on exchange
index 0. These costs are already reflected in the reported cash balance and
must not be debited a second time. Their combined original cost and fees is
$10.344100, but no current settlement payout is certified.

ProphetX order `partner_f951c27d-3ad4-4131-9b39-25e2688cebfe` has one
USD-risk unit filled and zero open quantity. Its `open` status denotes an
unsettled matched position, not a resting remainder. The authenticated wallet
reported $49.10 available, $1.00 matched-order balance, zero unmatched-order
balance and zero GEC. A market-filtered transaction read found the $1 trade;
the position is not settled. Polymarket US order `DB9JPZX011MQ` is filled
BUY_LONG for one contract at $0.37, with $0.02 reported commission. Its
complete current position page reports one unsettled long and no open orders.
The balance page reports $49.61 buying power but no `lastUpdated` time and an
unexplained $25 `balanceReservation`. The published balance schema lists that
field without enough semantics to allocate it to this order. Keep the
Polymarket US inventory review pending; do not infer zero other exposure or
cash verification from the $49.61 number.

The current Novig catalog still lists
`01a17652-f647-7623-80da-3b945ce96b12` as OPEN, with `RECEPTIONS`,
strike 1.5, two named outcomes, `voids=FMV`, and `WHEN_LIVE` fees. It does not
provide listing-bound participation, period or overtime terms. Its current
price is different from the saved Brevyn Spann-Ford draft, and no current
qualifying sportsbook estimate overlaps it in the production state. Neither
the old quote nor this catalog read is an executable mapping review.

Novig's published eligibility guidance says its app checks the device's
physical location when opened to trade. A new permission prompt is not
required if permission was previously granted; the owner's app opened without
a prompt. The Trading API/runtime still has no account-bound location expiry
field to populate its current preflight requirement. Novig's error contract
explicitly says an authenticated read admits stale geolocation and only a
placement can return `GEOLOCATION_EXPIRED`; other HTTP 451 codes reject a
restricted network, failed location, or blocked device. Therefore another
successful account read or Chrome visit cannot certify current placement
eligibility. This is a preflight/venue-contract mismatch requiring an explicit
review of venue-enforced-at-submission handling; do not fake an expiry time or
submit an order merely to probe location.
Sources: https://support.novig.com/en/articles/16194031-eligibility-legality
and https://docs.novig.com/api/errors

`novig_ioc_win_loss_halt_on_exception` is the only settlement route the
current adapter supports for this recurring scope. It reconciles a single
managed order per native market on an ordinary WIN/LOSS grade. FMV, push,
remediation or shared/netted positions halt new execution for manual cash
review; they are not represented as ordinary losses or refunds. A recurring
profile must select this exact narrow policy and disclose the halt behavior.
Contract-specific settlement controls the trade, per
https://support.novig.com/en/articles/9612523-market-rules; general FVS
guidance at https://support.novig.com/en/articles/16163706-what-is-fair-value-settlement-fvs
does not establish the participation or overtime term for this listing.

No recurring owner approval is ready to record yet. The remaining factual
inputs are complete aggregate exposure review (including Polymarket US's
reservation), listing-bound normal-condition and participation rules, a
reviewed location-enforcement route consistent with Novig's API contract, and
one current exact mapping with eligible references. The existing $25 limits
draft and proposed source scope remain
unapplied and unapproved. The later owner instruction supersedes the fixed $25
draft but does not turn a $5 reporting unit into an execution limit.
