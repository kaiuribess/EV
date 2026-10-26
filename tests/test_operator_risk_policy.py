"""Operator $5/cash-only/no-overlap configuration; entirely offline."""
from datetime import timedelta
from decimal import Decimal

import pytest

from evplatform.core.config import RiskConfig
from evplatform.core.models import OrderStatus, utcnow
from evplatform.core.readiness import LiveLimits
from evplatform.core.risk import RiskEngine, RiskRejected
from test_position_conflicts import risk, candidate, fresh
from test_native_oms import order


def test_explicitly_disabled_daily_loss_preserves_other_guards():
    engine = RiskEngine(RiskConfig(daily_loss_limit=None))
    engine.auto_trade = True
    engine.daily_realized = Decimal("-100000")
    engine.check_enabled()
    engine.halt("OFFLINE")
    with pytest.raises(RiskRejected, match="kill switch"):
        engine.check_enabled()


def test_fixed_daily_stop_still_works():
    engine = RiskEngine(RiskConfig(daily_loss_limit="25"))
    engine.auto_trade = True
    engine.daily_realized = Decimal("-25")
    with pytest.raises(RiskRejected, match="Daily realized"):
        engine.check_enabled()


def test_optional_exposure_caps_require_explicit_cash_enforcement():
    with pytest.raises(ValueError):
        RiskConfig(max_venue_exposure=None, max_total_exposure=None)
    config = RiskConfig(available_cash_only=True, max_venue_exposure=None, max_total_exposure=None,
                        daily_loss_limit=None, max_order_stake="5", live_max_order_stake="5")
    assert config.max_order_stake == 5 and config.daily_loss_limit is None
    limits = LiveLimits(max_order_stake_usd="5", max_event_exposure_usd="5",
        max_player_exposure_usd="5", max_correlated_exposure_usd="5", max_venue_exposure_usd=None,
        max_total_exposure_usd=None, daily_loss_limit_usd=None, available_cash_only=True,
        block_overlapping_events=True, block_overlapping_players=True, max_open_orders=20, max_orders_per_minute=30)
    assert limits.daily_loss_limit_usd is None
    with pytest.raises(ValueError):
        LiveLimits.model_validate({**limits.model_dump(), "available_cash_only": False})


def test_same_game_different_market_blocks_across_accounts(risk, candidate, market, book):
    risk.config.block_overlapping_events = True
    risk.reserve(candidate, market, book, "paper")
    changed = market.model_copy(update={"id": "total-market", "market_type": "total", "line": Decimal("200.5")})
    second, depth = fresh(candidate, book, changed, venue="second-account")
    with pytest.raises(RiskRejected, match="Overlapping event"):
        risk.reserve(second, changed, depth, "paper")


def test_same_player_different_event_blocks(risk, candidate, market, book):
    risk.config.block_overlapping_players = True
    first = market.model_copy(update={"player_id": "verified-player"})
    first_candidate, first_book = fresh(candidate, book, first)
    risk.reserve(first_candidate, first, first_book, "paper")
    second = first.model_copy(update={"id": "doubleheader", "event_id": "event2"})
    next_candidate, next_book = fresh(candidate, book, second, venue="second-account")
    with pytest.raises(RiskRejected, match="Overlapping player"):
        risk.reserve(next_candidate, second, next_book, "paper")


def test_cancelled_partial_keeps_event_locked_until_verified_settlement(risk, candidate, market, book):
    risk.config.block_overlapping_events = True
    existing = risk.reserve(candidate, market, book, "paper")
    risk.orders[existing.id] = existing.model_copy(update={"status": OrderStatus.CANCELLED, "filled_quantity": 1})
    changed = market.model_copy(update={"id": "other-type", "market_type": "total", "line": Decimal("220.5")})
    second, depth = fresh(candidate, book, changed)
    with pytest.raises(RiskRejected, match="Overlapping event"):
        risk.reserve(second, changed, depth, "paper")
    risk.settled.add(existing.id)
    assert risk.reserve(second, changed, depth, "paper")


def test_native_cash_policy_still_enforces_five_dollar_all_in_cap(market):
    config = RiskConfig(available_cash_only=True, max_venue_exposure=None, max_total_exposure=None,
                        daily_loss_limit=None, max_order_stake="5", live_max_order_stake="5")
    risk = RiskEngine(config)
    risk.auto_trade = True
    risk.native_authorization_expires_at = utcnow() + timedelta(minutes=5)
    valid = order(market)
    risk.native_enabled_venues = frozenset({valid.venue})
    risk.check_native_reservation(valid)  # Cash/preflight is a separate required OMS check.
    too_large = valid.model_copy(update={"intent": valid.intent.model_copy(update={"quantity": Decimal("1200")})})
    with pytest.raises(RiskRejected, match="stake limit"):
        risk.check_native_reservation(too_large)
