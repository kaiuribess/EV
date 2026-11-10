"""A non-executable, source/native identity review for one pilot event."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator, model_validator

from .models import Contract


class StructuralPilotReview(Contract):
    schema_version: Literal[1] = 1
    canonical_event_id: str = Field(min_length=1)
    canonical_market_id: str = Field(min_length=1)
    source_signatures: dict[str, str] = Field(min_length=1)
    native_venue: Literal["kalshi"] = "kalshi"
    native_event_id: str = Field(min_length=1)
    native_outcomes: dict[str, str] = Field(min_length=2)
    native_rule_sha256: dict[str, str] = Field(min_length=2)
    native_listing_urls: dict[str, str] = Field(min_length=2)
    review_provenance: str = Field(min_length=16)
    reviewed_at: datetime
    normal_completion_agreement: str = Field(min_length=16)
    exceptional_settlement_difference: str = Field(min_length=16)
    source_exceptional_rules_verified: Literal[False] = False
    settlement_equivalence_verified: Literal[False] = False
    pricing_model_approved: Literal[False] = False
    execution_authorized: Literal[False] = False

    @field_validator("reviewed_at")
    @classmethod
    def aware_review_clock(cls, value):
        if value.utcoffset() is None:
            raise ValueError("Structural review needs an aware clock")
        return value

    @model_validator(mode="after")
    def exact_native_evidence(self):
        tickers = set(self.native_outcomes.values())
        if (len(tickers) != 2 or set(self.native_rule_sha256) != tickers
                or set(self.native_listing_urls) != tickers
                or any(len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value)
                       for value in (*self.source_signatures.values(), *self.native_rule_sha256.values()))):
            raise ValueError("Structural review requires exact two-outcome native evidence")
        return self
