from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .reference_profiles import ReferenceFreshnessRule, ReferenceProfile


class PricingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    devig_method: Literal["proportional", "power"] = "proportional"
    min_references: int = Field(default=2, ge=1)
    eligible_books: list[str] | None = Field(default=None, description=(
        "Exact reviewed bookmaker identities eligible for fair value. Null preserves all mapped "
        "books; an empty list disables signals. Inclusion is not evidence of calibrated sharpness."
    ))
    required_books: list[str] = Field(default_factory=list)
    weights: dict[str, Decimal] = Field(default_factory=dict)
    max_quote_age_ms: int = Field(default=5000, ge=100, le=300000)
    max_disagreement: Decimal = Field(default=Decimal(".08"), gt=0, le=1)
    min_ev: Decimal = Field(default=Decimal(".015"), ge=0, le=1)
    min_roi: Decimal = Field(default=Decimal(".01"), ge=0)
    max_book_age_ms: int = Field(default=2500, ge=100, le=60000)
    provider_priority: dict[str, int] = Field(default_factory=dict)
    market_overrides: dict[str, dict] = Field(default_factory=dict)
    reference_profile_mode: Literal["legacy", "strict"] = "legacy"
    reference_profiles: dict[str, ReferenceProfile] = Field(default_factory=dict)
    enabled_reference_profiles: list[str] = Field(default_factory=list)
    profile_freshness_rules: list[ReferenceFreshnessRule] = Field(default_factory=list)

    @model_validator(mode="after")
    def positive_weights(self):
        if (len(set(self.enabled_reference_profiles)) != len(self.enabled_reference_profiles)
                or set(self.enabled_reference_profiles) - set(self.reference_profiles)):
            raise ValueError("Enabled reference profiles must name distinct registered profiles")
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

    @model_validator(mode="after")
    def require_cash_policy(self):
        if not self.available_cash_only and any(value is None for value in (
                self.max_order_stake, self.max_event_exposure, self.max_player_exposure,
                self.max_correlated_exposure, self.max_venue_exposure, self.max_total_exposure)):
            raise ValueError("Uncapped exposure requires authenticated available-cash enforcement")
        if self.max_order_stake is None and self.stake_sizing != "quarter_kelly":
            raise ValueError("No fixed stake cap requires quarter-Kelly sizing")
        return self


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="EV_", env_file=".env", extra="ignore")
    mode: Literal["demo", "paper", "sandbox", "production"] = "demo"
    auto_trade: bool = False
    live_enabled: bool = False
    database_path: str = "data/evplatform.db"
    host: Literal["127.0.0.1", "localhost", "::1"] = "127.0.0.1"
    port: int = 8000
    demo_interval: float = Field(default=.5, ge=.05)
    demo_seed: int = 73
    pricing: PricingConfig = Field(default_factory=PricingConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)

