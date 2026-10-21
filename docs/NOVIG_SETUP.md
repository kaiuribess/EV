# Novig production wallet setup

This setup tool prepares one trading wallet for the existing Novig account.
It does not place bets, arm the trading engine, certify mappings, or bypass
location verification. Financial submissions must be performed by the account
owner in an interactive PowerShell window. All paths below are relative to the
repository root.

## Already available

The production management key is saved only on this PC and has passed signed
read checks. Its private file stays outside the repository. The October 20 PDT
check reported $25.00 primary-wallet cash, zero trading subaccounts, and zero
production trading keys. The $75.00 website portfolio total is not a funding
limit: it can also contain open positions and trade credits. Refresh actual cash
before deciding an amount.

The existing paper connection retains its current credentials. Production setup
uses separate environment variables and does not overwrite the legacy pair.

## Prepare and review

```powershell
.\setup_novig.ps1
.\setup_novig.ps1 -Action prepare -Label 'EV Platform'
```

The first command is read-only. The second creates only a local protected
Ed25519 private key and a unique setup journal; it does not open a remote wallet.
Keep the journal path printed by preparation. Reuse that journal throughout this
setup, especially after an interruption. Never paste private keys into the
dashboard or chat.

## Owner-performed wallet opening

```powershell
.\setup_novig.ps1 -Action open -Journal 'PATH_PRINTED_BY_PREPARE'
```

Run this yourself. Read the account/environment summary and type the displayed
confirmation phrase. This submits the public half of the prepared key to Novig
and opens a trading wallet with its own trading key. It does not fund the wallet.
No private key is sent. A non-interactive invocation refuses this action.

If the outcome is unknown, do not run a second preparation or create another
wallet. Use read-only reconciliation with the original journal:

```powershell
.\setup_novig.ps1 -Action reconcile -Journal 'PATH_PRINTED_BY_PREPARE'
```

Recovery matches the public signing-key fingerprint, account environment and
wallet identity. A label alone never proves which wallet was opened. An
unresolved opening blocks another opening attempt from that journal.

## Owner-performed funding

After opening and reconciling the wallet, use `-Action fund` with the same
`-Journal` and an explicit `-Amount` of your choosing. There is no default
funding amount. The amount must be positive with at most five decimal places
and cannot exceed freshly reported main-wallet cash. The $5 per-order cap is
separate from the amount moved into this wallet.

The tool records a stable client transfer ID before submission. `Requested`
means pending; only `Applied` proves the movement completed. If interrupted,
use `-Action recover-funding`, the same journal, and the original `-Transfer`
client ID. If the server transfer ID is known, recovery reads its status. If it
is unknown, the owner must confirm an exact same-ID recovery request. Never
use a new ID to repeat an uncertain transfer.

## Connect the verified key without enabling bets

```powershell
.\setup_novig.ps1 -Action export -Journal 'PATH_PRINTED_BY_PREPARE'
```

Export rechecks the trading key's identity, signing fingerprint and wallet. It
saves only `NOVIG_PRODUCTION_TRADING_API_KEY` and
`NOVIG_PRODUCTION_TRADING_PRIVATE_KEY_PATH` in the protected local backend;
the legacy paper credentials remain untouched. Both `EV_LIVE_ENABLED` and
`EV_AUTO_TRADE` remain false.

Restart the paused application to reload backend environment variables. In
Provider Center, explicitly select Novig's `production` environment and
`environment` credential profile. Use production-discovered market IDs; sandbox
market IDs are not portable. Missing or partial production credentials block
connection instead of borrowing the paper or management key.

The signed trading-account read check is:

```powershell
.\.venv\Scripts\python.exe scripts\check_accounts.py --venues novig --environment production --novig-credential-profile environment
```

This is a read-side check, not order lifecycle certification. Reviewed market
equivalence, fees, fills/cancellation, settlement, account identity and the live
launch gate are still required. See [GO_LIVE.md](GO_LIVE.md).

## Location

Novig's API verifies both the request network and the account owner's recent
device-location check. Allow the actual device location in Novig's website or
official app and complete its normal verification. The API settings page shows
the current result and expiry. If the site still says permission is blocked,
resolve the browser's permission for `novig.com`; no fake location, proxy or
local flag can satisfy this check.

Official references: [key scopes and location](https://docs.novig.com/api/api-keys),
[wallet creation](https://docs.novig.com/api/subaccounts/manage),
[transfer states and recovery](https://docs.novig.com/api/subaccounts/funding),
[portfolio balance](https://support.novig.com/en/articles/16205700-how-portfolio-balance-works).
