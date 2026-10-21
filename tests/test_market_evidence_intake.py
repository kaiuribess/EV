"""Offline review evidence cannot become an execution authorization."""
from datetime import timedelta

import pytest
from pydantic import ValidationError

from evplatform.core.market_review import ContractReview, FACT_FIELDS, FieldEvidence, compare_contracts
from evplatform.integrations.review_report import ReviewPair, ReviewPairs, build_review_report
from test_market_review import review
from test_review_report import NOW, record
from test_launch_readiness import mapping_fixture, script


def evidenced(item):
    facts = {key: value for key, value in item.model_dump(mode="json").items()
             if key in FACT_FIELDS and value is not None}
    document = FieldEvidence(source=item.source, source_market_id=item.source_market_id,
        official_url="https://example.test/OFFLINE-document", document_sha256="a" * 64,
        jurisdiction="OFFLINE jurisdiction", scope="OFFLINE exact listing and all declared fields",
        binding_evidence="OFFLINE fixture states that this document applies to this exact listing",
        retrieved_at=item.observed_at - timedelta(seconds=1), facts=facts)
    return item.model_copy(update={"field_evidence": (document,)})


def test_missing_contingencies_are_not_erased_by_a_generic_void_label():
    item = review(postponement_rule=None, interruption_rule=None)
    result = compare_contracts(item, item)
    assert not result["structurally_equivalent"]
    assert {"field": "postponement_rule", "status": "unknown"} in result["differences"]


def test_exact_differences_retain_each_contract_value():
    item = review()
    result = compare_contracts(item, item.model_copy(update={"void_rule": "fair_value"}))
    difference = next(d for d in result["difference_details"] if d["field"] == "void_rule")
    assert difference["source_value"] == "refund" and difference["target_value"] == "fair_value"
    assert "distinct contracts" in difference["action"]


def test_urls_without_listing_and_field_binding_do_not_complete_dossier():
    item = review()
    result = compare_contracts(item, item)
    assert result["structurally_equivalent"]
    assert not result["evidence_complete"] and not result["external_facts_verified"]
    assert "void_rule" in result["evidence"]["source"]["missing_fields"]


def test_complete_offline_dossier_never_claims_external_verification_or_authorizes():
    item = evidenced(review())
    result = compare_contracts(item, item)
    assert result["evidence_complete"]
    assert not result["execution_authorized"] and not result["external_facts_verified"]


@pytest.mark.parametrize("change,code", [
    ({"source": "different source"}, "listing_binding_mismatch"),
    ({"source_market_id": "different native market"}, "listing_binding_mismatch"),
    ({"facts": {"line": "21.5"}}, "document_fact_mismatch"),
])
def test_document_cannot_be_reused_for_a_different_source_listing_or_fact(change, code):
    item = evidenced(review())
    document = item.field_evidence[0].model_copy(update=change)
    result = compare_contracts(item.model_copy(update={"field_evidence": (document,)}), item)
    assert not result["evidence_complete"]
    assert code in {i["code"] for i in result["evidence"]["source"]["issues"]}


@pytest.mark.parametrize("clock,delta", [("retrieved_at", 1), ("effective_from", 1), ("effective_until", -1)])
def test_future_or_expired_evidence_does_not_apply_to_the_review(clock, delta):
    item = evidenced(review())
    document = item.field_evidence[0].model_copy(update={clock: item.observed_at + timedelta(seconds=delta)})
    result = compare_contracts(item.model_copy(update={"field_evidence": (document,)}), item)
    assert not result["evidence_complete"]
    assert result["evidence"]["source"]["issues"][0]["code"] == "document_not_applicable_at_review_time"


@pytest.mark.parametrize("change", [
    {"official_url": "https://name:password@example.test/doc"}, {"jurisdiction": "unknown"},
    {"facts": {"invented_settlement_field": "true"}}, {"document_sha256": "not a hash"},
])
def test_invalid_evidence_intake_rejected(change):
    document = evidenced(review()).field_evidence[0]
    with pytest.raises(ValidationError):
        FieldEvidence.model_validate({**document.model_dump(), **change})


def test_recent_legacy_price_timestamp_and_active_default_are_not_current_quote_proof():
    row = build_review_report({"items": [record()]}, now=NOW)["observations"][0]
    assert row["freshness"] == "within_limit"
    assert row["observation_freshness"] == "unknown" and not row["quote_evidence_ready"]
    assert {"verified_market_status", "verified_quote_observation", "complete_quote_observation"} <= set(row["evidence_gaps"])


def test_verified_unchanged_old_price_can_have_fresh_observation_evidence():
    item = record(source_time=(NOW-timedelta(days=2)).isoformat())
    item["observation"].update(source_observed_at=NOW.isoformat(), observation_verified=True,
                               observation_complete=True, market_status_verified=True)
    row = build_review_report({"items": [item]}, now=NOW)["observations"][0]
    assert row["freshness"] == "stale" and row["quote_evidence_ready"]
    assert "fresh_source_price" not in row["evidence_gaps"]
    assert not row["execution_authorized"]  # identity and contract review remain separate


def test_string_true_is_not_verified_and_verified_suspension_is_not_tradable():
    item = record()
    item["observation"].update(source_observed_at=NOW.isoformat(), observation_verified="true",
                               observation_complete=True, market_status_verified=True, status="suspended")
    row = build_review_report({"items": [item]}, now=NOW)["observations"][0]
    assert not row["quote_evidence_ready"]
    assert {"active_market", "verified_quote_observation"} <= set(row["quote_evidence_gaps"])


def market_review(market, source, source_market_id):
    values = dict(source=source, source_market_id=source_market_id, event_id=market.event_id,
                  sport=market.sport, league=market.league, participant_ids=market.participants,
                  start_time=market.start_time, market_type=market.market_type, player_id=market.player_id,
                  statistic=market.statistic, line=market.line, outcomes=market.outcomes,
                  **{k: v for k, v in market.settlement.model_dump().items() if k != "payout"})
    return evidenced(review(**values))


def test_strict_validator_requires_review_for_every_provider_native_pair(market):
    module = script("validate_market_mappings")
    provider, native = mapping_fixture(market)
    report = module.validate_mappings([provider], [module.VenueMappingFile(venue="kalshi", mappings=[native])], require_evidence=True)
    assert not report["valid"]
    assert len([i for i in report["issues"] if i["code"] == "evidence.missing_contract_comparison"]) == 2


def test_same_fingerprint_cannot_conceal_different_reviewed_line(market):
    module = script("validate_market_mappings")
    provider, native = mapping_fixture(market)
    left = market_review(market, "pinnacle", "OFFLINE source market")
    right = market_review(market, "kalshi", "test-native")
    left = evidenced(ContractReview.model_validate({**left.model_dump(), "line": (market.line or 0) + 1}))
    pair = ReviewPair(left=left, right=right, provider_signature="a"*64,
                      canonical_market_id=market.id, canonical_identity=market.identity())
    report = module.validate_mappings([provider], [module.VenueMappingFile(venue="kalshi", mappings=[native])],
                                     reviews=ReviewPairs(comparisons=[pair]), require_evidence=True)
    assert not report["valid"]
    assert "evidence.canonical_fact_mismatch" in {i["code"] for i in report["issues"]}


def test_complete_fixture_reviews_validate_offline_but_do_not_authorize(market):
    module = script("validate_market_mappings")
    provider, native = mapping_fixture(market)
    pairs = [ReviewPair(left=market_review(market, item.bookmaker, "OFFLINE source market"),
                        right=market_review(market, "kalshi", "test-native"),
                        provider_signature=item.signature, canonical_market_id=market.id,
                        canonical_identity=market.identity()) for item in provider.mappings]
    report = module.validate_mappings([provider], [module.VenueMappingFile(venue="kalshi", mappings=[native])],
                                     reviews=ReviewPairs(comparisons=pairs), require_evidence=True)
    assert report["valid"] and report["evidence_required"]
    assert not report["execution_authorized"] and not report["external_facts_verified"]


def test_schema_export_is_offline_and_never_overwrites(tmp_path, capsys):
    module = script("review_market_readiness")
    path = tmp_path / "review-schema.json"
    assert module.main(["--write-comparison-schema", str(path)]) == 0
    original = path.read_bytes()
    assert b"FieldEvidence" in original
    assert module.main(["--write-comparison-schema", str(path)]) == 2
    assert path.read_bytes() == original
