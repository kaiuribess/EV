from datetime import timedelta
from decimal import Decimal as D

import pytest
from fastapi.testclient import TestClient

from evplatform.core.models import utcnow
from evplatform.core.settlement_bridge import ExceptionalSettlementBound, estimate_bridge
from test_signal_api import HEADERS, quiet_app


def paired_markets(market):
    target = market.model_copy(update={"id": "kalshi-target", "settlement": market.settlement.model_copy(
        update={"void_rule": "fair_price_on_cancellation"})})
    assert target.identity() != market.identity()
    return market, target


def bound(source, target, *, q="0.02", floor="0", **changes):
    now = utcnow()
    values = dict(source_market_id=source.id, source_identity=source.identity(),
        target_market_id=target.id, target_identity=target.identity(),
        target_outcome=source.outcomes[0], exception_probability_upper_bound=q,
        exception_payout_floor_fraction=floor,
        basis="OFFLINE scenario bound for the exceptional event states",
        observed_at=now-timedelta(minutes=1), valid_until=now+timedelta(minutes=1))
    return ExceptionalSettlementBound(**(values | changes))


def test_distinct_contracts_can_be_valued_without_declaring_them_identical(market):
    source, target = paired_markets(market)
    evidence = bound(source, target, q="0.02", floor="0")
    result = estimate_bridge(source, target, source.outcomes[0], D("0.60"), evidence)
    assert result.normal_completion_probability == D("0.60")
    assert result.conservative_payout_fraction == D("0.588")
    assert result.execution_eligible is False
    assert source.identity() != target.identity()


def test_unknown_exception_risk_has_no_positive_payout_floor(market):
    source, target = paired_markets(market)
    result = estimate_bridge(source, target, source.outcomes[0], D("0.6"),
                             bound(source, target, q="1", floor="0"))
    assert result.conservative_payout_fraction == 0


def test_endpoint_uses_normal_probability_when_exception_floor_is_higher(market):
    source, target = paired_markets(market)
    result = estimate_bridge(source, target, source.outcomes[0], D("0.6"),
                             bound(source, target, q="0.2", floor="0.8"))
    assert result.conservative_payout_fraction == D("0.6")


@pytest.mark.parametrize("change", ["target_identity", "source_identity", "target_outcome"])
def test_bound_cannot_be_reused_for_other_contract_or_outcome(market, change):
    source, target = paired_markets(market)
    replacement = "0" * 64 if change != "target_outcome" else source.outcomes[1]
    evidence = bound(source, target).model_copy(update={change: replacement})
    with pytest.raises(ValueError, match="exact contracts"):
        estimate_bridge(source, target, source.outcomes[0], D("0.6"), evidence)


def test_stale_bound_and_mismatched_sporting_question_are_rejected(market):
    source, target = paired_markets(market)
    evidence = bound(source, target)
    with pytest.raises(ValueError, match="stale"):
        estimate_bridge(source, target, source.outcomes[0], D("0.6"), evidence,
                        now=evidence.valid_until)
    mismatched = target.model_copy(update={"line": D("26.5")})
    with pytest.raises(ValueError, match="sporting scope"):
        estimate_bridge(source, mismatched, source.outcomes[0], D("0.6"), evidence)


@pytest.mark.parametrize("probability", [float("0.6"), D("NaN"), D("1.1")])
def test_invalid_probability_is_rejected(market, probability):
    source, target = paired_markets(market)
    with pytest.raises(ValueError, match="probability"):
        estimate_bridge(source, target, source.outcomes[0], probability, bound(source, target))


def test_read_only_api_does_not_create_order_or_authorize_execution(tmp_path, market):
    source, target = paired_markets(market)
    body = {"source": source.model_dump(mode="json"), "target": target.model_dump(mode="json"),
        "outcome": source.outcomes[0], "normal_probability": "0.6",
        "exceptional_bound": bound(source, target).model_dump(mode="json")}
    with TestClient(quiet_app(tmp_path / "bridge.db")) as client:
        assert client.post("/api/market-review/bridge-estimate", json=body).status_code == 403
        response = client.post("/api/market-review/bridge-estimate", json=body, headers=HEADERS)
        assert response.status_code == 200
        assert response.json()["conservative_payout_fraction"] == "0.588"
        assert response.json()["execution_eligible"] is False
        assert client.get("/api/native/orders").json()["orders"] == []
