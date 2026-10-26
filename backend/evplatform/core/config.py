from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .reference_profiles import ReferenceFreshnessRule, ReferenceProfile
from .paths import PROJECT_ROOT


class TrustedPilotMarketScope(BaseModel):
    """A rule-bound normal question, not approval of a discovered native ID."""
    model_config = ConfigDict(extra="forbid")
    venue: Literal["novig"]
    sport: Literal["football"]
    league: Literal["NFL"]
    market_type: Literal["player_prop"]
    statistics: tuple[Literal["receptions", "receiving_yards"], ...] = Field(min_length=1)
    period: Literal["FULL"] = "FULL"
    reviewed_normal_rule_id: str = Field(min_length=8)

    @model_validator(mode="after")
    def distinct_statistics(self):
        if len(set(self.statistics)) != len(self.statistics):
            raise ValueError("Pilot statistics must be distinct")
        return self

    def matches(self, market) -> bool:
        return bool(market.sport == self.sport and market.league == self.league
            and market.market_type == self.market_type and market.statistic in self.statistics
            and market.settlement.period == self.period
            and market.settlement.overtime == "included"
            and market.player and market.player_id and market.line is not None
            and market.line != market.line.to_integral_value()
            and set(market.outcomes) == {"over", "under"}
            and market.extensions.get("reviewed_normal_rule_id") == self.reviewed_normal_rule_id
            and market.extensions.get("normal_outcome_reviewed") is True
            and market.extensions.get("research_only") is not True)


class TrustedPilotPolicy(BaseModel):
    """Source approval independent of native mapping, account and kill-switch gates."""
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    owner_approved: bool = False
    exact_market_id: str | None = None
    market_scope: TrustedPilotMarketScope | None = None
    provider_id: str | None = None
    crosscheck_provider_id: str | None = None
    trusted_books: list[str] = Field(default_factory=list)
    min_distinct_books: int = Field(default=2, ge=2)
    accept_unknown_active_status: bool = False
    accept_unknown_quote_observation_age: bool = False
    max_indicator_age_ms: int | None = Field(default=None, ge=1000, le=600000)
    independent_crosscheck: Literal["two_distinct_books", "separate_provider"] | None = None
    uncertainty_margin_probability: Decimal | None = Field(default=None, gt=0, lt=1)
    max_debit_usd: Decimal | None = Field(default=None, gt=0)
    max_loss_usd: Decimal | None = Field(default=None, gt=0)
    pregame_only: Literal[True] = True
    # A recurring source approval is distinct from the original one-shot
    # validation pilot. It still needs a separately reviewed launch profile
    # and bounded, expiring runtime authorization before any order can route.
    recurring_authorization: bool = False
    maximum_native_attempts: int | None = Field(default=1, ge=1)
    automatic_followups: bool = False
    approval_evidence: str | None = None

    def cost_ceiling(self) -> Decimal | None:
        """Optional execution cap; reporting unit size never supplies one."""
        caps = [value for value in (self.max_debit_usd, self.max_loss_usd) if value is not None]
        return min(caps) if caps else None

    def permits_market(self, market) -> bool:
        return bool(not market.extensions.get("research_only") and (
            self.exact_market_id == market.id if self.exact_market_id is not None
            else self.market_scope is not None and self.market_scope.matches(market)))

    @model_validator(mode="after")
    def complete_if_enabled(self):
        if self.owner_approved and not self.enabled:
            raise ValueError("An approved pilot must be explicitly enabled")
        if self.enabled:
            required = (self.provider_id, self.max_indicator_age_ms,
                        self.independent_crosscheck, self.uncertainty_margin_probability,
                        self.approval_evidence, self.max_debit_usd, self.max_loss_usd)
            if (not self.owner_approved or any(value is None or value == "" for value in required)
                    or (self.exact_market_id is None) == (self.market_scope is None)
                    or len(set(self.trusted_books)) < self.min_distinct_books
                    or len(set(self.trusted_books)) != len(self.trusted_books)):
                raise ValueError("Trusted live pilot requires an exact, complete owner-reviewed source policy")
            if self.market_scope is not None and self.min_distinct_books < 3:
                raise ValueError("Dynamic pilot requires at least three distinct sportsbook references")
            if (not self.recurring_authorization and
                    (self.max_debit_usd > Decimal("5") or self.max_loss_usd > Decimal("5"))):
                raise ValueError("Trusted pilot debit and loss ceilings cannot exceed five dollars")
            if self.recurring_authorization:
                if self.maximum_native_attempts is not None or not self.automatic_followups:
                    raise ValueError("Recurring trusted source approval needs explicit followups and no one-shot limit")
            elif self.maximum_native_attempts != 1 or self.automatic_followups:
                raise ValueError("One-shot trusted pilot cannot authorize recurring orders")
            if self.independent_crosscheck == "separate_provider" and (
                    not self.crosscheck_provider_id or self.crosscheck_provider_id == self.provider_id):
                raise ValueError("A separate-provider cross-check needs a second named provider")
        return self


class PricingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    devig_method: Literal["proportional", "power"] = "proportional"
    # This is a pricing assumption, not a statement about actual venue grades.
    # Changing it changes the launch profile's pricing digest and needs a new
    # scoped owner authorization before production execution.
    normal_outcome_policy: Literal["strict_settlement_equivalence", "winner_only_binary"] = "strict_settlement_equivalence"
    min_references: int = Field(default=2, ge=1)
    eligible_books: list[str] | None = Field(default=None, description=(
        "Exact reviewed bookmaker identities eligible for fair value. Null preserves all mapped "
        "books; an empty list disables signals. Inclusion is not evidence of calibrated sharpness."
    ))
    required_books: list[str] = Field(default_factory=list)
    weights: dict[str, Decimal] = Field(default_factory=dict)
    max_quote_age_ms: int = Field(default=5000, ge=100, le=600000)
    max_disagreement: Decimal = Field(default=Decimal(".08"), gt=0, le=1)
    min_ev: Decimal = Field(default=Decimal(".015"), ge=0, le=1)
    min_roi: Decimal = Field(default=Decimal(".01"), ge=0)
    max_book_age_ms: int = Field(default=2500, ge=100, le=60000)
    provider_priority: dict[str, int] = Field(default_factory=dict)
    market_overrides: dict[str, dict] = Field(default_factory=dict)
    reference_profile_mode: Literal["legacy", "strict"] = "legacy"
    # Reference quality is independent of the win/loss payout model. Trusted
    # provider estimates remain research-only unless a separately approved
    # market or reviewed normal-question rule passes every other live gate.
    reference_quality_mode: Literal["strict_verified", "trusted_provider"] = "strict_verified"
    # An independent read-only strategy. It never changes the reference
    # profile used by the central execution engine.
    multi_book_consensus_enabled: bool = False
    multi_book_consensus_books: list[str] = Field(default_factory=lambda: [
        "fanduel", "draftkings", "caesars", "bet365", "betmgm", "betonline",
        "pinnacle", "circa", "bookmaker", "ballys", "betrivers", "espnbet",
        "hardrock", "pointsbet", "betparx", "sporttrade", "betway",
    ])
    trusted_max_delivery_age_ms: int = Field(default=30000, ge=1000, le=600000)
    trusted_max_indicator_age_ms: int = Field(default=30000, ge=1000, le=600000)
    # Read-only sensitivity assumption. A missing value means that net EV
    # after source uncertainty is unknown; it grants no pilot permission.
    trusted_research_margin_probability: Decimal | None = Field(default=None, ge=0, lt=1)
    # Exact-market research scenarios. These never authorize the live pilot;
    # its separately reviewed uncertainty_margin_probability remains required.
    trusted_research_market_margins: dict[str, Decimal] = Field(default_factory=dict)
    trusted_pilot_policy: TrustedPilotPolicy = Field(default_factory=TrustedPilotPolicy)
    reference_profiles: dict[str, ReferenceProfile] = Field(default_factory=dict)
    enabled_reference_profiles: list[str] = Field(default_factory=list)
    profile_freshness_rules: list[ReferenceFreshnessRule] = Field(default_factory=list)

    @model_validator(mode="after")
    def positive_weights(self):
        if any(not key.startswith("research:") or len(key) > 160 or not value.is_finite()
               or not 0 <= value < 1 for key, value in self.trusted_research_market_margins.items()):
            raise ValueError("Research allowances require exact research market IDs and finite probabilities")
        if (len(set(self.enabled_reference_profiles)) != len(self.enabled_reference_profiles)
                or set(self.enabled_reference_profiles) - set(self.reference_profiles)):
            raise ValueError("Enabled reference profiles must name distinct registered profiles")
        if (len(set(self.multi_book_consensus_books)) != len(self.multi_book_consensus_books)
                or any(not book or book != book.strip() for book in self.multi_book_consensus_books)):
            raise ValueError("Consensus books must be distinct reviewed sportsbook identities")
        selectors = [selector.key() for name in self.enabled_reference_profiles
                     for selector in self.reference_profiles[name].selectors]
        if len(selectors) != len(set(selectors)):
            raise ValueError("Enabled reference profiles must not overlap exact selectors")
        rule_keys = [rule.source_key() for rule in self.profile_freshness_rules]
        if len(rule_keys) != len(set(rule_keys)):
            raise ValueError("Reference TTL policies must have unique exact source selectors")
        for books in (self.required_books, self.eligible_books or []):
            if len(set(books)) != len(books) or any(not book or book != book.strip() for book in books):
                raise ValueError("Reference bookmaker identities must be nonblank, exact and unique")
        if any(not book or book != book.strip() for book in self.weights):
            raise ValueError("Weighted bookmaker identities must be nonblank and exact")
        if self.eligible_books is not None and set(self.required_books) - set(self.eligible_books):
            raise ValueError("Required books must be eligible references")
        if any(not v.is_finite() or v <= 0 for v in self.weights.values()):
            raise ValueError("Reference weights must be finite and positive")
        for override in self.market_overrides.values():
            if "market_overrides" in override:
                raise ValueError("Nested market overrides are unsupported")
            if set(override) & {"reference_profile_mode", "reference_profiles",
                                "enabled_reference_profiles", "profile_freshness_rules"}:
                raise ValueError("Reference profiles and TTLs require exact selectors, not broad market overrides")
            type(self).model_validate({**self.model_dump(), **override, "market_overrides": {}})
        return self


class RiskConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_order_stake: Decimal | None = Field(default=Decimal("10"), gt=0)
    max_event_exposure: Decimal | None = Field(default=Decimal("50"), gt=0)
    max_player_exposure: Decimal | None = Field(default=Decimal("25"), gt=0)
    max_venue_exposure: Decimal | None = Field(default=Decimal("100"), gt=0)
    max_correlated_exposure: Decimal | None = Field(default=Decimal("50"), gt=0)
    max_total_exposure: Decimal | None = Field(default=Decimal("200"), gt=0)
    daily_loss_limit: Decimal | None = Field(default=Decimal("50"), gt=0)
    available_cash_only: bool = False
    block_overlapping_events: bool = False
    block_overlapping_players: bool = False
    duplicate_window_seconds: int = Field(default=30, ge=1)
    max_candidate_age_ms: int = Field(default=1000, ge=10)
    live_max_order_stake: Decimal | None = Field(default=None, gt=0)
    live_stake_authorized: bool = False
    stake_sizing: Literal["capped", "quarter_kelly"] = "capped"
    unit_size_usd: Decimal = Field(default=Decimal("5"), gt=0, allow_inf_nan=False)
    max_open_orders: int = Field(default=20, ge=1)
    max_orders_per_minute: int = Field(default=30, ge=1)
    # Count durable native intents, including rejected/unknown attempts. Unlike
    # a rolling rate limit this remains exhausted across process restarts.
    max_native_order_attempts: int | None = Field(default=None, ge=1)
    # Recurring native authority must bound attempts and worst-case upfront
    # debits using durable intents, not a counter that resets on restart.
    max_native_daily_attempts: int | None = Field(default=None, ge=1)
    max_native_daily_debit: Decimal | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def require_cash_policy(self):
        if self.max_native_daily_debit is not None and not self.max_native_daily_debit.is_finite():
            raise ValueError("Daily native debit cap must be finite")
        if not self.available_cash_only and any(value is None for value in (
                self.max_order_stake, self.max_event_exposure, self.max_player_exposure,
                self.max_correlated_exposure, self.max_venue_exposure, self.max_total_exposure)):
            raise ValueError("Uncapped exposure requires authenticated available-cash enforcement")
        if self.max_order_stake is None and self.stake_sizing != "quarter_kelly":
            raise ValueError("No fixed stake cap requires quarter-Kelly sizing")
        return self


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="EV_", env_file=PROJECT_ROOT / ".env", extra="ignore")
    mode: Literal["demo", "paper", "sandbox", "production"] = "demo"
    auto_trade: bool = False
    live_enabled: bool = False
    database_path: str = "data/evplatform.db"
    data_dir: str | None = None
    host: Literal["127.0.0.1", "localhost", "::1"] = "127.0.0.1"
    port: int = 8000
    demo_interval: float = Field(default=.5, ge=.05)
    demo_seed: int = 73
    sample_feed_enabled: bool = True
    pricing: PricingConfig = Field(default_factory=PricingConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)

