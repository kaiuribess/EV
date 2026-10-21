from datetime import timedelta
from decimal import Decimal as D

import pytest
from pydantic import ValidationError

from evplatform.core.config import PricingConfig
from evplatform.core.models import FeeSchedule, Level, Quote, Settlement, utcnow
from evplatform.core.pricing import NoSignal, american_to_decimal, devig, expected_fee, fair_value, opportunity


@pytest.mark.parametrize("american,decimal", [(100, "2"), (150, "2.5"), (-200, "1.5"), (-100, "2")])
def test_american_conversion(american, decimal):
    assert american_to_decimal(D(american)) == D(decimal)


@pytest.mark.parametrize("value", [0, 99, -99, "NaN", "Infinity"])
def test_invalid_american(value):
    with pytest.raises(ValueError):
        american_to_decimal(D(value))


@pytest.mark.parametrize("method", ["proportional", "power"])
def test_devig(method):
    result = devig({"over": D("1.91"), "under": D("1.91")}, method)
    assert abs(result["over"] - D(".5")) < D(".00000001")
    assert abs(sum(result.values()) - 1) < D(".00000001")


def test_power_three_way():
    result = devig({"home": D("2.2"), "draw": D("3.2"), "away": D("3.4")}, "power")
    assert abs(sum(result.values()) - 1) < D(".00000001")
    assert result["home"] > result["draw"] > result["away"]


@pytest.mark.parametrize("bad", [{"home": D(2)}, {"a": D(1), "b": D(2)}, {"a": D("NaN"), "b": D(2)}])
def test_bad_devig(bad):
    with pytest.raises(ValueError):
        devig(bad)


def test_dedup_bookmaker(market, quotes, pricing):
    duplicate = quotes[0].model_copy(update={"provider": "second-pinnacle-feed", "source_time": utcnow()})
    fair = fair_value(market, quotes + [duplicate], pricing)
    assert len(fair.references) == 3
    assert next(r for r in fair.references if r["bookmaker"] == "pinnacle")["provider"] == "second-pinnacle-feed"


def test_duplicate_is_not_independent(market, quotes, pricing):
    with pytest.raises(NoSignal):
        fair_value(market, [quotes[0], quotes[0].model_copy(update={"provider": "second"})], pricing)


@pytest.mark.parametrize("update", [
    {"source_time": None}, {"source_time": timedelta(seconds=-20)},
    {"source_time": timedelta(seconds=20)}, {"status": "suspended"},
    {"status": "withdrawn"},
])
def test_invalid_references(market, quotes, pricing, update):
    if isinstance(update.get("source_time"), timedelta):
        update = {**update, "source_time": utcnow() + update["source_time"]}
    config = pricing.model_copy(update={"required_books": ["circa"]})
    quotes[1] = quotes[1].model_copy(update=update)
    with pytest.raises(NoSignal):
        fair_value(market, quotes, config)


def test_received_at_cannot_refresh_source(market, quotes, pricing):
    stale = [q.model_copy(update={"source_time": utcnow() - timedelta(seconds=30), "received_at": utcnow()}) for q in quotes]
    with pytest.raises(NoSignal):
        fair_value(market, stale, pricing)


def test_disagreement(market, quotes, pricing):
    quotes[0] = quotes[0].model_copy(update={"decimal_odds": {"home": D("1.1"), "away": D("9")}})
    with pytest.raises(NoSignal, match="disagreement"):
        fair_value(market, quotes, pricing)


def test_suspended_market(market, quotes, pricing):
    with pytest.raises(NoSignal):
        fair_value(market.model_copy(update={"status": "suspended"}), quotes, pricing)


def test_weights_and_override(market, quotes, pricing):
    config = PricingConfig(market_overrides={"basketball:moneyline": {"required_books": ["not-present"]}})
    with pytest.raises(NoSignal):
        fair_value(market, quotes, config)
    with pytest.raises(ValidationError):
        PricingConfig(weights={"pinnacle": D(-1)})


def test_fee_models():
    assert expected_fee(FeeSchedule(model="none", verified=True), D(".5"), 10, D(".6")) == 0
    assert expected_fee(FeeSchedule(model="binary_quadratic", rate=D(".07"), verified=True), D(".5"), 10, D(".6")) == D(".18")
    assert expected_fee(FeeSchedule(model="profit_commission", rate=D(".02"), verified=True), D(".5"), 10, D(".6")) == D(".06")
    with pytest.raises(ValueError):
        expected_fee(FeeSchedule(model="none"), D(".5"), 10, D(".6"))


def test_executable_ev_and_depth(market, quotes, pricing, book):
    fair = fair_value(market, quotes, pricing)
    candidate = opportunity(market, fair, book, pricing)
    assert candidate.ev_per_contract > D(".08")
    assert candidate.max_quantity == 50
    assert candidate.price == book.asks[0].price
    assert candidate.size_curve[0]["quantity"] == 50
    worse = book.model_copy(update={"asks": (Level(price=D(".7"), quantity=20),)})
    assert opportunity(market, fair, worse, pricing).ev_per_contract < 0


@pytest.mark.parametrize("update", [{"synchronized": False}, {"status": "suspended"}, {"asks": ()},
                                   {"source_time": utcnow() - timedelta(seconds=50)}])
def test_unexecutable_book(market, quotes, pricing, book, update):
    with pytest.raises(NoSignal):
        opportunity(market, fair_value(market, quotes, pricing), book.model_copy(update=update), pricing)


def test_nonbinary_settlement_requires_extension(market, quotes, pricing, book):
    for settlement in [Settlement(payout=D(".01")), Settlement(push_rule="refund_on_tie")]:
        with pytest.raises(NoSignal):
            opportunity(market.model_copy(update={"settlement": settlement}), fair_value(market, quotes, pricing), book, pricing)

