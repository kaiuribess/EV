# NBA pilot corrective pass — 2026-10-22

This is a narrow addendum to `NBA_MONEYLINE_AUDIT_2026-10-22.md`. Times are UTC. Baseline `f78bb41` had no production source mapping or launch profile. At the start of this pass the existing draft PR branch also had neither loaded. This pass made no venue placement, transfer, or arming request. Production on port 8002 remained `auto_trade=false`, `kill_switch=true`.

## 1. Production mapping and disabled scope

`ProviderBridge.start()` loads `runtime_data_file("verified_mappings.json", engine.settings.data_dir)` in `backend/evplatform/plugins/external.py`. `backend/evplatform/core/paths.py` resolves the default to `<checkout>/data/verified_mappings.json`, anchored to the module location, not the Windows current directory. The same root-anchoring applies to `.env`, the database, `launch_profile.json`, `native_mappings.json`, and the structural review. `VerifiedProviderMapper.from_file()` then matches the complete source-offer signature and exact outcome labels before it emits a canonical market and quote in `backend/evplatform/integrations/normalization.py`; unmatched or changed signatures stay quarantined.

`scripts/bootstrap_nba_pilot.py` validated `config/nba_pilot_identity_review_20261022.json`, read the stopped production state and Kalshi account probe, and persisted three reviewed *source identity* mappings. It also saved a separate structural native review for event `KXNBAGAME-26OCT22DENOKC`, YES tickers `...-DEN` and `...-OKC`, their listing URLs and rule hashes. The source mappings preserve original provider IDs, the `111`/`112` and `DEN`/`OKC` side labels, captured timestamps, and the crosswalk-v10 review provenance. Source exceptional settlement equivalence is **false**. The native review is **not** an executable `native_mappings.json` entry. Its `pricing_model_approved` and `execution_authorized` values are **false**.

The one disabled `launch_profile.json` selects canonical market `nba-20261022-den-okc-normal-winner`, Kalshi production account fingerprint suffix `5671d166`, one native attempt, quarter-Kelly, $1 proposed maximum debit/loss, $1 daily loss limit, and a $5 **reporting** unit. The separate `nba_pilot_owner_approval_draft.json` is unapproved and records the profile/build/pricing/mapping digests and unsupported assumptions. A safe bootstrap refresh may update only build-bound metadata of this still-disabled pilot; a changed scope, limits, approval, or existing native attempt causes refusal.

After a production restart, `/api/launch/setup` reported `source_signatures_loaded=true`, one profile, one real-reference market **with ineligible observations**, zero executable native mappings, zero native candidates, `risk_policy.applied=true`, and `safe_to_arm=false`. `/api/markets/nba-20261022-den-okc-normal-winner` showed the mapped OddsPapi and Oddize quotes. `setup_live_inputs.ps1` previously cleared `daily_loss_limit` on every restart; it now clears that field only in paper mode. A second restart confirmed the saved native $1 limit is applied. The server is not using an accidental current-working-directory copy of the mapping.

The recorded real OddsPapi frame in `tests/fixtures/oddspapi_den_okc_v4_20261022.json` maps Pinnacle and FanDuel to the canonical game; altered event, market, or outcome IDs remain quarantined. The captured prices retain their original clocks and never become live-eligible because they were loaded for an identity test. Tests launch `ProviderBridge` from the checkout and a different directory without contacting the provider.

## 2. What the five-second predicate actually checks

The strict predicate in `backend/evplatform/core/reference_profiles.py::quote_eligibility()` requires `quote.status == "active"`, `observation_verified`, a non-null `source_observed_at`, a complete two-sided observation, verified market status, reviewed settlement equivalence, and an exact provider/book/market freshness rule. It computes `(now - source_observed_at)` in milliseconds and rejects a negative age or an age beyond the rule's TTL. The disabled pilot's `max_reference_age_ms=5000` is a further launch limit; it is **not** substituted for a missing exact-source rule. Current NBA moneyline configuration has zero verified exact-source freshness rules.

The clocks have different meanings:

| Field | Meaning and scope | May renew one unchanged quote? |
|---|---|---|
| OddsPapi v4 `bookmakerChangedAt` | Last bookmaker-reported **change** for a selection; may be null | No |
| OddsPapi v4 `changedAt` | Last **recorded change** at OddsPapi for a selection | No |
| OddsPapi v4 `updatedAt` | Fixture update, not selection observation | No |
| Oddize `ts` / `timestamp` | Epoch-seconds / ISO pair for the price observation | Yes for the observation clock, **not** for active status |
| Parlay `last_update` | Retained row clock with varying origin | No |
| Parlay `last_observed` plus `last_update_type="collector_observation"` | Documented, validated collector observation of that row when both are present | Yes, provided the exact two-sided board and status also qualify |
| Local HTTP/WebSocket receipt or heartbeat | Delivery or connection health | No |

The OddsPapi v4 parser now retains `bookmakerChangedAt` as `source_price_changed_at` and `changedAt` as provider change time. It does **not** copy either into `source_observed_at`. It separately requires affirmative fixture/book `suspended=false`, market `marketActive=true`, and both selection `active=true`; `bookmakerIsActive` by itself mostly describes collection availability. Oddize now normalizes its market status as `unknown`, while retaining its actual `ts` observation; paper comparisons may show that unknown-status research price without authorizing production. A current HTTP response, fixture update, heartbeat, or identical repeated price cannot change these fields. [OddsPapi v4 field definitions](https://oddspapi.io/en/docs/get-odds), [Oddize Price schema](https://oddize.com/api/v1/openapi.json), [Parlay observation and complete game-board contracts](https://parlay-api.com/docs).

Focused tests cover an unchanged old price with a documented new Parlay observation, the same old price without verification, recent receipt of an old OddsPapi row, fixture-only and heartbeat-only frames, missing/future/millisecond-scaled clocks, inactive opposing side, and Oddize's unknown status. Thus “price unchanged,” “observation stale,” and “observation unknown” are distinct states.

## 3. Exact pregame Denver–Oklahoma City source evidence

The event was scheduled for 2026-10-23 01:30 UTC and was still pregame at this review. These are **captured** responses, not a standing promise of current quotes. OddsPapi capture was 19:03:48; Oddize capture was 19:05:17. The production process later reloaded these sources and preserved their clocks.

| Provider / book | Exact source IDs and both prices | Raw status and clocks | Eligibility result |
|---|---|---|---|
| OddsPapi v4 / Pinnacle | Fixture `id1100013273682538`, market `111`, bookmaker market `line/4/487/1637469558/3775284939/0/moneyline`; home `111` OKC 1.337, away `112` DEN 3.62 | `statusId=0`, `hasOdds=true`, `bookmakerIsActive=true`, `suspended=false`, `marketActive=true`, both `active=true`; both `bookmakerChangedAt=18:09:06.177`, `changedAt=18:09:06.491`, fixture `updatedAt=null` | Status fields affirmative; exact selection observation **unknown**. Both timestamps are old changes. |
| OddsPapi v4 / FanDuel | Same fixture/market, bookmaker market `734.180910683`; `111` OKC 1.30 / `112` DEN 3.65, native side IDs `3319741` / `237492` | Same affirmative status fields; both `bookmakerChangedAt=null`, `changedAt=18:41:18.600` | Exact selection observation **unknown**; a new REST receipt does not fix it. |
| Oddize / Circa | Event `202610220OKC`, `ml`, `DEN` +250 / `OKC` -300 | Both `ts=1792695022` / `timestamp=18:50:22`; no market/selection active or suspended field in the response or Price schema | Two-sided observed price is **stale** at capture and current market status **unknown**. |
| Parlay NBA game WebSocket / FanDuel | Event `2026-10-23_Oklahoma_City_Thunder_Denver_Nuggets`, `h2h`, OKC 1.303030… / DEN 3.65 in observed one-sided/full rows | Production review had no quote-bound `last_observed` and no verified same-pair status; no source mapping promoted. | Not a second independent FanDuel book. Frame/heartbeat alone cannot qualify it. |

The Parlay [game-line WebSocket documentation](https://parlay-api.com/docs) defines a complete `current_game_board` snapshot only with the required scope, completion, and no truncation flags; an ordinary `initial_state` is a bounded sample and `max_age_s` bounds retained rows rather than verifying a currently offered unchanged selection. Its separate `snapshot=complete` native prop guarantee is explicitly limited to particular **NFL Novig/Pick6** scopes, not this NBA FanDuel moneyline. The earlier Parlay REST response also reported a different 01:40 start, so it was not silently merged into this reviewed 01:30 event. Owls is disabled in the configured workspace; no Owls quote was asserted.

There is no parser-dropped quote observation in these captures. OddsPapi v4's missing capability is a documented exact-selection *re-observation/verification contract* (or a separate available endpoint furnishing it). Oddize's missing capability is documented active/suspended **bookmaker market and selection** status for `/api/v1/odds/latest`. Parlay's current NBA rows lack the documented quote-bound observation plus same-pair status. If a provider clarifies the contract, the technical question is: “For this exact endpoint and returned market/selection IDs, which field or endpoint proves that both displayed prices **and** active statuses were re-observed at a specified time when the price did not change? Are unchanged values served from cache, and does this account tier receive that guarantee?” This question was **not sent**.

The configured research weights are Pinnacle 35%, Circa 35%, BetOnline 10%, FanDuel 10%, DraftKings 10%; the minimum is two distinct books including Pinnacle or Circa. Duplicate FanDuel copies remain one book. **No book currently has all required observation, status, settlement and five-second TTL evidence; no effective weights or renormalized production blend exist.** If Pinnacle and FanDuel later independently pass, their original 35/10 weights would renormalize to 77.78%/22.22%; this is arithmetic only, not a current estimate or changed policy. The profile is `recommended_initial_paper_test`, `live_execution_allowed=false`, with power devig and proportional sensitivity; no weights or thresholds were retuned.

## 4. Settlement, fee and proposal boundary

Kalshi's public DEN and OKC contracts both say YES wins for the named full-game winner. A postponement starting within 48 hours follows the official result; cancellation or failure to start within 48 hours resolves at a **fair price**, whereas the source sportsbook exceptional void/refund terms are not verified. The normal winner question aligns for the reviewed OddsPapi sides; exact payout equivalence does not. The existing read-only `settlement_bridge.py` / `/api/market-review/bridge-estimate` keeps conditional normal-completion probability separate from a conservative exceptional-outcome payout bound and always reports `execution_eligible=false`. For a one-contract purchase at price `c` and maximum fee `f`, conditional normal EV is `p_normal - c - f`. With exceptional probability at most `q` and a justified payout floor `L`, the conservative EV is `min(p_normal, (1-q)*p_normal + q*L) - c - f`. Here neither a current qualified `p_normal` nor a verified nontrivial `q,L` bound exists. Taking unknown `q=1,L=0` gives `-c-f`, **not** an assumed zero cancellation probability or refund. No numerical live EV is claimed. [Kalshi market settlement documentation](https://docs.kalshi.com/getting_started/market_settlement).

The public Kalshi orderbook uses bids only: a DEN NO bid of $0.7300 implies a DEN YES buy offer of $0.2700; the OKC NO bid of $0.2600 implies an OKC YES offer of $0.7400. These prices and quantities are momentary read-only observations, not an executable mapped book in the engine. Both listings specify a $0.0100 price step. The [current linked fee schedule](https://kalshi.com/docs/kalshi-fee-schedule.pdf) states the general taker formula `ceil_to_centicent(0.07 × contracts × price × (1-price))`; no `KXNBAGAME` override was found there. At one contract, the formula rounds to $0.02 for either example price, yielding hypothetical maximum debits of $0.29 and $0.76 before any other applicable charges. Actual fee binding, native quantity minimum/increment, fresh executable depth, cash/exposure, and quarter-Kelly sizing must still pass for the exact proposal; $1 is only a proposed ceiling and $5 is never a quantity input. [Kalshi V2 order and IOC fields](https://docs.kalshi.com/api-reference/orders/create-order-v2), [orderbook semantics](https://docs.kalshi.com/getting_started/orderbook_responses).

`scripts/owner_nba_pilot.py prepare` now requires **event ID + ticker + outcome** and exactly one executable native selection in `launch_scope`; it cannot choose the first array row and attach a hardcoded team. The existing `ManualNativeService` adds a 90-second proposal digest binding account, native selection, market/mapping/build/pricing identity, reference evidence, depth, fees, quantity, price limit, risk cap and expiry. Submit consumes that same server-side review, recalculates the candidate and digest, uses the existing OMS and kill-switch veto, and has an atomic local one-attempt claim. Dry-run and blocked production prepare sent **zero** placement and **zero** arming requests. Concurrent, changed and expired proposal regressions pass. No owner submit was executed. Historical production order/fill/settlement validation remains unobserved.

## 5. Safe Windows checks and result

Run from PowerShell; these commands do not place an order:

```powershell
Set-Location 'C:\Users\SHU Esports\EV'
& .\.venv\Scripts\python.exe -m pytest -q tests\test_nba_corrective.py tests\test_owner_nba_pilot.py tests\test_settlement_bridge.py --basetemp data\pytest-nba-owner-check
$setup = Invoke-RestMethod 'http://127.0.0.1:8002/api/launch/setup'
$setup.scope; $setup.risk_policy; $setup.structural_review
Invoke-RestMethod 'http://127.0.0.1:8002/api/markets/nba-20261022-den-okc-normal-winner'
& .\.venv\Scripts\python.exe scripts\review_kalshi_nba.py --output data\kalshi-nba-review-new.json
Invoke-RestMethod 'http://127.0.0.1:8002/api/native/orders'
```

The exact owner preview syntax, **currently expected to reject before preview** because no executable native selection or qualified reference set exists, is:

```powershell
& .\.venv\Scripts\python.exe scripts\owner_nba_pilot.py prepare --event-id nba-20261022-den-okc --native-ticker KXNBAGAME-26OCT22DENOKC-DEN --outcome 'Denver Nuggets' --max-cost-usd 1 --proposal-file data\den-okc-owner-proposal.json
```

If a future authoritative order ID exists, read-only reconciliation remains available with the kill switch on: `Invoke-RestMethod 'http://127.0.0.1:8002/api/native/orders'` and `Invoke-RestMethod -Method Post 'http://127.0.0.1:8002/api/native/orders/EXACT_LOCAL_ORDER_ID/reconcile'`. The separate `submit --proposal-file ...` command is owner-only and was **not** run. It cannot currently pass the normal launch gate; do not use a structural ticker as if it were an executable mapping.

Focused checks: `test_recorded_real_pair_clears_identity_but_not_quote_or_settlement_gate`, `test_provider_bridge_loads_reviewed_mapping_from_both_working_directories`, `test_five_second_gate_uses_verified_observation_not_old_price_change`, `test_receipt_fixture_heartbeat_and_scaled_clock_do_not_refresh_selection`, `test_documented_parlay_quote_observation_can_follow_an_unchanged_price`, `test_odds_papi_inactive_opposing_side_rejects_complete_pair`, `test_oddize_observed_price_does_not_synthesize_active_market`, `test_bootstrap_persists_disabled_profile_and_structural_review`, `test_owner_dry_run_uses_only_local_reads_and_unsigned_preview`, `test_exact_selection_is_independent_of_launch_scope_order`, `test_expired_saved_proposal_fails_before_control_or_placement`, and `test_atomic_local_proposal_claim_allows_only_one_windows_invocation` all passed. The full backend suite passed **2,190 tests**, and the frontend passed **104 tests** plus a production build. These tests establish code behavior, not current provider permissions, quote eligibility, live execution or settlement.

Changed-file inventory (new files show their complete line span; existing files show changed lines in the current PR branch):

| File | Changed lines |
|---|---|
| `backend/evplatform/__main__.py` | 7, 11 |
| `backend/evplatform/api.py` | 87, 315–316 |
| `backend/evplatform/core/config.py` | 8, 100, 105 |
| `backend/evplatform/core/paths.py` | 1–30 |
| `backend/evplatform/core/structural_review.py` | 1–46 |
| `backend/evplatform/core/launch_setup.py` | 14, 21, 24, 157–161, 169–181, 289–290 |
| `backend/evplatform/core/manual_native.py` | 29–31, 78–105, 144–162, 180–201 |
| `backend/evplatform/core/models.py` | 154, 162 |
| `backend/evplatform/core/native_runtime.py` | 50, 78–88, 124–125 |
| `backend/evplatform/core/pricing.py` | 106–109, 189–191, 264–265 |
| `backend/evplatform/core/reference_profiles.py` | 131–132, 140–141 |
| `backend/evplatform/engine.py` | 61–62, 812–813 |
| `backend/evplatform/integrations/normalization.py` | 108–111, 593–624, 724–727, 1012 |
| `backend/evplatform/plugins/external.py` | 9, 50–51 |
| `backend/evplatform/plugins/oddize_paper.py` | 51, 88, 134–135 |
| `config/nba_pilot_identity_review_20261022.json` | 1–392 |
| `scripts/bootstrap_nba_pilot.py` | 1–195 |
| `scripts/owner_nba_pilot.py` | 3–6, 12–16, 45–220 |
| `setup_live_inputs.ps1` | 158–161 |
| `docs/NBA_MONEYLINE_AUDIT_2026-10-22.md` | 52–58 |
| `docs/NBA_PILOT_CORRECTIVE_2026-10-22.md` | 1–130 |
| `tests/test_nba_corrective.py` | 1–249 |
| `tests/fixtures/oddspapi_den_okc_v4_20261022.json` | 1–109 |
| `tests/fixtures/oddize_den_okc_20261022.json` | 1–75 |
| `tests/test_launch_portfolio.py` | 75 |
| `tests/test_launch_setup.py` | 32–33, 306–307, 333 |
| `tests/test_native_pricing.py` | 82, 87, 107, 129–175 |
| `tests/test_native_runtime_readiness.py` | 257–258 |
| `tests/test_oddize_paper.py` | 59, 88, 100, 106, 241 |
| `tests/test_owner_nba_pilot.py` | 3–6, 13, 23–25, 32, 35–36, 44, 63–65, 72, 87–89, 94–141 |
| `tests/test_providers.py` | 723, 739–741, 743, 791–793 |

Remaining blockers, ranked:

1. **Provider capability / fresh data:** obtain exact two-sided, current quote-observation and status evidence for two independent books, including Pinnacle or Circa, under the existing entitlement. Pass: both sides pass a documented five-second exact-source rule; fail: only change clocks, cached rows, heartbeat, or missing status.
2. **Configuration / settlement:** verify the sportsbook exceptional rules against the exact Kalshi payout and record a defensible exceptional-state treatment; only then create an executable native mapping. Pass: matching rule evidence and a reviewed payout bound, actual market grid/fees/depth; fail: winner labels or structural ticker alone.
3. **Owner action / model scope:** explicitly approve this exact research model, market, build and risk cap for one attempt if its assumptions are acceptable. Pass: scoped owner approval with no global profile promotion; fail: draft approval or paper preset alone.
4. **Fresh data / economics:** obtain an actually qualified two-book fair price and current Kalshi ask/fee/cash snapshot, then let quarter-Kelly and all risk checks calculate a bounded proposal. Pass: positive fee- and sensitivity-adjusted estimate and unsigned exact proposal; fail: no signal or sizing below the native minimum.
5. **Actual execution or settlement:** after any future owner submission, reconcile authoritative order, fill, fee, cash, position and later settlement records. Pass: native identities and financial deltas agree; fail: an acknowledgement called a fill or a fill called settled profit.

**Result:** source mapping and restart/configuration defects fixed; documented quote-verification/status facts are still unavailable for this exact two-book set; currently qualifying references **absent**; positive estimated EV **absent**; owner-operated submission **blocked**; real orders submitted during this pass **zero**.

## Read-only follow-up at 19:25–19:27 UTC, October 22

The connected Parlay NBA game WebSocket returned a complete, untruncated `current_game_board` for each requested book at a 120-second retained-row filter. Pinnacle returned **zero** rows. FanDuel returned **42** rows, all carrying `available=true`, but **zero** carried the documented `last_observed` plus `last_update_type="collector_observation"` observation pair or an explicit active status. A separate one-credit, `h2h`-only REST request returned 14 two-sided Pinnacle and 14 two-sided FanDuel offers (and two DraftKings offers). All 30 offers lacked a validated observation within the unchanged five-second requirement. FanDuel's per-side links verified market status for its 14 offers; Pinnacle and DraftKings had no verified active status in this response. Thus the REST endpoint supplies *prices*, while the snapshot still does not supply two execution-eligible independent references. A complete WebSocket board is a scope guarantee, not an unchanged-selection freshness guarantee. [Parlay WebSocket contract](https://parlay-api.com/docs/websocket), [Parlay REST field contract](https://parlay-api.com/redoc).

A second one-credit REST read inspected **field names at every nesting level** without printing odds or IDs. The Pinnacle and FanDuel bookmaker objects carried `verified_at`, `is_current`, `line_changed_at`, and `last_update`; their `h2h` market objects carried only `key`, `last_update`, and `outcomes`. The outcome objects carried `name` and `price`, plus `link` for FanDuel. Neither market nor outcome carried `last_observed`, `last_update_type`, an observation timestamp, or an active-status field. The current parser is not dropping a game-line quote-observation field present in this response; bookmaker-level polling and market-level retained change clocks cannot fill that gap.

The authenticated, read-only Kalshi production account diagnostic returned verified account identity, available-cash semantics, reconciled inventory, trading permission and a current geographic attestation, with **zero** reported open orders and positions. The diagnostic explicitly says `usable_for_execution=false`; it does not bind a native execution adapter or prove any live order. The normal runtime account snapshot remains absent because no executable native mapping is installed. The optional Owls Pinnacle realtime plugin remains disabled, and `OWLS_API_KEY` is empty in this workspace, so its documented market-level `last_seen` path cannot be sampled under the existing credentials. OddsPapi's [v4 WebSocket page](https://oddspapi.io/us/docs/websocket-api) directs streaming users to v5 on a B2B plan; that is not an available fallback for this configured v4/free feed. Oddize's [published Price schema](https://oddize.com/api/v1/openapi.json) defines `ts` as price observation but exposes no active/selection-status field. No subscription or credential was created.

Pinnacle's published basketball rules establish that full-game bets include overtime and that NBA game-period wagers void if fewer than 43 minutes are completed. This strengthens the ordinary-winner comparison but does not resolve how every exceptional state maps to Kalshi's fair-price settlement if the game does not start within 48 hours. FanDuel publishes jurisdiction-specific house rules; the Puerto Rico version voids an NBA game suspended and not fully completed within 24 hours, but that version is **not** evidence of the rule applicable to the generic FanDuel feed or this account. Structural settlement equivalence therefore remains false. [Pinnacle betting rules](https://www.pinnacle.com/en/future/betting-rules/), [FanDuel Puerto Rico house rules](https://www.fanduel.com/fanduel-sportsbook-house-rules-pr), [Kalshi market settlement](https://docs.kalshi.com/getting_started/market_settlement).

The 19:34 UTC read-only Kalshi series check returned `KXNBAGAME` with `fee_type=quadratic_with_maker_fees`, `fee_multiplier=1`, and `exchange_index=3`. The selected event and markets returned no fee override, and the series/event fee-change arrays were empty at that observation. The existing conservative **IOC taker** bound in `kalshi_fees.py` explicitly accepts that fee type; its focused fee and lifecycle tests passed (59 tests). V2 `order_body()` omits `exchange_index`, which [Kalshi's V2 contract](https://docs.kalshi.com/api-reference/orders/create-order-v2) says auto-routes when the ticker is supplied; it does not force shard zero. It also uses `immediate_or_cancel`, YES `bid` / NO-equivalent `ask`, and fixed-point dollars. This verifies serialization against the current documentation, **not** an actual accepted order or a fee certification for a later market snapshot. The fee metadata and changes must be refreshed and bound to an executable instrument at proposal time. [Series metadata](https://docs.kalshi.com/api-reference/market/get-series), [fee schedule](https://kalshi.com/docs/kalshi-fee-schedule.pdf).
