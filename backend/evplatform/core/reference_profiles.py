"""Typed vendor-independent reference priors and exact observation-age policies.

Profiles are research settings, not learned calibration or authorization to trade.
No source TTL or freshness decay is inferred from a weight specification.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .models import Market, Quote


class ProfileSelector(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    sport: str = Field(min_length=1)
    league: str = Field(min_length=1)
    market_type: str = Field(min_length=1)
    statistic: str | None = None

    @field_validator("sport", "league", "market_type", "statistic")
    @classmethod
    def exact_identifier(cls, value):
        if value is not None and (not value.strip() or value != value.strip() or "*" in value):
            raise ValueError("Profile selectors require exact nonblank canonical identities")
        return value

    @model_validator(mode="after")
    def complete_prop(self):
        if self.market_type == "player_prop" and self.statistic is None:
            raise ValueError("Player-prop profiles require an exact statistic")
        if self.market_type != "player_prop" and self.statistic is not None:
            raise ValueError("Non-prop profiles must not inherit player statistics")
        return self

    def matches(self, market: Market) -> bool:
        return (self.sport, self.league, self.market_type, self.statistic) == (
            market.sport, market.league, market.market_type, market.statistic)

    def key(self):
        return self.sport, self.league, self.market_type, self.statistic


class ReferenceProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    selectors: tuple[ProfileSelector, ...] = Field(min_length=1)
    phase: Literal["pregame"] = "pregame"
    weights_percent: dict[str, Decimal]
    required_any_anchor: tuple[str, ...] = Field(min_length=1)
    min_distinct_books: int = Field(ge=1)
    status: str = Field(min_length=1)
    live_execution_allowed: bool = False
    evidence_sources: tuple[str, ...] = ()
    note: str = ""
    primary_devig: Literal["power"] = "power"
    sensitivity_devig: Literal["proportional"] = "proportional"

    @model_validator(mode="after")
    def valid_prior(self):
        if (not self.weights_percent or any(not k or k != k.strip() for k in self.weights_percent)
                or any(not v.is_finite() or v <= 0 for v in self.weights_percent.values())
                or sum(self.weights_percent.values()) != Decimal(100)):
            raise ValueError("Profile weights must be finite, positive and sum to 100")
        if self.min_distinct_books > len(self.weights_percent):
            raise ValueError("Profile quorum exceeds its eligible bookmaker count")
        if len(set(self.required_any_anchor)) != len(self.required_any_anchor) or set(self.required_any_anchor) - set(self.weights_percent):
            raise ValueError("Anchors must be distinct eligible canonical bookmakers")
        if len({selector.key() for selector in self.selectors}) != len(self.selectors):
            raise ValueError("Profile selectors must be unique")
        return self


class ReferenceFreshnessRule(ProfileSelector):
    provider: str = Field(min_length=1)
    bookmaker: str = Field(min_length=1)
    phase: Literal["pregame"] = "pregame"
    ttl_seconds: Decimal = Field(gt=0)
    verified: bool = False
    evidence: str = Field(min_length=8)

    @field_validator("provider", "bookmaker")
    @classmethod
    def exact_source(cls, value):
        return cls.exact_identifier(value)

    @field_validator("ttl_seconds")
    @classmethod
    def finite_ttl(cls, value):
        if not value.is_finite():
            raise ValueError("Reference TTL must be finite")
        return value

    def source_key(self):
        return self.provider, self.bookmaker, *self.key(), self.phase


def profile_for_market(config, market: Market, now: datetime) -> tuple[str, ReferenceProfile] | None:
    if config.reference_profile_mode != "strict":
        return None
    if market.status != "scheduled" or now >= market.start_time:
        raise ValueError("Reference profiles are pregame only; scheduled future start required")
    matches = [(name, config.reference_profiles[name]) for name in config.enabled_reference_profiles
               if any(selector.matches(market) for selector in config.reference_profiles[name].selectors)]
    if not matches:
        raise ValueError("No enabled reference profile matches the exact sport, league, market and statistic")
    if len(matches) != 1:
        raise ValueError("Multiple reference profiles match; an explicit unambiguous policy is required")
    return matches[0]


def policy_digest(config, market: Market) -> str:
    payload = {"pricing": config.model_dump(mode="json"), "market": market.identity(),
               "scheduled_start": market.start_time.isoformat()}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def freshness_rule(config, market: Market, provider: str, bookmaker: str) -> ReferenceFreshnessRule | None:
    return next((rule for rule in config.profile_freshness_rules
                 if rule.provider == provider and rule.bookmaker == bookmaker and rule.matches(market)
                 and rule.phase == "pregame" and rule.verified), None)


def quote_eligibility(config, market: Market, quote: Quote, now: datetime) -> tuple[bool, tuple[str, ...], float | None]:
    reasons = []
    age = None if quote.source_observed_at is None else (now - quote.source_observed_at).total_seconds() * 1000
    if not quote.observation_verified or quote.source_observed_at is None:
        reasons.append("Validated outcome-level observation is missing")
    if not quote.observation_complete:
        reasons.append("Synchronized complete outcome observation is unverified")
    if not quote.market_status_verified:
        reasons.append("Current outcome-level market status is unverified")
    rule = freshness_rule(config, market, quote.provider, quote.bookmaker)
    if rule is None:
        reasons.append("Verified exact-source TTL is missing")
    if age is not None and (age < 0 or rule is not None and Decimal(str(age)) > rule.ttl_seconds * 1000):
        reasons.append("Validated observation is expired or future-dated")
    return not reasons, tuple(reasons), age
