"""Validate reviewed provider/native mappings offline without enabling them.

Exit 0 means structural and exact-identity checks passed. It is not evidence that
an external settlement rule is true, the market is current, or trading is ready.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from evplatform.core.readiness import digest_json
from evplatform.core.market_review import RULE_FIELDS, compare_contracts
from evplatform.integrations.normalization import MappingFile
from evplatform.integrations.review_report import ReviewPairs
from evplatform.venues.bridge import VerifiedVenueMapping
from evplatform.venues.book_normalization import NativeBookMapper, NativeMappingFile


class VenueMappingFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    venue: str
    mappings: list[VerifiedVenueMapping]


def validate_mappings(provider_files: list[MappingFile], venue_files: list[VenueMappingFile],
                      *, min_reference_books: int = 2, native_files: list[NativeMappingFile] | None = None,
                      reviews: ReviewPairs | None = None, require_evidence: bool = False) -> dict:
    issues: list[dict] = []
    signatures = {}
    markets = {}
    books: dict[str, set[str]] = {}
    native_keys = {}
    canonical_keys = {}
    native_files = native_files or []

    def issue(code, scope, action):
        # Scope contains only an ID/field, never arbitrary source evidence text.
        issues.append({"code": code, "scope": scope, "action": action})

    for bundle in provider_files:
        for item in bundle.mappings:
            previous = signatures.get(item.signature)
            if previous is not None:
                issue("provider.duplicate_signature" if previous == item else "provider.conflicting_signature",
                      item.signature, "Keep exactly one reviewed mapping per source signature")
            signatures[item.signature] = item
            previous_market = markets.get(item.market.id)
            if previous_market and previous_market.identity() != item.market.identity():
                issue("canonical.identity_collision", item.market.id,
                      "Use separate canonical IDs for different players, lines, periods or settlement rules")
            markets[item.market.id] = item.market
            books.setdefault(item.market.id, set()).add(item.bookmaker.casefold().strip())
            if not item.bookmaker.strip() or not item.evidence.strip():
                issue("provider.review_missing", item.signature, "Record the reviewed bookmaker and evidence")
            if item.market.settlement.currency != "USD" or item.market.settlement.contract_type != "binary":
                issue("canonical.unsupported_settlement", item.market.id,
                      "Register and validate an explicit type-specific settlement model")
    for market_id, references in books.items():
        if len(references) < min_reference_books:
            issue("provider.insufficient_independent_books", market_id,
                  "Review enough independent bookmakers; multiple API vendors for one bookmaker count once")
    for bundle in venue_files:
        if not bundle.venue.strip():
            issue("venue.id_missing", "venue", "Set the actual execution plugin ID")
        for item in bundle.mappings:
            canonical_key = (bundle.venue, item.canonical_market_id, item.canonical_outcome)
            native_key = (bundle.venue, item.native_instrument_id, item.native_outcome)
            if canonical_key in canonical_keys:
                issue("venue.duplicate_canonical_outcome", item.canonical_market_id,
                      "Keep exactly one native instrument per venue and canonical outcome")
            if native_key in native_keys and native_keys[native_key] != canonical_key:
                issue("venue.native_identity_collision", item.canonical_market_id,
                      "Do not map one native instrument/outcome to incompatible canonical markets")
            canonical_keys[canonical_key] = item
            native_keys[native_key] = canonical_key
            market = markets.get(item.canonical_market_id)
            if market is None:
                issue("venue.missing_provider_market", item.canonical_market_id,
                      "Review a matching provider market before native execution")
                continue
            if item.canonical_outcome not in market.outcomes:
                issue("venue.outcome_mismatch", item.canonical_market_id,
                      "Map an exact canonical outcome; do not guess home/away or yes/no equivalence")
            if item.settlement_fingerprint != market.identity():
                issue("venue.fingerprint_mismatch", item.canonical_market_id,
                      "Bind settlement_fingerprint to Market.identity(), covering event, player, line and all rules")
            if not item.verified or not item.mapping_evidence.strip():
                issue("venue.unverified", item.canonical_market_id, "Review exact event/outcome/settlement equivalence")
            if not item.fee_schedule_verified:
                issue("venue.fees_unverified", item.canonical_market_id, "Verify current market and account-specific fees")
            if item.canonical_payout_usd != market.settlement.payout:
                issue("venue.payout_mismatch", item.canonical_market_id, "Match the actual canonical gross payout")
            # This file format belongs to the original whole-$1 bridge. Extended
            # accounting uses its own explicit instrument schema and validator.
            if (item.canonical_payout_usd != 1 or item.native_payout_usd != 1
                    or item.native_quantity_unit != "contracts" or bundle.venue != "kalshi"):
                issue("venue.legacy_bridge_units_unsupported", item.canonical_market_id,
                      "Use the native accounting instrument schema for fractional/cent-contract or USD-stake venues")
    extended = [row for bundle in native_files for row in bundle.mappings]
    extended_digest = None
    try:
        extended_mapper = NativeBookMapper(extended)
        extended_digest = extended_mapper.mapping_digest
    except ValueError:
        issue("native.conflicting_mappings", "native", "Resolve duplicate or conflicting canonical/native instrument identities")
    for row in extended:
        i = row.instrument
        provider_market = markets.get(row.market.id)
        if provider_market is None:
            issue("native.missing_provider_market", row.market.id, "Review matching provider source identities")
        elif provider_market.identity() != row.market.identity():
            issue("native.provider_fingerprint_mismatch", row.market.id,
                  "Match event, player, threshold, period and all settlement rules exactly")
        key = (i.venue, i.instrument_id, i.native_outcome)
        if key in native_keys:
            issue("native.legacy_overlap", row.market.id, "Use one execution mapping format for this instrument")
        native_keys[key] = (i.venue, i.canonical_market_id, i.outcome)
    if not signatures:
        issue("provider.empty", "provider", "Supply reviewed provider mappings")
    if not native_keys:
        issue("venue.empty", "venue", "Supply reviewed native venue mappings")
    # Optional strict dossier validation is intentionally separate from legacy
    # structural checks. It never converts a review into an executable mapping.
    review_reports = []
    covered_pairs = set()
    for pair in (reviews or ReviewPairs()).comparisons:
        comparison = compare_contracts(pair.left, pair.right)
        review_reports.append({"provider_signature": pair.provider_signature,
                               "native_market_id": pair.right.source_market_id, **comparison})
        provider = signatures.get(pair.provider_signature)
        scope = pair.provider_signature or "unbound_review"
        if provider is None or pair.canonical_market_id != provider.market.id or pair.canonical_identity != provider.market.identity():
            issue("evidence.provider_binding_mismatch", scope,
                  "Bind the review to an existing exact source signature, canonical market and current Market.identity()")
            continue
        market = provider.market
        native_matches = [key for key, canonical in native_keys.items()
                          if key[:2] == (pair.right.source, pair.right.source_market_id) and canonical[1] == market.id]
        if pair.left.source != provider.bookmaker or not native_matches:
            issue("evidence.source_or_native_binding_mismatch", scope,
                  "Use the reviewed bookmaker ID and exact execution venue/instrument ID")
            continue
        values = {"event_id": market.event_id, "sport": market.sport, "league": market.league,
                  "participant_ids": market.participants, "start_time": market.start_time,
                  "market_type": market.market_type, "player_id": market.player_id,
                  "statistic": market.statistic, "line": market.line, "outcomes": market.outcomes,
                  **{field: getattr(market.settlement, field) for field in
                     ("period", "scope", "overtime", "participation", "void_rule", "push_rule", "currency", "contract_type")}}
        rules = market.settlement.contingencies
        if rules is None:
            issue("evidence.runtime_rules_missing", scope,
                  "Retain all reviewed contingency rules in settlement.contingencies before publishing a mapping")
        else:
            values.update({field: getattr(rules, field) for field in RULE_FIELDS})
        for side in (pair.left, pair.right):
            for field, expected in values.items():
                actual = getattr(side, field)
                if field in {"participant_ids", "outcomes"}:
                    actual, expected = set(actual or ()), set(expected)
                if actual != expected:
                    issue("evidence.canonical_fact_mismatch", f"{scope}:{field}",
                          "The reviewed fact must match the actual canonical mapping; do not copy a fingerprint over conflicting facts")
        if not comparison["evidence_complete"]:
            issue("evidence.incomplete_or_incompatible", scope,
                  "Resolve exact rule differences and provide listing-bound field evidence; see review_reports")
        for native_key in native_matches:
            covered_pairs.add((pair.provider_signature, *native_key[:2]))
    if require_evidence:
        for signature, provider in signatures.items():
            for native_key, canonical in native_keys.items():
                if canonical[1] == provider.market.id and (signature, *native_key[:2]) not in covered_pairs:
                    issue("evidence.missing_contract_comparison", signature,
                          "Supply a complete exact provider-to-native contract review for every referenced instrument")
    # Binding covers complete configuration including evidence and explicit units.
    digest_payload = {"providers": [f.model_dump(mode="json") for f in provider_files],
                      "venues": [f.model_dump(mode="json") for f in venue_files],
                      "native": [f.model_dump(mode="json") for f in native_files]}
    if reviews is not None:
        digest_payload["reviews"] = reviews.model_dump(mode="json")
    digest = digest_json(digest_payload)
    return {"schema_version": 1, "valid": not issues, "mapping_digest": digest,
            "native_mapping_digest": extended_digest if extended else None,
            "provider_mappings": len(signatures), "canonical_markets": len(markets),
            "native_outcomes": len(native_keys), "issues": issues,
            "evidence_required": require_evidence, "review_reports": review_reports,
            "external_facts_verified": False, "execution_authorized": False,
            "note": "Offline structural validation does not verify external facts or authorize trading."}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", type=Path, action="append", default=[])
    parser.add_argument("--venue", type=Path, action="append", default=[])
    parser.add_argument("--native", type=Path, action="append", default=[], help="Extended native accounting mapping bundle")
    parser.add_argument("--comparisons", type=Path, help="Listing-bound ContractReview evidence pairs")
    parser.add_argument("--require-evidence", action="store_true", help="Reject every mapping without a complete exact contract dossier")
    parser.add_argument("--min-reference-books", type=int, default=2)
    args = parser.parse_args(argv)
    if args.min_reference_books < 1:
        parser.error("--min-reference-books must be positive")
    try:
        providers = [MappingFile.model_validate_json(path.read_text(encoding="utf-8")) for path in args.provider]
        venues = [VenueMappingFile.model_validate_json(path.read_text(encoding="utf-8")) for path in args.venue]
        natives = [NativeMappingFile.model_validate_json(path.read_text(encoding="utf-8")) for path in args.native]
        reviews = ReviewPairs.model_validate_json(args.comparisons.read_text(encoding="utf-8")) if args.comparisons else None
        report = validate_mappings(providers, venues, min_reference_books=args.min_reference_books, native_files=natives,
                                   reviews=reviews, require_evidence=args.require_evidence)
        print(json.dumps(report, indent=2))
        return 0 if report["valid"] else 1
    except ValidationError as exc:
        print(json.dumps({"error": "invalid_mapping_schema", "fields": [
            {"location": list(item["loc"]), "type": item["type"]} for item in exc.errors()
        ]}))
    except Exception as exc:
        print(json.dumps({"error": "mapping_check_failed", "type": type(exc).__name__}))
    return 2


if __name__ == "__main__":
    sys.exit(main())
