"""Explicit cross-source contract review, separate from executable mappings.

Unknown facts never inherit the normalized settlement model's defaults. Review
records are evidence inputs, not permissions or verified mappings. The engine
continues to require its existing reviewed source/native mappings and preflight.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, field_validator, model_validator

from .models import Contract, utcnow


RULE_FIELDS = frozenset({"postponement_rule", "interruption_rule", "venue_change_rule",
                        "forfeit_rule", "tie_rule", "correction_rule", "official_result_rule", "payout_rule"})
FACT_FIELDS = frozenset({"event_id", "sport", "league", "participant_ids", "start_time", "market_type",
                        "player_id", "statistic", "line", "outcomes", "period", "scope", "overtime",
                        "participation", "void_rule", "push_rule", "currency", "contract_type"}) | RULE_FIELDS


class FieldEvidence(Contract):
    """A reviewer's field-specific claim, never automatic verification of a URL.

    The content hash identifies a captured document; a matching URL alone cannot
    prove which version, jurisdiction, listing, or fact was actually reviewed.
    """
    source: str = Field(min_length=1, max_length=200)
    source_market_id: str = Field(min_length=1, max_length=512)
    official_url: str = Field(max_length=4096)
    document_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    jurisdiction: str = Field(min_length=1, max_length=200)
    scope: str = Field(min_length=1, max_length=1000)
    binding_evidence: str = Field(min_length=16, max_length=4000)
    retrieved_at: datetime
    effective_from: datetime | None = None
    effective_until: datetime | None = None
    document_version: str | None = Field(default=None, max_length=200)
    facts: dict[str, str | list[str]] = Field(min_length=1, max_length=len(FACT_FIELDS))

    @field_validator("official_url")
    @classmethod
    def public_https(cls, value):
        parsed = urlparse(value)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.hostname in {"localhost", "127.0.0.1", "::1"}):
            raise ValueError("Evidence must name a public HTTPS document without credentials")
        return value

    @field_validator("source", "source_market_id", "jurisdiction", "scope", "binding_evidence")
    @classmethod
    def substantive(cls, value):
        if not value.strip() or value.strip().casefold() == "unknown":
            raise ValueError("Evidence needs an explicit source and applicability scope")
        return value

    @field_validator("retrieved_at", "effective_from", "effective_until")
    @classmethod
    def aware(cls, value):
        if value is not None and value.utcoffset() is None:
            raise ValueError("Evidence clocks need a timezone")
        return value

    @field_validator("facts")
    @classmethod
    def declared_facts(cls, value):
        if set(value) - FACT_FIELDS:
            raise ValueError("Evidence contains an unsupported contract field")
        return value

    @model_validator(mode="after")
    def validity_interval(self):
        if self.effective_from and self.effective_until and self.effective_until <= self.effective_from:
            raise ValueError("Evidence validity interval must be increasing")
        return self


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
    # These contingencies cannot safely be compressed into a generic 'void'.
    postponement_rule: str | None = None
    interruption_rule: str | None = None
    venue_change_rule: str | None = None
    forfeit_rule: str | None = None
    tie_rule: str | None = None
    correction_rule: str | None = None
    official_result_rule: str | None = None
    payout_rule: str | None = None
    evidence_urls: tuple[str, ...] = ()
    field_evidence: tuple[FieldEvidence, ...] = Field(default=(), max_length=50)
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
                "push_rule", "currency", "contract_type"} | RULE_FIELDS
    kinds = {left.market_type, right.market_type}
    if "player_prop" in kinds:
        required.update({"player_id", "statistic", "line"})
    if kinds & {"spread", "spreads", "total", "totals"}:
        required.add("line")
    sporting_fields = {"event_id", "sport", "league", "participant_ids", "start_time",
                       "market_type", "outcomes", "period", "scope", "overtime",
                       "participation", "currency"}
    if "player_prop" in kinds:
        sporting_fields.update({"player_id", "statistic", "line"})
    if kinds & {"spread", "spreads", "total", "totals"}:
        sporting_fields.add("line")
    settlement_fields = {"void_rule", "push_rule", "contract_type"} | RULE_FIELDS
    differences, details = [], []
    for field in sorted(required | {"player_id", "statistic", "line"}):
        a, b = getattr(left, field), getattr(right, field)
        unknown = lambda value: value is None or value == () or (isinstance(value, str)
                   and value.strip().casefold() in {"", "unknown"})
        if field in required and (unknown(a) or unknown(b)):
            differences.append(ReviewDifference(field=field, status="unknown"))
        elif (set(a) if field in {"participant_ids", "outcomes"} and a else a) != (
                set(b) if field in {"participant_ids", "outcomes"} and b else b):
            differences.append(ReviewDifference(field=field, status="mismatch"))
    if (not left.evidence_urls and not left.field_evidence) or (not right.evidence_urls and not right.field_evidence):
        differences.append(ReviewDifference(field="evidence_urls", status="unknown"))
    for difference in differences:
        field = difference.field
        details.append({**difference.model_dump(), "source_value": _fact_value(getattr(left, field)),
                        "target_value": _fact_value(getattr(right, field)),
                        "action": ("Obtain explicit listing-bound official evidence for the missing value"
                                   if difference.status == "unknown" else
                                   "Keep distinct contracts; a pricing transformation requires a separately validated model")})
    evidence = {"source": evidence_coverage(left, required), "target": evidence_coverage(right, required)}
    normal_scope_matches = not any(item.field in sporting_fields for item in differences)
    settlement_differences = [item.model_dump() for item in differences if item.field in settlement_fields]
    return {"schema_version": 1, "source": left.source, "target": right.source,
            "structurally_equivalent": not differences,
            "normal_sporting_scope_matches": normal_scope_matches,
            "settlement_transform_required": normal_scope_matches and bool(settlement_differences),
            "settlement_differences": settlement_differences,
            "differences": [item.model_dump() for item in differences],
            "difference_details": details, "evidence": evidence,
            "evidence_complete": not differences and all(row["complete"] for row in evidence.values()),
            "external_facts_verified": False,
            "execution_authorized": False,
            "note": "Structural comparison does not verify external facts, publish mappings, or authorize orders."}


def _fact_value(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value.normalize())
    if isinstance(value, tuple):
        return list(value)
    return value


def evidence_coverage(review: ContractReview, required: set[str] | frozenset[str]) -> dict:
    """Validate supplied claims against exact review fields without fetching URLs."""
    covered, issues = set(), []
    for index, document in enumerate(review.field_evidence):
        if document.source != review.source or document.source_market_id != review.source_market_id:
            issues.append({"document": index, "code": "listing_binding_mismatch"})
            continue
        if (document.retrieved_at > review.observed_at
                or document.effective_from and document.effective_from > review.observed_at
                or document.effective_until and document.effective_until <= review.observed_at):
            issues.append({"document": index, "code": "document_not_applicable_at_review_time"})
            continue
        for field, value in document.facts.items():
            # Re-parse the claimed field with its real type; decimal formatting,
            # UTC offsets and participant order are not substantive differences.
            try:
                claim = ContractReview.model_validate({**review.model_dump(), field: value})
                expected, actual = getattr(review, field), getattr(claim, field)
                if field in {"participant_ids", "outcomes"}:
                    expected, actual = set(expected or ()), set(actual or ())
                matches = expected is not None and expected != "unknown" and expected == actual
            except ValueError:
                matches = False
            if matches:
                covered.add(field)
            else:
                issues.append({"document": index, "field": field, "code": "document_fact_mismatch"})
    missing = sorted(required - covered)
    return {"complete": not missing and not issues, "covered_fields": sorted(covered),
            "missing_fields": missing, "issues": issues,
            "documents": [{"sha256": d.document_sha256, "url": d.official_url,
                           "jurisdiction": d.jurisdiction, "scope": d.scope,
                           "version": d.document_version, "retrieved_at": d.retrieved_at.isoformat()}
                          for d in review.field_evidence],
            "external_facts_verified": False}


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
