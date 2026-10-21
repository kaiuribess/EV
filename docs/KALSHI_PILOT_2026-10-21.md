# Kalshi NBA pilot: no real order

Read-only review on October 21, 2026, around 20:25 UTC. No order, mapping, launch profile, or live permission was created. This is a dated snapshot, not a current quote or reusable trade instruction.

The production Kalshi listing `KXNBAGAME-26OCT21ATLORL-ORL` (Orlando wins) was active with a YES ask of $0.55 and a NO ask of $0.46. Its listing says a game canceled or not started within 48 hours resolves to a fair price. The [official NBA winner terms](https://assets.kalshi.com/contract_terms/BASKETBALLGAMEWIN.pdf) and [public market endpoint](https://docs.kalshi.com/api-reference/market/get-market) govern the actual contract. This is not an unconditional sportsbook moneyline payout.

A single metered Parlay NBA game-odds request for this exact event returned:

| Book | Orlando / Atlanta | Book timestamp | Quote-bound active status |
| --- | --- | --- | --- |
| FanDuel | -134 / +114 | 20:25:08 UTC | absent |
| DraftKings | -130 / +110 | 20:25:06 UTC | absent |
| Pinnacle | -127 / +117 | 20:12:36 UTC; `topped_up=true` | absent |

The request returned HTTP 200. Its public prices are discovery inputs only. Parlay's [documented limitations](https://api.parlay-api.com/docs) state that bookmaker verification heartbeats do not certify individual outcomes, topped-up rows can retain old prices, and the FanDuel/DraftKings stream has no guaranteed withdrawal or complete replacement baseline. A separate 30-second authenticated Scale-stream capture saw 36 NBA moneyline offers and no validated observation clock, complete outcome observation, or verified market status. The stream connected and delivered updates, which is not proof that a particular displayed quote was active.

For an **illustration only**, proportional no-vig probabilities for Orlando from the three displayed two-sided prices were FanDuel 0.550657, DraftKings 0.542744, and the stale Pinnacle row 0.548340. Their equal average is 0.547247. The [Kalshi fee schedule](https://kalshi.com/docs/kalshi-fee-schedule.pdf) lists a $0.02 fee for one immediately matched 55-cent contract under the general fee table, giving an illustrative $0.57 total cost and **-$0.022753 expected profit per contract**. This calculation intentionally does not declare a valid fair value: the configured profile needs eligible, current independent books; quote status is absent; fee and fill bounds must be checked at order time; and the settlement rules differ. The NO side is not made attractive by those displayed probabilities either. One contract would fit a roughly $1 test budget, but its size does not create an edge.

The running paper workspace reported zero verified native market mappings and zero native opportunities. Its live-activation report remained `safe_to_arm=false`. The local monitor now includes both Orlando and Atlanta Kalshi winner tickers as **read-only** subscriptions. It does not authorize execution. The real-money pilot is therefore **no trade**.

To reach a defensible first real order, obtain a reference source contract with quote-bound current/active and withdrawal evidence for enough independent books, establish the exact contract settlement match or validate a separate interruption/payout model, bind the current exchange fees and executable depth, and pass the scoped account/order/settlement release checks. Re-evaluate a fresh market when those inputs exist; never reuse this snapshot as a signal. The [Kalshi demo account](https://help.kalshi.com/en/articles/13823775-creating-and-using-a-demo-account) has separate credentials from production and can validate exchange order handling with mock funds, but cannot establish real sportsbook quote quality or actual cash settlement.

Offline verification on this build: 1,946 backend tests passed with 89% aggregate coverage; 97 frontend tests passed and the Vite build succeeded. These tests do not constitute an authenticated production order-to-settlement test.

At 23:23 UTC on October 21, a fresh authenticated production account probe
confirmed $50.00 spendable cash, trading permission, an attested account
location, and zero open orders or positions. The Kalshi market stream was
authenticated. The paused production workspace now has a **one-attempt, $1
pilot cap**, quarter-Kelly sizing, and a matching local limits draft. This
selects Kalshi as the first operational venue; the Novig monitor may remain
read-only. No reviewed launch profile or native market mapping was created.
The dashboard still reports `safe_to_arm=false`, zero verified native markets,
zero eligible real reference markets and zero native candidates. The later
Orlando/Atlanta market price moved materially during this review; old odds
must never be reused as an EV signal. The remaining quote-status, settlement
and actual lifecycle evidence above still block automatic orders.

At 23:44–23:47 UTC, a new public Kalshi NBA catalog read returned 49 open
events, including the still-active Atlanta/Orlando winner listings. The listing
again identified an original October 21 game and the 48-hour/fair-price
cancellation condition. Kalshi's `occurrence_datetime` in this listing equaled
its `expected_expiration_time`; it was not treated as the sportsbook's kickoff
timestamp. Parlay's contemporaneous authenticated game-board snapshots did not
provide the documented quote-bound observation pair or verified outcome-level
active status for the sampled Pinnacle and FanDuel rows. The newer nonsecret
field counts are in [the baseline audit](PARLAY_BASELINE_2026-10-21.md).

The production dashboard still showed an authenticated, streaming Kalshi
monitor, `auto_trade=false`, no reviewed launch profile, no native mapping,
no qualified real candidate and **zero native order attempts**. A current
readiness check returned `safe_to_arm=false`. The fresh public listing is a
discovery lead, not a validated EV signal. No $1 production pilot was sent.
