"""Full lifespan tests of signal recording; all market data here are test fixtures.

The synthetic=False fixture checks origin isolation, not real exchange access.
"""
import asyncio
from datetime import timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from evplatform.api import create_app
from evplatform.core.config import Settings
from evplatform.core.models import Envelope, Level, SettlementResult, utcnow
from evplatform.core.signals import ResultProvenance, SignalResultImport

D = Decimal
HEADERS = {"X-EV-Control": "local"}


def quiet_app(path):
    app = create_app(Settings(database_path=str(path), mode="paper", auto_trade=False))
    # Disable only the autonomous source. The actual Engine.start workers, typed
    # bus, pricing strategies, storage and observer run through the app lifespan.
    app.state.engine.registry.records["synthetic_schedule"].enabled = False
    return app


@pytest.fixture
def signal_client(tmp_path):
    with TestClient(quiet_app(tmp_path / "signals-api.db")) as client:
        yield client


async def flush(engine):
    await engine.process_queue.join()
    await engine.signal_queue.join()
    await engine.audit_queue.join()
    await engine.signal_ledger.drain()


async def ingest(engine, rows):
    for schema, payload in rows:
        await engine.bus.publish(Envelope(schema_name=schema, source="offline-api-test", payload=payload))
    await flush(engine)


def seed(client, market, quotes, book, *, synthetic=True, venues=("kalshi_paper",)):
    now = utcnow()
    rows = [("sports.market", market)]
    rows += [("odds.quote", q.model_copy(update={"market_id": market.id, "synthetic": synthetic,
        "source_time": now - timedelta(milliseconds=100), "received_at": now})) for q in quotes]
    rows += [("exchange.book", book.model_copy(update={"market_id": market.id, "venue": venue,
        "synthetic": synthetic, "source_time": now - timedelta(milliseconds=20), "received_at": now}))
        for venue in venues]
    client.portal.call(ingest, client.app.state.engine, rows)


def emit(client, schema, payload):
    client.portal.call(ingest, client.app.state.engine, [(schema, payload)])


def import_body(client, market, *, synthetic=True, winning=True):
    # Local clock precision on Windows can coalesce immediate calls. Explicitly
    # separate observation from settlement so tests never grade a hindsight entry.
    client.portal.call(asyncio.sleep, .02)
    result = SettlementResult(market_id=market.id,
        outcome_payouts={market.outcomes[0]: D(1) if winning else D(0),
                        market.outcomes[1]: D(0) if winning else D(1)},
        settled_at=utcnow(), synthetic=synthetic, evidence="OFFLINE test canonical result and rules")
    provenance = ResultProvenance(source="offline_fixture", reference="fixture:canonical-outcome",
        verified_by="local_test_operator", verified_at=utcnow(),
        verification="canonical_settlement_verified", synthetic=synthetic)
    return SignalResultImport(market=market, result=result, provenance=provenance).model_dump(mode="json")


def test_all_signal_hooks_work_with_auto_off_and_kill_switch(signal_client, market, quotes, book):
    client = signal_client
    assert client.post("/api/control", headers=HEADERS, json={"kill_switch": True}).status_code == 200
    seed(client, market, quotes, book, venues=("kalshi_paper", "novig_paper"))
    state = client.get("/api/state").json()
    assert state["kill_switch"] and not state["auto_trade"]
    assert state["orders"] == []
    page = client.get("/api/signals").json()
    assert page["total"] == 2
    assert all(row["qualified"] for row in page["items"])
    assert all(row["grade"] == "pending" for row in page["items"])
    assert client.get("/api/signals/summary").json()["eligible_episodes"] == 2


def test_negative_and_no_signal_hooks_keep_ended_history(signal_client, market, quotes, book):
    client = signal_client
    seed(client, market, quotes, book)
    expensive = book.model_copy(update={"asks": (Level(price=D(".9"), quantity=50),),
        "sequence": 2, "source_time": utcnow(), "venue": "kalshi_paper"})
    emit(client, "exchange.book", expensive)
    rows = client.get("/api/signals").json()["items"]
    assert len(rows) == 2
    assert {row["qualified"] for row in rows} == {True, False}
    emit(client, "sports.market", market.model_copy(update={"status": "suspended"}))
    rows = client.get("/api/signals").json()["items"]
    assert all(row["status"] == "closed" for row in rows)
    transitions = client.get("/api/signals/transitions").json()["items"]
    assert transitions[0]["status"] == "NO SIGNAL"
    assert any(row["status"] == "NEGATIVE EV" for row in transitions)
    assert client.get("/api/signals/summary").json()["no_signal_count"] >= 1


def test_api_pagination_origin_isolation_and_frontend_fields(signal_client, market, quotes, book):
    client = signal_client
    seed(client, market, quotes, book, venues=("kalshi_paper", "novig_paper", "prophetx_paper"))
    other = market.model_copy(update={"id": "offline:real-origin-market", "event_id": "offline:real-origin-event"})
    seed(client, other, quotes, book, synthetic=False, venues=("test_real_origin",))
    first = client.get("/api/signals?limit=2&offset=0&synthetic=true").json()
    second = client.get("/api/signals?limit=2&offset=2&synthetic=true").json()
    assert first["total"] == second["total"] == 3
    assert len(first["items"]) == 2 and len(second["items"]) == 1
    assert not ({row["id"] for row in first["items"]} & {row["id"] for row in second["items"]})
    real_page = client.get("/api/signals?synthetic=false").json()
    assert real_page["total"] == 1
    assert real_page["items"][0]["synthetic"] is False
    assert client.get("/api/signals/summary?synthetic=false").json()["eligible_episodes"] == 1
    row = first["items"][0]
    for name in ("first_price", "first_fair_probability", "first_ev_per_contract"):
        assert isinstance(row[name], (int, float))
    for name in ("id", "first_seen", "event_name", "market_type", "venue", "strategy", "outcome", "status", "grade"):
        assert isinstance(row[name], str)
    assert row["hypothetical_pnl"] is None
    assert "references" in row["first"]["candidate"]
    assert client.get("/api/signals?limit=201").status_code == 422
    assert client.get("/api/signals?offset=-1").status_code == 422


def test_result_import_requires_local_header_and_exact_canonical_rules(signal_client, market, quotes, book):
    client = signal_client
    seed(client, market, quotes, book)
    body = import_body(client, market)
    assert client.post("/api/signals/result", json=body).status_code == 403
    assert client.post("/api/signals/result", headers={**HEADERS, "Origin": "https://untrusted.example"}, json=body).status_code == 403
    wrong = {**body, "market": {**body["market"], "line": "42.5"}}
    rejected = client.post("/api/signals/result", headers=HEADERS, json=wrong)
    assert rejected.status_code == 422
    assert "Canonical rules or identity conflict" in rejected.json()["detail"]
    assert client.get("/api/signals/summary").json()["pending"] == 1
    accepted = client.post("/api/signals/result", headers=HEADERS, json=body)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["hypothetical_only"]
    assert client.post("/api/signals/result", headers=HEADERS, json=body).status_code == 200
    summary = client.get("/api/signals/summary").json()
    assert summary["graded"] == summary["wins"] == 1
    conflict = {**body, "result": {**body["result"], "outcome_payouts": {
        market.outcomes[0]: "0", market.outcomes[1]: "1"}}}
    assert client.post("/api/signals/result", headers=HEADERS, json=conflict).status_code == 422
    assert client.get("/api/signals/summary").json()["wins"] == 1
    assert client.get("/api/state").json()["orders"] == []


def test_grades_only_qualifying_entry_metrics_and_never_actual_fills(signal_client, market, quotes, book):
    client = signal_client
    seed(client, market, quotes, book)
    emit(client, "exchange.book", book.model_copy(update={"venue": "diagnostic_negative",
        "asks": (Level(price=D(".9"), quantity=50),), "source_time": utcnow()}))
    body = import_body(client, market)
    response = client.post("/api/signals/result", headers=HEADERS, json=body)
    assert response.status_code == 200, response.text
    summary = client.get("/api/signals/summary").json()
    assert summary["episodes"] == summary["all_graded"] == 2
    assert summary["eligible_episodes"] == summary["graded"] == summary["wins"] == 1
    assert summary["hypothetical_pnl"] == pytest.approx(.54)
    assert summary["hypothetical_cost"] == pytest.approx(.45)
    assert summary["decision_count"] > summary["episodes"]
    assert "correlated" in summary["note"]
    assert summary["win_rate_interval"] is None
    assert "clustering" in summary["uncertainty_status"]
    assert summary["distinct_eligible_events"] == summary["distinct_eligible_markets"] == 1
    state = client.get("/api/state").json()
    assert state["orders"] == state["positions"] == []
    assert D(state["metrics"]["realized_pnl"]) == 0
    assert state["settlements"] == {}
    assert all(row["hypothetical_only"] for row in client.get("/api/signals").json()["items"])


def test_history_survives_app_restart_without_inventing_results(tmp_path, market, quotes, book):
    database = tmp_path / "restart-api.db"
    with TestClient(quiet_app(database)) as first:
        seed(first, market, quotes, book)
        episode_id = first.get("/api/signals").json()["items"][0]["id"]
        assert first.get("/api/signals/summary").json()["pending"] == 1
    with TestClient(quiet_app(database)) as second:
        page = second.get("/api/signals").json()
        assert page["total"] == 1
        row = page["items"][0]
        assert row["id"] == episode_id
        assert row["status"] == "closed" and row["close_reason"] == "shutdown"
        assert row["grade"] == "pending"
        summary = second.get("/api/signals/summary").json()
        assert summary["wins"] == summary["losses"] == summary["graded"] == 0
        assert summary["pending"] == 1


def test_explicit_paper_settlement_grades_existing_demo_episode(signal_client, market, quotes, book):
    client = signal_client
    seed(client, market, quotes, book)
    body = import_body(client, market)
    settled = client.post("/api/paper/settle", headers=HEADERS, json=body["result"])
    assert settled.status_code == 200, settled.text
    client.portal.call(flush, client.app.state.engine)
    summary = client.get("/api/signals/summary").json()
    assert summary["graded"] == summary["wins"] == 1
    row = client.get("/api/signals").json()["items"][0]
    assert row["provenance"]["source"] == "paper_operator"
    assert row["synthetic"] and row["grade"] == "win"
    state = client.get("/api/state").json()
    assert state["settlements"][market.id]["synthetic"]
    assert D(state["metrics"]["realized_pnl"]) == 0
    assert state["orders"] == []
