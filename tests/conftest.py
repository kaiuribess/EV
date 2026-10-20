from datetime import timedelta
from decimal import Decimal

import pytest

from evplatform.core.config import PricingConfig
from evplatform.core.models import FeeSchedule, Level, OrderBook, Quote, utcnow
from evplatform.plugins.demo import sample_markets


@pytest.fixture
def market():
    return sample_markets(utcnow())[0]


@pytest.fixture
def quotes(market):
    return [Quote(market_id=market.id, provider=f"feed-{i}", bookmaker=book,
                  decimal_odds={"home": Decimal("1.80"), "away": Decimal("2.10")},
                  source_time=utcnow() - timedelta(milliseconds=100), synthetic=True)
            for i, book in enumerate(("pinnacle", "circa", "bookmaker"))]


@pytest.fixture
def book(market):
    return OrderBook(market_id=market.id, venue="kalshi_paper", outcome="home",
                     asks=(Level(price=Decimal(".45"), quantity=50),), sequence=1,
                     source_time=utcnow(), synthetic=True,
                     fee=FeeSchedule(model="fixed", per_contract=Decimal(".004"), verified=True, source="test"))


@pytest.fixture
def pricing():
    return PricingConfig()

