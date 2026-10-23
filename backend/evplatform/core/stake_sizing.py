"""Conservative quarter Kelly in USD, using one account's available cash.

Fees are bounded at the smallest legal order so fixed-fee dilution cannot
inflate the Kelly fraction. Under strict settlement modeling, bounded
refund/void probability is treated as a loss for sizing only. Under the
winner-only policy, sizing is conditional on normal resolution instead; it
does not assert zero real-world exceptional-event probability.
"""
from decimal import Decimal as D, ROUND_CEILING, localcontext

from pydantic import Field

from .native_accounting import NativeContract, Nonnegative, Positive
from .pricing import NoSignal


class StakeSizing(NativeContract):
    method: str = "quarter_kelly"
    fraction: Positive = D("0.25")
    unit_size_usd: Positive
    account_cash_after_reservations_usd: Nonnegative
    conservative_probability: Nonnegative
    full_kelly_fraction: Nonnegative
    budget_usd: Nonnegative
    minimum_quantity: Positive
    minimum_cost_usd: Positive
    minimum_cost_with_fees_usd: Positive
    minimum_upfront_debit_usd: Positive
    minimum_evidence: str = Field(min_length=1)


def minimum_order(book, price, now):
    ins = book.instrument
    if not ins.verified or not ins.order_minimum_verified or ins.minimum_order_cost_usd is None or not ins.order_minimum_source:
        raise NoSignal("Native minimum stake and quantity rules are not verified")
    with localcontext() as context:
        context.prec = 100
        cost_per_step = ins.cost_for(ins.quantity_step, price)
        cash_steps = (ins.minimum_order_cost_usd / cost_per_step).to_integral_value(rounding=ROUND_CEILING)
        quantity = max(ins.minimum_quantity, cash_steps * ins.quantity_step)
        ins.validate_quantity(quantity, order=True)
        cost = ins.cost_for(quantity, price)
        fee = book.fee_bound
        return quantity, cost, cost + fee.maximum_fee(ins, quantity, now), cost + fee.maximum_upfront_fee(ins, quantity, now)


def quarter_kelly_budget(book, probability, bounds, *, available_cash_usd, unit_size_usd, now,
                        max_stake_usd=None, price=None):
    if isinstance(available_cash_usd, (float, bool)) or available_cash_usd is None:
        raise NoSignal("Quarter Kelly requires verified account cash")
    cash = D(available_cash_usd)
    if not cash.is_finite() or cash <= 0:
        raise NoSignal("No verified available account cash after pending reservations")
    price = book.asks[0].price if price is None else price
    ins = book.instrument
    quantity, cost, economic_cost, upfront_debit = minimum_order(book, price, now)
    with localcontext() as context:
        context.prec = 100
        # Match size may be much smaller than the NEW order minimum. Reserve
        # for a cash rounding loss on every smallest legal partial match.
        gross = ins.minimum_split_payout(quantity, price) / quantity
        unit_cost = economic_cost / quantity
        p = probability * (1 - bounds.non_decisive_probability())
        fraction = max(D(0), min(D(1), (p * gross - unit_cost) / (gross - unit_cost))) if gross > unit_cost else D(0)
        budget = cash * D("0.25") * fraction
        if max_stake_usd is not None:
            cap = D(max_stake_usd)
            if not cap.is_finite() or cap <= 0:
                raise NoSignal("Invalid native stake cap")
            budget = min(budget, cap)
    return StakeSizing(unit_size_usd=unit_size_usd, account_cash_after_reservations_usd=cash,
        conservative_probability=p, full_kelly_fraction=fraction, budget_usd=budget,
        minimum_quantity=quantity, minimum_cost_usd=cost, minimum_cost_with_fees_usd=economic_cost,
        minimum_upfront_debit_usd=upfront_debit,
        minimum_evidence=ins.order_minimum_source)
