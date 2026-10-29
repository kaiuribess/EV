from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProviderFrame(Contract):
    """Sanitized provider transport frame for independent input consumers."""
    provider_id: str
    kind: str
    payload: dict[str, Any]
    received_at: datetime
    published_at: datetime | None = None
    snapshot_complete: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class SettlementContingencies(Contract):
    """Versioned exact rule statements; none are inferred from a generic void.

    All fields are required when this extension is supplied. These statements
    preserve reviewed contract identity, not a claim that external facts were
    verified or a model for pricing different contracts.
    """
    schema_version: Literal[1] = 1
    postponement_rule: str = Field(min_length=1, max_length=4000)
    interruption_rule: str = Field(min_length=1, max_length=4000)
    venue_change_rule: str = Field(min_length=1, max_length=4000)
    forfeit_rule: str = Field(min_length=1, max_length=4000)
    tie_rule: str = Field(min_length=1, max_length=4000)
    correction_rule: str = Field(min_length=1, max_length=4000)
    official_result_rule: str = Field(min_length=1, max_length=4000)
    payout_rule: str = Field(min_length=1, max_length=4000)

    @field_validator("postponement_rule", "interruption_rule", "venue_change_rule",
                     "forfeit_rule", "tie_rule", "correction_rule", "official_result_rule", "payout_rule")
    @classmethod
    def explicit_rule(cls, value):
        if not value.strip() or value.strip().casefold() == "unknown":
            raise ValueError("An explicit reviewed rule is required")
        return value


class Settlement(Contract):
    period: str = "full_game"
    scope: str = "event"
    overtime: str = "included"
    participation: str = "not_applicable"
    void_rule: str = "cancelled_event_void"
    push_rule: str = "not_applicable"
    payout: Decimal = Field(default=Decimal("1"), gt=0)
    currency: str = "USD"
    contract_type: str = "binary"
    contingencies: SettlementContingencies | None = None

    def identity_payload(self) -> dict:
        result = self.model_dump(mode="json")
        # Keep existing persisted identities stable. Missing detail stays
        # missing; legacy records are not silently assigned reviewed rules.
        if self.contingencies is None:
            result.pop("contingencies")
        return result


class Market(Contract):
    id: str
    event_id: str
    event_name: str
    sport: str
    league: str
    participants: tuple[str, ...]
    start_time: datetime
    status: Literal["scheduled", "live", "suspended", "closed"] = "scheduled"
    market_type: str
    player: str | None = None
    player_id: str | None = None
    statistic: str | None = None
    line: Decimal | None = None
    outcomes: tuple[str, ...]
    settlement: Settlement
    original_ids: dict[str, str] = Field(default_factory=dict)
    extensions: dict[str, Any] = Field(default_factory=dict)

    @field_validator("start_time")
    @classmethod
    def aware_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("Timestamp must include a timezone")
        return value

    @model_validator(mode="after")
    def valid_market(self):
        if len(self.outcomes) < 2 or len(set(self.outcomes)) != len(self.outcomes):
            raise ValueError("Market needs distinct opposing outcomes")
        if self.market_type == "player_prop" and not all(
            (self.player_id, self.player, self.statistic, self.line is not None)
        ):
            raise ValueError("Player props require verified player, statistic and line")
        return self

    def identity(self) -> str:
        payload = {
            "event": self.event_id, "sport": self.sport, "league": self.league,
            "type": self.market_type, "player": self.player_id,
            "statistic": self.statistic, "line": str(self.line.normalize()) if self.line is not None else None,
            "outcomes": sorted(self.outcomes), "settlement": self.settlement.identity_payload(),
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


class TimedData(Contract):
    source_time: datetime | None
    provider_time: datetime | None = None
    received_at: datetime = Field(default_factory=utcnow)
    synthetic: bool = False

    @field_validator("source_time", "provider_time", "received_at")
    @classmethod
    def aware_time(cls, value):
        if value is not None and value.tzinfo is None:
            raise ValueError("Timestamp must include a timezone")
        return value

    def age_ms(self, now: datetime | None = None) -> float | None:
        if self.source_time is None:
            return None
        return ((now or utcnow()) - self.source_time).total_seconds() * 1000

    def fresh(self, max_age_ms: float, now: datetime | None = None) -> bool:
        age = self.age_ms(now)
        return age is not None and 0 <= age <= max_age_ms


class Quote(TimedData):
    market_id: str
    # A stable ID alone cannot prove that a player, line or period was unchanged.
    market_fingerprint: str | None = None
    provider: str
    bookmaker: str
    decimal_odds: dict[str, Decimal]
    status: Literal["active", "unknown", "suspended", "withdrawn"] = "active"
    sequence: int | None = None
    original_market_id: str | None = None
    quality: float = Field(default=1.0, ge=0, le=1)
    source_observed_at: datetime | None = None
    observation_verified: bool = False
    observation_complete: bool = False
    market_status_verified: bool = False
    settlement_equivalence_verified: bool = True
    # Independent review of the ordinary WIN/LOSS condition. This does not
    # certify that exceptional sportsbook and venue payouts are equivalent.
    normal_outcome_verified: bool = False
    normal_outcome_evidence: str | None = None
    source_price_changed_at: datetime | None = None
    # Parlay WebSocket price_age_s measures time since a price MOVED; it is
    # not an observation clock or evidence that the outcome is still open.
    provider_reported_price_age_s: Decimal | None = Field(default=None, ge=0)
    # Parlay REST line_changed_at_ms is the oldest stored served row, not a
    # guaranteed upstream price move or present selection verification.
    source_stored_at: datetime | None = None
    source_selection_ids: dict[str, str] = Field(default_factory=dict)
    source_status_historical_hint: bool = False
    verification_reason: str | None = None
    # Book/fixture poll evidence is not a selection observation. It is shown
    # separately and cannot make a quote strictly eligible.
    source_scope_poll_at: datetime | None = None
    source_scope_is_current: bool | None = None

    @field_validator("source_observed_at", "source_price_changed_at", "source_stored_at",
                     "source_scope_poll_at")
    @classmethod
    def aware_observation_clock(cls, value):
        if value is not None and value.tzinfo is None:
            raise ValueError("Observation timestamps must include a timezone")
        return value

    @field_validator("decimal_odds")
    @classmethod
    def valid_odds(cls, values):
        if len(values) < 2 or any(not v.is_finite() or v <= 1 for v in values.values()):
            raise ValueError("Complete opposing decimal odds > 1 are required")
        return values


class Level(Contract):
    price: Decimal = Field(gt=0, lt=1)
    quantity: int = Field(gt=0)


class FeeSchedule(Contract):
    model: Literal["none", "fixed", "binary_quadratic", "profit_commission"]
    rate: Decimal = Field(default=Decimal("0"), ge=0, le=1)
    per_contract: Decimal = Field(default=Decimal("0"), ge=0)
    rounding: Decimal = Field(default=Decimal("0.01"), gt=0)
    verified: bool = False
    source: str = "unspecified"


class OrderBook(TimedData):
    market_id: str
    venue: str
    outcome: str
    asks: tuple[Level, ...]
    bids: tuple[Level, ...] = ()
    sequence: int
    synchronized: bool = True
    status: Literal["active", "suspended", "closed"] = "active"
    fee: FeeSchedule

    @model_validator(mode="after")
    def ordered(self):
        if list(self.asks) != sorted(self.asks, key=lambda x: x.price):
            raise ValueError("Asks must be ascending")
        if list(self.bids) != sorted(self.bids, key=lambda x: x.price, reverse=True):
            raise ValueError("Bids must be descending")
        return self


class GameState(TimedData):
    event_id: str
    status: str
    score: dict[str, int]
    clock: str | None = None
    period: str | None = None
    source_match_id: str | None = None
    identity_verified: bool = False
    identity_basis: Literal["provider_id", "exact_same_provider_fixture", "unknown"] = "unknown"


class PlayerProjection(TimedData):
    event_id: str
    player_id: str
    statistic: str
    mean: float
    model: str
    variance: float | None = None


class FairValue(Contract):
    market_id: str
    probabilities: dict[str, Decimal]
    references: tuple[dict[str, Any], ...]
    model: str
    normal_outcome_policy: Literal["strict_settlement_equivalence", "winner_only_binary"] = "strict_settlement_equivalence"
    calculated_at: datetime
    disagreement: Decimal
    synthetic: bool
    reference_profile_id: str | None = None
    reference_policy_digest: str | None = None
    reference_profile_status: str | None = None
    live_execution_allowed: bool = True
    execution_eligible: bool = True
    ineligibility_reasons: tuple[str, ...] = ()
    sensitivity_model: str | None = None
    sensitivity_probabilities: dict[str, Decimal] | None = None
    trusted_pilot_margin_probability: Decimal | None = None


class Candidate(Contract):
    id: str = Field(default_factory=lambda: str(uuid4()))
    strategy: str
    market_id: str
    venue: str
    outcome: str
    price: Decimal = Field(gt=0, lt=1)
    fair_probability: Decimal = Field(ge=0, le=1)
    ev_per_contract: Decimal
    roi: Decimal
    max_quantity: int = Field(gt=0)
    book_sequence: int
    references: tuple[dict[str, Any], ...]
    calculated_at: datetime = Field(default_factory=utcnow)
    quote_age_ms: float
    synthetic: bool
    size_curve: tuple[dict[str, Any], ...] = ()
    reference_profile_id: str | None = None
    reference_policy_digest: str | None = None
    reference_profile_status: str | None = None
    live_execution_allowed: bool = True
    execution_eligible: bool = True
    reference_inputs_eligible: bool = True
    ineligibility_reasons: tuple[str, ...] = ()
    primary_model: str | None = None
    normal_outcome_policy: Literal["strict_settlement_equivalence", "winner_only_binary"] = "strict_settlement_equivalence"
    estimate_label: str = "Estimated EV"
    sensitivity_model: str | None = None
    sensitivity_fair_probability: Decimal | None = None
    sensitivity_ev_per_contract: Decimal | None = None
    sensitivity_roi: Decimal | None = None


class OrderStatus(StrEnum):
    INTENT = "intent"
    SUBMITTING = "submitting"
    OPEN = "open"
    PARTIAL = "partial"
    FILLED = "filled"
    CANCEL_PENDING = "cancel_pending"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


class Order(Contract):
    id: str
    client_order_id: str
    candidate_id: str
    strategy: str = "positive_ev"
    market_id: str
    event_id: str
    player_id: str | None
    correlation_id: str
    venue: str
    outcome: str
    price: Decimal
    quantity: int
    filled_quantity: int = 0
    average_fill_price: Decimal = Decimal("0")
    fees: Decimal = Decimal("0")
    reserved_cost: Decimal
    status: OrderStatus = OrderStatus.INTENT
    created_at: datetime = Field(default_factory=utcnow)
    submitted_at: datetime | None = None
    acknowledged_at: datetime | None = None
    filled_at: datetime | None = None
    venue_order_id: str | None = None
    synthetic: bool
    mode: str
    reason: str = ""
    # Complete immutable canonical identity survives discovery changes and restart.
    # Nullable defaults preserve old audit records; risk treats missing metadata
    # conservatively while such orders can still hold a position.
    market_snapshot: Market | None = None
    market_fingerprint: str | None = None
    risk_family_id: str | None = None
    reviewed_correlation_groups: tuple[str, ...] = ()


class Fill(Contract):
    id: str
    order_id: str
    venue: str
    quantity: int = Field(gt=0)
    price: Decimal = Field(gt=0, lt=1)
    fee: Decimal = Field(ge=0)
    confirmed_at: datetime = Field(default_factory=utcnow)
    synthetic: bool

    @field_validator("confirmed_at")
    @classmethod
    def aware_fill_time(cls, value):
        if value.tzinfo is None:
            raise ValueError("Fill time must include a timezone")
        return value


class SettlementResult(Contract):
    market_id: str
    outcome_payouts: dict[str, Decimal]
    refund: bool = False
    settled_at: datetime = Field(default_factory=utcnow)
    synthetic: bool
    evidence: str

    @field_validator("outcome_payouts")
    @classmethod
    def valid_payouts(cls, values):
        if any(not v.is_finite() or not 0 <= v <= 1 for v in values.values()):
            raise ValueError("Binary payout fractions must be between 0 and 1")
        return values


class Envelope(Contract):
    id: str = Field(default_factory=lambda: str(uuid4()))
    schema_name: str
    schema_version: int = 1
    source: str
    payload: Any
    emitted_at: datetime = Field(default_factory=utcnow)
    correlation_id: str | None = None

