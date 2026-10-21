# NBA moneyline settlement evidence

Reviewed October 20, 2026 (America/Los_Angeles). This is a read-only evidence review, not an execution approval or a claim that the sources have equivalent contracts.

**Finding: no exact common settlement scope was verified between the sampled Novig or Kalshi NBA winner contracts and Pinnacle/FanDuel/DraftKings references.** The ordinary completed-game sporting question overlaps, but cancellation, interruption, postponement, and payout rules do not. Do not assign these offers the same canonical settlement fingerprint by dropping those differences.

## Official rule evidence

| Source and scope | Verified facts relevant to matching |
|---|---|
| Novig NBA Winner, ticker `NBAWIN`, rulebook `NBA-001` | Full game includes overtime. An official winner can settle a shortened game or a forfeit after commencement. The ordinary postponement/resumption window is 48 hours; postseason is 45 days. Voids use Fair Value Settlement. The operative full-game payout provision sends a game without an official winner to void; the explicit 50/50 tied payout applies to designated segments. |
| Pinnacle NBA game period | Includes overtime; fewer than 43 minutes voids game-period bets. General rules void fixtures not started within 12 hours, or started but not completed within 30 hours, subject to sport/market exceptions. Venue changes ordinarily void. |
| FanDuel **Indiana**, effective October 15, 2026 | Includes overtime unless specified otherwise. Suspended NBA games require all 48 minutes within 24 hours of the original start; otherwise wagers void unless already determined. Failure to start on the venue-local scheduled date voids. NBA venue changes void earlier wagers. These are Indiana rules, not proof of the jurisdiction behind a Parlay `fanduel` quote. |
| DraftKings | The official basketball-rule URL returned a page shell without the operative rules in this review. No current jurisdiction-specific rule equivalence was verified. Older retail rules and third-party copies were not substituted for the rules governing the actual feed. |

Sources: [Novig Contracts: NBA Winner Series](https://support.novig.com/en/articles/16083642-contracts), [Pinnacle betting rules](https://www.pinnacle.com/en/future/betting-rules/), [FanDuel Indiana house rules](https://www.fanduel.com/fanduel-sportsbook-house-rules-in), [DraftKings basketball rules](https://sportsbook.draftkings.com/help/sport-rules/basketball).

The Novig PDF is a contract-specific filing: its filled variables identify Knicks–76ers, October 20, 2026, Full Game. Its internal document title includes `NBA-winner-anchor-2.0-a.docx`; this is not independent proof of a universal current version. No separate publication/effective date was verified in the PDF. Applying it to another market still requires a verified binding to that listing. The stable contracts page above supplies the current download; the exact download inspected was [NBA Winner Series PDF](https://novig.intercom-attachments-1.com/i/o/y5ui11zw/2754156010/d646dde1501437e4dc9bcbf3d900/NBA%2BWinner%2BSeries.pdf?expires=1792638000&req=diciEsh7m4FeWfMW3Hu4gUmdQQ5VZjlz%2FC%2FLnzv26nPcLEAzSW1QEAg5G%2FPA%0Ayg%3D%3D%0A&signature=295fd72322911c945f5288a554584112f9a024f947f1856bedd43d59be27ccfa). That download URL expires. SHA-256 of its 301,870 bytes: `2733d7280dced1d33fc85480c04a343ca85a73c13a473dda00159202330b4974`.

Novig's separate [Fair Value Settlement guide](https://support.novig.com/en/articles/16163706-what-is-fair-value-settlement-fvs), dated September 2, 2026, confirms that void settlement pays a pre-disruption fair value rather than refunding entry cost. A generic sportsbook stake-refund rule is therefore not an interchangeable contract. This also requires an expected-payout model that represents non-binary settlement states instead of assuming every outcome pays only zero or one.

## Bounded Kalshi comparison

A public `GET /trade-api/v2/series/KXNBAGAME` returned [BASKETBALLGAMEWIN contract terms](https://assets.kalshi.com/contract_terms/BASKETBALLGAMEWIN.pdf), series `last_updated_ts=2026-10-19T16:32:29.600271Z`. The five-page PDF includes overtime by default, a 48-hour delay/resumption framework, fair-price settlement for certain cancellations/incomplete games, and official-result settlement for an abandoned full game if 48 minutes were played **or** the league declared its outcome official. Ties without a listed tie strike pay 0.50 per side. These conditions also differ from Pinnacle. SHA-256 of its 55,313 bytes: `7db0904a1d578a2bca0f6a1f55d0d7962d32b76ce5fafb0e23823e5444381556`. The [documented series endpoint](https://docs.kalshi.com/api-reference/market/get-series) supplies the contract link; no authenticated account request was needed.

## Local observation evidence and unknowns

### ProphetX follow-up, October 21

The current [official filing index](https://www.prophetx.co/lobby/t-c/filings)
links the July 9 NBA mainline and player-statistic filings. The
[mainline terms](https://framerusercontent.com/assets/dPuLRtFql7mQm5oFvuAiCOzSAdk.pdf)
include overtime by default, return collateral for tied full-game moneylines
and exact spread/total pushes, and adjust expiration when an event is postponed,
suspended or rescheduled (PDF pages 3–7). Page 7 was also rendered and visually
checked. This does not establish Pinnacle's 12/30-hour cancellation windows or
43-minute completion rule. Keep these contracts separate pending a reviewed
pricing transformation or evidence of exact applicability and equivalence.

The [player-statistic filing](https://framerusercontent.com/assets/xFIxMV69o6oKsLMzIkdlbTdqrTc.pdf)
requires participation and allows official statistical corrections within 48
hours to trigger resettlement. A normal payout is therefore insufficient to
validate the correction path. The
[NBA source amendment](https://framerusercontent.com/assets/5bm2KSelyDxp6fWU8MJ5u7KZJo.pdf)
identifies futures in its operative scope; it was not silently applied as a
new mainline rule. These are series-level findings, not approved bindings to
individual live strike IDs or sportsbook jurisdictions.

Local source hashes under `data/prophet-nba-filings-20261021/`:

- Mainlines: `7fdce16ed1af6d3c82301382b17f55c327e53fae0b226b9561023fa9db329565`.
- Player statistics: `9122dfc75db147cee69200285235617c8e9b684d60ab6fe38c4636730f218578`.
- Futures amendment: `9581ebf6dc0b307f53c0473ff4d7a7d31bcc65dbeb1e3965926cc824c6dde63d`.

### Earlier feed evidence

- `data/mapping-novig-catalog.json`: 19 sampled moneyline markets all explicitly carry `voids: FMV`; no period or contract-document version field was present. Receipt was October 20 at 21:28 PDT. This bounded sample is not the full universe.
- `data/parlay_rest_sample.json`: 14 events across `pinnacle`, `fanduel`, and `draftkings`; no per-market settlement, void, overtime, or period fields were present. Team names, event IDs, and scheduled times support candidate discovery; they do not establish settlement equivalence or sportsbook jurisdiction.
- `data/mapping-public-catalogs.json`: 100 `KXNBAGAME` rows, sampled October 20 at 21:24 PDT, explicitly retain the 48-hour-start/fair-price cancellation condition.

[Parlay's current stream documentation](https://api.parlay-api.com/docs/websocket) distinguishes a row's retained `last_update` from the frame send clock and poll heartbeat. Its [main documentation](https://www.parlay-api.com/docs) additionally warns that bookmaker `verified_at`/`is_current` does not bind or verify each outcome; cached rows can remain old and expiry does not itself emit a withdrawal. Some legacy freshness descriptions on that page conflict with the more specific caveats. No heartbeat can safely replace a missing outcome timestamp or certify tradability.

The current engine's exact-identity mapping contract should remain strict. A future model may use a sportsbook's conditional completed-game probabilities to value a different exchange contract, but it must declare, estimate, and validate the additional interruption/void payout states separately. That would be a pricing model with explicit assumptions, not a verified identity mapping or guaranteed arbitrage.
