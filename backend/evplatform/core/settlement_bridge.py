"""Conservative, read-only valuation of a different settlement contract.

A sportsbook's devigged probability can describe the winner of a normally
completed game without describing an exchange contract's cancellation payout.
This module keeps both contract identities and prices the exceptional states
separately. It never publishes an exact mapping or an executable candidate.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .models import Contract, Market, utcnow
from .native_accounting import ExactDecimal


Probability = Annotated[ExactDecimal, Field(ge=0, le=1)]


class ExceptionalSettlementBound(Contract):
    """A documented assumption, not proof that the source is authoritative.

    The upper bound must cover *every* event in which the normal-completion
    result is not the target contract's actual payout, including cancellation,
    shortened games, venue changes, forfeits, ties, and corrections. The floor
    must hold for every such event. An unknown probability is represented by 1;
    an unknown payout floor is represented by 0.
    """

    schema_version: Literal[1] = 1
    source_market_id: str = Field(min_length=1)
    source_identity: str = Field(pattern=r"^[0-9a-f]{64}$")
    target_market_id: str = Field(min_length=1)
    target_identity: str = Field(pattern=r"^[0-9a-f]{64}$")
    target_outcome: str = Field(min_length=1)
    exception_probability_upper_bound: Probability
    exception_payout_floor_fraction: Probability
    basis: str = Field(min_length=16)
    observed_at: datetime
    valid_until: datetime

    @model_validator(mode="after")
    def valid_interval(self):
        if self.observed_at.utcoffset() is None or self.valid_until.utcoffset() is None:
            raise ValueError("Settlement-bound timestamps require a timezone")
        if self.valid_until <= self.observed_at:
            raise ValueError("Settlement-bound validity interval must be increasing")
        return self


class BridgeEstimate(Contract):
    schema_version: Literal[1] = 1
    source_market_id: str
    target_market_id: str
    target_outcome: str
    normal_completion_probability: Probability
    conservative_payout_fraction: Probability
    exceptional_probability_upper_bound: Probability
    exceptional_payout_floor_fraction: Probability
    assumed_basis: str
    calculated_at: datetime
    execution_eligible: Literal[False] = False


def same_normal_sporting_question(source: Market, target: Market) -> bool:
    """Compare sporting scope only; do not compare or erase settlement rules."""
    return (
        source.event_id == target.event_id
        and source.sport == target.sport
        and source.league == target.league
        and set(source.participants) == set(target.participants)
        and source.start_time == target.start_time
        and source.market_type == target.market_type
        and source.player_id == target.player_id
        and source.statistic == target.statistic
        and source.line == target.line
        and set(source.outcomes) == set(target.outcomes)
        and source.settlement.period == target.settlement.period
        and source.settlement.scope == target.settlement.scope
        and source.settlement.overtime == target.settlement.overtime
        and source.settlement.participation == target.settlement.participation
        and source.settlement.currency == target.settlement.currency
        and source.settlement.contract_type == target.settlement.contract_type == "binary"
        and len(source.outcomes) == len(target.outcomes) == 2
    )


def estimate_bridge(source: Market, target: Market, outcome: str,
                    normal_probability: Decimal, bound: ExceptionalSettlementBound,
                    *, now: datetime | None = None) -> BridgeEstimate:
    """Return a lower bound on expected target payout per $1 normal payout.

    For q in [0, q_max], the payout is (1-q)*p_normal + q*p_floor at worst.
    Taking the worse endpoint preserves a lower bound even if the exceptional
    payout floor is higher than p_normal. This is an analytic estimate only.
    """
    now = now or utcnow()
    if now.utcoffset() is None:
        raise ValueError("Valuation time needs a timezone")
    if (source.identity() == target.identity() or not same_normal_sporting_question(source, target)
            or outcome not in source.outcomes
            or outcome not in target.outcomes):
        raise ValueError("Different contracts with the same declared normal sporting scope are required")
    if (bound.source_market_id != source.id or bound.source_identity != source.identity()
            or bound.target_market_id != target.id or bound.target_identity != target.identity()
            or bound.target_outcome != outcome):
        raise ValueError("Settlement bound is not tied to these exact contracts and outcome")
    if not bound.observed_at <= now < bound.valid_until:
        raise ValueError("Settlement bound is stale or future-dated")
    if isinstance(normal_probability, (float, bool)):
        raise ValueError("Normal probability requires exact decimal input")
    probability = Decimal(normal_probability)
    if not probability.is_finite() or not 0 <= probability <= 1:
        raise ValueError("Normal-completion probability must be in [0, 1]")
    q = bound.exception_probability_upper_bound
    floor = bound.exception_payout_floor_fraction
    conservative = min(probability, (1 - q) * probability + q * floor)
    return BridgeEstimate(source_market_id=source.id, target_market_id=target.id,
        target_outcome=outcome, normal_completion_probability=probability,
        conservative_payout_fraction=conservative,
        exceptional_probability_upper_bound=q, exceptional_payout_floor_fraction=floor,
        assumed_basis=bound.basis, calculated_at=now)
