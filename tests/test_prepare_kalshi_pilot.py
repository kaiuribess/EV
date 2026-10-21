"""The operator pilot helper must fail closed before changing native limits."""

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from scripts import prepare_kalshi_pilot as pilot


def _client(monkeypatch, *, auto_trade=False, orders=(), authenticated=True, profile_count=0):
    calls = []
    risk = {**pilot.RUNTIME_LIMITS}
    state = {
        "mode": "production", "sample_feed_enabled": False,
        "auto_trade": auto_trade, "kill_switch": False,
        "plugins": [{"manifest": {"name": "kalshi"}, "enabled": True,
                     "config": {"environment": "production"},
                     "health": {"authenticated": authenticated, "streaming": True}}],
        "risk": risk,
    }

    def handle(request):
        calls.append((request.method, request.url.path))
        route = (request.method, request.url.path)
        if route == ("GET", "/api/state"):
            payload = state
        elif route == ("GET", "/api/native/orders"):
            payload = {"orders": list(orders)}
        elif route == ("POST", "/api/launch/probe"):
            payload = {"read_only": True, "account": {
                "environment": "production", "identity_verified": True,
                "account_fingerprint": "reviewed-account",
                "cash_available_verified": True, "inventory_reconciled": True,
                "trading_permission_verified": True,
                "geographic_attestation_expires_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
                "balance_usd": "50.0000",
            }}
        elif route == ("POST", "/api/settings"):
            assert json.loads(request.content) == pilot.RUNTIME_LIMITS
            payload = state
        elif route == ("GET", "/api/launch/setup"):
            payload = {"safe_to_arm": False, "scope": {"profile_count": profile_count, "verified_native_markets": 0,
                        "real_reference_markets": 0}, "risk_policy": {
                            "values": pilot.LiveLimits.model_validate(pilot.PILOT_LIMITS).model_dump(mode="json")}}
        else:
            raise AssertionError(f"Unexpected API call: {route}")
        return httpx.Response(200, json=payload)

    original_client = httpx.Client
    monkeypatch.setattr(pilot.httpx, "Client", lambda **kwargs: original_client(
        transport=httpx.MockTransport(handle), **kwargs))
    return calls


def test_prepares_only_paused_one_order_scope(monkeypatch):
    calls = _client(monkeypatch)
    drafts = []
    monkeypatch.setattr(pilot, "_write_limits_draft", lambda: drafts.append(True))

    result = pilot.prepare("http://127.0.0.1:8002")

    assert result["ok"] is True
    assert result["selected_first_venue"] == "kalshi"
    assert result["safe_to_arm"] is False
    assert result["auto_trade"] is False
    assert drafts == [True]
    assert ("POST", "/api/settings") in calls
    assert all("submit" not in path and "control" not in path for _, path in calls)


@pytest.mark.parametrize("options", [
    {"orders": ({"id": "existing"},)},
    {"auto_trade": True},
    {"authenticated": False},
    {"profile_count": 1},
])
def test_refuses_existing_order_or_running_automation(monkeypatch, options):
    calls = _client(monkeypatch, **options)
    with pytest.raises(ValueError):
        pilot.prepare("http://127.0.0.1:8002")
    assert all(method == "GET" for method, _ in calls)
