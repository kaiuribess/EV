# Parlay sportsbook source selection

The multi-book and sharp-book probability weights have **not** changed. When
Parlay REST and WebSocket supply the same sportsbook and exact market, pricing
selects one copy using quote evidence rather than whichever price creates more
EV. It first rejects ineligible copies, then prefers quote-bound observation
and verified market status, then the newer row-specific stored/observation
clock. A FanDuel or DraftKings stream row breaks a tie with a REST row. A
bookmaker/fixture heartbeat does not outrank a newer price row. Another API
copy of the same sportsbook remains one book in the consensus.

Parlay's October 23 support clarification is treated as a description of the
current feed, not a permanent latency guarantee:

| Field | Meaning in this integration |
| --- | --- |
| REST `verified_at`, `is_current` | Bookmaker/fixture poll indicators, not quote verification. |
| REST `line_changed_at_ms` | Stored served-row age for the two-sided pair; not necessarily a price move. |
| REST `stale_seconds` | Not used as exact quote age. |
| WebSocket `price_age_s` | Age since price movement, displayed separately from quote observation. |
| WebSocket `last_observed_ms` on Pinnacle current-state | Quote-bound observation when the documented row conditions hold. |
| FanDuel outcome links | Historical OPEN/ACTIVE hint, not present availability. |
| `topped_up`, `X-Parlay-Stale`, degraded source | Exclude affected rows from trusted research; `X-Result-Degraded` names affected books. |

Research can label unknown selection observation or active status. It cannot
promote that uncertainty into live-order eligibility. The strict five-second
rule and the separate owner-approved pilot policy remain unchanged. Parlay
Circa is not requested. BetOnline is requested only for NFL, college football,
and MLB game lines, never player props. REST remains for discovery and bounded
recovery; WebSocket updates do not guarantee a complete current board or a
removal event for an unchanged FanDuel price.
