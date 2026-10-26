"""A reviewed venue lease never grants placement rights to sibling feeds."""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from evplatform.core.config import RiskConfig, Settings
from evplatform.core.models import utcnow
from evplatform.core.native_runtime import NativeRuntime
from evplatform.core.portfolio import LaunchPortfolio
from evplatform.core.risk import RiskEngine, RiskRejected
from evplatform.engine import Engine

from test_launch_portfolio import accounts


def test_selected_portfolio_venue_can_be_ready_while_sibling_is_blocked():
    first, second, first_facts, second_facts = accounts()
    runtime = NativeRuntime(Engine(Settings()))
    runtime.portfolio = LaunchPortfolio(profiles=(first, second))
    facts = {first.venue: first_facts,
             second.venue: second_facts.model_copy(update={"unresolved_unknown_orders": 1})}
    runtime.runtime_facts = lambda venue=None: facts[venue]

    assert runtime.report(venues={first.venue}).safe_to_arm
    assert runtime.scope_digest({first.venue}) == first.digest()
    assert not runtime.report(venues={first.venue, second.venue}).safe_to_arm
    assert not runtime.report().safe_to_arm


def test_unselected_venue_cannot_reserve_and_emergency_stop_revokes_all():
    risk = RiskEngine(RiskConfig())
    risk.auto_trade = True
    risk.native_enabled_venues = frozenset({"novig"})
    order = SimpleNamespace(mode="production", execution_origin="automatic", venue="prophetx")
    with pytest.raises(RiskRejected, match="outside the active execution lease"):
        risk.check_native_reservation(order)
    risk.halt("OFFLINE emergency stop")
    assert risk.kill_switch and not risk.auto_trade
    assert risk.native_enabled_venues == frozenset()


@pytest.mark.asyncio
async def test_control_arms_only_selected_reviewed_venue_without_arming_sibling():
    first, second, _, _ = accounts()
    engine = Engine(Settings(mode="production", live_enabled=True, _env_file=None))
    engine.native.portfolio = LaunchPortfolio(profiles=(first, second))
    engine.risk.kill_switch = True
    engine.storage.save_config = AsyncMock()
    engine.notice = AsyncMock()
    selected = []

    def report(*, venues):
        selected.append(frozenset(venues))
        return SimpleNamespace(safe_to_arm=set(venues) == {first.venue},
            profile_digest=engine.native.scope_digest(venues))

    engine.native.report = report
    expiry = utcnow() + timedelta(minutes=5)
    await engine.control(auto_trade=True, kill_switch=False, native_venues=[first.venue],
        expected_launch_digest=first.digest(), authorization_expires_at=expiry)
    assert selected == [frozenset({first.venue}), frozenset({first.venue})]
    assert engine.risk.auto_trade and not engine.risk.kill_switch
    assert engine.risk.native_enabled_venues == {first.venue}
    with pytest.raises(ValueError, match="Pause native automation"):
        await engine.control(auto_trade=True, native_venues=[second.venue],
            expected_launch_digest=second.digest(), authorization_expires_at=expiry)
    assert engine.risk.native_enabled_venues == {first.venue}
    await engine.control(auto_trade=False, kill_switch=True)
    assert engine.risk.native_enabled_venues == frozenset()
