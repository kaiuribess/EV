from types import SimpleNamespace

import pytest

from evplatform.core.market_review import ContractReview, compare_contracts, pending_market_reviews
from evplatform.core.models import utcnow
from evplatform.integrations.normalization import SourceOffer, VerifiedProviderMapper


def review(**changes):
    values = dict(source="OFFLINE book", source_market_id="OFFLINE market", event_id="event1",
        sport="basketball", league="nba", participant_ids=("team1", "team2"), start_time=utcnow(),
        market_type="player_prop", player_id="player1", statistic="points", line="20.5",
        outcomes=("over", "under"), period="full_game", scope="player", overtime="included",
        participation="must_play", void_rule="refund", push_rule="not_applicable", currency="USD",
        contract_type="binary", evidence_urls=("https://example.test/OFFLINE",))
    return ContractReview(**{**values, **changes})


def test_equal_review_does_not_publish_a_mapping_or_authorize():
    item = review()
    report = compare_contracts(item, item.model_copy(update={"source": "OFFLINE venue"}))
    assert report["structurally_equivalent"] and not report["execution_authorized"]


@pytest.mark.parametrize("field,value", [("line", "21.5"), ("player_id", "player2"),
    ("period", "first_half"), ("void_rule", "fair_market_value"), ("overtime", "excluded"),
    ("event_id", "event2"), ("statistic", "rebounds")])
def test_nonidentical_contract_cannot_pass(field, value):
    left = review()
    right = ContractReview.model_validate({**left.model_dump(), field: value})
    report = compare_contracts(left, right)
    assert {"field": field, "status": "mismatch"} in report["differences"]
    assert not report["structurally_equivalent"]


@pytest.mark.parametrize("field", ["player_id", "line", "period", "void_rule", "overtime", "event_id"])
def test_missing_facts_never_inherit_default_settlement(field):
    item = review(**{field: None})
    assert {"field": field, "status": "unknown"} in compare_contracts(item, item)["differences"]


def test_bounded_pending_observations_keep_original_clocks_and_prices():
    from decimal import Decimal
    at = utcnow()
    mapper = VerifiedProviderMapper(quarantine_limit=1)
    offer = SourceOffer("provider", "book", "event", "h2h", None, None, None,
                        {"a": None, "b": None}, {"a": Decimal("2.1"), "b": Decimal("1.9")}, at, None)
    mapper._quarantine(offer, "Needs rules review", "provider")
    engine = SimpleNamespace(registry=SimpleNamespace(records={"provider": SimpleNamespace(plugin=SimpleNamespace(mapper=mapper))}))
    report = pending_market_reviews(engine)
    assert report["bounded_window"] and report["total"] == 1
    assert report["items"][0]["observation"]["source_time"] == at.isoformat()
    assert report["items"][0]["observation"]["decimal_odds"] == {"a": "2.1", "b": "1.9"}
    assert pending_market_reviews(engine, provider="other")["total"] == 0
    assert pending_market_reviews(engine, offset=1)["items"] == []
