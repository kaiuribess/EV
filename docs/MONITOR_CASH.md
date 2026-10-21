# Account balance display

`core/monitor_cash.py` records manual read-only observations from connected account monitors even when no launch profile exists. Its cache is separate from `NativeRuntime.account_snapshots`. It never provides cash to quarter-Kelly sizing, reconciles inventory, establishes trading permissions, or changes an account. `cash_available_verified` and `available_for_kelly` are always false.

Each refresh uses one balance read per selected venue, with a 30-second per-client cooldown and a 10-second deadline. Concurrent refreshes share the lock rather than issuing duplicate reads. A successful response records local receipt time; it does not invent a source timestamp. A failed refresh retains the old amount and original time with `refresh_failed`; old observations become `stale`. Replacing/disabling a client or changing its environment/key binding hides the old amount. Disconnected observations remain explicitly historical, with identity verification cleared.

| Reader | Read and display meaning |
|---|---|
| Kalshi | `GET /portfolio/balance?subaccount=0`, `balance_dollars`; requested primary-subaccount reported cash, without the separate authenticated key/inventory checks required for execution |
| Novig | `GET /v3/account/subaccounts/{configuredTradingKeyId}/balance`; exact echoed `keyId` must match before displaying the USD `balance`; no management-key wallet aggregation |
| ProphetX | `GET /v4/mm/get_balance`; balance and exposure credits shown separately; a confirmed private-session identity may be retained, but a synchronized unmatched-order timestamp does **not** refresh the cash clock |
| Polymarket US | `GET /v1/account/balances`; exactly one USD row; `currentBalance`, `buyingPower`, reservations, and unsettled funds stay separate reported fields, with `lastUpdated` retained when valid |

Sources: [Kalshi balance endpoint](https://docs.kalshi.com/api-reference/portfolio/get-balance), [Novig documentation index](https://docs.novig.com/llms.txt), [ProphetX integration Step 2](https://docs.prophetx.co/docs/integration), [Polymarket US balances](https://docs.polymarket.us/api-reference/account/get-account-balances). These balance reads are narrower than the existing full account preflight and cannot replace it.

## Registering another venue

The service constructor takes a supplier returning the current enabled monitor clients by plugin ID. API integration uses cached `service.state()` for GET and `await service.refresh(venue=None)` for a guarded manual refresh. Neither call accesses launch-profile data.

Register an additional reader with `service.register_reader("your_venue", async_reader)`. The reader receives only its already-authenticated monitor client. It must make documented read-only account requests and return `MonitorCashObservation` or its validated dictionary. Registration does not create a connection, obtain credentials, change subscriptions, or authorize any financial operation.

The result contract requires exact decimal reported cash, venue/environment, real/play currency label, aware local observation/request clocks, and the account scope. Optional fields cover buying power, reservations, exposure credits and source clocks. Extra fields, nonfinite/floating-point cash, wrong environments, and claims that the result is available for Kelly are rejected. Keep all authentication headers, key IDs, raw private user IDs, arbitrary response bodies and exception text out of the result. The service rechecks the supplied client after each await; cached values cannot be installed under a replacement account.

The dashboard renders the returned account dictionary, so registering a reader requires no new venue branch in the UI. A new venue's execution cash certification remains a separate account-probe integration with its own inventory and identity evidence.
