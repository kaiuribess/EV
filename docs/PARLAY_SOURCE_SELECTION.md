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

## Focused NFL props for Novig research

Production can enable `parlay_rest.focused_novig_nfl_props`. The existing Novig
catalog first finds an exact pregame receptions or receiving-yards identity from
the already normalized source markets. At most two such native books are
prewarmed through the existing read-only subscription path. A current native
book and missing complete/current source pairs trigger a free Parlay `/events`
identity lookup and `/props/coverage` check, followed by a narrowly scoped
`/v1/sports/americanfootball_nfl/props` request only when at least two books
are visible. The request binds the current event ID, exact player, market and
line, and FanDuel/DraftKings/Caesars/bet365 where configured. An obsolete event
ID that yields a different event is rejected. The local scheduler deduplicates
scope requests and caps focused requests at 36 reserved credits per hour by
default; the provider's usage headers remain authoritative.

Each page keeps its original completeness/degradation headers and timestamps.
Pagination does not turn separate pages into an atomic board. REST prop
`last_update` is retained as a stored-row clock, not a quote observation. Only
`last_observed` paired with `last_update_type=collector_observation` is treated
as documented collector observation evidence; active selection status remains
unverified unless separately established. FanDuel selection IDs are retained
with the exact quote when supplied. A non-truncated page can improve reference
completeness, but it cannot by itself authorize a Novig order.
