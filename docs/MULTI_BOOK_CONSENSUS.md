# Multi-book consensus research

`multi_book_consensus` is a separate, read-only sportsbook-pricing profile. It
uses the existing ParlayAPI and Oddize normalized quotes and the existing
four-venue native comparison path. It does not alter the sharp-book profile,
strict source-verification rule, risk engine, or order authorization.

Enable it in the local production workspace through **Settings** or the
supported `POST /api/settings` body `{"multi_book_consensus_enabled": true}`.
The default is disabled in new workspaces. It never enables automatic orders.
The configurable `multi_book_consensus_books` list is an allowlist of reviewed
canonical sportsbook identities; exchange prices and DFS pick'em payouts are
excluded. Keep `auto_trade=false` and `kill_switch=true` during research.

For a scheduled pregame market, the strategy requires two complete opposing
prices per bookmaker, exact normalized market identity, and at least two
distinct eligible sportsbooks. The same FanDuel quote supplied by both APIs
counts once. Eligible copies are selected by the existing source-evidence
ranking, not by which price creates a larger edge. Explicit suspension,
withdrawal, degraded source, topped-up rows, and stale cached boards are
excluded. Unknown active status or unknown exact quote-observation age remains
visible and research-only under the configured provider-trusted delivery and
indicator limits. There is no hidden five-second claim.

When every contributing book is covered by an existing exact sport/market
research prior, its book weights are renormalized. If any contributing book is
outside that prior, **all** books receive equal neutral weights; the dashboard
labels this provisional choice. The strategy uses the prior's devig method
where available. Per-book no-vig probabilities, effective weights, cross-book
disagreement, and leave-one-book-out probabilities are exposed. Two books are
marked as a baseline; three or more are separately marked as corroboration.
Neither label is statistical calibration.

Current Oddize cross-provider matching consumes the existing REST snapshots
without extra API calls. It joins game moneylines, spreads, and totals only
when event participants, scheduled start, line, and both outcomes match a
Parlay research market exactly. A small, reviewed NBA team-code dictionary is
supported; unknown team codes and start-time differences remain unmatched.
Oddize selection-active status is unknown. Oddize player props have no period
field in the reviewed API schema, so they are not merged with full-game Parlay
props as if that period were proven. The existing Parlay prop normalization
continues to provide exact two-sided research markets where available.

Native research comparisons use the existing Kalshi, Novig, ProphetX, and
Polymarket US matchers, executable depth, payout units, and fee calculations.
Gross EV, fee-adjusted EV, and any configured uncertainty adjustment are
separate fields. The 0%, 1%, and 2% source-uncertainty examples are **unapproved
illustrations**, not a live default. Without an explicitly configured
allowance, net after uncertainty is unknown and no comparison is labeled
live-execution eligible. The UI keeps expired estimates historical with their
original clocks. The signal ledger retains market inputs and policy versions
for later replay and calibration; a positive estimate is not evidence of
profitability.

Native catalog coverage still depends on each venue's exact-event matcher.
The code does not broaden its event-time tolerances or equate different
settlement questions. No separate cross-exchange arbitrage scanner is present
in this checkout; a cross-venue price discrepancy must not be labeled a
guaranteed arbitrage without compatible outcomes, collateral, depth, fees,
and settlement review.
