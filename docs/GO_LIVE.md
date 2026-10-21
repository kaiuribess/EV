# Preparing a scoped native launch

The readiness report is a release gate, not a promise of profit. Demo results,
synthetic tests, a working login and a connected heartbeat do not establish live
execution readiness. The gate evaluates the exact venue, environment, account,
software build, strategy set, market set, mappings, prices and limits selected
for launch. It never enables trading itself.

The imported sportsbook priors add a separate reference-model requirement:
every active profile is currently research-only, its live flag is false, and
there are no verified exact-source TTL rules. A connected Parlay or Oddize feed
cannot satisfy those requirements by itself. Current documented observations
also do not establish every outcome's active status. See
[reference policy](REFERENCE_MODEL.md), [source observation evidence](QUOTE_OBSERVATION_CONTRACT.md)
and [offline validation](MODEL_VALIDATION.md). Altering a flag is not a substitute
for the missing market/status/settlement/lifecycle evidence.

## Start a paused native workspace on Windows

Run these commands from the repository folder. The existing demo can keep
running on port 8001. Use the native launcher for account testing:

```powershell
.\start_native.ps1 -Environment sandbox -Port 8002 -Background
```

This opens the dashboard at `http://127.0.0.1:8002` and starts with automatic
trading paused. It uses `data/native-sandbox.db`, separate from demo history.
The launcher applies `setup_live_inputs.ps1` automatically: Parlay feed scopes
and the research sportsbook-weight profiles are the same ones used by
`start_live_paper.ps1`. Order ledgers stay separate, and the paper-only
hypothetical sportsbook offer is never promoted to a native exchange book.

Kalshi's [official demo account](https://help.kalshi.com/en/articles/13823775-creating-and-using-a-demo-account)
and [demo API credentials](https://docs.kalshi.com/getting_started/demo_env)
are separate from production. The Kalshi monitor now selects
`KALSHI_SANDBOX_API_KEY_ID` and `KALSHI_SANDBOX_PRIVATE_KEY_PATH` for a sandbox
workspace, while production keeps `KALSHI_API_KEY_ID` and
`KALSHI_PRIVATE_KEY_PATH`. A production key is never borrowed for sandbox by
the default monitor or the read-only account diagnostic. After a demo account
and key exist, save the one-time private key in a local file, set those two
sandbox variables in `.env`, restart the sandbox workspace, and run:

```powershell
.\.venv\Scripts\python.exe scripts/check_accounts.py --venues kalshi --environment sandbox
```

The [official API-key guide](https://docs.kalshi.com/getting_started/api_keys)
documents creation from the demo account's settings and one-time private-key
download. Kalshi says demo funds are not preloaded and provides mock-funding
instructions on its demo help page. A successful read-only balance check would
establish access only; it would not certify order or settlement behavior.
For production monitoring, the corresponding command is:

```powershell
.\start_native.ps1 -Environment production -Port 8003 -Background
```

That workspace also starts paused and uses `data/native-production.db`.
Neither command grants order authorization or turns on automation, including
after a restart. Native launch artifacts currently share the paths below;
the profile's environment must match the workspace. Keep one approved native
environment/profile active at a time. Do not reuse the demo database as an
account ledger.

Stop a managed workspace safely with its actual port:

```powershell
.\stop_local.ps1 -Port 8002
```

## Read the actual blockers

With the sandbox workspace running on port 8002:

```powershell
.\.venv\Scripts\python.exe scripts/check_live_readiness.py --url http://127.0.0.1:8002
```

Use port 8003 for the production example above. The Trading page also shows the
same live readiness checks and their next actions. **Live setup** also lists
configured native monitors before any launch profile exists, separates real
market coverage from synthetic opportunities, and distinguishes saved limits
from limits applied to a reviewed launch scope. `GET /api/launch/setup` supplies
these read-only preparation facts. No balance from an old diagnostic file is
shown as current spendable cash.

The final owner-controlled enable button remains disabled unless the current
report is complete, fresh, production-scoped and safe to arm, with native
execution explicitly configured. It fetches the checks again for review and
confirmation and binds the control request to the reviewed profile digest.
Changed accounts, configuration or stale evidence require a fresh review. The
backend rechecks the gate before changing the emergency stop, and again after
the persistence wait. A rejected combined enable/reset cannot silently clear
the previous stop. A missing or stale response displays readiness as unconfirmed.

### Review and place one native order manually

The Trading desk has a separate **Place one trade manually** panel in a paused
native sandbox or production workspace. Choose a maximum spend (default $1,
including fees), select a currently qualified native EV candidate, review the
exact venue, side, native quantity, limit price, fee cap, fair estimate and
reference books, then click **Place one order**. The review token is one-use and
expires after 30 seconds. If the current price, quantity, fee, model, fair
probability, launch scope or order checks change, the click is rejected and a
fresh review is required. Venue minimums and quarter-Kelly sizing can make a $1
order impossible; the UI shows the actual cost before submission.

Manual submission leaves automation paused. It still requires a production or
sandbox native workspace with explicit native execution enablement, a clear
emergency stop, complete scoped readiness, current account cash, reviewed
market mappings, verified offers and all central risk checks. The normal OMS
persists intent before transmission, never retries an unknown result, and
tracks acknowledgement separately from fills and settlement. An API response
or transport interruption is not proof of a fill; inspect the native ledger.
The paper workspace on port 8001 cannot submit a real exchange order through
this panel, and no test order is sent merely by opening it.

For an unattended **single-order** test, set `max_native_order_attempts: 1` in
the native workspace's risk settings before enabling automatic trading. The
limit counts every durable native intent, including rejected and unknown
outcomes, and survives a restart in the same native ledger. After the first
attempt the automatic switch pauses; any queued later candidate is rejected.
The limit does not relax launch readiness, reference quality, settlement
matching, quarter-Kelly sizing, account-cash checks, or the approved stake cap.
It is a total attempt cap for this ledger, so an existing native order exhausts
it. Review the order and exchange history before changing or removing the cap.

### Current unresolved release work

The [October 21 Kalshi NBA pilot](KALSHI_PILOT_2026-10-21.md) records a concrete
live no-trade decision: the observed Orlando contract had no validated positive
edge, and its reference quotes did not meet the current-status and settlement
requirements. It did not place a real order.
The [October 21 Parlay baseline probe](PARLAY_BASELINE_2026-10-21.md) found that
the full-game, unfiltered NBA request can return a complete replacement board;
adding an event filter produced an incomplete recent-row sample. Neither frame
alone verifies each retained outcome's active status.

As reviewed October 20, 2026, the working installation has connected real feeds
but no reviewed real sportsbook/native market mappings. Displayed demo
opportunities are synthetic. The NBA review found different interruption,
postponement and void payouts across Novig, Kalshi and sportsbook references;
no common exact settlement scope was established. Novig fair-value settlement
cannot be relabeled as a stake refund. See [the source evidence](NBA_SETTLEMENT_EVIDENCE.md).

An explicit, validated model would be needed to use reference contracts with
different settlement rules. No such model or profitable weighting calibration
has been established. Otherwise, select genuinely equivalent contracts and
retain the supporting rules and source clocks before publishing their mappings.

Genuine order/fill/recovery and settlement certification is still missing.
Polymarket US has an additional implementation blocker: account-attributed
settlement is unsupported, so an all-four-venue launch cannot pass merely by
adding keys or signing a profile. The other venues support only the narrow
settlement policies documented in [native accounting](NATIVE_ACCOUNTING.md).
Polymarket does document resolution events; the remaining cash-posting and
replay gaps are detailed in [the retail settlement review](POLYMARKET_US_SETTLEMENT_GAPS.md).
Existing connections and funded balances are useful setup results, not proof
that these remaining checks passed.

The report includes machine-readable blocker codes and an action for each. Exit
status 0 means `safe_to_arm`; 1 means blocked; 2 means the check could not be
completed. `technical_ready` and `authorization_ready` are separate.
`safe_to_arm` requires both. `live_trading_ready` additionally requires production
mode, explicit enablement, automatic trading enabled and a cleared emergency
stop. Sandbox readiness is reported separately. A report is a snapshot; normal
risk, freshness and native authorization checks still apply to every order.

The offline command without arguments intentionally reports missing evidence:

```powershell
.\.venv\Scripts\python.exe scripts/check_live_readiness.py
```

For offline review, pass `--profile data/launch_profile.json --runtime
data/readiness-runtime.json`. Runtime facts expire after five seconds. Files
typed by an operator cannot prove that their claims are true. The running engine
must independently establish the current facts and retain original evidence.
Neither CLI reads `.env` nor submits orders. Never put API keys into these JSON
files. Configuration error messages omit rejected values.

The running engine reads these exact local files:

| File | Purpose |
| --- | --- |
| `data/verified_mappings.json` | Reviewed source signatures, canonical markets and bookmaker outcome mappings. Loaded when an input plugin starts. |
| `data/native_mappings.json` | Reviewed canonical-to-native instruments, explicit native units and expiring fee bounds. Loaded at backend startup. |
| `data/launch_profile.json` | Legacy single account/environment, limits, configuration digests and reviewed launch evidence. Loaded at backend startup. |
| `data/launch_portfolio.json` | Multiple reviewed account profiles under one shared ledger; mutually exclusive with the legacy file. |

These files are local runtime artifacts excluded from Git and must not contain secrets.
Missing files leave markets quarantined and native execution blocked. After
changing launch or native mapping artifacts, restart the paused native
workspace and repeat the checks. An input mapping change requires restarting
the affected input plugin or the workspace. A configuration change invalidates
evidence bound to the previous profile/build; do not edit a passed certificate
to make its digest match.

For multiple venues, the portfolio JSON contains `schema_version: 1` and
`profiles: [...]`, with each item a complete `LaunchProfile` as described below.
The portfolio supports one account per distinct venue. Accounts must share the
environment, build/configuration digests, strategies and explicit global limits;
market and provider scopes and account evidence stay individual. Startup applies
limits once, recovers one durable ledger, and binds one process lock across all
venues. Every selected account must pass preflight before any new order. Unknown
orders and overlap reservations remain global; authenticated cash remains local
to each account. No ready portfolio is generated from diagnostic balances.

`GET /api/readiness` includes individual `accounts` reports plus aggregate gate
results. `GET /api/readiness/runtime?venue=kalshi` selects a reviewed account's
facts; `accounts` includes every selected venue. The old no-query single-account
fields remain for compatibility. No endpoint creates launch evidence or arms trading.

Check actual account access without placing orders:

```powershell
.\.venv\Scripts\python.exe scripts/check_accounts.py --help
```

The read-only diagnostic distinguishes production from sandbox and writes only
allowlisted results. A sandbox key is never silently retried against production
as a trading fallback. It does not certify order lifecycle or settlement.

## Review exact market mappings

Provider mappings use the existing `MappingFile` contract: `version: 1` and a
`mappings` list of `VerifiedOfferMapping` objects. A mapping binds the exact
source signature to a canonical `Market`, a one-to-one outcome map, bookmaker,
explicit verification and review evidence. Preserve original source IDs and
review the actual event, player, statistic, threshold, period, overtime,
participation, void and push rules. If any fact is unavailable, keep that source
observation quarantined.

The original whole-dollar Kalshi bridge's venue mapping bundle is:

```json
{
  "schema_version": 1,
  "venue": "kalshi",
  "mappings": []
}
```

Each row is `VerifiedVenueMapping`. Its `settlement_fingerprint` must equal
`Market.identity()`, which binds the full event/player/line/outcome/rules
identity, not just a hash of settlement text. Marking `verified: true` is a
review assertion, not an automatic matcher. The empty example intentionally
fails validation; no live mappings are invented.

```powershell
.\.venv\Scripts\python.exe scripts/validate_market_mappings.py --provider data/verified_mappings.json --venue data/venue-mappings.json --min-reference-books 2
```

Repeat `--provider` or `--venue` for multiple files. The validator checks schema,
canonical identity collisions, duplicate source signatures, exact native
outcomes, full contract fingerprints, fees, independent reference counts and
the original bridge's supported units. Multiple vendors supplying Pinnacle
count as one bookmaker. An exit status of 0 proves these structural checks only;
the operator must still verify external rules. Novig cent contracts, fractional
contracts and ProphetX USD stakes require the extended native accounting
instrument contract; do not disguise them as whole-dollar contracts to pass the
legacy mapping checker.

The extended native pipeline uses `NativeMappingFile` in
`venues/book_normalization.py`: `schema_version: 1` and `mappings`, each containing
the complete canonical `market`, verified native `instrument`, and an expiring
verified `fee_bound` tied to that instrument's fingerprint. Novig requires the
reviewed opposing outcome ID. ProphetX additionally requires exact source event
and market IDs and, for props, the authorized sub-type. Numeric ProphetX
`updated_at` units remain unknown until `source_timestamp_unit` and
`timestamp_unit_evidence` explicitly document the actual source convention;
unknown clocks cannot qualify execution.

```powershell
.\.venv\Scripts\python.exe scripts/validate_market_mappings.py --provider data/verified_mappings.json --native data/native_mappings.json
```

This validates the extended unit/fee contract and compares its full market
identity against the provider mappings. The result includes a separate
`native_mapping_digest` matching `NativeBookMapper.mapping_digest`.
The mapper preserves untouched price-level observation clocks across deltas,
uses the oldest executable level as the book's conservative observation time,
and suspends cached books on transport invalidation. It never substitutes
receipt time for missing source timestamps. Native revision numbers remain
monotonic across reconnects in the same process.

## Explicit launch profile and evidence

`core/readiness.py` defines the exact Pydantic contracts. A `LaunchProfile`
requires all of the following; there is no default stake authorization:

- The venue, sandbox or production environment, SHA-256 of the authenticated
  account identity, and software build identity. Do not hash a credential.
- Explicit canonical market IDs, enabled strategy IDs and required provider IDs.
- Digests of the complete reviewed mapping configuration and pricing model.
- Every exposure policy, an explicit daily-loss limit or `null` to disable it, order-count/rate limit,
  minimum independent reference count and quote/book freshness limit.
- Reviewed `LaunchEvidence` records bound to `LaunchProfile.digest()` and the
  same build, venue and account. The digest excludes only the evidence list.

`GET /api/readiness/runtime` exposes the current nonsecret build, mapping and
pricing digests and runtime checks. Use actual authenticated account identity
from the relevant native account probe. A successful REST response or a
credential's presence does not prove write permission or geographical
eligibility. No ready-to-trade profile is shipped with sample money limits.

The requested quarter-Kelly sizing, $5 reporting unit, available-account-cash
and no-overlap settings are documented
in [OPERATOR_LIMITS.md](OPERATOR_LIMITS.md). Venue/total monetary caps may be
`null` only with `available_cash_only: true`; current authenticated spendable
cash is still mandatory. An uncapped per-order amount also requires
`stake_sizing: quarter_kelly`. The local limits draft is not genuine launch evidence.

Evidence records contain the kind, actual environment/account, observation and
expiry timestamps, reviewer, source class, original artifact SHA-256, passed
result and performed checks. The following evidence is mandatory:

| Kind | Required genuine evidence |
| --- | --- |
| `account_access` | An authenticated native read of the target account and its permissions. Maximum age 24 hours. |
| `geographic_eligibility` | Official eligibility verification for the production account. Maximum age 24 hours; required only for production. |
| `native_lifecycle` | Actual native acknowledgement, private fill, partial fill, cancellation race, unknown outcome, duplicate protection, exact fees/units and sequence-gap recovery. |
| `restart_recovery` | Durable-intent restart, authoritative open-order reconciliation and position reconciliation. |
| `settlement_reconciliation` | Native result, actual payout/fee accounting, position release and idempotency. |
| `risk_authorization` | Explicit operator authorization of the exact profile and all limits. |

Lifecycle/recovery/settlement/authorization evidence has a maximum age of seven
days and may expire earlier. Later failed checks supersede earlier passes.
Future timestamps, expired records, synthetic evidence, missing checks, wrong
account/environment/build or configuration changes block release. Production
access and geographic evidence must come from production. A sandbox lifecycle
certificate can qualify the same build only when the actual sandbox account is
explicitly named in `sandbox_account_fingerprint` and its certificate binds the
reviewed production profile. It does not transfer production account access.

Only collect native execution evidence in an authorized native sandbox or an
explicitly authorized bounded production certification procedure. A unit test
using mocked transport is valuable software validation and must never be
relabeled as a native certificate. This module does not mint certificates.

## Runtime checks that credentials alone cannot satisfy

The running engine must establish central risk and opposing-position guards,
durable intent persistence, healthy audit and signal writers, exact native
routing/accounting, authenticated and synchronized private streams, healthy
reconciliation, completed startup recovery, cancellation-race handling and
verified settlement reconciliation. Unknown orders or reconciliation
discrepancies block new orders until resolved.

The new native settlement adapter is deliberately narrow: Novig ordinary
WIN/LOSS results for one terminal, fully reconciled, taker-only managed order
per account and native market. It compares actual position-history payout,
cost, quantity and P&L against complete personal fill history and matching
settlement timestamps. Shared/external/netted positions, maker credits,
PUSH/FMV results, remediation/resettled grades, incomplete history and cash
discrepancies remain unsupported and blocked. Kalshi and ProphetX have their own
restricted settlement readers, with offline tests; each still requires actual
account-specific lifecycle and settlement validation.

This settlement adapter has offline protocol tests; no genuine native
settlement has been certified. The default `settlement_policy: "full"` remains blocked. An operator may explicitly
review and select `settlement_policy: "novig_ioc_win_loss_halt_on_exception"`
for the narrow Novig IOC path. That capability admits only reviewed cent-contract
mappings and one managed order per native market for the account, including
historical settled orders. The order router blocks a second intent for that
market. Exceptional results stop automation and retain exposure for audited
cash reconciliation; they are never guessed or silently marked settled.
Matching genuine account-specific lifecycle and settlement evidence is still
required. Selecting the policy changes the profile digest and invalidates older
evidence. It does not enable execution or certify the account. Adding a key cannot satisfy
that requirement, and a public final score cannot release account exposure.

Every required provider must have confirmed authentication, entitlement, quota,
connection and synchronization. Every selected market must have verified source
and native mappings, current fee information, supported native units, enough
independent references, required books, active status, executable liquidity and
a synchronized order book. Individual source clocks determine freshness;
heartbeats and REST delivery times cannot refresh old prices. Synthetic markets
never satisfy native readiness.

The report keeps performance evidence separate. It may show counts of real
signals, graded events, reconciled fills and verified settlements. Those counts
do not certify profitability, and synthetic results never establish a real
track record. Closing-line analysis is informative when contracts match; it is
not a substitute for verified actual settlement and fee accounting.

## When a remaining key or permission arrives

Novig production and paper keys are separate. The website's
[Profile → Settings → Novig API](https://docs.novig.com/api/api-keys) creates a
management key, which cannot place orders or connect to the trading stream.
Keep that pair in `NOVIG_PRODUCTION_MANAGEMENT_API_KEY` and
`NOVIG_PRODUCTION_MANAGEMENT_PRIVATE_KEY_PATH` for operator setup diagnostics;
those variables do not enable the runtime. The [Novig setup helper](NOVIG_SETUP.md)
exports a separately verified pair to `NOVIG_PRODUCTION_TRADING_API_KEY` and
`NOVIG_PRODUCTION_TRADING_PRIVATE_KEY_PATH`. Provider Center must explicitly
select `production` and `credential_profile: environment` to use it. The default
`legacy` profile preserves `NOVIG_API_KEY` and `NOVIG_PRIVATE_KEY_PATH`; no paper
connection is silently switched. Missing/partial scoped credentials are blocked.

The production management check on October 20 PDT verified the key and $25.00
primary-wallet cash, but found no trading wallet or production trading key.
Novig documents [opening a trading subaccount](https://docs.novig.com/api/subaccounts/manage)
and [funding it](https://docs.novig.com/api/subaccounts/funding) as separate API
operations. These are operator-performed financial actions, not read-only setup.
The management key cannot substitute for the resulting trading key. A transfer
acknowledgement is pending until its status is `Applied` and the new wallet is
reconciled. The $75.00 website portfolio total is not an API cash limit: Novig's
[portfolio definition](https://support.novig.com/en/articles/16205700-how-portfolio-balance-works)
also includes positions and trade credits. No unexplained balance is assumed
spendable. Browser location permission must be resolved through actual device
verification; neither keys nor local settings bypass that requirement.

Save it in the protected local backend `.env` or secret store, then configure
only the matching plugin. Confirm the documented account tier and endpoint;
local tier settings cannot grant server-side access. For Parlay, the current paid
Business connection was verified on October 20, 2026 local time: the server
reported `tier: business`, `push_mode: coalesced`, and `min_push_interval_s: 1.0`.
Authenticated NBA streaming was ready with zero reported errors, and actual game
and player-prop offers continued arriving. The earlier entitlement refusal is
resolved for that observed connection; a future key or connection must establish
its own current access. The push interval is not measured source-to-client latency.

This does not complete launch readiness. In `data/parlay-paid-access.json`,
all unreviewed real markets remained quarantined and `mapped_quotes` was zero.
Automation stayed off, the kill switch active, and native orders zero. Review
exact source/native market equivalence and settlement rules before those real
offers can become reference signals; verified fees, account permissions and
genuine native lifecycle evidence remain separate requirements.

Enable the input/monitor plugin and verify actual native/source messages,
timestamps and supported scopes. Review the resulting exact mappings and fee
schedule, complete the genuine account-specific native checks, and rerun the
readiness report. A newly supplied credential must never silently arm trading.
The final switch remains explicit and only succeeds once all current blockers
are resolved. Keep the first approved scope narrow enough that its mapping,
fees, order lifecycle and settlement behavior have all been exercised.

After genuine preflight and explicit stake/limit approval, the operator enables
native execution with `EV_LIVE_ENABLED=true` in the protected backend
configuration, restarts the matching paused native workspace, and verifies
`safe_to_arm: true` from the current report. This setting alone does not start
orders. Automatic activation uses the guarded local `POST /api/control` API
with `X-EV-Control: local` and `{"auto_trade":true}`; it rejects unresolved
launch checks. An active emergency stop must first have its cause resolved and
be explicitly reset through that same control API. Never treat a saved report
or an old passing response as permission to bypass the current gate.
