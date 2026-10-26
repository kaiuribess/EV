"""A recurring lease cannot skip the launch gate or revive an emergency stop."""
from datetime import datetime, timedelta, timezone
import json

import httpx

from scripts import watch_native_one_shot as one_shot
from scripts import watch_native_recurring as gate
from tests.test_watch_native_one_shot import state, setup, DIGEST


NOW = datetime(2026, 10, 26, 18, 0, tzinfo=timezone.utc)
CLIENT = httpx.Client


def authorization():
    return {"schema_version": 1, "authorization_id": "reviewed-recurring-20261026",
        "issued_at": (NOW-timedelta(hours=1)).isoformat(),
        "expires_at": (NOW+timedelta(hours=1)).isoformat(),
        "launch_scope_digest": DIGEST, "maximum_native_attempts": None,
        "cash_only": True, "stake_sizing": "quarter_kelly", "unit_size_usd": "5",
        "minimum_ev": "0.015", "uncertainty_probability": "0.01",
        "venues": ["novig"]}


def recurring_state(*, armed=False):
    result = state(armed=armed)
    result["risk"].update(max_native_order_attempts=None,
        max_order_stake=None, live_max_order_stake=None,
        max_total_exposure=None, max_venue_exposure=None)
    return result


def recurring_setup(*, ready=True):
    result = setup(ready=ready)
    result["launch_scope"][0]["validation_pilot"] = False
    result["launch_scope"][0]["settlement_policy"] = "full"
    result["risk_policy"]["values"].update(max_native_order_attempts=None,
        max_order_stake_usd=None, available_cash_only=True,
        stake_sizing="quarter_kelly", unit_size_usd="5")
    result["pricing"]["trusted_pilot_policy"].update(
        recurring_authorization=True, maximum_native_attempts=None,
        automatic_followups=True, max_debit_usd=None, max_loss_usd=None)
    return result


def server(monkeypatch, tmp_path, *, armed=False, ready=True, orders=None):
    auth = tmp_path / "recurring.json"
    auth.write_text(json.dumps(authorization()), encoding="utf-8")
    monkeypatch.setattr(gate, "AUTH_PATH", auth)
    monkeypatch.setattr(gate, "STATUS_PATH", tmp_path / "status.json")
    monkeypatch.setattr(one_shot, "AUTH_PATH", tmp_path / "missing-one-shot.json")
    monkeypatch.setattr(one_shot, "LOCK_PATH", tmp_path / "controller.lock")
    current, launch, calls = recurring_state(armed=armed), recurring_setup(ready=ready), []
    def handle(request):
        calls.append((request.method, request.url.path))
        if request.url.path == "/api/control":
            body = json.loads(request.content)
            if body["auto_trade"]:
                assert body["expected_launch_digest"] == DIGEST
                assert body["authorization_expires_at"] == authorization()["expires_at"]
            current.update(auto_trade=body["auto_trade"], kill_switch=body["kill_switch"],
                           native_authorization_expires_at=body.get("authorization_expires_at"))
            return httpx.Response(200, json=current)
        return httpx.Response(200, json={
            "/api/state": current, "/api/launch/setup": launch,
            "/api/native/orders": {"orders": orders or []}}[request.url.path])
    monkeypatch.setattr(gate.httpx, "Client", lambda **kwargs: CLIENT(
        transport=httpx.MockTransport(handle), **kwargs))
    return current, launch, calls


def test_recurring_lease_arms_once_and_never_rearms_after_emergency_stop(monkeypatch, tmp_path):
    current, _, calls = server(monkeypatch, tmp_path)
    assert gate.tick(now=NOW, check_only=True) == "ready"
    assert ("POST", "/api/control") not in calls
    assert gate.tick(now=NOW) == "armed"
    assert current["auto_trade"] and not current["kill_switch"]
    assert gate.tick(now=NOW) == "armed"
    assert calls.count(("POST", "/api/control")) == 1
    current.update(auto_trade=False, kill_switch=True)
    assert gate.tick(now=NOW) == "interrupted"
    assert calls.count(("POST", "/api/control")) == 1


def test_recurring_scope_and_uncertain_inventory_block_activation(monkeypatch, tmp_path):
    current, launch, calls = server(monkeypatch, tmp_path)
    launch["pricing"]["trusted_pilot_policy"]["owner_approved"] = False
    assert gate.tick(now=NOW) == "waiting_for_recurring_source_review"
    assert ("POST", "/api/control") not in calls
    launch["pricing"]["trusted_pilot_policy"]["owner_approved"] = True
    launch["activation"]["scope_digest"] = "e" * 64
    assert gate.tick(now=NOW) == "reviewed_launch_digest_changed"
    launch["activation"]["scope_digest"] = DIGEST
    assert gate.tick(now=NOW) == "armed"
    current.update(auto_trade=True, kill_switch=False)
    # An unknown transmission must be reconciled before the next order.
    old = gate.shared._get
    gate.shared._get = lambda client, path: ({"orders": [{"status": "unknown"}]}
        if path == "/api/native/orders" else old(client, path))
    try:
        assert gate.tick(now=NOW) == "blocked"
    finally:
        gate.shared._get = old
    assert current["kill_switch"] and not current["auto_trade"]


def test_recurring_expiry_disarms_without_stopping_research(monkeypatch, tmp_path):
    current, _, calls = server(monkeypatch, tmp_path, armed=True)
    assert gate.tick(now=NOW+timedelta(hours=2)) == "expired"
    assert current["kill_switch"] and not current["auto_trade"]
    assert ("POST", "/api/control") in calls
