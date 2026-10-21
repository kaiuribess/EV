"""Legacy blends remain usable offline without implicitly authorizing money."""
import pytest

from evplatform.core.config import PricingConfig
from evplatform.core.pricing import NoSignal, fair_value, validate_fair_references
from evplatform.core.native_pricing import native_opportunity
from test_native_pricing import setup_native


def test_legacy_blend_does_not_grant_implicit_live_permission(market, quotes):
    config = PricingConfig()
    fair = fair_value(market, quotes, config)
    assert fair.execution_eligible  # Paper/research behavior remains available.
    assert fair.reference_profile_id is None
    assert not fair.live_execution_allowed


def test_unverified_real_quotes_cannot_authorize_production_through_legacy_mode(market, quotes):
    market, quotes, book, fair, config, now = setup_native(market, quotes)
    assert not any(quote.observation_verified or quote.market_status_verified for quote in quotes)
    with pytest.raises(NoSignal, match="production"):
        native_opportunity(market, fair, book, config, max_stake_usd=1, mode="production", now=now)


def test_editing_the_cached_permission_flag_does_not_bypass_current_policy(market, quotes):
    market, _, book, fair, config, now = setup_native(market, quotes)
    forged = fair.model_copy(update={"live_execution_allowed": True})
    with pytest.raises(NoSignal, match="permission"):
        validate_fair_references(market, forged, config, now)
    with pytest.raises(NoSignal, match="permission"):
        native_opportunity(market, forged, book, config, max_stake_usd=1, mode="production", now=now)
