from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class PricingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    devig_method: Literal["proportional", "power"] = "proportional"
    min_references: int = Field(default=2, ge=1)
    required_books: list[str] = Field(default_factory=list)
    weights: dict[str, Decimal] = Field(default_factory=dict)
    max_quote_age_ms: int = Field(default=5000, ge=100, le=300000)
    max_disagreement: Decimal = Field(default=Decimal(".08"), gt=0, le=1)
    min_ev: Decimal = Field(default=Decimal(".015"), ge=0, le=1)
    min_roi: Decimal = Field(default=Decimal(".01"), ge=0)
    max_book_age_ms: int = Field(default=2500, ge=100, le=60000)
    provider_priority: dict[str, int] = Field(default_factory=dict)
    market_overrides: dict[str, dict] = Field(default_factory=dict)

    @model_validator(mode="after")
    def positive_weights(self):
        if any(not v.is_finite() or v <= 0 for v in self.weights.values()):
            raise ValueError("Reference weights must be finite and positive")
        for override in self.market_overrides.values():
            if "market_overrides" in override:
                raise ValueError("Nested market overrides are unsupported")
            type(self).model_validate({**self.model_dump(), **override, "market_overrides": {}})
        return self


class RiskConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_order_stake: Decimal = Field(default=Decimal("10"), gt=0)
    max_event_exposure: Decimal = Field(default=Decimal("50"), gt=0)
    max_player_exposure: Decimal = Field(default=Decimal("25"), gt=0)
    max_venue_exposure: Decimal | None = Field(default=Decimal("100"), gt=0)
    max_correlated_exposure: Decimal = Field(default=Decimal("50"), gt=0)
    max_total_exposure: Decimal | None = Field(default=Decimal("200"), gt=0)
    daily_loss_limit: Decimal | None = Field(default=Decimal("50"), gt=0)
    available_cash_only: bool = False
    block_overlapping_events: bool = False
    block_overlapping_players: bool = False
    duplicate_window_seconds: int = Field(default=30, ge=1)
    max_candidate_age_ms: int = Field(default=1000, ge=10)
    live_max_order_stake: Decimal | None = Field(default=None, gt=0)
    max_open_orders: int = Field(default=20, ge=1)
    max_orders_per_minute: int = Field(default=30, ge=1)

    @model_validator(mode="after")
    def require_cash_policy(self):
        if not self.available_cash_only and (self.max_venue_exposure is None or self.max_total_exposure is None):
            raise ValueError("Uncapped exposure requires authenticated available-cash enforcement")
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

