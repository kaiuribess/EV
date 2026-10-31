"""Fail-closed release gates based on scoped evidence and current runtime facts.

This module performs no network calls and never enables trading. A saved report
is not permission to trade: callers must evaluate current facts at every arm and
retain the normal risk, freshness and execution checks for every order.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_serializer, model_validator

from .models import utcnow
from .config import TrustedPilotMarketScope


Environment = Literal["sandbox", "production"]
EvidenceKind = Literal[
    "account_access", "geographic_eligibility", "native_lifecycle",
    "restart_recovery", "settlement_reconciliation", "risk_authorization",
]


class StrictRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LiveLimits(StrictRecord):
    """No default money authorization; every launch limit is explicit."""

    max_order_stake_usd: Decimal | None = Field(gt=0)
    max_event_exposure_usd: Decimal | None = Field(gt=0)
    max_player_exposure_usd: Decimal | None = Field(gt=0)
    max_venue_exposure_usd: Decimal | None = Field(gt=0)
    max_correlated_exposure_usd: Decimal | None = Field(gt=0)
    max_total_exposure_usd: Decimal | None = Field(gt=0)
    daily_loss_limit_usd: Decimal | None = Field(gt=0)
    available_cash_only: bool = False
    # This opt-in uses a fixed, approved stake and durable local exposure caps.
    # It does not assert a current balance; ProphetX may reject insufficient
    # funds. Every other venue keeps authenticated current cash enforcement.
    cash_policy: Literal["verified_current", "prophetx_exchange_rejection"] = "verified_current"
    block_overlapping_events: bool = False
    block_overlapping_players: bool = False
    stake_sizing: Literal["capped", "quarter_kelly"] = "capped"
    unit_size_usd: Decimal = Field(default=Decimal("5"), gt=0, allow_inf_nan=False)
    max_open_orders: int = Field(ge=1)
    max_orders_per_minute: int = Field(ge=1)
    # A durable intent counts even if submission fails or its outcome is unknown.
    max_native_order_attempts: int | None = Field(default=None, ge=1)
    max_native_daily_attempts: int | None = Field(default=None, ge=1)
    max_native_daily_debit_usd: Decimal | None = Field(default=None, gt=0)
    # Opt-in single-account recurring scope. The default keeps the existing
    # portfolio-wide fence; the profile digest binds any narrower choice.
    external_exposure_scope: Literal["global", "profile_account"] = "global"

    @field_validator("max_order_stake_usd", "max_event_exposure_usd", "max_player_exposure_usd",
                     "max_venue_exposure_usd", "max_correlated_exposure_usd",
                     "max_total_exposure_usd", "daily_loss_limit_usd", "max_native_daily_debit_usd")
    @classmethod
    def finite_money(cls, value: Decimal) -> Decimal:
        if value is not None and not value.is_finite():
            raise ValueError("Limits must be finite")
        return value

    @model_validator(mode="after")
    def coherent_limits(self):
        caps = (self.max_order_stake_usd, self.max_event_exposure_usd, self.max_player_exposure_usd,
                self.max_venue_exposure_usd, self.max_correlated_exposure_usd, self.max_total_exposure_usd)
        if not self.available_cash_only and any(value is None for value in caps):
            raise ValueError("Exposure without fixed caps requires available-cash enforcement")
        if self.max_order_stake_usd is None and self.stake_sizing != "quarter_kelly":
            raise ValueError("No fixed stake cap requires quarter-Kelly sizing")
        if self.max_order_stake_usd is not None and any(self.max_order_stake_usd > value for value in (
            self.max_event_exposure_usd, self.max_player_exposure_usd,
            self.max_venue_exposure_usd, self.max_correlated_exposure_usd,
            self.max_total_exposure_usd,
        ) if value is not None):
            raise ValueError("Per-order stake cannot exceed an exposure limit")
        if self.cash_policy == "prophetx_exchange_rejection" and (
                self.available_cash_only or self.stake_sizing != "capped"
                or any(value is None for value in (*caps, self.daily_loss_limit_usd,
                    self.max_native_daily_debit_usd))):
            raise ValueError("Exchange-rejection cash policy needs fixed stake and complete monetary caps")
        return self


class LaunchEvidence(StrictRecord):
    """Operator-reviewed evidence, never generated from synthetic tests.

    account_fingerprint is SHA-256 of the actual venue account identity, not a
    credential. artifact_sha256 identifies the reviewed local test/report file.
    Both evidence and its original artifact must be retained for audit. The
    evaluator validates scope, age and outcome; it cannot authenticate a claim
    typed into a JSON file. Runtime facts remain an independent mandatory gate.
    """

    kind: EvidenceKind
    venue: str = Field(min_length=1)
    environment: Environment
    account_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    profile_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    build_id: str = Field(min_length=1)
    observed_at: datetime
    expires_at: datetime
    verified_by: str = Field(min_length=1)
    source: Literal["authenticated_native", "official_account_verification", "operator_authorization"]
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    passed: bool = False
    synthetic: bool = False
    checks: tuple[str, ...] = ()

    @field_validator("observed_at", "expires_at")
    @classmethod
    def aware_clock(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Evidence timestamps must include a timezone")
        return value

    @model_validator(mode="after")
    def evidence_interval(self):
        if self.expires_at <= self.observed_at:
            raise ValueError("Evidence expiry must follow observation")
        if not self.verified_by.strip() or not self.build_id.strip():
            raise ValueError("Evidence needs a reviewer and build identity")
        return self


class ApprovedNativeFamilyRule(StrictRecord):
    """Account-bound reusable question rule; each discovered ID is rechecked."""
    schema_version: Literal[1] = 1
    scope: TrustedPilotMarketScope
    rule_version: str = Field(min_length=8)
    approved_by: str = Field(min_length=1)
    approval_evidence: str = Field(min_length=16)
    approved_at: datetime
    expires_at: datetime
    # Kalshi's primary rule is parsed against winner, teams and game date by
    # the existing crosswalk. Its secondary clause must match exactly. For
    # ProphetX these are approved exact type|sub_type|period metadata tuples.
    # Its unqualified main Moneyline may use a separately reviewed full-game
    # token. The legacy unprefixed token covers NBA only; another league needs
    # its own LEAGUE|moneyline|moneyline|default_full_game token and settlement
    # evidence. A token never supplies a missing strike or event identity.
    allowed_native_rule_texts: tuple[str, ...] = Field(min_length=1)
    max_start_delta_seconds: int = Field(default=0, ge=0, le=30)
    # Explicit native order-grid evidence is reviewed once per family. Current
    # catalog, fee, account and liquidity evidence is still required per game.
    quantity_step: Decimal = Field(gt=0)
    minimum_quantity: Decimal = Field(gt=0)
    minimum_order_cost_usd: Decimal = Field(ge=0)
    cash_quantum_usd: Decimal = Field(gt=0)
    order_minimum_source: str = Field(min_length=16)
    kalshi_exchange_index: int | None = Field(default=None, ge=0)
    # ProphetX's current account fee bound is separate from the advertised
    # rate; it remains absent until genuine evidence supports a safe bound.
    prophetx_winnings_fee_upper_per_stake: Decimal | None = Field(default=None, ge=0)
    prophetx_fee_source: str | None = None
    prophetx_fee_evidence_kind: Literal["exchange_verified", "owner_approved_assumption"] = "exchange_verified"
    prophetx_assumed_winnings_profit_rate: Decimal | None = Field(default=None, ge=0, lt=1)
    prophetx_native_settlement_evidence: str | None = None
    # A multi-competition rule cannot inherit NBA settlement evidence. Every
    # additional competition has its own reviewed native contract source.
    additional_native_settlement_evidence: dict[str, str] = Field(default_factory=dict)
    prophetx_source_timestamp_unit: Literal["unknown", "seconds", "milliseconds", "microseconds", "nanoseconds"] = "unknown"
    prophetx_timestamp_unit_evidence: str | None = None
    # A separate in-play rule needs an explicit, reviewed PBP state TTL.
    # No pregame approval inherits an in-play clock or native permission.
    inplay_state_max_age_ms: int | None = Field(default=None, ge=100, le=10000)
    inplay_event_identity_basis: Literal["provider_id", "exact_same_provider_fixture"] = "provider_id"
    inplay_identity_evidence: str | None = None

    @model_serializer(mode="wrap")
    def serialize_rule(self, handler):
        payload = handler(self)
        if not self.additional_native_settlement_evidence:
            payload.pop("additional_native_settlement_evidence", None)
        return payload

    @model_validator(mode="after")
    def coherent_rule(self):
        if (self.scope.market_type not in {"moneyline", "player_prop"}
                or self.scope.venue == "kalshi" and (
                    self.scope.league != "NBA" or self.scope.market_type != "moneyline")
                or not self.scope.source_normal_rule_evidence
                or self.approved_at.tzinfo is None or self.expires_at.tzinfo is None
                or self.expires_at <= self.approved_at
                or any(not value.is_finite() for value in (self.quantity_step,
                    self.minimum_quantity, self.minimum_order_cost_usd, self.cash_quantum_usd))
                or self.minimum_quantity % self.quantity_step
                or any(not text.strip() for text in self.allowed_native_rule_texts)
                or len(set(self.allowed_native_rule_texts)) != len(self.allowed_native_rule_texts)):
            raise ValueError("Approved family rule needs exact source, native and order-grid evidence")
        if (self.scope.phase == "inplay") != (self.inplay_state_max_age_ms is not None):
            raise ValueError("In-play family needs its own reviewed live-state age limit")
        if self.inplay_event_identity_basis == "exact_same_provider_fixture":
            if self.scope.phase != "inplay" or not self.inplay_identity_evidence or len(self.inplay_identity_evidence) < 16:
                raise ValueError("Structural PBP identity requires a reviewed in-play family rule")
        elif self.inplay_identity_evidence is not None:
            raise ValueError("Provider-ID identity does not use structural crosswalk approval")
        if self.scope.venue == "prophetx" and (
                (self.prophetx_fee_evidence_kind == "exchange_verified" and
                 (self.prophetx_winnings_fee_upper_per_stake is None or
                  not self.prophetx_winnings_fee_upper_per_stake.is_finite() or
                  self.prophetx_assumed_winnings_profit_rate is not None))
                or (self.prophetx_fee_evidence_kind == "owner_approved_assumption" and
                    (self.prophetx_winnings_fee_upper_per_stake is not None or
                     self.prophetx_assumed_winnings_profit_rate != Decimal("0.02")))
                or not self.prophetx_fee_source
                or not self.prophetx_native_settlement_evidence
                or self.prophetx_source_timestamp_unit == "unknown"
                or not self.prophetx_timestamp_unit_evidence):
            raise ValueError("ProphetX family rule needs an account-applicable winnings fee bound")
        expected = {f"{sport}:{league}" for sport, league in self.scope.additional_competitions}
        if (set(self.additional_native_settlement_evidence) != expected
                or any(not isinstance(value, str) or len(value.strip()) < 16
                       for value in self.additional_native_settlement_evidence.values())):
            raise ValueError("Each added competition needs its own reviewed native settlement evidence")
        if self.scope.venue == "kalshi" and self.kalshi_exchange_index is None:
            raise ValueError("Kalshi family rule needs an exact approved exchange index")
        return self

    def digest(self) -> str:
        payload = self.model_dump(mode="json")
        if not self.additional_native_settlement_evidence:
            payload.pop("additional_native_settlement_evidence", None)
        if self.scope.phase == "pregame":
            payload["scope"].pop("phase", None)
        if self.inplay_state_max_age_ms is None:
            payload.pop("inplay_state_max_age_ms")
        if self.inplay_event_identity_basis == "provider_id":
            payload.pop("inplay_event_identity_basis")
        if self.inplay_identity_evidence is None:
            payload.pop("inplay_identity_evidence")
        return digest_json(payload)

    def settlement_evidence_for(self, market) -> str | None:
        if (market.sport, market.league) == (self.scope.sport, self.scope.league):
            return self.prophetx_native_settlement_evidence
        return self.additional_native_settlement_evidence.get(f"{market.sport}:{market.league}")

    def matches(self, market) -> bool:
        return self.scope.matches(market)


class LaunchProfile(StrictRecord):
    schema_version: Literal[1] = 1
    venue: str = Field(min_length=1)
    environment: Environment
    account_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    build_id: str = Field(min_length=1)
    sandbox_account_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    market_ids: tuple[str, ...] = ()
    family_rule: ApprovedNativeFamilyRule | None = None
    strategy_ids: tuple[str, ...] = Field(min_length=1)
    provider_ids: tuple[str, ...] = Field(min_length=1)
    mapping_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    pricing_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    limits: LiveLimits
    min_reference_books: int = Field(ge=1)
    max_reference_age_ms: int = Field(gt=0)
    max_orderbook_age_ms: int = Field(gt=0)
    # Full settlement support stays the default requirement. The narrow Novig
    # policy requires explicit operator authorization bound to this profile.
    settlement_policy: Literal["full", "novig_ioc_win_loss_halt_on_exception",
                               "kalshi_ioc_binary_halt_on_exception",
                               "prophetx_funded_cash_win_loss_halt_on_exception"] = "full"
    # A first, capped production order cannot already have genuine lifecycle
    # and settlement evidence. Defer only those historical checks for this
    # narrow validation pilot; current account, market, quote and risk checks
    # remain mandatory at every stage of the order.
    validation_pilot: bool = False
    evidence: tuple[LaunchEvidence, ...] = ()

    @model_validator(mode="after")
    def exact_or_family_scope(self):
        if (bool(self.market_ids) == (self.family_rule is not None)
                or self.family_rule is not None and self.family_rule.scope.venue != self.venue):
            raise ValueError("Launch profile needs either exact markets or one matching approved family rule")
        if self.limits.cash_policy == "prophetx_exchange_rejection" and self.venue != "prophetx":
            raise ValueError("Exchange-rejection cash policy requires a ProphetX profile")
        if self.validation_pilot and self.family_rule is not None and (
                self.venue != "prophetx" or self.family_rule.scope.sport != "basketball"
                or self.family_rule.scope.league != "NBA"
                or self.family_rule.scope.market_type != "moneyline"
                or self.family_rule.scope.period != "FULL"
                or self.family_rule.scope.additional_competitions
                or self.min_reference_books < 3):
            raise ValueError("Conditional validation pilot requires a reviewed ProphetX NBA full-game moneyline family")
        return self

    @model_validator(mode="after")
    def bounded_validation_pilot(self):
        if not self.validation_pilot:
            return self
        limits = self.limits
        caps = (limits.max_order_stake_usd, limits.max_event_exposure_usd,
                limits.max_player_exposure_usd, limits.max_venue_exposure_usd,
                limits.max_correlated_exposure_usd, limits.max_total_exposure_usd)
        pilot_settlement = {
            "kalshi": "kalshi_ioc_binary_halt_on_exception",
            "novig": "novig_ioc_win_loss_halt_on_exception",
            "prophetx": "prophetx_funded_cash_win_loss_halt_on_exception",
        }
        fixed_prophetx = (self.venue == "prophetx" and self.family_rule is not None
            and limits.cash_policy == "prophetx_exchange_rejection"
            and not limits.available_cash_only and limits.stake_sizing == "capped")
        verified_cash = (limits.cash_policy == "verified_current"
            and limits.available_cash_only and limits.stake_sizing == "quarter_kelly")
        if (self.environment != "production"
                or self.settlement_policy != pilot_settlement.get(self.venue)
                or any(cap is None or cap > Decimal("5") for cap in caps)
                or (self.family_rule is not None and (
                    limits.daily_loss_limit_usd is None or limits.daily_loss_limit_usd > Decimal("5")
                    or limits.max_native_daily_debit_usd is None
                    or limits.max_native_daily_debit_usd > Decimal("5")))
                or limits.max_native_order_attempts != 1
                or limits.max_open_orders != 1 or limits.max_orders_per_minute != 1
                or not (fixed_prophetx or verified_cash) or not limits.block_overlapping_events
                or not limits.block_overlapping_players
                or limits.unit_size_usd != Decimal("5")
                or set(self.strategy_ids) != {"native_positive_ev"}
                or self.min_reference_books < 2):
            raise ValueError("Validation pilot requires one at-most-$5 supported-venue attempt with cash and overlap limits")
        return self

    @field_validator("market_ids", "strategy_ids", "provider_ids")
    @classmethod
    def distinct_nonempty_scope(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(values)) != len(values) or any(not value.strip() for value in values):
            raise ValueError("Launch scopes must be distinct and nonempty")
        return values

    def digest(self) -> str:
        """Scope/limits/configuration changes invalidate previously issued evidence."""
        payload = self.model_dump(mode="json", exclude={"evidence"})
        # Preserve the digest of existing version-1 profiles. Adding an opt-in
        # pilot must not invalidate unrelated reviewed accounts/evidence.
        if not self.validation_pilot:
            payload.pop("validation_pilot")
        if self.family_rule is None:
            payload.pop("family_rule")
        else:
            # The in-play fields are opt-in. Keep an existing pregame family
            # authorization bound to the same rules when these defaults are
            # introduced by a newer build, while hashing every approved
            # in-play value when it is actually configured.
            family = payload["family_rule"]
            if self.family_rule.scope.phase == "pregame":
                family["scope"].pop("phase", None)
            if self.family_rule.inplay_state_max_age_ms is None:
                family.pop("inplay_state_max_age_ms", None)
            if self.family_rule.inplay_event_identity_basis == "provider_id":
                family.pop("inplay_event_identity_basis", None)
            if self.family_rule.inplay_identity_evidence is None:
                family.pop("inplay_identity_evidence", None)
            if not self.family_rule.additional_native_settlement_evidence:
                family.pop("additional_native_settlement_evidence", None)
        if self.limits.max_native_order_attempts is None:
            payload["limits"].pop("max_native_order_attempts")
        if self.limits.max_native_daily_attempts is None:
            payload["limits"].pop("max_native_daily_attempts")
        if self.limits.max_native_daily_debit_usd is None:
            payload["limits"].pop("max_native_daily_debit_usd")
        if self.limits.cash_policy == "verified_current":
            payload["limits"].pop("cash_policy")
        if self.limits.external_exposure_scope == "global":
            payload["limits"].pop("external_exposure_scope")
        return digest_json(payload)


def digest_json(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode("utf-8")).hexdigest()


class FeedReadiness(StrictRecord):
    provider_id: str
    enabled: bool = False
    authenticated: bool = False
    connected: bool = False
    entitlement_confirmed: bool = False
    quota_available: bool = False
    circuit_open: bool = True
    # Polling can be supported. Its quote freshness is checked per market below.
    transport: Literal["rest", "websocket", "sse", "unknown"] = "unknown"
    synchronization_ready: bool = False


class MarketReadiness(StrictRecord):
    market_id: str
    identity_verified: bool = False
    provider_mapping_verified: bool = False
    venue_mapping_verified: bool = False
    fee_schedule_verified: bool = False
    fee_assumption_approved: bool = False
    native_units_supported: bool = False
    order_minimum_verified: bool = False
    usable_reference_books: int = Field(default=0, ge=0)
    required_books_present: bool = False
    oldest_reference_age_ms: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    # Provider delivery is a bounded input indicator, never a measured
    # selection-observation age. The source policy is separately authorized.
    reference_age_basis: Literal["selection_observation", "provider_delivery"] = "selection_observation"
    orderbook_age_ms: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    orderbook_synchronized: bool = False
    market_active: bool = False
    executable_liquidity: bool = False
    synthetic: bool = True


def family_market_ready(market: MarketReadiness, profile: LaunchProfile, *,
                        runtime_age_seconds: float = 0) -> bool:
    """One current exact fixture may arm a reusable scope; others cannot veto it."""
    required = ("identity_verified", "provider_mapping_verified", "venue_mapping_verified",
        "native_units_supported", "order_minimum_verified",
        "required_books_present", "orderbook_synchronized", "market_active",
        "executable_liquidity")
    return bool((market.fee_schedule_verified or market.fee_assumption_approved)
        and all(getattr(market, name) for name in required) and not market.synthetic
        and market.usable_reference_books >= profile.min_reference_books
        and market.oldest_reference_age_ms is not None
        and market.orderbook_age_ms is not None
        and 0 <= market.oldest_reference_age_ms + max(0, runtime_age_seconds)*1000 <= profile.max_reference_age_ms
        and 0 <= market.orderbook_age_ms + max(0, runtime_age_seconds)*1000 <= profile.max_orderbook_age_ms)


class RuntimeReadiness(StrictRecord):
    """Facts gathered by the running engine, never loaded as user configuration."""

    environment: Literal["demo", "paper", "sandbox", "production"] = "demo"
    venue: str = ""
    account_fingerprint: str = ""
    build_id: str = ""
    mapping_digest: str = ""
    pricing_digest: str = ""
    checked_at: datetime = Field(default_factory=utcnow)
    configured_limits: LiveLimits | None = None
    enabled_strategy_ids: tuple[str, ...] = ()
    central_risk_enforced: bool = False
    external_exposure_bounded: bool = False
    opposing_position_guards: bool = False
    durable_intents: bool = False
    persistence_healthy: bool = False
    signal_ledger_healthy: bool = False
    native_routing_supported: bool = False
    private_stream_authenticated: bool = False
    private_stream_synchronized: bool = False
    reconciliation_healthy: bool = False
    startup_recovery_complete: bool = False
    cancellation_races_supported: bool = False
    settlement_reconciliation_supported: bool = False
    native_credentials_present: bool = False
    account_identity_verified: bool = False
    geographic_attestation_current: bool = False
    # ProphetX can verify the actual owner's location for each exact order at
    # dispatch. This is capability evidence, never a claim of current location.
    order_bound_location_check_supported: bool = False
    unresolved_unknown_orders: int = Field(default=0, ge=0)
    reconciliation_discrepancies: int = Field(default=0, ge=0)
    native_pilot_prior_attempts: int = Field(default=0, ge=0)
    feeds: tuple[FeedReadiness, ...] = ()
    markets: tuple[MarketReadiness, ...] = ()
    live_enabled: bool = False
    auto_trade: bool = False
    kill_switch: bool = True

    @field_validator("checked_at")
    @classmethod
    def aware_clock(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Runtime check must include a timezone")
        return value


class PerformanceEvidence(StrictRecord):
    """Descriptive counts only. Positive backtest P&L is not a release approval."""

    real_signal_episodes: int = Field(default=0, ge=0)
    real_graded_episodes: int = Field(default=0, ge=0)
    distinct_real_events: int = Field(default=0, ge=0)
    reconciled_real_fills: int = Field(default=0, ge=0)
    verified_real_settlements: int = Field(default=0, ge=0)
    conclusion: Literal["not_established"] = "not_established"
    note: str = "Technical readiness does not establish a profitable strategy or guarantee future returns."


class ReadinessBlocker(StrictRecord):
    code: str
    category: Literal["technical", "authorization", "arming"]
    scope: str
    message: str
    action: str


class ReadinessReport(StrictRecord):
    schema_version: Literal[1] = 1
    evaluated_at: datetime
    environment: str
    venue: str
    profile_digest: str | None = None
    technical_ready: bool
    authorization_ready: bool
    safe_to_arm: bool
    live_trading_ready: bool
    sandbox_trading_ready: bool
    blockers: tuple[ReadinessBlocker, ...]
    performance: PerformanceEvidence

    @property
    def blocking_items(self) -> list[str]:
        """Compatibility display for the initial dashboard API."""
        return [item.message for item in self.blockers]


_RUNTIME_FLAGS = {
    "central_risk_enforced": "Connect every native submission to central risk approval",
    "external_exposure_bounded": "Reconcile or conservatively bound external account exposure before native submission",
    "opposing_position_guards": "Enable global opposing and correlated position checks",
    "durable_intents": "Persist and acknowledge each order intent before transmission",
    "persistence_healthy": "Restore the audit writer and durable order store",
    "signal_ledger_healthy": "Restore independent durable signal tracking",
    "native_routing_supported": "Complete native routing and exact venue quantity/fee accounting",
    "private_stream_authenticated": "Authenticate the private order and fill stream",
    "private_stream_synchronized": "Reconcile the private stream against an authoritative snapshot",
    "reconciliation_healthy": "Reconcile native orders, fills, balances and positions",
    "startup_recovery_complete": "Complete restart recovery before accepting new orders",
    "cancellation_races_supported": "Account for fills racing cancellation or replacement",
    "settlement_reconciliation_supported": "Connect verified native settlement and payout reconciliation",
    "native_credentials_present": "Configure the required backend venue credentials",
    "account_identity_verified": "Read and bind the authenticated venue account identity",
}
# These facts are assessed for an explicitly selected venue/account. Without
# that scope their default False values mean unassessed, not missing credentials
# or an unimplemented adapter. Global risk/storage failures still show normally.
_SCOPED_RUNTIME_FLAGS = frozenset({
    "native_routing_supported", "private_stream_authenticated", "private_stream_synchronized",
    "reconciliation_healthy", "cancellation_races_supported", "settlement_reconciliation_supported",
    "native_credentials_present", "account_identity_verified",
})
_LIFECYCLE_CHECKS = {
    "submit_ack", "private_fill", "partial_fill", "cancel_race", "unknown_outcome",
    "duplicate_prevention", "fees_and_units", "sequence_gap_recovery",
}
_RECOVERY_CHECKS = {"durable_intent_restart", "open_order_reconciliation", "position_reconciliation"}
_SETTLEMENT_CHECKS = {"native_result", "payout_and_fees", "position_release", "idempotency"}


def evaluate_readiness(profile: LaunchProfile | None, runtime: RuntimeReadiness, *,
                       now: datetime | None = None,
                       performance: PerformanceEvidence | None = None) -> ReadinessReport:
    now = now or utcnow()
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Evaluation time must include a timezone")
    blockers: list[ReadinessBlocker] = []

    def block(code, message, action, *, category="technical", scope="runtime"):
        blockers.append(ReadinessBlocker(code=code, category=category, scope=scope,
                                         message=message, action=action))

    age = (now - runtime.checked_at).total_seconds()
    if age < 0 or age > 5:
        block("runtime.stale", "Current runtime facts are unavailable or stale",
              "Collect new runtime facts immediately before preflight")
    for flag, action in _RUNTIME_FLAGS.items():
        if profile is None and flag in _SCOPED_RUNTIME_FLAGS:
            continue
        if not getattr(runtime, flag):
            block(f"runtime.{flag}", action, action)
    if runtime.unresolved_unknown_orders or runtime.reconciliation_discrepancies:
        block("runtime.unresolved_orders", "Unknown orders or reconciliation discrepancies remain",
              "Resolve native order outcomes and positions without blindly resubmitting")
    if profile is None:
        block("runtime.scope_unassessed", "Venue and account readiness have not been assessed for a launch scope",
              "Select a reviewed venue, account and market scope; monitor authentication alone is insufficient")
        block("profile.missing", "No explicit venue launch profile is configured",
              "Create a reviewed, account-specific launch profile with explicit limits",
              category="authorization")
    else:
        pilot = profile.validation_pilot
        if pilot and runtime.native_pilot_prior_attempts:
            block("pilot.consumed", "The one-order validation pilot has already been attempted",
                  "Reconcile and review its outcome before any further live order",
                  category="authorization", scope=profile.venue)
        for name in ("environment", "venue", "account_fingerprint", "build_id", "mapping_digest", "pricing_digest"):
            if getattr(profile, name) != getattr(runtime, name):
                block(f"profile.{name}_mismatch", f"Launch profile {name.replace('_', ' ')} differs from runtime",
                      "Revalidate the profile against the actual account, build and configuration",
                      category="authorization", scope=profile.venue)
        if profile.limits != runtime.configured_limits:
            block("profile.limit_mismatch", "Current limits differ from explicitly authorized limits",
                  "Apply the reviewed limits and repeat authorization for any change", category="authorization")
        if profile.family_rule is not None:
            rule = profile.family_rule
            if not rule.approved_at <= now < rule.expires_at:
                block("family.expired", "Approved market-family rule is expired or future-dated",
                      "Review and reapprove the exact rule version", category="authorization")
        if set(profile.strategy_ids) != set(runtime.enabled_strategy_ids):
            block("profile.strategy_mismatch", "Enabled strategies differ from authorized strategies",
                  "Limit native routing to the exact approved strategy set", category="authorization")
        if (profile.environment == "production" and profile.venue == "novig"
                and not runtime.geographic_attestation_current):
            block("account.geographic_attestation_missing",
                  "Novig has no current account-bound location attestation in its runtime snapshot",
                  "Obtain a venue-supported current location result bound to this trading account before arming",
                  category="authorization", scope=profile.venue)

        feeds = {item.provider_id: item for item in runtime.feeds}
        if len(feeds) != len(runtime.feeds):
            block("feed.duplicate", "Runtime contains ambiguous duplicate provider records", "Rebuild provider health facts")
        for provider_id in profile.provider_ids:
            feed = feeds.get(provider_id)
            if feed is None:
                block("feed.missing", "Required provider is absent", "Enable and validate the required provider", scope=provider_id)
                continue
            for flag in ("enabled", "authenticated", "connected", "entitlement_confirmed", "quota_available", "synchronization_ready"):
                if not getattr(feed, flag):
                    block(f"feed.{flag}", f"Required provider has no confirmed {flag.replace('_', ' ')} state",
                          "Restore provider access, quota and synchronized data", scope=provider_id)
            if feed.circuit_open or feed.transport == "unknown":
                block("feed.unusable", "Provider circuit is open or transport is unsupported",
                      "Restore the documented provider transport", scope=provider_id)

        markets = {item.market_id: item for item in runtime.markets}
        if len(markets) != len(runtime.markets):
            block("market.duplicate", "Runtime contains ambiguous duplicate market records", "Resolve duplicate canonical identities")
        if profile.family_rule is not None and not any(
                family_market_ready(item, profile, runtime_age_seconds=age)
                for item in runtime.markets):
            block("family.no_current_market", "No discovered market satisfies every reviewed family and live-data check",
                  "Keep monitoring for a current exact match with references, fees and executable depth")
        for market_id in profile.market_ids:
            market = markets.get(market_id)
            if market is None:
                block("market.missing", "Approved market has no current readiness facts",
                      "Review exact source and venue mappings and acquire current quotes", scope=market_id)
                continue
            for flag in ("identity_verified", "provider_mapping_verified", "venue_mapping_verified", "fee_schedule_verified",
                         "native_units_supported", "required_books_present", "orderbook_synchronized", "market_active", "executable_liquidity"):
                if not getattr(market, flag):
                    block(f"market.{flag}", f"Market check failed: {flag.replace('_', ' ')}",
                          "Review this exact market and restore valid native/reference data", scope=market_id)
            if market.synthetic:
                block("market.synthetic", "Synthetic data cannot authorize native orders", "Connect actual reference and native data", scope=market_id)
            if profile.limits.stake_sizing == "quarter_kelly" and not market.order_minimum_verified:
                block("market.order_minimum_verified", "Quarter Kelly requires the actual market minimum stake and quantity",
                      "Verify the native cash minimum, quantity increment and minimum lot for this account and instrument", scope=market_id)
            if market.usable_reference_books < profile.min_reference_books:
                block("market.references", "Too few independent usable reference bookmakers",
                      "Restore the configured independent books; duplicate vendors do not count twice", scope=market_id)
            for name, maximum in (("oldest_reference_age_ms", profile.max_reference_age_ms),
                                  ("orderbook_age_ms", profile.max_orderbook_age_ms)):
                age_ms = getattr(market, name)
                if age_ms is None or age_ms + max(0, age) * 1000 > maximum:
                    block(f"market.{name}", "Source quote or executable book age is unknown or stale",
                          "Wait for current source timestamps and a synchronized executable book", scope=market_id)

        digest = profile.digest()
        for kind in ("account_access", "native_lifecycle", "restart_recovery", "settlement_reconciliation", "risk_authorization",
                     *(("geographic_eligibility",) if profile.environment == "production" else ())):
            if (kind == "geographic_eligibility" and profile.venue == "prophetx"
                    and runtime.order_bound_location_check_supported):
                # A fresh, account/order-bound location result is required by
                # the ProphetX preflight immediately before every submission.
                continue
            if (pilot and runtime.native_pilot_prior_attempts == 0
                    and kind in {"native_lifecycle", "restart_recovery", "settlement_reconciliation"}
                    and not any(e.kind == kind for e in profile.evidence)):
                continue
            candidates = [e for e in profile.evidence if e.kind == kind]
            # A later failed/revoked check must not be hidden by an earlier pass.
            if candidates:
                latest = max(e.observed_at for e in candidates)
                candidates = [e for e in candidates if e.observed_at == latest]
            # A sandbox certificate is tied to the *actual* sandbox account,
            # explicitly named in this production profile. Production access and
            # geography cannot be inherited from that sandbox account.
            account_scopes = {(profile.environment, profile.account_fingerprint)}
            if (kind in {"native_lifecycle", "restart_recovery", "settlement_reconciliation"}
                    and profile.environment == "production" and profile.sandbox_account_fingerprint):
                account_scopes.add(("sandbox", profile.sandbox_account_fingerprint))
            expected_source = {"risk_authorization": "operator_authorization",
                               "geographic_eligibility": "official_account_verification"}.get(kind, "authenticated_native")
            required_checks = {"native_lifecycle": _LIFECYCLE_CHECKS, "restart_recovery": _RECOVERY_CHECKS,
                               "settlement_reconciliation": _SETTLEMENT_CHECKS}.get(kind, set())
            # Limit retained evidence age regardless of a generously typed expiry.
            max_age = timedelta(hours=24) if kind in {"account_access", "geographic_eligibility"} else timedelta(days=7)
            valid = [e for e in candidates if e.passed and not e.synthetic and e.venue == profile.venue
                     and (e.environment, e.account_fingerprint) in account_scopes and e.profile_digest == digest
                     and e.build_id == profile.build_id
                     and e.source == expected_source and e.observed_at <= now < e.expires_at
                     and now - e.observed_at <= max_age and required_checks <= set(e.checks)]
            if not valid or len(valid) != len(candidates):
                block(f"evidence.{kind}", f"Current, scoped {kind.replace('_', ' ')} evidence is missing",
                      "Complete the genuine native check and retain its reviewed, expiring evidence for this profile",
                      category="authorization" if kind in {"account_access", "geographic_eligibility", "risk_authorization"} else "technical",
                      scope=profile.venue)

    if not runtime.live_enabled:
        block("arming.disabled", "Native execution is disabled", "Explicitly enable native execution after preflight passes", category="arming")
    if not runtime.auto_trade:
        block("arming.paused", "Automatic trading is paused", "Explicitly arm the approved profile after preflight passes", category="arming")
    if runtime.kill_switch:
        block("arming.kill_switch", "Emergency stop is active", "Resolve its cause and explicitly clear the emergency stop", category="arming")
    technical = not any(b.category == "technical" for b in blockers)
    authorized = not any(b.category == "authorization" for b in blockers)
    safe_to_arm = technical and authorized
    ready = safe_to_arm and not any(b.category == "arming" for b in blockers)
    return ReadinessReport(
        evaluated_at=now, environment=runtime.environment, venue=runtime.venue,
        profile_digest=profile.digest() if profile else None, technical_ready=technical,
        authorization_ready=authorized, safe_to_arm=safe_to_arm,
        live_trading_ready=ready and runtime.environment == "production",
        sandbox_trading_ready=ready and runtime.environment == "sandbox",
        blockers=tuple(blockers), performance=performance or PerformanceEvidence(),
    )
