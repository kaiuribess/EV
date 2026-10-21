from fastapi.testclient import TestClient

from test_signal_api import HEADERS, quiet_app


def test_cash_view_and_refresh_never_arm_or_claim_sizing_funds(tmp_path):
    with TestClient(quiet_app(tmp_path / "cash.db")) as client:
        before = client.get("/api/state").json()
        cached = client.get("/api/accounts/cash")
        assert cached.status_code == 200
        assert cached.json()["available_for_kelly"] is False
        assert client.post("/api/accounts/cash/refresh", json={}).status_code == 403
        report = client.post("/api/accounts/cash/refresh", json={}, headers=HEADERS).json()
        assert report["read_only"] and not report["available_for_kelly"]
        assert all(row["reported_balance_usd"] is None for row in report["accounts"].values())
        assert client.post("/api/accounts/cash/refresh", json={"venue": "unknown"}, headers=HEADERS).status_code == 422
        assert client.post("/api/accounts/cash/refresh", json={"amount": 25}, headers=HEADERS).status_code == 422
        after = client.get("/api/state").json()
        assert after["auto_trade"] == before["auto_trade"] == False
        assert after["kill_switch"] == before["kill_switch"]
        assert client.get("/api/native/orders").json()["orders"] == []


def test_settings_cannot_grant_native_stake_authorization(tmp_path):
    with TestClient(quiet_app(tmp_path / "authority.db")) as client:
        assert client.post("/api/settings", headers=HEADERS, json={"live_stake_authorized": True}).status_code == 422
