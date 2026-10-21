"""Injected offline transport: these tests do not establish real venue behavior."""
import json
from copy import deepcopy

import pytest

from evplatform.core.lifecycle_capture import local_oms_snapshot
from evplatform.core.native_trace import review_application_trace
from evplatform.core.models import OrderStatus, utcnow
from evplatform.core.native_oms import NativeSettlement
from test_native_oms import system, order, report


async def rows(storage):
    records = await (await storage.connection.execute(
        "SELECT seq,order_id,data FROM native_lifecycle_trace ORDER BY seq")).fetchall()
    return [dict(zip(("seq", "order_id", "data"), r)) for r in records]


async def test_commit_receipt_exists_before_dispatch_and_ack_keeps_trace(system, market):
    manager, risk, storage, adapter = system
    submit = adapter.submit_intent
    async def checked(intent, authorization):
        phases = [json.loads(r["data"])["phase"] for r in await rows(storage)]
        assert "intent_commit_returned" in phases
        # There is no extra disk wait in front of the transport. Its entry
        # observation becomes durable with the next ack/unknown transition.
        assert "dispatch_entered" not in phases
        return await submit(intent, authorization)
    adapter.submit_intent = checked
    placed = await manager.execute(order(market))
    result = review_application_trace(await rows(storage), placed)
    assert result["valid"] and result["intent_commit_before_dispatch_recorded"]
    assert not result["certifies_native_lifecycle"]
    snapshot = local_oms_snapshot(__import__("pathlib").Path(storage.path))
    assert snapshot["tables"]["native_lifecycle_trace"]["complete"]


async def test_unknown_outcome_preserves_dispatch_without_retry(system, market):
    manager, risk, storage, adapter = system
    adapter.fail = True
    placed = await manager.execute(order(market))
    assert placed.status == OrderStatus.UNKNOWN
    result = review_application_trace(await rows(storage), placed)
    assert result["intent_commit_before_dispatch_recorded"] and len(adapter.submissions) == 1


async def test_audit_failure_blocks_dispatch_and_rolls_back_order(system, market):
    manager, risk, storage, adapter = system
    await storage.write("CREATE TRIGGER reject_trace BEFORE INSERT ON native_lifecycle_trace BEGIN SELECT RAISE(ABORT, 'offline audit failure'); END", (), True)
    with pytest.raises(Exception):
        await manager.execute(order(market))
    assert not adapter.submissions and risk.kill_switch
    assert await (await storage.connection.execute("SELECT COUNT(*) FROM native_orders")).fetchone() == (0,)


async def test_manager_reload_is_not_a_distinct_process_restart(system, market):
    manager, risk, storage, adapter = system
    placed = await manager.execute(order(market))
    await manager.start()
    result = review_application_trace(await rows(storage), placed)
    assert result["valid"] and not result["recovery_in_distinct_process_recorded"]


async def test_settlement_trace_duplicate_leaves_accounting_unchanged(system, market):
    manager, risk, storage, adapter = system
    placed = await manager.execute(order(market))
    adapter.report = report(placed, quantity="100", cost=".45", status="filled", terminal=True)
    await manager.reconcile(placed.id)
    settlement = NativeSettlement(order_id=placed.id, venue="novig", account_fingerprint="a"*64,
        environment="sandbox", market_fingerprint=market.identity(), settlement_id="trace-fixture",
        payout_usd="1", settled_at=utcnow(), evidence="OFFLINE settlement evidence")
    await manager.settle(settlement)
    before = risk.daily_realized
    await manager.settle(settlement)
    assert risk.daily_realized == before
    result = review_application_trace(await rows(storage), placed)
    assert result["settlement_duplicate_observed"] and not result["certifies_native_lifecycle"]
    phases = [json.loads(r["data"])["phase"] for r in await rows(storage)]
    assert "financial_reconciliation" in phases and phases.count("settlement_recorded") == 1


@pytest.mark.parametrize("change", ["account", "intent", "clock", "regress", "duplicate", "phase", "native"])
async def test_corrupt_trace_cannot_claim_valid_evidence(system, market, change):
    manager, risk, storage, adapter = system
    placed = await manager.execute(order(market))
    records = deepcopy(await rows(storage))
    record = json.loads(records[-1]["data"])
    if change == "account": record["account_fingerprint"] = "b"*64
    elif change == "intent": record["intent_digest"] = "b"*64
    elif change == "clock": record["observed_at"] = "2026-10-21T00:00:00"
    elif change == "regress": record["monotonic_ns"] = 1
    elif change == "phase": record["phase"] = "made_up"
    elif change == "native": record["native_order_id"] = "unrelated"
    records[-1]["data"] = json.dumps(record)
    if change == "duplicate": records.append(deepcopy(records[-1]))
    result = review_application_trace(records, placed)
    assert not result["valid"] and not result["intent_commit_before_dispatch_recorded"]


async def test_missing_dispatch_is_not_filled_in_from_current_order(system, market):
    manager, risk, storage, adapter = system
    placed = await manager.execute(order(market))
    records = [r for r in await rows(storage) if json.loads(r["data"])["phase"] != "dispatch_entered"]
    assert not review_application_trace(records, placed)["intent_commit_before_dispatch_recorded"]
    assert not review_application_trace(None, placed)["available"]
