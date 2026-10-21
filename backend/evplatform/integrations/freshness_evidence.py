"""Bounded source-clock diagnostics, never a source of live authorization.

Measurements describe delivered observations, not upstream publication latency
or how long a quote remains tradable. No credentials/raw payloads are retained.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from datetime import datetime, timezone

from .base import ProviderEnvelope
from .normalization import extract_offers


FIELDS = ("last_observed", "last_update_type", "last_update", "line_changed_at_ms", "price_age_s",
          "ts", "timestamp", "status", "available", "suspended", "action", "verified_at", "is_current",
          "topped_up", "snapshot_complete", "snapshot_scope", "snapshot_in_progress", "truncated", "partial")


def _type(value):
    if value is None:
        return "null"
    return {bool: "boolean", int: "integer", float: "number", str: "string", dict: "object", list: "array"}.get(type(value), "unknown")


def _stats(values):
    ordered = sorted(values)
    def quantile(fraction):
        return ordered[max(0, math.ceil(fraction*len(ordered))-1)] if ordered else None
    return {"count": len(ordered), "minimum": ordered[0] if ordered else None,
            "median": quantile(.5), "p95": quantile(.95) if len(ordered) >= 20 else None,
            "p99": quantile(.99) if len(ordered) >= 100 else None,
            "maximum": ordered[-1] if ordered else None}


def _structures(payload):
    """Inspect declared fields only, in bounded known provider containers."""
    if isinstance(payload, dict):
        yield "envelope", payload
        if isinstance(payload.get("data"), list):
            for row in payload["data"]:
                if isinstance(row, dict):
                    yield "quote_row", row
        if isinstance(payload.get("props"), list):
            for row in payload["props"]:
                if isinstance(row, dict):
                    yield "quote_row", row
        events = payload.get("events", [payload["event"]] if isinstance(payload.get("event"), dict) else [])
    else:
        events = payload if isinstance(payload, list) else []
    for event in events:
        if not isinstance(event, dict):
            continue
        if "bookmaker" in event:
            yield "quote_row", event
        else:
            yield "event", event
        for row in event.get("odds", []):
            if isinstance(row, dict):
                yield "quote_row", row
        for book in event.get("bookmakers", []):
            if not isinstance(book, dict):
                continue
            yield "book", book
            for market in book.get("markets", []):
                if isinstance(market, dict):
                    yield "market", market
                    for row in market.get("outcomes", []):
                        if isinstance(row, dict):
                            yield "quote_row", row


class FreshnessEvidence:
    def __init__(self, *, max_offers=100000, max_groups=1000):
        if not 1 <= max_offers <= 1000000 or not 1 <= max_groups <= 10000:
            raise ValueError("Use bounded evidence collection limits")
        self.max_offers, self.max_groups = max_offers, max_groups
        self.frames, self.fields, self.groups = Counter(), {}, {}
        self.first_received = self.last_received = None
        self.offers_seen = self.extraction_errors = self.omitted = 0
        self.synthetic = False

    def observe(self, envelope: ProviderEnvelope):
        if envelope.received_at.tzinfo is None:
            raise ValueError("Evidence receipt time must include a timezone")
        self.first_received = min(self.first_received, envelope.received_at) if self.first_received else envelope.received_at
        self.last_received = max(self.last_received, envelope.received_at) if self.last_received else envelope.received_at
        self.synthetic |= envelope.synthetic
        self.frames[f"{envelope.provider_id}:{envelope.kind}"] += 1
        for scope, row in _structures(envelope.payload):
            if len(self.fields) >= 1000 and (envelope.provider_id, scope) not in self.fields:
                continue
            fields = self.fields.setdefault((envelope.provider_id, scope), {name: Counter() for name in FIELDS})
            for name in FIELDS:
                fields[name][_type(row[name]) if name in row else "missing"] += 1
        try:
            offers = extract_offers(envelope)
        except (ValueError, KeyError, TypeError, AttributeError, ArithmeticError):
            self.extraction_errors += 1
            return
        for offer in offers:
            self.offers_seen += 1
            if self.offers_seen > self.max_offers:
                self.omitted += 1
                continue
            sport = envelope.metadata.get("sport") or offer.context.get("event_sport") or "unknown"
            key = (envelope.provider_id, str(sport), offer.bookmaker, offer.market_key)
            if key not in self.groups and len(self.groups) >= self.max_groups:
                self.omitted += 1
                continue
            group = self.groups.setdefault(key, {"seen": set(), "counts": Counter(), "ages": [], "legacy": [],
                "delivery": [], "delivery_frames": set(), "intervals": [], "last_observation": {}, "reasons": Counter()})
            group["counts"]["deliveries"] += 1
            observation = offer.source_observed_at
            signature = offer.signature
            dedup = (signature, observation or offer.observed_at,
                     tuple(sorted((name, str(price)) for name, price in offer.prices.items())))
            if dedup in group["seen"]:
                group["counts"]["repeated_same_observation"] += 1
                continue
            group["seen"].add(dedup)
            group["counts"]["distinct_observations"] += 1
            group["counts"]["observation_verified"] += int(offer.observation_verified)
            group["counts"]["observation_complete"] += int(offer.observation_complete)
            group["counts"]["market_status_verified"] += int(offer.market_status_verified)
            group["reasons"][offer.verification_reason or "no_observation_evidence"] += 1
            if offer.observed_at is not None:
                group["legacy"].append((envelope.received_at-offer.observed_at).total_seconds()*1000)
            delivery_frame = (envelope.received_monotonic_ns, envelope.published_at)
            if envelope.published_at is not None and delivery_frame not in group["delivery_frames"]:
                group["delivery"].append((envelope.received_at-envelope.published_at).total_seconds()*1000)
                group["delivery_frames"].add(delivery_frame)
            if offer.observation_verified and observation is not None:
                age = (envelope.received_at-observation).total_seconds()*1000
                group["ages"].append(age)
                old = group["last_observation"].get(signature)
                if old is not None and observation > old:
                    group["intervals"].append((observation-old).total_seconds()*1000)
                if old is None or observation > old:
                    group["last_observation"][signature] = observation

    def report(self):
        rows = []
        for key, group in sorted(self.groups.items()):
            ages, intervals = _stats(group["ages"]), _stats(group["intervals"])
            enough = ages["count"] >= 100 and intervals["count"] >= 20 and (ages["minimum"] or 0) >= 0
            candidate = math.ceil((ages["p99"]+intervals["p95"])/1000) if enough else None
            rows.append({"provider": key[0], "source_sport": key[1], "source_bookmaker": key[2], "source_market": key[3],
                "counts": dict(group["counts"]), "observation_to_receipt_ms": ages,
                "legacy_clock_age_ms_not_publication_latency": _stats(group["legacy"]),
                "provider_send_to_receipt_ms": _stats(group["delivery"]), "same_offer_observation_interval_ms": intervals,
                "verification_reasons": dict(group["reasons"]),
                "ttl_proposal": {"seconds": candidate, "validated": False, "installable": False,
                    "status": "measurement_only_candidate" if enough else "insufficient_verified_observation_series",
                    "formula": "ceil((observation_age_p99_ms + same_offer_interval_p95_ms) / 1000)",
                    "requires": ["independent quote-validity and suspension audit", "canonical market and phase scope",
                                 "representative sessions and clock validation", "explicit review before live use"]}})
        return {"schema_version": 1, "generated_at": datetime.now(timezone.utc).isoformat(),
            "first_received_at": self.first_received.isoformat() if self.first_received else None,
            "last_received_at": self.last_received.isoformat() if self.last_received else None,
            "synthetic": self.synthetic, "execution_enabled": False, "configuration_changed": False,
            "frame_counts": dict(self.frames), "offers_seen": self.offers_seen,
            "extraction_errors": self.extraction_errors, "observations_omitted_by_bounds": self.omitted,
            "field_types": [{"provider": key[0], "scope": key[1], "fields": {name: dict(counts) for name, counts in fields.items()}}
                            for key, fields in sorted(self.fields.items())], "groups": rows,
            "interpretation": ["Delivered observation age is not sportsbook publication-to-client latency.",
                "A new packet or an unchanged price alone does not establish current tradability.",
                "TTL candidates describe delivery/cadence only and never certify active status or price validity.",
                "Short bounded captures cannot certify coverage, absence of withdrawals or long-run availability."]}


def evidence_digest(report):
    return hashlib.sha256(json.dumps(report, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
