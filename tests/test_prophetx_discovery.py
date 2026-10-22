"""Read-only catalog diagnostics never grant market or order authorization."""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from evplatform.api import create_app
from evplatform.core.config import Settings
from evplatform.integrations.prophetx_discovery import (
    open_event_ids, summarize_batched_markets, summarize_events, summarize_markets)
from evplatform.plugins.external import NativeMonitor, ProphetXMonitorConfig


def test_catalog_summaries_are_bounded_allowlists():
    events = summarize_events({"data": {"sport_events": [
        {"event_id": 123, "name": "Away v Home", "scheduled": "2026-10-22T02:00:00Z",
         "status": "active", "access_token": "private"},
        {"event_id": "not-numeric", "name": "Rejected"},
    ]}}, limit=2)
    assert events["events"] == [{"event_id": "123", "name": "Away v Home",
                                  "scheduled": "2026-10-22T02:00:00Z", "status": "active"}]
    assert events["mapping_verified"] is False and "private" not in str(events)
    markets = summarize_markets({"data": {"markets": [
        {"id": 77, "type": "moneyline", "status": "active",
         "selections": [[{"strike_id": "test"}]], "secret_key": "private"},
    ]}}, limit=1)
    assert markets["markets"][0]["selection_shape"] == "nested"
    assert markets["markets"][0]["selection_count"] == 1
    assert markets["markets"][0]["selection_review"][0]["strike_id"] == "test"
    assert markets["markets"][0]["selection_review_truncated"] is False
    assert markets["mapping_verified"] is False and "private" not in str(markets)


def test_market_review_exposes_bounded_public_strike_fields_without_prices_or_private_metadata():
    payload = {"data": {"markets": [{"id": 9, "name": "Moneyline", "selections": [
        [{"strike_id": "home", "outcome_id": 1, "name": "Home", "line": "1.5",
          "status": "active", "updated_at": 1800000000, "price": -120,
          "value": "999", "private_token": "never-expose"}],
        [{"strike_id": "away", "outcome_id": 2, "name": "Away", "price": 110}],
    ]}]}}
    summary = summarize_markets(payload, limit=1)
    reviewed = summary["markets"][0]["selection_review"]
    assert [(row["group"], row["strike_id"], row["outcome_id"]) for row in reviewed] == [
        (0, "home", "1"), (1, "away", "2")]
    assert reviewed[0]["updated_at"] == "1800000000"
    assert "never-expose" not in str(summary) and "999" not in str(summary)
    assert "price" not in str(summary) and summary["mapping_verified"] is False
    many = {"data": {"markets": [{"id": 1, "selections": [{"strike_id": str(i)} for i in range(60)]}]}}
    bounded = summarize_markets(many, limit=1)["markets"][0]
    assert len(bounded["selection_review"]) == 20 and bounded["selection_review_truncated"] is True


def test_moneyline_filter_runs_before_catalog_cap():
    props = [{"id": index, "sub_type": "player_total_points", "secret": "private"}
             for index in range(100)]
    moneyline = {"id": 219, "type": "moneyline", "sub_type": "moneyline",
                 "status": "active", "selections": []}
    result = summarize_markets({"data": {"markets": props + [moneyline]}},
                               limit=1, sub_type="moneyline")
    assert result["unfiltered_total"] == 101
    assert result["total"] == 1 and result["truncated"] is False
    assert result["filter_sub_type"] == "moneyline"
    assert [item["id"] for item in result["markets"]] == ["219"]
    assert "private" not in str(result)


def test_batch_market_review_requires_all_exact_event_ids():
    row = {"id": 219, "type": "moneyline", "sub_type": "moneyline",
           "selections": [], "private_token": "hidden"}
    result = summarize_batched_markets({"data": {"1": [row], "2": [row]}},
                                       event_ids=["1", "2"], limit=50,
                                       sub_type="moneyline")
    assert set(result["by_event"]) == {"1", "2"}
    assert result["by_event"]["1"]["markets"][0]["id"] == "219"
    assert "hidden" not in str(result) and result["mapping_verified"] is False
    with pytest.raises(ValueError):
        summarize_batched_markets({"data": {"1": [row]}}, event_ids=["1", "2"], limit=50)


@pytest.mark.parametrize("payload", [{}, {"data": {}}, {"data": {"markets": None}}])
def test_invalid_market_catalog_rejected(payload):
    with pytest.raises(ValueError):
        summarize_markets(payload, limit=1)


def test_discovery_subscribes_only_bounded_known_active_or_upcoming_events():
    now = datetime(2026, 10, 22, 1, tzinfo=timezone.utc)
    events = {"data": {"sport_events": [
        {"event_id": 4, "name": "B at A", "scheduled": "2026-10-22T02:00:00Z", "status": "not_started"},
        {"event_id": 5, "name": "C at D", "scheduled": "2026-10-22T00:00:00Z", "status": "live"},
        {"event_id": 6, "name": "E at F", "scheduled": "2026-10-23T12:00:00Z", "status": "not_started"},
        {"event_id": 7, "name": "G at H", "scheduled": "2026-10-22T00:00:00Z", "status": "closed"},
        {"event_id": 8, "name": "I at J", "scheduled": "unknown", "status": "live"},
    ]}}
    assert open_event_ids([events], now=now, max_events=2) == ["4", "5"]
    conflict = {"data": {"sport_events": [{"event_id": 4, "name": "different fixture",
        "scheduled": "2026-10-22T02:00:00Z", "status": "not_started"}]}}
    assert open_event_ids([events, conflict], now=now, max_events=10) == ["5"]


@pytest.mark.asyncio
async def test_native_monitor_discovery_uses_existing_client_and_keeps_last_good_scope(monkeypatch):
    fixed = datetime(2026, 10, 22, 1, tzinfo=timezone.utc)
    monkeypatch.setattr("evplatform.plugins.external.utcnow", lambda: fixed)

    class Client:
        fail = False

        async def discover_events(self, **kwargs):
            assert kwargs == {"tournament_id": 132}
            if self.fail:
                raise RuntimeError("read failed")
            return {"data": {"sport_events": [{"event_id": 123, "name": "A at B",
                "status": "not_started", "scheduled": "2026-10-22T02:00:00Z"}]}}

    client = Client()
    monitor = SimpleNamespace(client=client,
        context=SimpleNamespace(config={"discovery_tournament_ids": [132], "max_discovered_events": 5}),
        discovery_status="disabled", discovered_event_ids=[])
    await NativeMonitor._refresh_discovery(monitor)
    assert monitor.discovered_event_ids == ["123"] and monitor.discovery_status == "current"
    client.fail = True
    await NativeMonitor._refresh_discovery(monitor)
    assert monitor.discovered_event_ids == ["123"] and monitor.discovery_status == "read_failed"


def test_prophetx_discovery_config_rejects_duplicate_or_invalid_tournaments():
    assert ProphetXMonitorConfig(discovery_tournament_ids=[132]).discovery_tournament_ids == [132]
    with pytest.raises(ValueError):
        ProphetXMonitorConfig(discovery_tournament_ids=[132, 132])
    with pytest.raises(ValueError):
        ProphetXMonitorConfig(discovery_tournament_ids=[0])


@pytest.mark.asyncio
async def test_periodic_discovery_changes_stream_scope_only_after_success(monkeypatch):
    sleeps = 0

    async def one_cycle(_):
        nonlocal sleeps
        sleeps += 1
        if sleeps > 1:
            raise asyncio.CancelledError

    monkeypatch.setattr("evplatform.plugins.external.asyncio.sleep", one_cycle)

    class Client:
        def __init__(self): self.calls = []

        async def subscribe(self, event_ids, props):
            self.calls.append((event_ids, props))

    client = Client()
    async def refresh():
        monitor.discovered_event_ids = ["200"]
        monitor.discovery_status = "current"

    monitor = SimpleNamespace(client=client, context=SimpleNamespace(config={
        "discovery_interval_seconds": 60, "event_ids": ["100"], "prop_subtypes": {}}),
        discovered_event_ids=["100"], _subscribed_event_ids=["100"],
        discovery_status="current", _refresh_discovery=refresh)
    with pytest.raises(asyncio.CancelledError):
        await NativeMonitor._discover_periodically(monitor)
    assert client.calls == [(["100", "200"], {})]
    assert monitor._subscribed_event_ids == ["100", "200"]


def test_catalog_endpoint_requires_one_scope_and_connected_monitor(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    app = create_app(Settings(_env_file=None, database_path=str(tmp_path / "catalog.db")))
    with TestClient(app) as client:
        headers = {"X-EV-Control": "local"}
        assert client.post("/api/native/discovery/prophetx", json={"tournament_id": 132}).status_code == 403
        assert client.post("/api/native/discovery/prophetx", json={}, headers=headers).status_code == 422
        assert client.post("/api/native/discovery/prophetx", json={"tournament_id": 132,
            "event_id": "123"}, headers=headers).status_code == 422
        assert client.post("/api/native/discovery/prophetx", json={"event_ids": ["123", "123"]},
                           headers=headers).status_code == 422
        assert client.post("/api/native/discovery/prophetx", json={"tournament_id": 132},
                           headers=headers).status_code == 409


def test_catalog_uses_existing_monitor_session_and_returns_only_allowlisted_fields(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    app = create_app(Settings(_env_file=None, database_path=str(tmp_path / "catalog-read.db")))

    class Task:
        def done(self): return False

    class Client:
        async def discover_events(self, **kwargs):
            assert kwargs == {"tournament_id": 132}
            return {"data": {"sport_events": [{"event_id": 123, "name": "A vs B",
                "scheduled": "2026-10-23T00:00:00Z", "private_token": "do-not-return"}]}}

        async def fetch_snapshot(self, event_id):
            assert event_id == "123"
            return {"data": {"markets": [{"id": 45, "type": "moneyline", "sub_type": "moneyline",
                "status": "active",
                "selections": [{"strike_id": "public-strike"}], "private_token": "do-not-return"}]}}

        async def discover_markets(self, event_ids):
            assert event_ids == ["123", "124"]
            return {"data": {event_id: [{"id": 45, "type": "moneyline",
                "sub_type": "moneyline", "status": "active", "private_token": "do-not-return"}]
                for event_id in event_ids}}

    with TestClient(app) as caller:
        record = app.state.engine.registry.records["prophetx"]
        plugin = record.plugin
        original = (record.enabled, record.health, plugin.client, plugin.task)
        record.enabled, record.health, plugin.client, plugin.task = True, {"authenticated": True}, Client(), Task()
        try:
            headers = {"X-EV-Control": "local"}
            events = caller.post("/api/native/discovery/prophetx", json={"tournament_id": 132},
                                 headers=headers)
            markets = caller.post("/api/native/discovery/prophetx", json={"event_id": "123",
                "market_sub_type": "moneyline"},
                                  headers=headers)
            batch = caller.post("/api/native/discovery/prophetx", json={"event_ids": ["123", "124"],
                "market_sub_type": "moneyline"}, headers=headers)
            assert events.status_code == markets.status_code == batch.status_code == 200
            assert events.json()["events"][0]["event_id"] == "123"
            assert markets.json()["markets"][0]["id"] == "45"
            assert markets.json()["filter_sub_type"] == "moneyline"
            assert batch.json()["by_event"]["124"]["markets"][0]["id"] == "45"
            assert "do-not-return" not in events.text + markets.text + batch.text
            assert not events.json()["mapping_verified"] and not markets.json()["mapping_verified"]
        finally:
            record.enabled, record.health, plugin.client, plugin.task = original
