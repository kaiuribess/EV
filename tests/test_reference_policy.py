"""Reference universe and cached-evidence regressions; all prices are offline fixtures."""
from datetime import timedelta
from decimal import Decimal as D

import pytest
from pydantic import ValidationError

from evplatform.core.config import PricingConfig
from evplatform.core.models import utcnow
from evplatform.core.pricing import NoSignal, fair_value, market_config, opportunity


def test_eligibility_filters_before_count_consensus_and_disagreement(market, quotes):
    quotes[2] = quotes[2].model_copy(update={"decimal_odds": {"home": D("1.1"), "away": D("9")}})
    config = PricingConfig(eligible_books=["pinnacle", "circa"], required_books=["pinnacle"])
    fair = fair_value(market, quotes, config)
    assert {r["bookmaker"] for r in fair.references} == {"pinnacle", "circa"}
    assert fair.probabilities["home"] == D("7") / D("13")
    assert all(r["weight"] == "1" for r in fair.references)
    with pytest.raises(NoSignal, match="Insufficient"):
        fair_value(market, quotes, config.model_copy(update={"min_references": 3}))


def test_empty_eligibility_means_no_signal_and_null_preserves_legacy(market, quotes):
    with pytest.raises(NoSignal, match="Insufficient"):
        fair_value(market, quotes, PricingConfig(eligible_books=[]))
    assert len(fair_value(market, quotes, PricingConfig(eligible_books=None)).references) == 3


def test_duplicate_vendor_cannot_fill_eligible_reference_count(market, quotes):
    clone = quotes[0].model_copy(update={"provider": "backup-provider", "source_time": utcnow()})
    with pytest.raises(NoSignal, match="Insufficient"):
        fair_value(market, [quotes[0], clone, quotes[1]], PricingConfig(eligible_books=["pinnacle"]))


@pytest.mark.parametrize("config", [
    {"eligible_books": ["pinnacle"], "required_books": ["circa"]},
    {"eligible_books": [], "required_books": ["pinnacle"]},
    {"eligible_books": ["pinnacle", "pinnacle"]},
    {"required_books": ["circa", "circa"]},
    {"eligible_books": [" pinnacle"]},
    {"required_books": [""]},
    {"weights": {"pinnacle ": "1"}},
])
def test_invalid_reference_policy_is_rejected(config):
    with pytest.raises(ValidationError):
        PricingConfig(**config)


def test_overrides_resolve_independently_without_cross_sport_revalidation(market):
    config = PricingConfig(market_overrides={
        "basketball:moneyline": {"eligible_books": ["pinnacle"], "required_books": ["pinnacle"], "min_references": 1},
        "baseball:moneyline": {"required_books": ["circa"]},
    })
    basketball = market_config(config, market)
    assert basketball.required_books == ["pinnacle"]
    assert basketball.eligible_books == ["pinnacle"]
    baseball = market_config(config, market.model_copy(update={"sport": "baseball"}))
    assert baseball.required_books == ["circa"]
    assert baseball.eligible_books is None
    assert config.eligible_books is None


@pytest.mark.parametrize("clock", ["provider_time", "received_at"])
def test_future_delivery_clock_does_not_qualify_reference(market, quotes, clock):
    now = utcnow()
    quotes[0] = quotes[0].model_copy(update={clock: now + timedelta(seconds=1)})
    with pytest.raises(NoSignal, match="Required reference"):
        fair_value(market, quotes, PricingConfig(required_books=["pinnacle"]), now)


@pytest.mark.parametrize("bookmaker", ["", " pinnacle", "pinnacle "])
def test_unreviewed_blank_or_whitespace_book_is_not_independent_evidence(market, quotes, bookmaker):
    quote = quotes[1].model_copy(update={"bookmaker": bookmaker})
    with pytest.raises(NoSignal, match="Insufficient"):
        fair_value(market, [quotes[0], quote], PricingConfig())


def test_cached_fair_source_ages_cannot_be_refreshed_by_calculation_time(market, quotes, book):
    config = PricingConfig()
    now = utcnow()
    fair = fair_value(market, quotes, config, now)
    later = now + timedelta(seconds=6)
    fair = fair.model_copy(update={"calculated_at": later})
    book = book.model_copy(update={"source_time": later, "received_at": later})
    with pytest.raises(NoSignal, match="reference expired"):
        opportunity(market, fair, book, config, later)


@pytest.mark.parametrize("policy,error", [
    ({"eligible_books": ["pinnacle", "circa"]}, "no longer eligible"),
    ({"required_books": ["fanduel"]}, "requirements"),
    ({"min_references": 4}, "requirements"),
    ({"weights": {"pinnacle": D("2")}}, "weight changed"),
    ({"devig_method": "power"}, "method changed"),
])
def test_cached_fair_requires_recalculation_after_reference_policy_change(market, quotes, book, policy, error):
    config = PricingConfig()
    fair = fair_value(market, quotes, config)
    updated = PricingConfig.model_validate(config.model_dump() | policy)
    with pytest.raises(NoSignal, match=error):
        opportunity(market, fair, book, updated)


@pytest.mark.parametrize("status", ["closed", "suspended"])
def test_cached_fair_cannot_price_inactive_market(market, quotes, book, status):
    config = PricingConfig()
    fair = fair_value(market, quotes, config)
    with pytest.raises(NoSignal, match="suspended or closed"):
        opportunity(market.model_copy(update={"status": status}), fair, book, config)


@pytest.mark.parametrize("changes", [
    {"probabilities": {"home": D("NaN"), "away": D("0.5")}},
    {"probabilities": {"home": D("0.7"), "away": D("0.7")}},
    {"disagreement": D("NaN")},
    {"calculated_at": utcnow().replace(tzinfo=None)},
])
def test_invalid_cached_model_evidence_fails_closed(market, quotes, book, changes):
    config = PricingConfig()
    fair = fair_value(market, quotes, config).model_copy(update=changes)
    with pytest.raises(NoSignal):
        opportunity(market, fair, book, config)


def test_cached_duplicate_evidence_fails_closed(market, quotes, book):
    config = PricingConfig()
    fair = fair_value(market, quotes, config)
    fair = fair.model_copy(update={"references": (fair.references[0], fair.references[0])})
    with pytest.raises(NoSignal, match="double-counts"):
        opportunity(market, fair, book, config)


@pytest.mark.parametrize("clock", ["provider_time", "received_at"])
def test_future_book_delivery_clock_fails_closed(market, quotes, book, clock):
    config = PricingConfig()
    now = utcnow()
    fair = fair_value(market, quotes, config, now)
    book = book.model_copy(update={clock: now + timedelta(seconds=1)})
    with pytest.raises(NoSignal, match="future-dated"):
        opportunity(market, fair, book, config, now)
