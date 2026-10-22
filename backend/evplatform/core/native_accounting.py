"""Exact, versioned native-unit accounting; never coerces into legacy $1 orders.

Official contracts consulted 2026-10-21:
https://docs.novig.com/api/concepts/money
https://docs.kalshi.com/getting_started/fixed_point_migration
https://docs.prophetx.co/docs/direct-link-order-lifecycle

Prices remain in their native unit. American odds are compared as rational
numbers, avoiding an inexact odds -> probability -> odds round trip. Market
grids, fee bounds and cash precision must be supplied by a verified adapter.
This module does not authenticate, authorize, transmit, or infer fills.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, localcontext
from fractions import Fraction
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator, model_validator

from .models import OrderStatus, utcnow


def _decimal(value):
    if isinstance(value, (float, bool)):
        raise ValueError("Native money and quantities require Decimal, string, or integer; never float/bool")
    try:
        result = Decimal(value)
    except Exception as exc:
        raise ValueError("Invalid native decimal") from exc
    if not result.is_finite():
        raise ValueError("Native decimal must be finite")
    return result


ExactDecimal = Annotated[Decimal, BeforeValidator(_decimal)]
Nonnegative = Annotated[ExactDecimal, Field(ge=0)]
Positive = Annotated[ExactDecimal, Field(gt=0)]


class NativeContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)

    @field_validator("*", mode="after")
    @classmethod
    def aware_times(cls, value):
        if isinstance(value, datetime) and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("Native timestamps require a timezone")
        return value


class NativePriceBand(NativeContract):
    minimum: Positive
    maximum: Positive
    step: Positive

    @model_validator(mode="after")
    def valid_band(self):
        if self.maximum < self.minimum or self.maximum >= 1:
            raise ValueError("Probability price band must be within (0, 1)")
        return self

    def contains(self, price: Decimal) -> bool:
        return self.minimum <= price <= self.maximum and (price - self.minimum) % self.step == 0


class NativeInstrument(NativeContract):
    schema_version: Literal[1] = 1
    venue: str = Field(min_length=1)
    instrument_id: str = Field(min_length=1)
    # Older persisted instruments can omit this, but production Kalshi routing
    # must bind the exchange shard reviewed from the native market listing.
    exchange_index: int | None = Field(default=None, ge=0)
    canonical_market_id: str = Field(min_length=1)
    outcome: str = Field(min_length=1)
    native_outcome: str = Field(min_length=1)
    currency: Literal["USD"] = "USD"
    quantity_unit: Literal["contracts", "whole_cent_contracts", "usd_stake"]
    price_format: Literal["probability", "american"]
    payout_per_unit_usd: Positive | None = None
    quantity_step: Positive
    minimum_quantity: Positive
    # Distinct from price ticks and payout denominations. None means the
    # adapter has not established whether a minimum cash stake also applies.
    minimum_order_cost_usd: Nonnegative | None = None
    order_minimum_verified: bool = False
    order_minimum_source: str | None = None
    maximum_quantity: Positive | None = None
    # Explicit cash unit is used only for conservative bounds, never to silently
    # rewrite the venue's actual cash amounts or round a native quantity.
    cash_quantum_usd: Positive
    price_bands: tuple[NativePriceBand, ...] = ()
    settlement_rules_fingerprint: str = Field(min_length=1)
    verified: bool = False
    source: str = Field(min_length=1)

    @model_validator(mode="after")
    def coherent_units(self):
        if self.order_minimum_verified and (self.minimum_order_cost_usd is None or not self.order_minimum_source):
            raise ValueError("Verified order minimum requires cash rule and evidence source")
        if self.minimum_quantity % self.quantity_step:
            raise ValueError("Minimum quantity must be on the native grid")
        if self.maximum_quantity is not None and self.maximum_quantity < self.minimum_quantity:
            raise ValueError("Invalid native quantity range")
        if self.quantity_unit == "usd_stake":
            if self.price_format != "american" or self.payout_per_unit_usd is not None or self.price_bands:
                raise ValueError("USD stakes require native American odds and variable payout")
        elif self.price_format != "probability" or self.payout_per_unit_usd is None or not self.price_bands:
            raise ValueError("Contracts require payout and explicit native probability grid")
        if self.quantity_unit == "whole_cent_contracts" and (
            self.quantity_step != 1 or self.payout_per_unit_usd != Decimal("0.01")
        ):
            raise ValueError("Cent contracts have whole quantity and a $0.01 payout")
        if self.venue == "novig" and self.quantity_unit != "whole_cent_contracts":
            raise ValueError("Novig uses whole cent contracts")
        if self.venue == "prophetx" and self.quantity_unit != "usd_stake":
            raise ValueError("ProphetX quantity is USD stake")
        if self.venue == "kalshi" and (self.quantity_unit != "contracts" or self.payout_per_unit_usd != 1):
            raise ValueError("This Kalshi schema supports $1 binary contracts")
        if self.venue != "kalshi" and self.exchange_index is not None:
            raise ValueError("Exchange shard identity is only defined for Kalshi instruments")
        for index, band in enumerate(self.price_bands):
            if index and band.minimum <= self.price_bands[index - 1].maximum:
                raise ValueError("Price bands must be ordered and non-overlapping")
        return self

    def validate_quantity(self, quantity: Decimal, *, order: bool = False) -> Decimal:
        quantity = _decimal(quantity)
        if quantity <= 0 or quantity % self.quantity_step:
            raise ValueError("Quantity is not on the native grid")
        if order and quantity < self.minimum_quantity:
            raise ValueError("Quantity is below the native order minimum")
        if self.maximum_quantity is not None and quantity > self.maximum_quantity:
            raise ValueError("Quantity exceeds native maximum")
        return quantity

    def validate_price(self, price: Decimal) -> Decimal:
        price = _decimal(price)
        if self.price_format == "american":
            if price != price.to_integral_value() or abs(price) < 100:
                raise ValueError("American prices must be integral and outside (-100, 100)")
        elif not any(band.contains(price) for band in self.price_bands):
            raise ValueError("Price is not on the native market grid")
        return price

    def gross_ratio(self, price: Decimal) -> Fraction:
        """Gross winning payout per native quantity, represented exactly."""
        price = self.validate_price(price)
        if self.price_format == "probability":
            return Fraction(self.payout_per_unit_usd)
        return Fraction(1) + (Fraction(price) / 100 if price > 0 else Fraction(100) / Fraction(-price))

    def cost_for(self, quantity: Decimal, price: Decimal) -> Decimal:
        self.validate_quantity(quantity)
        self.validate_price(price)
        with localcontext() as ctx:
            ctx.prec = 256
            return quantity if self.quantity_unit == "usd_stake" else quantity * price * self.payout_per_unit_usd

    def payout_bound(self, quantity: Decimal, price: Decimal, *, upper: bool = False) -> Decimal:
        self.validate_quantity(quantity)
        ratio = Fraction(quantity) * self.gross_ratio(price)
        with localcontext() as ctx:
            ctx.prec = max(60, len(str(ratio.numerator)) + len(str(ratio.denominator)) + 10)
            value = Decimal(ratio.numerator) / Decimal(ratio.denominator)
            rounding = ROUND_CEILING if upper else ROUND_FLOOR
            return (value / self.cash_quantum_usd).to_integral_value(rounding=rounding) * self.cash_quantum_usd

    def price_satisfies_limit(self, actual: Decimal, limit: Decimal) -> bool:
        self.validate_price(actual)
        self.validate_price(limit)
        if self.price_format == "probability":
            return actual <= limit
        return self.gross_ratio(actual) >= self.gross_ratio(limit)

    def minimum_split_payout(self, quantity: Decimal, price: Decimal) -> Decimal:
        """Worst aggregate payout when each minimum-grid match rounds down.

        An order minimum does not restrict partial-match size. Actual individual
        fills still use payout_bound, with their recorded native cash evidence.
        """
        self.validate_quantity(quantity)
        with localcontext() as context:
            context.prec = 256
            return self.payout_bound(self.quantity_step, price) * (quantity / self.quantity_step)


class NativeOrderIntent(NativeContract):
    schema_version: Literal[1] = 1
    id: str = Field(min_length=1)
    client_order_id: str = Field(min_length=1)
    instrument: NativeInstrument
    quantity: Positive
    limit_price: ExactDecimal
    max_fee_usd: Nonnegative
    fee_source: str = Field(min_length=1)
    fees_verified: bool = False
    created_at: datetime = Field(default_factory=utcnow)
    mode: Literal["sandbox", "production"]

    @model_validator(mode="after")
    def valid_intent(self):
        if not self.instrument.verified or not self.fees_verified:
            raise ValueError("Native intent requires reviewed instrument and fee bounds")
        self.instrument.validate_quantity(self.quantity, order=True)
        self.instrument.validate_price(self.limit_price)
        if (self.instrument.minimum_order_cost_usd is not None and
                self.instrument.cost_for(self.quantity, self.limit_price) < self.instrument.minimum_order_cost_usd):
            raise ValueError("Native intent is below the minimum cash stake")
        return self

    @property
    def maximum_cost_usd(self) -> Decimal:
        return self.instrument.cost_for(self.quantity, self.limit_price)

    @property
    def minimum_gross_payout_usd(self) -> Decimal:
        return self.instrument.minimum_split_payout(self.quantity, self.limit_price)

    @property
    def reserved_cost_usd(self) -> Decimal:
        return self.maximum_cost_usd + self.max_fee_usd


# Short alias for consumers constructing a separate risk/OMS order wrapper.
NativeIntent = NativeOrderIntent


class NativeFill(NativeContract):
    fill_id: str = Field(min_length=1)
    # Some venues distinguish an execution/delivery ID from its exchange trade
    # ID. Preserve the latter for exact history joins; never infer it later.
    trade_id: str | None = Field(default=None, min_length=1)
    order_id: str = Field(min_length=1)
    client_order_id: str = Field(min_length=1)
    quantity: Positive
    price: ExactDecimal
    cost_usd: Nonnegative
    gross_payout_usd: Positive
    fee_usd: Nonnegative
    currency: Literal["USD"] = "USD"
    filled_at: datetime | None = None
    observed_at: datetime = Field(default_factory=utcnow)
    timestamp_kind: Literal["matched", "publication", "receipt"] = "matched"
    source: str = Field(min_length=1)
    cumulative_quantity_after: Positive | None = None

    @model_validator(mode="after")
    def accurate_clock_label(self):
        if (self.timestamp_kind == "matched") != (self.filled_at is not None):
            raise ValueError("Only a source match timestamp may be labeled filled_at")
        return self


class NativeCumulativeSnapshot(NativeContract):
    order_id: str = Field(min_length=1)
    client_order_id: str = Field(min_length=1)
    cumulative_quantity: Nonnegative
    cumulative_cost_usd: Nonnegative
    cumulative_gross_payout_usd: Nonnegative
    cumulative_fee_usd: Nonnegative
    currency: Literal["USD"] = "USD"
    observed_at: datetime
    status: OrderStatus
    authoritative: bool = False
    source: str = Field(min_length=1)
    revision: int | None = Field(default=None, ge=0)


class NativeTotals(NativeContract):
    quantity: Nonnegative = Decimal(0)
    cost_usd: Nonnegative = Decimal(0)
    gross_payout_usd: Nonnegative = Decimal(0)
    fee_usd: Nonnegative = Decimal(0)

    @property
    def total_cost_usd(self) -> Decimal:
        return self.cost_usd + self.fee_usd


class NativeAccountingDelta(NativeContract):
    quantity: Nonnegative = Decimal(0)
    cost_usd: Nonnegative = Decimal(0)
    gross_payout_usd: Nonnegative = Decimal(0)
    fee_usd: Nonnegative = Decimal(0)
    duplicate: bool = False


class ReconciliationRequired(ValueError):
    """Keep exposure reserved; missing or contradictory evidence is not zero."""


def merge_native_fill_metadata(previous: NativeFill, incoming: NativeFill) -> NativeFill:
    """Merge only a newly documented trade binding on the exact same execution.

    Missing optional metadata in a legacy journal or later response is not a
    financial change. Two different known trade IDs remain contradictory.
    """
    if (previous.model_dump(exclude={"observed_at", "trade_id"}) != incoming.model_dump(exclude={"observed_at", "trade_id"})
            or previous.trade_id is not None and incoming.trade_id is not None and previous.trade_id != incoming.trade_id):
        raise ReconciliationRequired("Conflicting duplicate native fill ID")
    return previous.model_copy(update={"trade_id": incoming.trade_id}) if previous.trade_id is None and incoming.trade_id else previous


class NativeAccounting:
    """Order-local journal with exact idempotency and conservative overlap rules.

    A fill arriving after an aggregate snapshot needs an explicit cumulative
    quantity watermark. Without it, adding the fill might double-count an
    execution already included in the snapshot, so reconciliation is required.
    Consumers must durably journal evidence and serialize calls under the OMS
    lock. A raised validation never partially changes this accumulator.
    """

    def __init__(self, intent: NativeOrderIntent, native_order_id: str):
        if not native_order_id:
            raise ValueError("Native order identity is required")
        self.intent = intent
        self.native_order_id = native_order_id
        self.totals = NativeTotals()
        self._fills: dict[str, NativeFill] = {}
        self._has_cumulative_baseline = False
        self._last_snapshot: NativeCumulativeSnapshot | None = None

    def _identity(self, evidence) -> None:
        if evidence.order_id != self.native_order_id or evidence.client_order_id != self.intent.client_order_id:
            raise ReconciliationRequired("Native order/client identity mismatch")

    def _validate_totals(self, totals: NativeTotals) -> None:
        instrument = self.intent.instrument
        if totals.quantity > self.intent.quantity:
            raise ReconciliationRequired("Cumulative quantity exceeds durable intent")
        if totals.quantity:
            instrument.validate_quantity(totals.quantity)
            max_cost = instrument.cost_for(totals.quantity, self.intent.limit_price)
            min_payout = instrument.minimum_split_payout(totals.quantity, self.intent.limit_price)
            if totals.cost_usd > max_cost or totals.gross_payout_usd < min_payout:
                raise ReconciliationRequired("Fill economics breach the limit")
            if instrument.quantity_unit == "usd_stake" and totals.cost_usd != totals.quantity:
                raise ReconciliationRequired("ProphetX cost must equal USD stake")
            if instrument.price_format == "probability" and totals.gross_payout_usd != totals.quantity * instrument.payout_per_unit_usd:
                raise ReconciliationRequired("Contract payout/quantity mismatch")
            if not totals.cost_usd:
                raise ReconciliationRequired("Executed quantity requires explicit positive cost")
        elif totals.cost_usd or totals.gross_payout_usd:
            raise ReconciliationRequired("Monetary fill totals have no executed quantity")
        if totals.fee_usd > self.intent.max_fee_usd:
            raise ReconciliationRequired("Executed fees exceed reserved bound")

    def _commit(self, totals: NativeTotals) -> NativeAccountingDelta:
        self._validate_totals(totals)
        values = {key: getattr(totals, key) - getattr(self.totals, key) for key in NativeTotals.model_fields}
        if any(value < 0 for value in values.values()):
            raise ReconciliationRequired("Native cumulative accounting regressed")
        delta = NativeAccountingDelta(**values, duplicate=not any(values.values()))
        self.totals = totals
        return delta

    def apply_fill(self, fill: NativeFill) -> NativeAccountingDelta:
        self._identity(fill)
        old = self._fills.get(fill.fill_id)
        if old is not None:
            # A repeated REST/history read has a new local receipt clock, but
            # the same venue fill identity and economic evidence. Keep source
            # matched time, quantity, price, money and cumulative watermark in
            # the comparison; only delivery observation is non-economic.
            self._fills[fill.fill_id] = merge_native_fill_metadata(old, fill)
            return NativeAccountingDelta(duplicate=True)
        instrument = self.intent.instrument
        instrument.validate_quantity(fill.quantity)
        if not instrument.price_satisfies_limit(fill.price, self.intent.limit_price):
            raise ReconciliationRequired("Native fill price breaches order limit")
        if fill.cost_usd != instrument.cost_for(fill.quantity, fill.price):
            raise ReconciliationRequired("Native fill cost conflicts with quantity/price")
        lower = instrument.payout_bound(fill.quantity, fill.price)
        upper = instrument.payout_bound(fill.quantity, fill.price, upper=True)
        if not lower <= fill.gross_payout_usd <= upper:
            raise ReconciliationRequired("Native fill payout conflicts with quantity/price")
        watermark = fill.cumulative_quantity_after
        if self._has_cumulative_baseline and watermark is None:
            raise ReconciliationRequired("Fill/snapshot overlap needs a cumulative watermark")
        if watermark is not None:
            if watermark < fill.quantity or watermark > self.intent.quantity:
                raise ReconciliationRequired("Invalid native fill cumulative watermark")
            if watermark <= self.totals.quantity:
                if not self._has_cumulative_baseline:
                    raise ReconciliationRequired("Unseen fill overlaps prior individual executions")
                # Two distinct fill IDs cannot represent the same cumulative
                # interval. Aggregate observations don't create synthetic IDs.
                start = watermark - fill.quantity
                for known in self._fills.values():
                    end = known.cumulative_quantity_after
                    if end is not None and max(start, end - known.quantity) < min(watermark, end):
                        raise ReconciliationRequired("Different fill IDs overlap a cumulative interval")
                known_quantity = sum((item.quantity for item in self._fills.values()), Decimal(0)) + fill.quantity
                known_cost = sum((item.cost_usd for item in self._fills.values()), Decimal(0)) + fill.cost_usd
                known_payout = sum((item.gross_payout_usd for item in self._fills.values()), Decimal(0)) + fill.gross_payout_usd
                known_fees = sum((item.fee_usd for item in self._fills.values()), Decimal(0)) + fill.fee_usd
                known = NativeTotals(quantity=known_quantity, cost_usd=known_cost,
                                     gross_payout_usd=known_payout, fee_usd=known_fees)
                if any(getattr(known, key) > getattr(self.totals, key) for key in NativeTotals.model_fields):
                    raise ReconciliationRequired("Covered fills exceed cumulative monetary evidence")
                if known.quantity == self.totals.quantity and known != self.totals:
                    raise ReconciliationRequired("Covered fills disagree with complete aggregate evidence")
                # Do not add a late fill to an already accounted aggregate.
                self._fills[fill.fill_id] = fill
                return NativeAccountingDelta(duplicate=True)
            if watermark != self.totals.quantity + fill.quantity:
                raise ReconciliationRequired("Native fill gap requires aggregate reconciliation")
        totals = NativeTotals(quantity=self.totals.quantity + fill.quantity,
                              cost_usd=self.totals.cost_usd + fill.cost_usd,
                              gross_payout_usd=self.totals.gross_payout_usd + fill.gross_payout_usd,
                              fee_usd=self.totals.fee_usd + fill.fee_usd)
        delta = self._commit(totals)
        self._fills[fill.fill_id] = fill
        return delta

    def apply_cumulative(self, snapshot: NativeCumulativeSnapshot) -> NativeAccountingDelta:
        self._identity(snapshot)
        if not snapshot.authoritative:
            raise ReconciliationRequired("Non-authoritative snapshot cannot change accounting")
        previous = self._last_snapshot
        if previous is not None:
            if snapshot.source == previous.source and snapshot.revision is not None and previous.revision is not None:
                if snapshot.revision < previous.revision:
                    return NativeAccountingDelta(duplicate=True)
                if snapshot.revision == previous.revision and snapshot != previous:
                    raise ReconciliationRequired("Conflicting native snapshot revision")
            if snapshot.observed_at < previous.observed_at:
                return NativeAccountingDelta(duplicate=True)
        totals = NativeTotals(quantity=snapshot.cumulative_quantity, cost_usd=snapshot.cumulative_cost_usd,
                              gross_payout_usd=snapshot.cumulative_gross_payout_usd, fee_usd=snapshot.cumulative_fee_usd)
        if snapshot.status == OrderStatus.FILLED and totals.quantity != self.intent.quantity:
            raise ReconciliationRequired("Filled status contradicts original native quantity")
        if snapshot.status == OrderStatus.REJECTED and totals.quantity:
            raise ReconciliationRequired("Rejected status contradicts native fills")
        if totals.quantity == self.totals.quantity and (
            totals.cost_usd != self.totals.cost_usd or totals.gross_payout_usd != self.totals.gross_payout_usd
        ):
            raise ReconciliationRequired("Same quantity has conflicting cumulative economics")
        delta = self._commit(totals)
        self._last_snapshot = snapshot
        self._has_cumulative_baseline = True
        return delta


class NativeSettlement(NativeContract):
    order_id: str = Field(min_length=1)
    client_order_id: str = Field(min_length=1)
    settlement_id: str = Field(min_length=1)
    kind: Literal["win", "loss", "push", "void", "partial"]
    gross_return_usd: Nonnegative
    returned_fees_usd: Nonnegative
    settled_at: datetime
    source: str = Field(min_length=1)
    verified: bool = False

    def realized_profit(self, accounting: NativeAccounting) -> Decimal:
        accounting._identity(self)
        if not self.verified:
            raise ReconciliationRequired("Unverified settlement cannot release exposure")
        totals = accounting.totals
        if not totals.quantity:
            raise ReconciliationRequired("Settlement has no reconciled position")
        if self.returned_fees_usd > totals.fee_usd:
            raise ReconciliationRequired("Returned fees exceed paid fees")
        expected = {"win": totals.gross_payout_usd, "loss": Decimal(0),
                    "push": totals.cost_usd, "void": totals.cost_usd}.get(self.kind)
        if expected is not None and self.gross_return_usd != expected:
            raise ReconciliationRequired("Settlement cash conflicts with declared outcome")
        if self.gross_return_usd > totals.gross_payout_usd:
            raise ReconciliationRequired("Settlement return exceeds recorded payout")
        return self.gross_return_usd + self.returned_fees_usd - totals.total_cost_usd
