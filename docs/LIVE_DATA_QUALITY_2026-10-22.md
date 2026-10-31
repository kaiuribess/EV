# Read-only live data-quality evaluation — 22 October 2026

## Decision

The existing paid Parlay feeds supplied many complete, two-sided *research* quotes, but only two markets satisfied the unchanged configured fair-pricing profiles during this 30-minute window. Both were on one upcoming MLB game, remained visible for three 30-second samples, and had no current exact native comparison. The capture therefore establishes **intermittent research pricing, no measured fee-adjusted candidate, and no live-order evidence**. It does not establish that profitable opportunities do not exist.

Production remained `auto_trade=false` and `kill_switch=true` throughout. The local native-order count was zero in every sample. No provider requests, orders, cancellations, transfers, model changes, or threshold changes were made by this evaluation. The observer called only the already-running local `/api/state` endpoint.

## Window, grain, and limits

- 20:10:18–20:40:23 Pacific on 22 October 2026 (30.09 minutes), 61 successful snapshots about 30 seconds apart, zero sampling errors.
- A **quote-record instance** is one normalized provider/book/market pair appearing in one snapshot; repeated appearances are not independent upstream updates. A **book-market pair** deduplicates providers when measuring independent sportsbook availability. A **market** identifies one exact normalized sporting question. A **fair market** passes the existing sport/market profile, minimum-book and anchor rules. A **native direction** additionally needs a current, executable venue comparison.
- The local state exposes normalized, two-sided prices. It cannot measure how many incomplete raw provider rows were rejected before normalization. Sub-30-second opportunities can be missed; this is a bounded sample, not a full tick-level census.
- The archived, timestamp-preserving capture is `data/quality_eval/2026-10-22-production-30m-final.jsonl.gz`; its SHA-256 after decompression is `f2f9076a1423df46cbd2d283c99040e355e264a6db16b366f58bc3cc5ef159ec`. The derived counts and rejection reasons are in `data/quality_eval/final-summary.json`. These files are intentionally ignored by Git and contain no credentials.

| Funnel stage | Result | Interpretation |
| --- | ---: | --- |
| Quote-record instances across snapshots | 104,609 | Repeated cached rows included. |
| Distinct provider/book/market keys | 1,802 | Provider copies remain separate here. |
| Distinct complete sportsbook/market pairs | 1,735 | Same book through multiple APIs counted once. |
| Distinct research-eligible sportsbook/market pairs | 855 | Provider-trusted research only; all 14,422 eligible snapshot instances had **unknown** selection-status and quote-observation evidence. |
| Distinct markets with ≥1 eligible book | 430 | Not a fair estimate. |
| Distinct markets with ≥2 independent eligible books | 162 | Often below the configured profile quorum or missing its anchor. |
| Distinct markets reaching configured fair pricing | **2** | Six market-snapshot instances; one MLB event. |
| Current native depth comparisons / fee-positive directions | **0 / 0** | No fee or uncertainty scenario could be evaluated; this is not a negative-EV finding. |

## Provider, book, sport, and market findings

The counts below are **distinct research-eligible normalized markets** during the window, except where explicitly described as non-eligible. They do not establish current active selection status.

| Source and book | Sport / market family | Observed result and pricing consequence |
| --- | --- | --- |
| Parlay REST FanDuel / DraftKings / bet365 / Caesars | NFL receptions | 214 / 125 / 145 / 117 eligible book-markets respectively across two scheduled REST collections. Only FanDuel and DraftKings are in the existing NFL volume-prop profile; they never provided its required three independent profile books. |
| Parlay game/WebSocket FanDuel, DraftKings, BetOnline | NFL moneyline and totals | Thirteen markets had FanDuel with another targeted book at some point. These research pairs lacked the required current Pinnacle/Circa anchor, so **zero** reached fair pricing. |
| Parlay game/WebSocket FanDuel | NBA moneyline | Twelve eligible markets, without a second eligible configured book. NBA points had 18 eligible DraftKings markets; threes/rebounds/assists had 8/7/5, also without the required profile quorum. |
| Parlay game/WebSocket FanDuel | NHL moneyline / total | Two eligible markets in each family, without enough configured independent books. |
| Parlay game/WebSocket BetOnline, DraftKings, FanDuel, Pinnacle | MLB moneyline and total | One upcoming Tampa Bay Rays–Milwaukee Brewers fixture supplied both fair markets. All four books were present for five market-snapshot instances; the moneyline briefly retained only BetOnline and Pinnacle for one more. Quote-observation times and active statuses remained unknown for the selected research references. |
| Oddize Circa | NBA moneyline | One previously captured, stale market; **zero** eligible in this window. |
| OddsPapi | All | Adapter remained in backoff; it supplied no eligible quote in this capture. No extra paid probes were made. |

For **snapshot visibility** rather than independent upstream update reliability, selected research-eligible / present normalized-record instances were: NBA FanDuel moneyline **732/732 (100%)**, NBA Pinnacle moneyline **0/549**, NFL BetOnline moneyline **780/793 (98.4%)**, NFL FanDuel moneyline **1,387/2,233 (62.1%)**, NFL DraftKings moneyline **84/793 (10.6%)**, and NFL Pinnacle moneyline **0/793**. NFL receptions REST prices were eligible for only **9.7–13.4%** of their observed record-snapshots across FanDuel, DraftKings, bet365 and Caesars, reflecting brief post-collection eligibility followed by retained stale rows. These rates are conditional on a record being present in a 30-second scanner snapshot; they are not provider uptime percentages or a measure of quote reobservation.

Among Pinnacle, FanDuel, DraftKings, Circa, and BetOnline, the number of **distinct markets** on which both named books were simultaneously research-eligible was: FanDuel+DraftKings **131**; FanDuel+BetOnline **24**; DraftKings+BetOnline **23**; Pinnacle paired with any one of those books **2**; Circa paired with any **0**. All five were simultaneously eligible on **0** markets. These are not five independent opportunities; they are co-availability counts for exact markets across the sampled times.

### Pinnacle degradation and timing

Two existing Parlay REST collections delivered 770 newly received Pinnacle rows: **689** marked `degraded_source` and **81** `retained_top_up`; **zero** were research-eligible. Only 117 of the 770 exposed a usable stored-row timestamp. For those 117, stored-price age at local receipt ranged **447.4–1,503.9 seconds**, median **647.7 seconds**. Do not project that age distribution onto the 653 rows without the timestamp.

The separate Parlay game WebSocket delivered 85 newly received Pinnacle rows with quote-bound collector-observation evidence, but their observation-to-local-receipt gap was **42.4–133.9 seconds**, median **48.4 seconds**. They did not satisfy the unchanged source-age requirement. Two different MLB Pinnacle rows without a verified observation clock briefly entered **provider-trusted research**, clearly labeled with unknown age/status; they did not become live-execution evidence. This independently corroborates that Pinnacle degradation was visible during this window, while not claiming every Pinnacle market was unavailable at all times.

Parlay's [detailed documentation](https://parlay-api.com/docs) distinguishes bookmaker/fixture verification pulses from per-selection observation and warns that retained rows can be topped up. Its [provider-state documentation](https://api.parlay-api.com/static/docs-provider-state.html) describes degraded source state. A one-second WebSocket push interval does not prove the age of the sportsbook quote.

## Fair estimates, native overlap, and uncertainty scenarios

The two fair estimates appeared at 20:33:47, 20:34:17, and 20:34:47 Pacific and were absent by 20:35:17. Their historical records retain original timestamps and expiration reasons; both expired because the required profile anchor was no longer available. The event was scheduled for 17:00 Pacific the next day. The scanner reported: “No exact native catalog match for this sport, market family and current event; or monitored executable depth is unavailable.” The current API message combines those alternatives, so this capture does **not** isolate missing catalog coverage from missing depth.

Kalshi streamed throughout all 61 samples. Novig was reconnecting, with no streaming book, throughout all 61. Neither venue produced a current executable comparison for either fair market. Accordingly, **zero** current directions were available for fee-adjusted EV, and zero directions survived the illustrative research-only uncertainty allowances of **0%, 0.5%, 1%, or 2% of normal payout**. The denominator for these scenario comparisons is zero; no scenario was approved or used to change trading policy. Older, expired native comparisons present before this window were excluded from current counts.

## Operational conclusion

Prioritize the existing Parlay MLB mainline combinations for **research monitoring** because they were the only configured fair-price sets observed. NFL FanDuel/DraftKings/BetOnline research pairs and NFL receptions have broader raw coverage but do not satisfy existing book-profile anchor/quorum rules. NBA and NHL coverage in this sample is mostly one-book. Do not retune weights or lower minimums from this short window.

For a first pilot, the observed bottlenecks are **intermittent anchor availability and unverified quote status/age**, followed by **no contemporaneous exact native comparison**. Novig's reconnecting stream is an additional venue-data limitation. The data cannot distinguish native catalog absence from lack of executable liquidity for the brief MLB window, and it cannot establish that genuine profitable opportunities are absent. The existing Parlay subscription supports provider-trusted research estimates, but this sample does **not** support a live-eligible two-book pilot under the unchanged execution evidence rules.

To reproduce the aggregate locally, run `python scripts/analyze_live_data_quality.py data/quality_eval/2026-10-22-production-30m-final.jsonl.gz`. The focused observer/aggregation tests are `tests/test_live_data_quality_observer.py`.
