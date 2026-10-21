"""Read-only evidence inventory. This module cannot publish or certify mappings.

Source labels, periods and settlement text remain source facts. In particular,
matching labels do not establish canonical IDs or equivalent settlement rules.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, ConfigDict, Field

from evplatform.core.market_review import ContractReview, compare_contracts


class ReviewPair(BaseModel):
    model_config = ConfigDict(extra="forbid")
    left: ContractReview
    right: ContractReview
    provider_signature: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    canonical_market_id: str | None = None
    canonical_identity: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class ReviewPairs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(default=1, ge=1, le=1)
    comparisons: list[ReviewPair] = Field(default_factory=list, max_length=100)


def _clock(value):
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result if result.utcoffset() is not None else None
    except ValueError:
        return None


def source_freshness(value, *, now: datetime, max_age_seconds: float) -> dict:
    """Never substitute provider delivery, a heartbeat, or report creation time."""
    if now.utcoffset() is None or not 0 < max_age_seconds <= 86400:
        raise ValueError("Use an aware review clock and a bounded freshness limit")
    clock = _clock(value)
    age = (now - clock).total_seconds() if clock is not None else None
    state = "unknown" if age is None else "future" if age < 0 else "stale" if age > max_age_seconds else "within_limit"
    return {"source_time": value if isinstance(value, str) else None,
            "source_age_seconds": age, "freshness": state}


def observation_evidence(observation: dict, *, now: datetime, max_age_seconds: float) -> dict:
    """Assess a quote-bound observation and active status, never a price-change clock."""
    clock = source_freshness(observation.get("source_observed_at"), now=now, max_age_seconds=max_age_seconds)
    problems = []
    if observation.get("observation_verified") is not True:
        problems.append("verified_quote_observation")
    if observation.get("observation_complete") is not True:
        problems.append("complete_quote_observation")
    if observation.get("market_status_verified") is not True:
        problems.append("verified_market_status")
    if observation.get("status") != "active":
        problems.append("active_market")
    if clock["freshness"] != "within_limit":
        problems.append("fresh_quote_observation")
    return {"source_observed_at": clock["source_time"],
            "observation_age_seconds": clock["source_age_seconds"],
            "observation_freshness": clock["freshness"],
            "observation_verified": observation.get("observation_verified") is True,
            "observation_complete": observation.get("observation_complete") is True,
            "market_status_verified": observation.get("market_status_verified") is True,
            "verification_reason": observation.get("verification_reason"),
            "quote_evidence_ready": not problems, "quote_evidence_gaps": problems}


def fetch_pending_reviews(url: str, *, max_rows=3000, client: httpx.Client | None = None) -> dict:
    """Only GET the local quarantine. Pagination is bounded and non-atomic."""
    parsed = urlparse(url)
    if (parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
            or parsed.username or parsed.password or parsed.path not in {"", "/"}
            or parsed.query or parsed.fragment):
        raise ValueError("Only a loopback HTTP application URL is accepted")
    if isinstance(max_rows, bool) or not isinstance(max_rows, int) or not 1 <= max_rows <= 10000:
        raise ValueError("Review size must be 1 to 10000")
    if client is None:
        with httpx.Client(timeout=10, trust_env=False, follow_redirects=False) as session:
            return fetch_pending_reviews(url, max_rows=max_rows, client=session)
    items, observed, totals, providers, offset, pages = [], [], [], [], 0, 0
    while offset < max_rows:
        limit = min(1000, max_rows - offset)
        response = client.get(url.rstrip("/") + "/api/market-review", params={"offset": offset, "limit": limit})
        response.raise_for_status()
        page = response.json()
        rows, total = page.get("items"), page.get("total")
        if (not isinstance(rows, list) or len(rows) > limit or not isinstance(total, int)
                or isinstance(total, bool) or total < 0 or page.get("offset") != offset):
            raise ValueError("Invalid bounded quarantine page")
        items.extend(rows)
        totals.append(total)
        observed.append(page.get("observed_at"))
        providers = page.get("providers", [])
        pages += 1
        offset += len(rows)
        if not rows or offset >= total or len(rows) < limit:
            break
    return {"schema_version": 1, "items": items, "total": max(totals), "offset": 0,
            "providers": providers, "observed_at": observed[-1], "bounded_window": True,
            "collection": {"pages": pages, "atomic": pages == 1,
                           "total_changed": len(set(totals)) > 1,
                           "truncated": offset < max(totals)}}


def build_review_report(snapshot: dict, *, now: datetime | None = None,
                        max_age_seconds=5.0, pairs: ReviewPairs | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    source_freshness(None, now=now, max_age_seconds=max_age_seconds)
    rows = snapshot.get("items")
    if not isinstance(rows, list) or len(rows) > 10000:
        raise ValueError("Expected at most 10000 quarantined observations")
    observations, counts, gaps, seen, duplicates = [], Counter(), Counter(), set(), 0
    quote_counts = Counter()
    for raw in rows:
        if not isinstance(raw, dict) or not isinstance(raw.get("identity"), dict):
            counts["invalid_identity"] += 1
            continue
        identity, observation = raw["identity"], raw.get("observation", {})
        context = identity.get("context", {})
        if not isinstance(context, dict) or not isinstance(observation, dict):
            counts["invalid_identity"] += 1
            continue
        signature = raw.get("signature")
        if not isinstance(signature, str) or len(signature) != 64 or any(c not in "0123456789abcdef" for c in signature):
            counts["invalid_identity"] += 1
            continue
        if signature in seen:
            duplicates += 1
            continue
        seen.add(signature)
        clocks = source_freshness(observation.get("source_time"), now=now, max_age_seconds=max_age_seconds)
        counts[clocks["freshness"]] += 1
        quote_proof = observation_evidence(observation, now=now, max_age_seconds=max_age_seconds)
        quote_counts[quote_proof["observation_freshness"]] += 1
        quote_counts["ready" if quote_proof["quote_evidence_ready"] else "not_ready"] += 1
        prices = observation.get("decimal_odds", {})
        try:
            valid_prices = isinstance(prices, dict) and len(prices) >= 2 and all(
                Decimal(str(value)).is_finite() and Decimal(str(value)) > 1 for value in prices.values())
        except (InvalidOperation, ValueError):
            valid_prices = False
        participants = context.get("participants")
        missing = ["verified_event_identity", "verified_participant_aliases", "equivalent_settlement_rules"]
        if not isinstance(participants, list) or len(participants) < 2 or any(not isinstance(p, str) or not p for p in participants):
            participants = None
            missing.append("participants")
        if _clock(context.get("scheduled_start")) is None:
            missing.append("scheduled_start")
        if not identity.get("period"):
            missing.append("period")
        for field in ("overtime", "participation", "void_rule", "push_rule", "scope"):
            if context.get(field) in (None, "", "unknown"):
                missing.append(field)
        if not valid_prices:
            missing.append("complete_valid_prices")
        if observation.get("status") != "active":
            missing.append("active_market")
        missing.extend(quote_proof["quote_evidence_gaps"])
        missing = sorted(set(missing))
        gaps.update(missing)
        observations.append({"signature": signature, "provider": identity.get("provider"),
            "bookmaker": identity.get("bookmaker"), "source_event_id": identity.get("event_id"),
            "source_market_key": identity.get("market_key"), "source_player_key": identity.get("player_key"),
            "source_line": identity.get("line"), "source_period": identity.get("period"),
            "outcome_points": identity.get("outcome_points"), "participant_labels": participants,
            "scheduled_start": context.get("scheduled_start"), "source_sport": context.get("event_sport"),
            **clocks, "provider_time": observation.get("provider_time"), "source_status": observation.get("status"),
            **quote_proof, "source_price_changed_at": observation.get("source_price_changed_at"),
            "price_count": len(prices) if isinstance(prices, dict) else 0,
            "raw_settlement_supplied": bool(context.get("settlement") or context.get("settlement_rules")),
            "evidence_gaps": sorted(missing), "execution_authorized": False})
    comparisons = [compare_contracts(pair.left, pair.right) for pair in (pairs or ReviewPairs()).comparisons]
    return {"schema_version": 1, "reviewed_at": now.isoformat(), "max_source_age_seconds": max_age_seconds,
        "source_window_observed_at": snapshot.get("observed_at"), "bounded_window": True,
        "collection": snapshot.get("collection", {"atomic": False, "truncated": len(rows) < snapshot.get("total", len(rows))}),
        "summary": {"received_observations": len(rows), "reviewed_observations": len(observations),
                    "duplicate_signatures": duplicates, "source_freshness": dict(sorted(counts.items())),
                    "quote_observation_evidence": dict(sorted(quote_counts.items())),
                    "evidence_gaps": dict(sorted(gaps.items())), "authorized_mappings": 0},
        "observations": sorted(observations, key=lambda item: (str(item["source_event_id"]), str(item["source_market_key"]), item["signature"])),
        "contract_comparisons": comparisons, "execution_authorized": False,
        "note": "Read-only review. source_time/freshness are legacy source-clock diagnostics, not quote validity. "
                "Only quote-bound source_observed_at plus verified complete observations and verified active status "
                "establish quote evidence; unchanged prices can remain valid. Labels and supplied contract comparisons are not externally verified. "
                "No aliases, signatures, defaults, settlement equivalence, independent-book counts, or execution permissions are invented."}
