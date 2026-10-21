# Quarter-Kelly stakes and $5 reporting units

The owner's policy is quarter Kelly, with **$5 = 1 reporting unit**, no fixed dollar cap per bet, and no daily-loss stop. Each venue uses only its own authenticated, reconciled available USD cash. Pending reservations are deducted conservatively, even if the venue has already frozen that cash. Other accounts' money and uncertain/unsettled payouts are never added. Overlapping events and players remain blocked across all accounts.

The saved policy in `data/operator_launch_limits.json` is a draft until it is bound to reviewed launch profiles. Saving it does not enable real trading. Legacy capped-stake configurations still work; they do not silently change to Kelly.

For a no-push binary wager, the full Kelly fraction of cash at risk is `(p × gross_payout − all_in_cost) / (gross_payout − all_in_cost)`. The allowed budget is one quarter of that fraction times current account cash. The calculation uses the lower fair probability from the primary and sensitivity models, conservative fee bounds, and a conservative payout bound. Verified upper push/void probabilities reduce the winning probability for sizing; treating those cases as losses gives a conservative budget rather than assuming a refund is a win. If net winning payout is nonpositive or the edge is nonpositive, the budget is zero.

Fixed fees are allocated at the smallest legal order, making the fraction conservative for larger orders. All costs include worst-case execution fees. Payouts also allow for rounding down on every smallest legal partial match; rounding once for the entire order can overstate the available edge. Quantities round **down** to the native increment and are limited by executable depth. If Kelly's budget or available depth cannot meet the verified minimum quantity and cash stake, skip the order. Never round up to meet a minimum. A partial fill can leave less than the minimum for a new order; valuing that residual does not authorize another order.

Native mappings must carry the actual `minimum_quantity`, `quantity_step`, `minimum_order_cost_usd`, `order_minimum_verified`, and `order_minimum_source`. A zero cash minimum means an explicitly verified absence of a separate notional minimum. `null` means unknown and blocks Kelly sizing. Price ticks, contract face value, and profit thresholds are not interchangeable with minimum stake. An API example's amount is not proof of a venue-wide rule.

Account identity, environment, observation age, reconciliation, ledger generation, available cash, prices, fair probability, fees, and minimum rules are checked again at the final submission boundary. A changed bankroll or lost edge blocks an already prepared order. No disk/network lookup is added to that calculation; account evidence is refreshed by the independent account worker.

The native order journal retains the sizing calculation and unit denomination. Filled cost plus paid execution fees divided by the saved unit size gives units actually risked. Unit size does not set a $5 minimum, force $5 increments, or impose a $5 cap. Kelly sizing cannot establish that estimated probabilities are accurate or guarantee profit.

Run the offline sizing checks with `python -m pytest tests/test_stake_sizing.py`. These are simulated financial fixtures, not evidence of real account execution.
