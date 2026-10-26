# Novig recurring execution review — pending

This is an owner review record, not a launch profile, source-policy approval,
location attestation, or trading authorization. Production remains disarmed.

## Scope and limits supplied by the owner

- Venue: Novig production only. Other venues remain outside this proposed scope.
- Strategy: multi-book sportsbook EV, winner-only normal-result estimate, quarter Kelly.
- Minimum estimated EV: 1.5% after actual fees and a 1% normal-payout uncertainty deduction.
- Monetary ceilings: $25 per order, $25 total open exposure, $25 daily upfront debit, and $25 daily realized loss. The same $25 ceiling is proposed for event, player, venue and correlated exposure. Verified spendable cash is an additional limit.
- Frequency: no daily attempt-count cap requested; the existing one-open-order and one-order-per-minute limits remain. Rejected and uncertain intents still consume daily debit budget.
- `unit_size_usd=5` is for reporting only. It never sets a stake or minimum order.
- The saved `data/operator_launch_limits.json` is an unapplied draft of these values.

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
4. Bind an approved recurring source policy and the $25 risk limits to the
   actual account, mapping, pricing and build digests in the existing launch
   profile. Then record a separate, expiring recurring authorization through
   the supported controller. The old one-shot authorization is not reusable.

Current production state: no launch profile, no executable native mapping,
no recurring authorization, `auto_trade=false`, `kill_switch=true`.
