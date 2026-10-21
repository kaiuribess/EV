"""Offline launch preparation and owner-control regressions; no venue requests."""
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from evplatform.api import create_app
from evplatform.core.config import Settings
from evplatform.core.launch_setup import launch_setup, operator_limits_draft
from evplatform.core.models import utcnow
from evplatform.core.readiness import LiveLimits
from evplatform.engine import Engine
from evplatform.plugins.external import register_external
from evplatform.venues.account_probe import NativeAccountSnapshot

from test_signal_api import HEADERS
from test_native_pricing import setup_native
from test_launch_readiness import ready_fixture


class RunningWorker:
    def done(self): return False
    def cancelled(self): return False


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = Engine(Settings(_env_file=None, database_path=str(tmp_path / "offline.db")))
    register_external(result.registry, result)
    return result


def account(report, venue="novig"):
    return next(row for row in report["accounts"] if row["venue"] == venue)


def test_all_registered_native_monitors_appear_without_launch_profile(engine):
    report = launch_setup(engine)
    assert {row["venue"] for row in report["accounts"]} == {"kalshi", "novig", "prophetx", "polymarket_us"}
    assert not report["activation"]["available"]
    assert report["activation"]["requires_restart"]
    assert report["activation"]["scope_digest"] is None
    assert not report["safe_to_arm"] and not report["profile_draft"]["can_prepare"]
    assert report["scope"]["profile_count"] == 0
    assert report["launch_scope"] == []
    assert not account(report)["profile_configured"]
    assert report["pricing"] == engine.settings.pricing.model_dump(mode="json")
    assert all(row["status"] == "pending" for row in report["requirements"])


def test_future_native_monitor_registration_needs_no_dashboard_name_change(engine):
    row = engine.registry.records.pop("novig")
    engine.registry.records["future_venue"] = row
    assert account(launch_setup(engine), "future_venue")["venue"] == "future_venue"


def test_review_scope_uses_only_profile_whitelist_and_nonsecret_identity(engine, market):
    profile, *_ = ready_fixture("production")
    engine.native.profile = profile.model_copy(update={"venue": "novig",
        "market_ids": (market.id, "not_loaded"), "settlement_policy": "novig_ioc_win_loss_halt_on_exception"})
    engine.markets[market.id] = market
    engine.markets["outside_launch"] = market.model_copy(update={"id": "outside_launch"})
    scope = launch_setup(engine)["launch_scope"]
    assert len(scope) == 1
    assert scope[0]["venue"] == "novig" and scope[0]["environment"] == "production"
    assert scope[0]["account_fingerprint"] == profile.account_fingerprint
    assert scope[0]["provider_ids"] == list(profile.provider_ids)
    assert scope[0]["strategy_ids"] == list(profile.strategy_ids)
    assert [item["id"] for item in scope[0]["markets"]] == [market.id, "not_loaded"]
    assert market.event_name in scope[0]["markets"][0]["name"]
    assert scope[0]["markets"][1] == {"id": "not_loaded", "name": "not_loaded"}
    assert "outside_launch" not in str(scope)


def test_unsupported_settlement_is_visible_even_without_launch_profile(engine):
    report = launch_setup(engine)
    poly = account(report, "polymarket_us")
    assert poly["supported_settlement_policies"] == []
    assert {row["code"] for row in poly["blockers"]} == {"profile.missing", "software.settlement_policy"}
    assert {row["scope"] for row in report["software_blockers"]} == {"polymarket_us"}
    assert account(report)["supported_settlement_policies"] == ["novig_ioc_win_loss_halt_on_exception"]
    assert all(row["settlement_policy"] is None for row in report["accounts"])


def test_profile_full_policy_does_not_inherit_a_narrow_capability(engine):
    profile, *_ = ready_fixture("production")
    engine.native.profile = profile.model_copy(update={"venue": "novig", "settlement_policy": "full"})
    report = launch_setup(engine)
    assert "software.settlement_policy" in {row["code"] for row in account(report)["blockers"]}
    assert not report["activation"]["available"]


def test_stopped_monitor_health_cannot_claim_authenticated_or_streaming(engine):
    record = engine.registry.records["novig"]
    record.enabled = True
    record.health = {"authenticated": True, "streaming": True}
    report = launch_setup(engine)
    assert not account(report)["authenticated"] and not account(report)["streaming"]
    record.plugin.task = RunningWorker()
    assert account(launch_setup(engine))["authenticated"]
    record.enabled = False
    assert not account(launch_setup(engine))["authenticated"]


def test_selected_environment_pair_status_never_uses_legacy_fallback(engine, monkeypatch):
    record = engine.registry.records["novig"]
    record.config = {"environment": "production", "credential_profile": "environment"}
    for key in ("NOVIG_API_KEY", "NOVIG_PRIVATE_KEY_PATH"):
        monkeypatch.setenv(key, "OFFLINE-legacy-secret")
    monkeypatch.setenv("NOVIG_PRODUCTION_TRADING_API_KEY", "OFFLINE-production-secret")
    monkeypatch.delenv("NOVIG_PRODUCTION_TRADING_PRIVATE_KEY_PATH", raising=False)
    assert not account(launch_setup(engine))["credentials_configured"]
    monkeypatch.setenv("NOVIG_PRODUCTION_TRADING_PRIVATE_KEY_PATH", "OFFLINE-secret-path")
    report = launch_setup(engine)
    assert account(report)["credentials_configured"]
    assert "OFFLINE-production-secret" not in str(report)
    assert "OFFLINE-secret-path" not in str(report)


def test_cached_account_without_profile_is_not_current_spendable_cash(engine):
    record = engine.registry.records["novig"]
    record.plugin.client = SimpleNamespace(settings=SimpleNamespace(environment="production"))
    engine.native.account_snapshots["novig"] = NativeAccountSnapshot(
        venue="novig", environment="production", observed_at=utcnow(), identity_verified=True,
        account_fingerprint="a"*64, balance_usd=Decimal("25"), cash_available_verified=True, reconciled=True)
    snapshot = account(launch_setup(engine))["account_snapshot"]
    assert snapshot["available"] and not snapshot["current"]
    assert snapshot["balance_usd"] is None
    assert not snapshot["cash_available_verified"]


@pytest.mark.parametrize("mutation", [{"source_time": None}, {"source_time": "old"},
    {"source_time": "future"}, {"synthetic": True}, {"status": "suspended"}, {"asks": ()},
    {"synchronized": False}])
def test_current_native_book_count_rejects_unusable_books(engine, market, quotes, mutation):
    market, quotes, book, *_ = setup_native(market, quotes)
    if mutation.get("source_time") == "old":
        mutation = {"source_time": utcnow()-timedelta(minutes=1)}
    elif mutation.get("source_time") == "future":
        mutation = {"source_time": utcnow()+timedelta(minutes=1)}
    book = book.model_copy(update={"source_time": utcnow(), "synchronized": True, "synthetic": False})
    engine.native.books[(market.id, "novig", "over")] = book
    assert account(launch_setup(engine))["current_native_books"] == 1
    engine.native.books[(market.id, "novig", "over")] = book.model_copy(update=mutation)
    assert account(launch_setup(engine))["current_native_books"] == 0


def test_saved_limits_are_validated_and_never_reported_applied(engine, tmp_path):
    profile, *_ = ready_fixture("production")
    file = tmp_path / "limits.json"
    file.write_text(profile.limits.model_dump_json())
    limits, error = operator_limits_draft(file)
    report = launch_setup(engine, limits_draft=limits, limits_error=error)
    assert report["risk_policy"]["values"] == profile.limits.model_dump(mode="json")
    assert report["risk_policy"]["source"] == "saved_operator_draft"
    assert not report["risk_policy"]["applied"]
    assert engine.risk.config.live_max_order_stake is None


@pytest.mark.parametrize("contents", ['{"secret":"OFFLINE-secret-value"}', 'bad', 'x'*16385])
def test_invalid_limits_do_not_echo_values(tmp_path, contents):
    file = tmp_path / "limits.json"
    file.write_text(contents)
    limits, error = operator_limits_draft(file)
    assert limits is None and error
    assert "OFFLINE-secret-value" not in error


def native_app(tmp_path):
    app = create_app(Settings(_env_file=None, mode="production", database_path=str(tmp_path / "api.db")))
    return app


def test_setup_endpoint_is_read_only_with_unapplied_limits(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with TestClient(native_app(tmp_path)) as client:
        engine = client.app.state.engine
        before = (engine.settings.live_enabled, engine.risk.auto_trade, engine.risk.kill_switch)
        report = client.get("/api/launch/setup")
        assert report.status_code == 200
        assert not report.json()["activation"]["available"]
        assert not report.json()["profile_draft"]["can_prepare"]
        assert before == (engine.settings.live_enabled, engine.risk.auto_trade, engine.risk.kill_switch)
        assert client.get("/api/native/orders").json()["orders"] == []


def test_rejected_combined_activation_preserves_emergency_stop(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with TestClient(native_app(tmp_path)) as client:
        engine = client.app.state.engine
        engine.risk.kill_switch = True
        response = client.post("/api/control", headers=HEADERS, json={"auto_trade": True, "kill_switch": False})
        assert response.status_code == 409
        assert engine.risk.kill_switch and not engine.risk.auto_trade


def test_review_digest_mismatch_rejected_before_changing_stop(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with TestClient(native_app(tmp_path)) as client:
        engine = client.app.state.engine
        report = engine.native.report().model_copy(update={"safe_to_arm": True, "profile_digest": "a"*64})
        engine.native.report = lambda *args: report
        engine.settings.live_enabled = True
        engine.risk.kill_switch = True
        response = client.post("/api/control", headers=HEADERS, json={"auto_trade": True,
            "kill_switch": False, "expected_launch_digest": "b"*64})
        assert response.status_code == 409
        assert engine.risk.kill_switch and not engine.risk.auto_trade


def test_gate_change_during_reset_restores_saved_emergency_stop(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with TestClient(native_app(tmp_path)) as client:
        engine = client.app.state.engine
        base = engine.native.report()
        healthy = base.model_copy(update={"safe_to_arm": True, "profile_digest": "a"*64})
        failed = base.model_copy(update={"profile_digest": "a"*64})
        engine.native.report = lambda *args: healthy
        engine.settings.live_enabled = True
        engine.risk.kill_switch = True
        original_save = engine.storage.save_config
        saved = []
        async def interrupted_save(key, value):
            saved.append((key, value))
            await original_save(key, value)
            if key == "kill_switch" and value is False:
                engine.native.report = lambda *args: failed
        engine.storage.save_config = interrupted_save
        response = client.post("/api/control", headers=HEADERS, json={"auto_trade": True,
            "kill_switch": False, "expected_launch_digest": "a"*64})
        assert response.status_code == 409
        assert engine.risk.kill_switch and not engine.risk.auto_trade
        assert saved == [("kill_switch", False), ("kill_switch", True)]


def test_matching_review_digest_allows_operator_control_in_offline_fixture(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with TestClient(native_app(tmp_path)) as client:
        engine = client.app.state.engine
        report = engine.native.report().model_copy(update={"safe_to_arm": True, "profile_digest": "a"*64})
        engine.native.report = lambda *args: report
        engine.settings.live_enabled = True
        engine.risk.kill_switch = True
        response = client.post("/api/control", headers=HEADERS, json={"auto_trade": True,
            "kill_switch": False, "expected_launch_digest": "a"*64})
        assert response.status_code == 200
        assert not engine.risk.kill_switch and engine.risk.auto_trade
        assert client.get("/api/native/orders").json()["orders"] == []


def test_activation_failure_cannot_erase_new_safety_halt(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with TestClient(native_app(tmp_path)) as client:
        engine = client.app.state.engine
        base = engine.native.report()
        healthy = base.model_copy(update={"safe_to_arm": True, "profile_digest": "a"*64})
        engine.native.report = lambda *args: healthy
        engine.settings.live_enabled = True
        engine.risk.kill_switch = False
        original_save = engine.storage.save_config
        async def interrupted_save(key, value):
            await original_save(key, value)
            if key == "kill_switch" and value is False:
                engine.risk.halt("OFFLINE new safety halt")
                engine.native.report = lambda *args: base
        engine.storage.save_config = interrupted_save
        response = client.post("/api/control", headers=HEADERS, json={"auto_trade": True,
            "kill_switch": False, "expected_launch_digest": "a"*64})
        assert response.status_code == 409
        assert engine.risk.kill_switch and engine.risk.kill_reason == "OFFLINE new safety halt"
        assert not engine.risk.auto_trade
