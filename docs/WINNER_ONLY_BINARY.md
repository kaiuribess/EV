# Winner-only binary pricing

`PricingConfig.normal_outcome_policy` selects `strict_settlement_equivalence`
(the default) or `winner_only_binary`. The setting can also be applied through
the existing `sport:market_type` pricing override. It is part of the launch
pricing digest; changing it does not approve a research profile, enable orders,
clear the kill switch, or authorize a new venue or market.

`winner_only_binary` estimates EV **conditional on a normal WIN/LOSS
resolution**. For a matched native YES selection, the engine uses the
devigged, weighted sportsbook win probability and computes:

```
winner-only estimated net EV = p(win | normal resolution) * native winning payout
                               - bounded acquisition cost - bounded fees
ROI = estimated net EV / reserved cost including fees
```

The native instrument supplies the payout denomination: ordinary Kalshi
binary contracts pay $1; current Novig contracts pay $0.01. Exceptional
settlements are *outside* this estimate. The model does not claim their
probability is zero, that the books and venue have identical rules, or that
any cancellation will be refunded. The strict policy remains available and
retains its settlement-equivalence and exceptional-state bound checks.

The source mapping must separately review the ordinary winner condition,
including event, team/player, statistic, direction, exact line, period and
overtime treatment. A `normal_outcome_verified` flag requires its own
`normal_outcome_evidence`; it does not set
`settlement_equivalence_verified`. The quote also carries the full canonical
market fingerprint, so reusing a market ID after changing a player, line or
period cannot silently price the new market. An integer spread/total with a
normal-completion push is rejected until an unconditional probability model
exists. A three-way regulation market must contain the draw in each complete
bookmaker outcome set. One-sided scorers and split-stake Asian lines cannot be
priced as ordinary two-sided binary references.

Normal-outcome pricing does **not** relax reference evidence. Production still
requires complete same-book outcomes, current and status-verified source
observations within the exact registered TTL, the configured number of
distinct books and anchor, agreement checks and the sensitivity model.
Bookmaker observations from multiple vendors count once. Native matching,
fresh executable depth, fees, available cash, quarter-Kelly, risk, launch
scope and one-attempt authorization still apply. The current research
profiles are paper-only; this policy alone cannot produce a live order.

## Source-contract notes checked 2026-10-22

| Source | Useful coverage | What still prevents broad live eligibility |
| --- | --- | --- |
| ParlayAPI Business | `/v1/sports/{sport}/odds` game lines, `/props` and `/props/markets`, `/v1/ws/odds/{sport}` changes, `/event-markets/search` discovery. | The speed page describes `verified_at` as price confirmation, but detailed docs say bookmaker `verified_at`/`is_current` can be fixture-level heartbeat, not verification of the displayed outcome. `line_changed_at`/`last_update` may be retained-row clocks. Only quote-bound, documented observation and status evidence qualifies. A Business 1 s WS interval is coalescing, not an upstream age bound. Prop collection is often 5–15 s and broad snapshots may be incomplete/degraded. |
| OddsPapi v4 | Fixture `/v4/odds` provides bookmaker/market/selection activity and both sides when offered. | `bookmakerChangedAt` and `changedAt` are movement clocks; fixture `updatedAt` is not current selection observation. No v5 entitlement is assumed. |
| Oddize | Game and selected prop/alternate prices; `ts` is a price observation clock. Prop categories are queried explicitly and cost credits. | The published Price/PropRow schemas have no affirmative active/suspended selection field. A listed price is not a status guarantee. |
| Kalshi / Novig | Native market identity/status and current executable books/depth. | The target venue's own price is excluded from independent fair-value references. Contract rules, quantity grids, fees and account scope still require market-specific review. |

Parlay's `/ev` is its own Pinnacle/Novig comparison, not a substitute for this
project's independent, fee-aware price. Coverage and source-health endpoints
help discovery and feed diagnostics but do not affirm the exact selection's
current status. The five-second source-evidence rule is unchanged. There is
no universal claim that props or in-play markets can meet it.

References: [Parlay speed](https://parlay-api.com/speed), [Parlay API
documentation](https://parlay-api.com/docs), [Parlay WebSocket
contract](https://parlay-api.com/docs/websocket), [OddsPapi v4
odds](https://oddspapi.io/en/docs/get-odds), [Oddize OpenAPI](https://oddize.com/api/v1/openapi.json),
[Kalshi order books](https://docs.kalshi.com/getting_started/orderbook_responses),
[Novig monetary units](https://docs.novig.com/api/concepts/money),
[Novig contract catalog](https://support.novig.com/en/articles/16083642-contracts).

Actual order/fill/settlement accounting is separate from the conditional
estimate. The core ledger records authoritative payout, refunded fees and
cost, including an exceptional partial or refunded payout, without converting
it into a modeled loss. Venue adapters currently halt and retain exposure
when a settlement grade cannot be attributed from supported cash evidence;
that is not a verified refund or realized P&L.
