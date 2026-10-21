"""Persist operator controls without enabling paper or native transmission."""
from fastapi.testclient import TestClient

from test_market_review import review
from test_signal_api import HEADERS, quiet_app


def test_policy_persists_restart_and_cannot_clear_kill_switch(tmp_path):
    path = tmp_path / "policy.db"
    policy = {"max_order_stake": "5", "max_event_exposure": "5",
              "max_player_exposure": "5", "max_correlated_exposure": "5",
              "daily_loss_limit": None, "block_overlapping_events": True,
              "block_overlapping_players": True}
    with TestClient(quiet_app(path)) as client:
        assert client.post("/api/control", headers=HEADERS, json={"kill_switch": True}).status_code == 200
        assert client.post("/api/settings", headers=HEADERS, json=policy).status_code == 200
        state = client.get("/api/state").json()
        assert state["kill_switch"] and not state["auto_trade"]
        assert state["risk"]["daily_loss_limit"] is None
    with TestClient(quiet_app(path)) as client:
        state = client.get("/api/state").json()
        assert state["kill_switch"] and not state["auto_trade"]
        assert state["risk"]["daily_loss_limit"] is None
        assert state["risk"]["block_overlapping_events"]
        assert state["risk"]["block_overlapping_players"]
        assert str(state["risk"]["max_order_stake"]) in {"5", "5.0"}
        assert client.get("/api/native/orders").json()["orders"] == []


def test_market_review_api_validates_inputs_and_never_authorizes(tmp_path):
    with TestClient(quiet_app(tmp_path / "review.db")) as client:
        assert client.get("/api/market-review?limit=1001").status_code == 422
        assert client.get("/api/market-review").json()["bounded_window"]
        item = review().model_dump(mode="json")
        body = {"source": item, "target": item}
        assert client.post("/api/market-review/compare", json=body).status_code == 403
        response = client.post("/api/market-review/compare", headers=HEADERS, json=body)
        assert response.status_code == 200
        assert response.json()["structurally_equivalent"]
        assert not response.json()["execution_authorized"]
        body["target"] = {**item, "line": "21.5"}
        assert not client.post("/api/market-review/compare", headers=HEADERS, json=body).json()["structurally_equivalent"]
        assert client.get("/api/native/orders").json()["orders"] == []
