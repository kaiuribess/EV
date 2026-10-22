# Novig v3 live book update observation

Reviewed October 21, 2026 (America/Los_Angeles). This is a read-only production
stream observation. It does not establish location eligibility, settlement
equivalence, order permission, or live execution success.

The [official Novig book guide](https://docs.novig.com/api/streaming/book)
documents `add` and `remove` deltas and says a partial fill arrives as a
`remove` followed by an `add` of the remainder. On two explicitly subscribed
production market IDs, the actual feed also sent `update` deltas with only
`kind`, `orderId`, and `remaining`. The previous parser treated these as unknown,
invalidated the synchronized book, and reconnected. In a bounded 45-second
read-only sample, it received 795 ordinary frames and five invalidations; all
five captured parser failures were this `update` shape and all five reduced the
remaining size of a known order. A separate 40-second sample captured two more
such reductions. No raw frames, credentials, or private order data were saved.

The parser now applies an `update` only when it names an existing order and
contains exactly those three fields, with a nonnegative integer remainder no
greater than the prior size. It retains the previously verified price and
outcome. A zero remainder removes the order. Missing orders, increases, extra
fields, or non-integer values invalidate the book and require a fresh snapshot.
Sequence validation and atomic batch application still apply. Tests cover both
the accepted reduction and rejection paths.

The native book mapper advances the source clock only for that updated order.
An untouched order at the same price keeps its older clock, so aggregation
cannot make the entire ladder appear fresh from one partial fill.

This observation is limited to the sampled wire behavior. If Novig changes or
documents the `update` form, the parser contract should be reviewed again.
