"""One-shot watcher cannot turn a pending launch report into authorization."""
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from scripts import watch_kalshi_one_shot as pilot


FINGERPRINT = "a" * 64
DIGEST = "b" * 64


def state():
    risk = {name: "1" for name in (
        "max_order_stake", "max_event_exposure", "max_player_exposure",
        "max_venue_exposure", "max_correlated_exposure", "max_total_exposure")}
    risk.update({"max_native_order_attempts": 1, "max_open_orders": 1,
        "max_orders_per_minute": 1, "available_cash_only": True,
        "block_overlapping_events": True, "block_overlapping_players": True,
        "stake_sizing": "quarter_kelly", "unit_size_usd": "5",
        "live_max_order_stake": "1", "live_stake_authorized": True})
    return {"mode": "production", "sample_feed_enabled": False,
            "live_enabled": True, "auto_trade": False, "kill_switch": False, "risk": risk}


def setup(*, ready=True):
    return {"scope": {"profile_count": 1}, "launch_scope": [{
        "venue": "kalshi", "environment": "production", "account_fingerprint": FINGERPRINT,
        "validation_pilot": True, "settlement_policy": "kalshi_ioc_binary_halt_on_exception",
        "strategy_ids": ["native_positive_ev"], "markets": [{"id": "m"}],
    }], "risk_policy": {"source": "launch_profile", "applied": True,
        "values": {"max_native_order_attempts": 1, "max_order_stake_usd": "1"}},
        "accounts": [{"venue": "kalshi", "account_snapshot": {
            "current": True, "identity_verified": True, "reconciled": True,
            "cash_available_verified": True, "balance_usd": "50"}}],
        "safe_to_arm": ready, "activation": {"available": ready, "scope_digest": DIGEST}}


def probe():
    return {"read_only": True, "account": {"venue": "kalshi",
        "environment": "production", "identity_verified": True,
        "cash_available_verified": True, "inventory_reconciled": True,
        "trading_permission_verified": True, "open_order_count": 0,
        "position_count": 0, "balance_usd": "50", "account_fingerprint": FINGERPRINT,
        "geographic_attestation_expires_at": (datetime.now(timezone.utc)
            + timedelta(hours=1)).isoformat()}}


def test_pending_launch_never_posts_live_control(monkeypatch):
    calls = []
    def handle(request):
        calls.append((request.method, request.url.path))
        result = {"/api/state": state(), "/api/native/orders": {"orders": []},
                  "/api/launch/setup": setup(ready=False), "/api/launch/probe": probe()}
        return httpx.Response(200, json=result[request.url.path])
    original = httpx.Client
    monkeypatch.setattr(pilot.httpx, "Client", lambda **kwargs: original(
        transport=httpx.MockTransport(handle), **kwargs))
    monkeypatch.setattr(pilot, "_status", lambda *args, **kwargs: None)
    assert pilot.watch("http://127.0.0.1:8002", check_only=True) == "waiting_for_live_checks"
    assert ("POST", "/api/control") not in calls


def test_ready_one_dollar_scope_arms_then_disarms_after_intent(monkeypatch):
    current = state()
    calls = []
    order_reads = 0
    def handle(request):
        nonlocal order_reads
        calls.append((request.method, request.url.path))
        if request.url.path == "/api/control":
            body = __import__("json").loads(request.content)
            if body["auto_trade"]:
                assert body["expected_launch_digest"] == DIGEST
                assert current["auto_trade"] is False
            current["auto_trade"] = body["auto_trade"]
            return httpx.Response(200, json=current)
        if request.url.path == "/api/native/orders":
            order_reads += 1
            return httpx.Response(200, json={"orders": [{"id": "first"}] if order_reads >= 3 else []})
        result = {"/api/state": current, "/api/launch/setup": setup(), "/api/launch/probe": probe()}
        return httpx.Response(200, json=result[request.url.path])
    original = httpx.Client
    monkeypatch.setattr(pilot.httpx, "Client", lambda **kwargs: original(
        transport=httpx.MockTransport(handle), **kwargs))
    monkeypatch.setattr(pilot, "_status", lambda *args, **kwargs: None)
    monkeypatch.setattr(pilot.time, "sleep", lambda _: None)
    assert pilot.watch("http://127.0.0.1:8002", poll_seconds=1) == "attempted"
    assert calls.count(("POST", "/api/control")) == 2
    assert current["auto_trade"] is False


def test_wrong_account_or_uncapped_limits_cannot_arm():
    wrong = setup()
    wrong["launch_scope"][0]["account_fingerprint"] = "c" * 64
    assert pilot._scope_status(wrong, FINGERPRINT)[0] == "wrong_profile_scope"
    uncapped = state()
    uncapped["risk"]["max_native_order_attempts"] = None
    assert not pilot._one_dollar_risk(uncapped, applied=True)
    with pytest.raises(ValueError):
        pilot._local_url("https://kalshi.com")
