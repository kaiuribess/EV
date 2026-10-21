"""Explicit cross-source contract review, separate from executable mappings.

Unknown facts never inherit the normalized settlement model's defaults. Review
records are evidence inputs, not permissions or verified mappings. The engine
continues to require its existing reviewed source/native mappings and preflight.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import Field, field_validator

from .models import Contract, utcnow


class ContractReview(Contract):
    source: str = Field(min_length=1)
    source_market_id: str = Field(min_length=1)
    event_id: str | None = None
    sport: str | None = None
    league: str | None = None
    participant_ids: tuple[str, ...] | None = None
    start_time: datetime | None = None
    market_type: str | None = None
    player_id: str | None = None
    statistic: str | None = None
    line: Decimal | None = None
    outcomes: tuple[str, ...] | None = None
    period: str | None = None
    scope: str | None = None
    overtime: str | None = None
    participation: str | None = None
    void_rule: str | None = None
    push_rule: str | None = None
    currency: str | None = None
    contract_type: str | None = None
    evidence_urls: tuple[str, ...] = ()
    observed_at: datetime = Field(default_factory=utcnow)

    @field_validator("start_time", "observed_at")
    @classmethod
    def clock(cls, value):
        if value is not None and value.tzinfo is None:
            raise ValueError("Review clocks need a timezone")
        return value

    @field_validator("line")
    @classmethod
    def finite_line(cls, value):
        if value is not None and not value.is_finite():
            raise ValueError("Nonfinite threshold")
        return value

    @field_validator("participant_ids", "outcomes")
    @classmethod
    def distinct(cls, value):
        if value is not None and (len(value) < 2 or len(set(value)) != len(value)
                                  or any(not item.strip() for item in value)):
            raise ValueError("Distinct, nonempty participants and outcomes are required")
        return value


class ReviewDifference(Contract):
    field: str
    status: Literal["unknown", "mismatch"]


def compare_contracts(left: ContractReview, right: ContractReview) -> dict:
    required = {"event_id", "sport", "league", "participant_ids", "start_time", "market_type",
                "outcomes", "period", "scope", "overtime", "participation", "void_rule",
                "push_rule", "currency", "contract_type"}
    kinds = {left.market_type, right.market_type}
    if "player_prop" in kinds:
        required.update({"player_id", "statistic", "line"})
    if kinds & {"spread", "spreads", "total", "totals"}:
        required.add("line")
    differences = []
    for field in sorted(required | {"player_id", "statistic", "line"}):
        a, b = getattr(left, field), getattr(right, field)
        unknown = lambda value: value is None or value == () or (isinstance(value, str)
                   and value.strip().casefold() in {"", "unknown"})
        if field in required and (unknown(a) or unknown(b)):
            differences.append(ReviewDifference(field=field, status="unknown"))
        elif (set(a) if field in {"participant_ids", "outcomes"} and a else a) != (
                set(b) if field in {"participant_ids", "outcomes"} and b else b):
            differences.append(ReviewDifference(field=field, status="mismatch"))
    if not left.evidence_urls or not right.evidence_urls:
        differences.append(ReviewDifference(field="evidence_urls", status="unknown"))
    return {"schema_version": 1, "source": left.source, "target": right.source,
            "structurally_equivalent": not differences,
            "differences": [item.model_dump() for item in differences],
            "execution_authorized": False,
            "note": "Structural comparison does not verify external facts, publish mappings, or authorize orders."}


def pending_market_reviews(engine, *, provider: str | None = None, offset=0, limit=100) -> dict:
    rows, providers = [], []
    for name, record in sorted(engine.registry.records.items()):
        mapper = getattr(record.plugin, "mapper", None)
        if mapper is None or not hasattr(mapper, "quarantine") or (provider and provider != name):
            continue
        providers.append({"provider": name, "pending": len(mapper.quarantine),
                          "capacity": mapper.quarantine_limit, "mapped_updates": mapper.mapped_count})
        rows.extend(mapper.quarantine.values())
    return {"schema_version": 1, "observed_at": utcnow().isoformat(), "total": len(rows),
            "offset": offset, "limit": limit, "providers": providers,
            "items": rows[offset:offset + limit], "bounded_window": True,
            "note": "Unreviewed source observations; no executable mapping or native order approval."}
