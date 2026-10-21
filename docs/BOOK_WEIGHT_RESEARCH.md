# Reference-book weighting evidence

Reviewed **2026-10-20 (America/Los_Angeles)** against the original sources linked in `1-book_weight_priors_2026-10-20.json`, current provider documentation, and the existing settlement review. Public documentation only; no account calls, financial actions, feed audit, weight fitting, or backtest were performed.

**Conclusion:** the attachment is suitable as a collection of explicitly unvalidated paper-research priors. The sources support separating sports, mainlines, and props; they do not establish its exact percentages, thresholds, or profitability. No better percentages can honestly be inferred from these sources alone. Source-owner research and product presets are evidence of their authors' methods and claims, not independent validation of this platform.

## Original research and presets

### Pikkit: NBA and NFL

[PromoGuy, December 20, 2024](https://pikkit.com/blog/which-sportsbooks-are-sharp-best-apps-sports-betting-2024) describes measuring which books lead subsequent market movement, split by sport and wager class. The published mainline rankings begin Circa, Pinnacle, BookMaker for both NBA and NFL. NBA secondary markets begin FanDuel, BookMaker, PropBuilder, Novig, Pinnacle, DraftKings; NFL secondary markets begin Pinnacle, FanDuel, BookMaker, Circa, BetOnline.

The page publishes **rankings, not numeric percentages**. It does not provide a reproducible dataset, objective function, sample counts, confidence intervals, or held-out evaluation. “Mostly player props” is broader than any specific statistic. This supports candidate references, not a 50% FanDuel NBA allocation or identical weights for points, rebounds, assists, threes, and PRA. Nor does it establish current 2026 in-play behavior.

### Pikkit: MLB

[PromoGuy, August 8, 2025](https://pikkit.com/blog/promoguys-which-sportsbooks-are-the-sharpest) uses the same market-movement rationale. Published mainline scores include Pinnacle 1.163, Circa 1.111, BookMaker 1.097, BetOnline 1.065, and DraftKings 1.022. Prop scores include Circa 1.278, FanDuel 1.182, PropBuilder 1.116, Pinnacle 0.962, DraftKings 0.910, and bet365 0.888. These are published scores, not percentages summing to 100.

The author explicitly limits Circa's prop evidence to its narrower offering, particularly strikeouts and home runs. Extrapolation to hits, total bases, runs, or RBI is unsupported. The page does not disclose enough sampling or fitting detail to reproduce those scores, and does not validate the attachment's exact allocations.

### Outlier's current published presets

[Outlier, July 16, 2026](https://help.outlier.bet/en/articles/11908672-how-to-read-use-positive-ev-filter-presets-in-outlier) publishes these **raw relative weights**:

| Preset | Published weights |
| --- | --- |
| NBA props | FanDuel 100; Pinnacle, BookMaker, DraftKings, Caesars 25 each; FanDuel required |
| NFL props | Pinnacle, BookMaker, FanDuel, BetOnline equally weighted |
| MLB props | FanDuel 75; Pinnacle 50; DraftKings, Caesars, bet365 25 each |
| Mainlines | Pinnacle, Circa, BookMaker 50 each |

All four specify minimum one book and “Average” devig; that label is not evidence for power-only devig. The mainline preset lists MLB/NHL/NFL/NBA and excludes college/WNBA. The page supplies product rationale, not a reproducible historical validation or uncertainty analysis. The attachment's weights, minimum-book requirements, and exclusions are its author's changes, **not a faithful copy of these presets**. Neither a product's “optimized” wording nor its stated 3% variation setting establishes an optimal configuration for our feeds, costs, or execution venues.

### Upside's exchange-heavy results

[Matt Downs, October 14, 2026](https://upside.tools/research/sharpest-sportsbook-exchange) reports 27,579 games, 252,387 props, 22.5 million Novig trades, and over 1.1 million line moves across roughly two years. It combines closing-price/result checks, movement toward a source at 24/6/1-hour horizons, and same-player/same-line prop disagreements. Its final relative weights heavily favor Novig for mainlines and Novig/Kalshi for props.

Crucially, the article says Pinnacle, Circa, ProphetX, Polymarket, and other sources had only about a week of history and received provisional weights. Thus this is not a mature comparison against the proposed sharp-book set. It does not publish the raw dataset, executable depth/fee treatment, full fitting formula, temporal holdout, or uncertainty needed to reproduce a deployable model. Its percentage measures are not win rates. Treat the claims as a separate challenger hypothesis; they do not justify importing large exchange weights, using the target venue to establish its own edge, or assuming a particular venue/jurisdiction's settlement terms.

### Power devig

[Clarke, Kovalchik and Ingram, December 25, 2017](https://www.sciencepg.com/article/10.11648/j.ajss.20170506.12), DOI `10.11648/j.ajss.20170506.12`, compares additive, multiplicative, Shin, and power transformations. The [author-uploaded full text](https://www.researchgate.net/publication/326510904_Adjusting_Bookmaker%27s_Odds_to_Allow_for_Overround) describes ATP men's tennis bookmaker averages and greyhound/thoroughbred tote datasets. It evaluates probability assigned to winners, log loss, and squared-error measures. Power outperforms multiplicative in the reported comparisons; it is not uniformly superior to every other method on every metric.

The method raises implied probabilities to a common fitted exponent so they sum to one. Its mathematical range properties and those historical results justify implementing and testing it. They do **not** establish superiority for today's NBA/NFL/MLB props, book weights, market discontinuities, or executable profit. A two-outcome transformation also cannot estimate a missing push/void probability from the same two prices. Preserve proportional as a comparison and report method sensitivity.

### Conflicting opinion and calibration

[Jack Andrews, Unabated, July 31, 2026](https://unabated.com/post/the-biggest-mistake-youre-making-when-betting-nfl-player-props) argues against treating any sportsbook as universally authoritative on NFL props, emphasizing limits and independent statistical projections. This is practitioner commentary, not a quantitative weight study. It is useful counterevidence to a universal “sharpest” claim; its supply-chain assertions were not independently verified here.

[Scikit-learn's calibration documentation](https://scikit-learn.org/stable/modules/calibration.html) supports evaluating probabilities with proper scoring rules and reliability curves using data separate from model fitting. Lower Brier/log loss alone is not proof of better calibration: those scores also reflect discrimination and uncertainty. This documentation supplies evaluation methods, not sportsbook rankings or profitability evidence.

## Provider keys and actual coverage

### ParlayAPI

The current public [bookmaker registry](https://parlay-api.com/v1/bookmakers) verifies these attachment keys: `pinnacle`, `fanduel`, `draftkings`, `caesars`, `bet365`, `betonline`, `bookmaker_eu`. Mapping `bookmaker_eu` to our canonical `bookmaker` is a local naming choice. Circa was absent from the reviewed registry.

- `bookmaker_eu`: NFL/MLB moneylines; boards checked every 60 seconds. It is not a documented prop, NBA, spread, or total source.
- `betonline`: NFL/NCAAF/MLB pregame moneyline/spread/total coverage; the registry does not establish props or NBA/NHL coverage.
- The other five expose broad endpoint capability declarations, not an exhaustive sport/stat/book availability matrix.
- `_open` and `_an` are historical variants of the same book, never additional independent current references.

The [main documentation](https://parlay-api.com/docs) distinguishes bounded observations from complete source inventories. It warns that specific FanDuel/DraftKings bookmaker heartbeats do not verify each outcome and that those views lack guaranteed withdrawals. Some generic freshness wording differs from the scoped caveats. Keep row observation, price-change, receipt, source health, and completeness separate; an unchanged quote can be current, but an unqualified heartbeat cannot establish that. Provider event/selection identifiers do not by themselves certify settlement equivalence.

### Oddize

The [official roster](https://oddize.com/odds-api) verifies these keys and names: `pin` Pinnacle; `fd` FanDuel; `dk` DraftKings; `czr` Caesars; `b365` Bet365; `boag` BetOnlineAG; `circa` Circa; `novig` NoVig; `phtx` ProphetX; `poly` Polymarket. BookMaker is not in the published roster. The documented leagues are MLB, NBA, NFL, NCAABM, NCAAF, and NHL. **`poly` is not documented as Polymarket US**; do not automatically bind it to our US execution adapter.

All proposed prop query strings are present in the current [OpenAPI specification](https://oddize.com/api/v1/openapi.json):

| League | Verified attachment `prop_type` strings |
| --- | --- |
| NBA | `player_pts`, `player_reb`, `player_ast`, `player_3pm`, `player_pra` |
| NFL | `player_pass_yds`, `player_rush_yds`, `player_rec_yds`, `player_rec` |
| MLB | `pitcher_k`, `batter_h`, `batter_tb`, `batter_rbi`, `batter_r`, `batter_hr` |
| NHL | `player_sog`, `player_pts` |

No invented Oddize query code was found in that list. The canonical right-hand names are local aliases, not provider-issued settlement certifications. `GET /props/{sport}` requires `prop_type`; unsupported codes can return an empty result rather than an error. `GET /sports` and `/offer-types` are the discovery contracts. Preserve exact `alt_lines`/`all_odds` thresholds. The schema describes `ts` as price observation time; it does not supply a universally bound sportsbook-publication clock, per-outcome active status, jurisdiction, or complete settlement rules. The event-props route defaults to team totals unless player `prop_types` are supplied. Coverage must still be checked per sport, book, statistic, side, and event.

## Claims that remain unsupported

The attachment's exact percentages; statistic-specific superiority; neutral NHL/college weights as empirically fitted estimates; universal source freshness TTLs; guaranteed independent errors between books; simultaneous two-sided source verification; and use of these presets as live authorization are unsupported by this review. Its blank TTL map and paper-only status are honest limitations. Do not fill those gaps with guessed constants or silently inherit NBA/NFL profiles for new leagues.

Deduplicate a canonical book across feeds before assigning weight. Eligibility precedes weighting: native identity, compatible contract, complete opposing outcomes, exact threshold, freshness/status, and source completeness must be established first. Exclude the execution venue from its own fair-value references. These are platform design requirements, not empirical proof that the remaining blend is profitable.

## Settlement scope: no newly verified shortcut

The previous [NBA contract review](NBA_SETTLEMENT_EVIDENCE.md) still applies. In this bounded follow-up, no exact new sportsbook/exchange settlement scope was verified. Moving to player props does not automatically solve the mismatch: Novig's [September 2, 2026 FVS explanation](https://support.novig.com/en/articles/16163706-what-is-fair-value-settlement-fvs) explicitly includes player scratches and pays pre-disruption fair value rather than refunding entry cost. Generic source aliases cannot erase this state. This is not a claim that every possible market is incompatible; each candidate needs its actual venue contract, sportsbook jurisdiction/rules, and bound source identity reviewed. If pricing different contracts, use a separately validated payout-state model rather than marking them equivalent.

## Next evidence to collect

Use the attachment as a named paper challenger alongside equal-weight eligible books and single-anchor baselines. Record the full candidate universe and rejection reasons. Fit only on time-ordered historical data with all repeated snapshots, sides, and alternate lines from an event kept together; evaluate on later unseen events. Compare log loss, Brier score, reliability curves, market-specific calibration, and fee/liquidity-aware simulated returns. These are proposed validation steps; this report performed none of them and declares no optimized weights.
