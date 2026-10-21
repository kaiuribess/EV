"""Settlement review must survive canonicalization and durable recovery."""
import hashlib
import json

import pytest
from pydantic import ValidationError

from evplatform.core.models import Market, SettlementContingencies
from evplatform.core.market_review import RULE_FIELDS
from evplatform.integrations.review_report import ReviewPair, ReviewPairs
from test_market_review import review
from test_market_evidence_intake import market_review, evidenced
from test_launch_readiness import mapping_fixture, script


def with_rules(market):
    rules = SettlementContingencies(**{field: getattr(review(), field) for field in RULE_FIELDS})
    return market.model_copy(update={"settlement": market.settlement.model_copy(update={"contingencies": rules})})


def test_legacy_identity_and_recovery_remain_stable_without_inventing_rules(market):
    settlement = market.settlement.model_dump(mode="json", exclude={"contingencies"})
    payload = {"event": market.event_id, "sport": market.sport, "league": market.league,
        "type": market.market_type, "player": market.player_id, "statistic": market.statistic,
        "line": str(market.line.normalize()) if market.line is not None else None,
        "outcomes": sorted(market.outcomes), "settlement": settlement}
    old_identity = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    legacy = market.model_dump(mode="json")
    legacy["settlement"] = settlement
    recovered = Market.model_validate(legacy)
    assert market.identity() == recovered.identity() == old_identity
    assert recovered.settlement.contingencies is None


@pytest.mark.parametrize("field", sorted(RULE_FIELDS))
def test_each_contingency_changes_identity_and_survives_restart(market, field):
    market = with_rules(market)
    changed_rules = market.settlement.contingencies.model_copy(update={field: "OFFLINE different contract rule"})
    changed = market.model_copy(update={"settlement": market.settlement.model_copy(update={"contingencies": changed_rules})})
    assert changed.identity() != market.identity()
    recovered = Market.model_validate_json(changed.model_dump_json())
    assert recovered.identity() == changed.identity()
    assert getattr(recovered.settlement.contingencies, field) == "OFFLINE different contract rule"


@pytest.mark.parametrize("change", [{"schema_version": 2}, {"payout_rule": "unknown"},
                                   {"postponement_rule": " "}, {"interruption_rule": None}])
def test_unknown_version_or_missing_rules_rejected(market, change):
    rules = with_rules(market).settlement.contingencies.model_dump()
    with pytest.raises(ValidationError):
        SettlementContingencies.model_validate(rules | change)


@pytest.mark.parametrize("stored", [False, True])
def test_complete_dossier_cannot_drop_or_replace_runtime_contingencies(market, stored):
    module = script("validate_market_mappings")
    original = market
    reviewed_market = with_rules(market)
    market = reviewed_market if stored else original
    provider, native = mapping_fixture(market)
    pairs = []
    for item in provider.mappings:
        left = market_review(reviewed_market, item.bookmaker, "OFFLINE source market")
        right = market_review(reviewed_market, "kalshi", "test-native")
        if stored:
            # Both documents agree with each other, but contradict runtime.
            left = evidenced(left.model_copy(update={"payout_rule": "OFFLINE different payout"}))
            right = evidenced(right.model_copy(update={"payout_rule": "OFFLINE different payout"}))
        pairs.append(ReviewPair(left=left, right=right, provider_signature=item.signature,
            canonical_market_id=market.id, canonical_identity=market.identity()))
    report = module.validate_mappings([provider], [module.VenueMappingFile(venue="kalshi", mappings=[native])],
        reviews=ReviewPairs(comparisons=pairs), require_evidence=True)
    assert not report["valid"]
    codes = {row["code"] for row in report["issues"]}
    assert ("evidence.canonical_fact_mismatch" if stored else "evidence.runtime_rules_missing") in codes
