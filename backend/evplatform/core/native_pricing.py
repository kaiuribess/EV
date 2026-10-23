"""Memory-only native-unit pricing with explicit cash, fee and settlement bounds."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal, ROUND_FLOOR, localcontext
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import Field, model_validator

from .config import PricingConfig
from .models import FairValue, Market, utcnow
from .native_accounting import (
    ExactDecimal, NativeContract, NativeInstrument, NativeOrderIntent, Nonnegative, Positive,
)
from .pricing import NoSignal, market_config, validate_fair_references
from .stake_sizing import StakeSizing, quarter_kelly_budget

D = Decimal
AnnotatedProbability = Annotated[ExactDecimal, Field(ge=0, le=1)]


def _fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def instrument_fingerprint(instrument: NativeInstrument) -> str:
    return _fingerprint(instrument)


def settlement_fingerprint(value: Market) -> str:
    """Bind settlement to the complete market (event, player, line and rules)."""
    return value.identity()


class NativeLevel(NativeContract):
    price: ExactDecimal
    quantity: Positive


class NativeFeeBound(NativeContract):
    """Verified maximum USD charges, including every possible minimum-lot fill.

    A commission paid only on a win must still be bounded as if it is paid.
    These are caller-supplied bounds, not a hardcoded venue fee schedule.
    """
    instrument_fingerprint: str = Field(min_length=1)
    per_unit_usd: Nonnegative
    per_fill_usd: Nonnegative
    fixed_order_usd: Nonnegative
    verified: bool = False
    source: str = Field(min_length=1)
    observed_at: datetime
    valid_until: datetime

    @model_validator(mode="after")
    def valid_window(self):
        if self.valid_until <= self.observed_at:
            raise ValueError("Fee bounds must have a positive validity window")
        return self

    def maximum_fee(self, instrument: NativeInstrument, quantity: Decimal, now: datetime) -> Decimal:
        if not self.verified or self.instrument_fingerprint != instrument_fingerprint(instrument):
            raise NoSignal("Unverified or incompatible native fee bound")
        if not self.observed_at <= now < self.valid_until:
            raise NoSignal("Native fee bound is stale or future-dated")
        instrument.validate_quantity(quantity)
        with localcontext() as context:
            context.prec = 100
            maximum_fills = quantity / instrument.quantity_step
            return quantity * self.per_unit_usd + maximum_fills * self.per_fill_usd + self.fixed_order_usd


class NativeBook(NativeContract):
    schema_version: Literal[1] = 1
    instrument: NativeInstrument
    market_fingerprint: str = Field(min_length=1)
    asks: tuple[NativeLevel, ...]
    fee_bound: NativeFeeBound
    source_time: datetime | None
    provider_time: datetime | None = None
    received_at: datetime = Field(default_factory=utcnow)
    revision: int = Field(ge=0)
    synchronized: bool = False
    status: Literal["active", "suspended", "closed"] = "active"
    synthetic: bool = False

    @model_validator(mode="after")
    def valid_levels(self):
        previous = None
        for level in self.asks:
            self.instrument.validate_price(level.price)
            self.instrument.validate_quantity(level.quantity)
            if previous is not None and not self.instrument.price_satisfies_limit(previous, level.price):
                raise ValueError("Native asks must be ordered best price first")
            previous = level.price
        return self

    def age_ms(self, now: datetime | None = None) -> float | None:
        return None if self.source_time is None else ((now or utcnow()) - self.source_time).total_seconds() * 1000


class NativeSettlementProbabilities(NativeContract):
    """Reviewed bounds for non-binary cash outcomes; never inferred from labels."""
    verified: bool = False
    source: str = Field(min_length=1)
    push_probability_upper_bound: AnnotatedProbability
    void_probability_upper_bound: AnnotatedProbability
    refund_cost_on_push: bool
    refund_cost_on_void: bool

    @model_validator(mode="after")
    def valid_probability_mass(self):
        if self.push_probability_upper_bound + self.void_probability_upper_bound > 1:
            raise ValueError("Settlement probability bounds exceed one")
        return self

    def non_decisive_probability(self) -> Decimal:
        return self.push_probability_upper_bound + self.void_probability_upper_bound


class WinnerOnlyNormalOutcome(NativeContract):
    """Conditional WIN/LOSS estimate, with exceptional states outside the model.

    This carries no assertion that exceptional outcomes have zero real-world
    probability or that the venue would refund a contract.
    """
    source: Literal["winner_only_binary: conditional normal WIN/LOSS estimate; exceptional settlements excluded"] = (
        "winner_only_binary: conditional normal WIN/LOSS estimate; exceptional settlements excluded")

    def non_decisive_probability(self) -> Decimal:
        return D(0)  # Zero mass *inside the conditional normal-resolution model*.


class NativeCandidate(NativeContract):
    id: str
    strategy: str = "native_positive_ev"
    market_id: str
    event_id: str
    venue: str
    outcome: str
    intent: NativeOrderIntent
    fair_probability: AnnotatedProbability
    expected_profit_usd: ExactDecimal
    ev_per_usd_payout: ExactDecimal
    roi: ExactDecimal
    book_revision: int = Field(ge=0)
    references: tuple[dict[str, Any], ...]
    calculated_at: datetime
    # Null when a sportsbook selection has no measured observation clock.
    # Native-book age remains available separately from reference evidence.
    quote_age_ms: float | None = Field(default=None, ge=0)
    native_book_age_ms: float | None = Field(default=None, ge=0)
    unadjusted_fair_probability: AnnotatedProbability | None = None
    source_quality_mode: Literal["strict_verified", "trusted_provider"] = "strict_verified"
    uncertainty_margin_probability: AnnotatedProbability | None = None
    qualified: bool
    synthetic: Literal[False] = False
    model: str
    normal_outcome_policy: Literal["strict_settlement_equivalence", "winner_only_binary"] = "strict_settlement_equivalence"
    estimate_label: str = "Settlement-aware estimated EV"
    settlement_probability_source: str
    reference_profile_id: str | None = None
    reference_policy_digest: str | None = None
    reference_profile_status: str | None = None
    live_execution_allowed: bool = True
    execution_eligible: bool = True
    ineligibility_reasons: tuple[str, ...] = ()
    sensitivity_model: str | None = None
    sensitivity_fair_probability: AnnotatedProbability | None = None
    sensitivity_expected_profit_usd: ExactDecimal | None = None
    sensitivity_ev_per_usd_payout: ExactDecimal | None = None
    sensitivity_roi: ExactDecimal | None = None
    sizing: StakeSizing | None = None


def _settlement_bounds(market: Market, *, winner_only: bool = False) -> NativeSettlementProbabilities | WinnerOnlyNormalOutcome:
    rules = market.settlement
    if rules.contract_type != "binary" or len(market.outcomes) not in ({2, 3} if winner_only else {2}) or rules.currency != "USD":
        raise NoSignal("Native pricing needs reviewed two-outcome USD settlement")
    if winner_only:
        # A three-way sportsbook reference includes draws in its probability
        # distribution; one native YES selection still has a binary payout.
        # An integer-line push can occur in an otherwise normally completed
        # game and is therefore NOT an exceptional state we may discard.
        if rules.push_rule != "not_applicable":
            raise NoSignal("Normal-completion push requires an unconditional probability model")
        return WinnerOnlyNormalOutcome()
    needs_push = rules.push_rule != "not_applicable"
    needs_void = rules.void_rule != "not_applicable"
    raw = market.extensions.get("native_settlement_probabilities_v1")
    if raw is None and not needs_push and not needs_void:
        return NativeSettlementProbabilities(verified=True, source="canonical rules: no push or void",
            push_probability_upper_bound="0", void_probability_upper_bound="0",
            refund_cost_on_push=True, refund_cost_on_void=True)
    try:
        bounds = NativeSettlementProbabilities.model_validate(raw)
    except (TypeError, ValueError) as exc:
        raise NoSignal("Reviewed push/void probability bounds are missing") from exc
    if not bounds.verified or (needs_push and not bounds.refund_cost_on_push) or (needs_void and not bounds.refund_cost_on_void):
        raise NoSignal("Push/void payout model is not verified")
    if not needs_push and bounds.push_probability_upper_bound or not needs_void and bounds.void_probability_upper_bound:
        raise NoSignal("Settlement probability bounds contradict canonical rules")
    return bounds


def _validate_market(market: Market, fair: FairValue, book: NativeBook,
                     config: PricingConfig, now: datetime) -> NativeSettlementProbabilities | WinnerOnlyNormalOutcome:
    config = market_config(config, market)
    instrument = book.instrument
    if not instrument.verified or instrument.canonical_market_id != market.id or book.market_fingerprint != market.identity():
        raise NoSignal("Native book has no exact reviewed canonical mapping")
    if instrument.settlement_rules_fingerprint != settlement_fingerprint(market):
        raise NoSignal("Native settlement rules do not match the canonical contract")
    if market.status in {"suspended", "closed"} or book.status != "active" or not book.synchronized or not book.asks:
        raise NoSignal("Native book unavailable or not synchronized")
    if book.synthetic or fair.synthetic:
        raise NoSignal("Synthetic observations cannot produce native execution candidates")
    if fair.market_id != market.id or instrument.outcome not in fair.probabilities:
        raise NoSignal("Fair value does not match the native outcome")
    age = book.age_ms(now)
    if age is None or age < 0 or age > config.max_book_age_ms:
        raise NoSignal("Native source price timestamp is missing, future-dated or stale")
    if book.received_at > now or book.provider_time is not None and book.provider_time > now:
        raise NoSignal("Native receipt/publication clock is future-dated")
    validate_fair_references(market, fair, config, now)
    if not fair.execution_eligible:
        raise NoSignal("Reference profile is research-only: validated observations, status and TTL are required")
    if any(reference["bookmaker"] == instrument.venue for reference in fair.references):
        raise NoSignal("Target native venue cannot be its own independent reference")
    return _settlement_bounds(market, winner_only=config.normal_outcome_policy == "winner_only_binary")


def research_native_estimate(market: Market, fair: FairValue, book: NativeBook,
                             config: PricingConfig, *, now: datetime | None = None) -> dict[str, Any]:
    """Read-only minimum-lot comparison; never creates an order candidate.

    This keeps a provider-trusted fair estimate visible beside a reviewed
    native offer while leaving every execution and account gate untouched.
    """
    now = now or utcnow()
    config = market_config(config, market)
    instrument = book.instrument
    if config.normal_outcome_policy != "winner_only_binary":
        raise NoSignal("Research comparison requires winner-only binary pricing")
    if (not instrument.verified or instrument.canonical_market_id != market.id
            or book.market_fingerprint != market.identity()
            or instrument.settlement_rules_fingerprint != settlement_fingerprint(market)
            or instrument.outcome not in fair.probabilities):
        raise NoSignal("Reviewed native market/outcome identity is unavailable")
    if (market.status in {"closed", "suspended"} or book.status != "active"
            or not book.synchronized or not book.asks or book.synthetic or fair.synthetic):
        raise NoSignal("Current real native offer is unavailable")
    age = book.age_ms(now)
    if (age is None or not 0 <= age <= config.max_book_age_ms or book.received_at > now
            or book.provider_time is not None and book.provider_time > now):
        raise NoSignal("Native executable offer is stale or future-dated")
    if not instrument.order_minimum_verified or instrument.minimum_order_cost_usd is None:
        raise NoSignal("Native minimum order cost is unverified")
    if any(reference["bookmaker"] == instrument.venue for reference in fair.references):
        raise NoSignal("Target venue cannot supply its own fair reference")
    validate_fair_references(market, fair, config, now)
    _settlement_bounds(market, winner_only=True)
    level = book.asks[0]
    quantity = instrument.minimum_quantity
    if level.quantity < quantity:
        raise NoSignal("Insufficient depth at the best executable price for one minimum lot")
    cost = instrument.cost_for(quantity, level.price)
    fee_cap = book.fee_bound.maximum_fee(instrument, quantity, now)
    debit = cost + fee_cap
    if cost < instrument.minimum_order_cost_usd:
        raise NoSignal("Minimum lot is below the venue's verified minimum cash order")
    payout = instrument.minimum_split_payout(quantity, level.price)
    if payout <= 0 or debit <= 0:
        raise NoSignal("Native cash units do not support a minimum-lot comparison")
    outcome = instrument.outcome
    expected_profit = fair.probabilities[outcome] * payout - debit
    sensitivity_profit = (fair.sensitivity_probabilities[outcome] * payout - debit
                          if fair.sensitivity_probabilities else None)
    return {"market_id": market.id, "venue": instrument.venue,
            "native_market_id": instrument.instrument_id, "outcome": outcome,
            "price": str(level.price), "minimum_quantity": str(quantity),
            "available_at_best_price": str(level.quantity),
            "cash_cost_usd": str(cost), "conservative_fee_usd": str(fee_cap),
            "maximum_debit_usd": str(debit), "normal_winning_payout_usd": str(payout),
            "expected_profit_usd": str(expected_profit), "roi": str(expected_profit / debit),
            "sensitivity_expected_profit_usd": str(sensitivity_profit) if sensitivity_profit is not None else None,
            "positive_estimate": expected_profit > 0 and (sensitivity_profit is None or sensitivity_profit > 0),
            "quality_mode": config.reference_quality_mode,
            "execution_eligible": False, "reason": "Read-only research comparison; cash, sizing, risk and authorization not checked",
            "calculated_at": now.isoformat()}


def _make_candidate(market: Market, fair: FairValue, book: NativeBook, config: PricingConfig,
                    intent: NativeOrderIntent, bounds: NativeSettlementProbabilities | WinnerOnlyNormalOutcome,
                    now: datetime, *, durable_intent: NativeOrderIntent | None = None) -> NativeCandidate:
    config = market_config(config, market)
    unadjusted_probability = fair.probabilities[intent.instrument.outcome]
    margin = fair.trusted_pilot_margin_probability or D(0)
    probability = unadjusted_probability - margin
    if not probability.is_finite() or not 0 <= probability <= 1:
        raise NoSignal("Invalid native fair probability")
    cost, payout = intent.maximum_cost_usd, intent.minimum_gross_payout_usd
    if not payout or not cost:
        raise NoSignal("Native order size is below representable cash bounds")
    non_decisive = bounds.non_decisive_probability()
    # Refunded cost earns no edge; retain the entire fee cap even when a
    # venue might return some fees. Use the worse endpoint for negative edges.
    def profit_at(p):
        binary_profit = p * payout - cost
        return min(binary_profit, (1 - non_decisive) * binary_profit) - intent.max_fee_usd
    expected_profit = profit_at(probability)
    ev = expected_profit / payout
    roi = expected_profit / intent.reserved_cost_usd
    sensitivity_p = (fair.sensitivity_probabilities[intent.instrument.outcome] - margin
                     if fair.sensitivity_probabilities else None)
    if sensitivity_p is not None and not 0 <= sensitivity_p <= 1:
        raise NoSignal("Pilot uncertainty allowance exceeds sensitivity probability")
    sensitivity_profit = profit_at(sensitivity_p) if sensitivity_p is not None else None
    sensitivity_ev = sensitivity_profit / payout if sensitivity_profit is not None else None
    sensitivity_roi = sensitivity_profit / intent.reserved_cost_usd if sensitivity_profit is not None else None
    qualified = ev >= config.min_ev and roi >= config.min_roi and (
        sensitivity_ev is None or sensitivity_ev >= config.min_ev and sensitivity_roi >= config.min_roi)
    reasons = () if qualified else ("Both primary and sensitivity models must meet native EV and ROI thresholds",)
    ref_ages = ([(now - datetime.fromisoformat(ref["source_time"])).total_seconds() * 1000
                 for ref in fair.references] if all(ref.get("source_time") for ref in fair.references) else [])
    measured_quote_age = (max([book.age_ms(now), *ref_ages]) if ref_ages else None)
    return NativeCandidate(id=intent.id, market_id=market.id, event_id=market.event_id,
        venue=intent.instrument.venue, outcome=intent.instrument.outcome, intent=durable_intent or intent,
        fair_probability=probability, expected_profit_usd=expected_profit, ev_per_usd_payout=ev,
        roi=roi, book_revision=book.revision, references=fair.references, calculated_at=now,
        quote_age_ms=measured_quote_age, native_book_age_ms=book.age_ms(now),
        unadjusted_fair_probability=unadjusted_probability if margin else None,
        source_quality_mode=config.reference_quality_mode,
        uncertainty_margin_probability=margin if margin else None,
        qualified=qualified, model=fair.model, normal_outcome_policy=fair.normal_outcome_policy,
        estimate_label=("Winner-only estimated EV — exceptional settlements excluded"
                        if fair.normal_outcome_policy == "winner_only_binary"
                        else "Settlement-aware estimated EV"),
        settlement_probability_source=bounds.source,
        reference_profile_id=fair.reference_profile_id, reference_policy_digest=fair.reference_policy_digest,
        reference_profile_status=fair.reference_profile_status, live_execution_allowed=fair.live_execution_allowed,
        execution_eligible=qualified and fair.execution_eligible, ineligibility_reasons=reasons,
        sensitivity_model=fair.sensitivity_model, sensitivity_fair_probability=sensitivity_p,
        sensitivity_expected_profit_usd=sensitivity_profit, sensitivity_ev_per_usd_payout=sensitivity_ev,
        sensitivity_roi=sensitivity_roi)


def native_opportunity(market: Market, fair: FairValue, book: NativeBook, config: PricingConfig, *,
                       max_stake_usd: Decimal | None, mode: Literal["sandbox", "production"],
                       stake_sizing: Literal["capped", "quarter_kelly"] = "capped",
                       available_cash_usd: Decimal | None = None, unit_size_usd: Decimal = D("5"),
                       now: datetime | None = None) -> NativeCandidate:
    """Size the best executable native level within a USD budget, including fees."""
    now = now or utcnow()
    if mode not in {"sandbox", "production"}:
        raise NoSignal("Native execution mode is not explicitly configured")
    if (max_stake_usd is None and stake_sizing != "quarter_kelly") or isinstance(max_stake_usd, (float, bool)):
        raise NoSignal("Native stake budget must be explicitly configured as an exact decimal")
    bounds = _validate_market(market, fair, book, config, now)
    if mode == "production" and not fair.live_execution_allowed:
        raise NoSignal("Reference profile does not authorize production execution")
    scoped = market_config(config, market)
    if scoped.reference_quality_mode == "trusted_provider":
        pilot = scoped.trusted_pilot_policy
        ceiling = min(pilot.max_debit_usd, pilot.max_loss_usd)
        max_stake_usd = ceiling if max_stake_usd is None else min(D(max_stake_usd), ceiling)
    sizing = None
    if stake_sizing == "quarter_kelly":
        margin = fair.trusted_pilot_margin_probability or D(0)
        probabilities = [fair.probabilities[book.instrument.outcome] - margin]
        if fair.sensitivity_probabilities:
            probabilities.append(fair.sensitivity_probabilities[book.instrument.outcome] - margin)
        if min(probabilities) <= 0:
            raise NoSignal("Pilot uncertainty allowance eliminates the modeled winning chance")
        sizing = quarter_kelly_budget(book, min(probabilities), bounds, available_cash_usd=available_cash_usd,
            unit_size_usd=unit_size_usd, now=now, max_stake_usd=max_stake_usd)
        if sizing.budget_usd < sizing.minimum_cost_with_fees_usd:
            raise NoSignal("Quarter-Kelly budget is below the verified native minimum including fees; skipped")
        max_stake_usd = sizing.budget_usd
    try:
        max_stake_usd = D(max_stake_usd)
    except (ValueError, TypeError, ArithmeticError) as exc:
        raise NoSignal("Native stake budget is invalid") from exc
    if not max_stake_usd.is_finite() or max_stake_usd <= 0:
        raise NoSignal("Native stake budget is invalid")
    instrument = book.instrument
    price = book.asks[0].price
    available = sum((level.quantity for level in book.asks if level.price == price), D(0))
    step = instrument.quantity_step
    fee = book.fee_bound
    fee.maximum_fee(instrument, step, now)  # Validate binding and expiry before sizing.
    with localcontext() as context:
        context.prec = 100
        unit_cost = instrument.cost_for(step, price) / step
        variable_cost = unit_cost + fee.per_unit_usd + fee.per_fill_usd / step
        if max_stake_usd <= fee.fixed_order_usd:
            raise NoSignal("Native order budget is smaller than fixed fees")
        affordable = ((max_stake_usd - fee.fixed_order_usd) / variable_cost / step).to_integral_value(rounding=ROUND_FLOOR) * step
    quantity = min(affordable, available)
    if instrument.maximum_quantity is not None:
        quantity = min(quantity, instrument.maximum_quantity)
    quantity = (quantity / step).to_integral_value(rounding=ROUND_FLOOR) * step
    minimum = sizing.minimum_quantity if sizing else instrument.minimum_quantity
    if quantity < minimum:
        raise NoSignal("No executable native quantity within stake and liquidity limits")
    maximum_fee = fee.maximum_fee(instrument, quantity, now)
    identity = str(uuid4())
    intent = NativeOrderIntent(id=identity, client_order_id=str(uuid4()), instrument=instrument,
        quantity=quantity, limit_price=price, max_fee_usd=maximum_fee, fee_source=fee.source,
        fees_verified=True, created_at=now, mode=mode)
    if intent.reserved_cost_usd > max_stake_usd:
        raise NoSignal("Native rounded reservation exceeds stake budget")
    return _make_candidate(market, fair, book, config, intent, bounds, now).model_copy(update={"sizing": sizing})


def native_revalidate_intent(intent: NativeOrderIntent, market: Market, fair: FairValue,
                             book: NativeBook, config: PricingConfig, *, now: datetime | None = None,
                             remaining_quantity: Decimal | None = None,
                             fees_already_paid_usd: Decimal = D(0)) -> NativeCandidate:
    """Reprice a durable intent at its original limit and current fee/liquidity cap."""
    now = now or utcnow()
    bounds = _validate_market(market, fair, book, config, now)
    if intent.mode == "production" and not fair.live_execution_allowed:
        raise NoSignal("Reference profile does not authorize production execution")
    scoped = market_config(config, market)
    if scoped.reference_quality_mode == "trusted_provider":
        pilot = scoped.trusted_pilot_policy
        if intent.reserved_cost_usd > min(pilot.max_debit_usd, pilot.max_loss_usd):
            raise NoSignal("Durable native intent exceeds the owner-approved pilot ceiling")
    if instrument_fingerprint(intent.instrument) != instrument_fingerprint(book.instrument):
        raise NoSignal("Native instrument or grid changed after intent creation")
    quantity = intent.quantity if remaining_quantity is None else remaining_quantity
    if isinstance(quantity, (float, bool)) or isinstance(fees_already_paid_usd, (float, bool)):
        raise NoSignal("Native remaining exposure must use exact decimals")
    quantity, paid = D(quantity), D(fees_already_paid_usd)
    if not quantity.is_finite() or not paid.is_finite() or paid < 0 or quantity > intent.quantity:
        raise NoSignal("Invalid native remaining exposure")
    intent.instrument.validate_quantity(quantity)
    available = sum((level.quantity for level in book.asks
                     if intent.instrument.price_satisfies_limit(level.price, intent.limit_price)), D(0))
    if available < quantity:
        raise NoSignal("Insufficient native liquidity at the original limit")
    fees = book.fee_bound.maximum_fee(intent.instrument, quantity, now)
    if fees + paid > intent.max_fee_usd:
        raise NoSignal("Current native fees exceed the original reservation")
    # Keep the original durable ID and price. This is a validation result, not
    # authorization to submit a new order or retry an unknown outcome.
    # A residual after a real partial fill may be smaller than a NEW order's
    # minimum. Identity/grid/fees were validated above; this valuation copy is
    # never transmitted as a new order or used to increase the original size.
    remaining_intent = intent.model_copy(update={
        "quantity": quantity, "max_fee_usd": fees, "fee_source": book.fee_bound.source,
    })
    result = _make_candidate(market, fair, book, config, remaining_intent, bounds, now, durable_intent=intent)
    if not result.qualified:
        raise NoSignal("Native intent no longer meets configured edge and ROI")
    return result
