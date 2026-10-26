"""Portfolio scope/recovery tests use offline fixtures only, never venue access."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
from decimal import Decimal
import hashlib

import pytest
from pydantic import ValidationError

from evplatform.core.config import Settings
from evplatform.core.external_inventory import ExternalInventoryFile, PendingExternalOrder
from evplatform.core.native_runtime import NativeRuntime
from evplatform.core.portfolio import LaunchPortfolio
from evplatform.engine import Engine

from test_launch_readiness import ready_fixture
from test_native_oms import system
from test_native_oms import order, Adapter
from evplatform.core.risk import RiskRejected
from test_native_runtime_maintenance import runtime_for


def accounts():
    a, af, _ = ready_fixture()
    b = a.model_copy(update={"venue": "second", "account_fingerprint": "e"*64})
    b = b.model_copy(update={"evidence": tuple(e.model_copy(update={"venue": b.venue,
        "account_fingerprint": b.account_fingerprint, "profile_digest": b.digest()}) for e in a.evidence)})
    bf = af.model_copy(update={"venue": b.venue, "account_fingerprint": b.account_fingerprint})
    return a, b, af, bf


@pytest.mark.parametrize("change", ["venue", "account_fingerprint", "environment", "limits", "build_id",
                                   "mapping_digest", "pricing_digest", "strategy_ids"])
def test_conflicting_accounts_and_global_policy_are_rejected(change):
    a, b, _, _ = accounts()
    value = {"venue": a.venue, "account_fingerprint": a.account_fingerprint, "environment": "sandbox",
        "limits": a.limits.model_copy(update={"max_open_orders": 9}), "build_id": "changed",
        "mapping_digest": "f"*64, "pricing_digest": "f"*64, "strategy_ids": ("other",)}[change]
    with pytest.raises(ValidationError):
        LaunchPortfolio(profiles=(a, b.model_copy(update={change: value})))


def test_all_active_accounts_must_pass_and_reports_remain_separate():
    a, b, af, bf = accounts()
    runtime = NativeRuntime(Engine(Settings()))
    runtime.portfolio = LaunchPortfolio(profiles=(a, b))
    facts = {a.venue: af, b.venue: bf}
    runtime.runtime_facts = lambda venue=None: facts[venue]
    assert runtime.report().live_trading_ready
    assert set(runtime.reports()) == {a.venue, b.venue}
    facts[b.venue] = bf.model_copy(update={"unresolved_unknown_orders": 1})
    assert not runtime.report().safe_to_arm
    assert runtime.report(a.venue).safe_to_arm
    assert any(block.scope.startswith("second/") for block in runtime.report().blockers)
    runtime.profile_error = "Invalid local bundle"
    assert not runtime.report(a.venue).safe_to_arm


def test_profile_lookup_never_borrows_another_venues_account():
    a, b, _, _ = accounts()
    runtime = NativeRuntime(Engine(Settings()))
    runtime.portfolio = LaunchPortfolio(profiles=(a, b))
    assert runtime.profile_for(b.venue) is b
    assert runtime.profile_for("unapproved") is None
    runtime.execution_lease = SimpleNamespace(held=True, scope=("portfolio", "production"))
    assert runtime.owns_execution_lease()
    runtime.execution_lease.scope = (a.venue, "production")
    assert not runtime.owns_execution_lease()


async def test_both_launch_files_fail_closed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path/"data").mkdir()
    a, b, _, _ = accounts()
    (tmp_path/"data/launch_profile.json").write_text(a.model_dump_json())
    (tmp_path/"data/launch_portfolio.json").write_text(LaunchPortfolio(profiles=(a,b)).model_dump_json())
    runtime = NativeRuntime(Engine(Settings(data_dir=str(tmp_path / "data"))))
    runtime.manager.start = AsyncMock()
    await runtime.start()
    assert "conflicting" in runtime.profile_error
    assert not runtime.profiles_by_venue() and runtime.execution_lease is None
    assert not runtime.report().safe_to_arm


async def test_pending_external_raw_order_blocks_production_execution(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    receipt = data / "kalshi_raw_smoke_attempt.json"
    receipt.write_text('{"order_id":"external-1","status":"filled_unsettled"}', encoding="utf-8")
    row = PendingExternalOrder(venue="kalshi", native_order_id="external-1",
        source_artifact=receipt.name, source_sha256=hashlib.sha256(receipt.read_bytes()).hexdigest())
    (data / "external_native_positions.json").write_text(
        ExternalInventoryFile(pending_orders=(row,)).model_dump_json(), encoding="utf-8")
    engine = Engine(Settings(mode="production", data_dir=str(data),
        database_path=str(tmp_path / "native.db")))
    runtime = NativeRuntime(engine)
    runtime.manager.start = AsyncMock()
    await runtime.start()
    assert len(runtime.pending_external_orders) == 1
    assert runtime.profile_error == ""
    assert runtime.engine.risk.pending_external_orders == (row,)
    assert not runtime.runtime_facts().external_exposure_bounded
    assert "runtime.external_exposure_bounded" in {b.code for b in runtime.report().blockers}
    assert not runtime.report().safe_to_arm


async def test_portfolio_refresh_visits_each_account_under_one_coordinator(system):
    runtime = runtime_for(system)
    a, b, _, _ = accounts()
    runtime.portfolio = LaunchPortfolio(profiles=(a,b))
    runtime.mapper = object()
    runtime._refresh_account = AsyncMock()
    await runtime.refresh_account()
    assert {call.args[0].venue for call in runtime._refresh_account.await_args_list} == {a.venue,b.venue}


def scoped_portfolio(runtime, market):
    a, _, _ = ready_fixture("sandbox")
    a = a.model_copy(update={"venue": "novig", "market_ids": (market.id,), "strategy_ids": ("native_positive_ev",)})
    b = a.model_copy(update={"venue": "second", "account_fingerprint": "e"*64})
    runtime.portfolio = LaunchPortfolio(profiles=(a,b))
    runtime.report = lambda: SimpleNamespace(live_trading_ready=False, sandbox_trading_ready=True)
    runtime.engine.risk.config.available_cash_only = True
    runtime.account_snapshots = {"novig": SimpleNamespace(cash_available_verified=True, balance_usd=Decimal(".1")),
        "second": SimpleNamespace(cash_available_verified=True, balance_usd=Decimal("100"))}


async def test_funded_other_account_cannot_cover_insufficient_target_cash(system, market):
    runtime = runtime_for(system)
    scoped_portfolio(runtime, market)
    candidate = order(market)
    with pytest.raises(RiskRejected, match="cash balance"):
        runtime.require_preflight(candidate)
    runtime.account_snapshots["novig"].balance_usd = Decimal("5")
    runtime.require_preflight(candidate)
    wrong = candidate.model_copy(update={"account_fingerprint": "e"*64})
    with pytest.raises(RiskRejected, match="authorized launch scope"):
        runtime.require_preflight(wrong)


async def test_cross_venue_concurrent_candidates_share_one_game_lock(system, market):
    import asyncio
    manager, risk, _, first = system
    risk.config.block_overlapping_events = True
    second = Adapter(risk)
    second.bound_account_fingerprint = "e"*64
    manager.register("second", second)
    other = order(market, venue="second").model_copy(update={"account_fingerprint": "e"*64})
    outcomes = await asyncio.gather(manager.execute(order(market)), manager.execute(other), return_exceptions=True)
    assert len(first.submissions)+len(second.submissions) == 1
    assert sum(isinstance(item, RiskRejected) for item in outcomes) == 1
    assert len(risk.native_orders) == 1


def test_api_reports_each_selected_account_and_rejects_unknown_selection(tmp_path):
    from fastapi.testclient import TestClient
    from test_signal_api import quiet_app
    app = quiet_app(tmp_path/"portfolio-api.db")
    with TestClient(app) as client:
        a, b, _, _ = accounts()
        app.state.engine.native.portfolio = LaunchPortfolio(profiles=(a,b))
        report = client.get("/api/readiness").json()
        assert report["venue"] == "portfolio"
        assert set(report["accounts"]) == {a.venue, b.venue}
        assert not report["safe_to_arm"]
        facts = client.get("/api/readiness/runtime", params={"venue": b.venue}).json()
        assert facts["venue"] == b.venue and set(facts["accounts"]) == {a.venue,b.venue}
        assert client.get("/api/readiness/runtime?venue=unknown").status_code == 404
        assert client.get("/api/native/orders").json()["orders"] == []
