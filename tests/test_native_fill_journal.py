"""Exact execution evidence survives incomplete reports and process recovery."""
from datetime import timedelta
from decimal import Decimal as D

import pytest

from evplatform.core.models import OrderStatus, utcnow
from evplatform.core.native_oms import NativeSettlement
from evplatform.core.native_accounting import NativeFill
from evplatform.venues.reconciliation import NativeOrderReport
from test_native_oms import order, report, system


def fill(placed, *, fill_id="actual-execution", quantity="40", watermark="40", fee=".001", price=".45"):
    qty, px = D(quantity), D(price)
    return NativeFill(fill_id=fill_id,order_id=placed.venue_order_id,client_order_id=placed.client_order_id,
        quantity=qty,price=px,cost_usd=qty*px*D(".01"),gross_payout_usd=qty*D(".01"),fee_usd=fee,
        filled_at=utcnow()-timedelta(seconds=1),observed_at=utcnow(),source="OFFLINE exact fill",
        cumulative_quantity_after=D(watermark))


async def test_incomplete_report_keeps_exact_fill_for_restart_without_releasing_risk(system,market):
    manager,risk,storage,adapter=system
    placed=await manager.execute(order(market))
    evidence=fill(placed)
    adapter.report=NativeOrderReport(native_order_id=placed.venue_order_id,client_order_id=placed.client_order_id,
        status="cancelled",terminal=True,financial_complete=False,fills=(evidence,))
    updated=await manager.reconcile(placed.id)
    assert updated.status==OrderStatus.UNKNOWN and updated.filled_quantity==0 and risk.exposures()==D(".47")
    assert manager.recovered_fills(placed.id)==(evidence,)
    assert await (await storage.connection.execute("SELECT COUNT(*) FROM native_reconciliations")).fetchone()==(1,)
    risk.native_orders.clear()
    await manager.start()
    assert manager.recovered_fills(placed.id)==(evidence,)
    assert risk.native_orders[placed.id].status==OrderStatus.UNKNOWN and risk.kill_switch
    assert not manager.invalidated_settlements  # An unresolved fill is not a fabricated settled position.


async def test_repeated_fill_receipt_is_idempotent_but_changed_cash_is_durably_conflicted(system,market):
    manager,risk,storage,adapter=system
    placed=await manager.execute(order(market))
    evidence=fill(placed)
    adapter.report=report(placed).model_copy(update={"fills":(evidence,)})
    await manager.reconcile(placed.id)
    adapter.report=adapter.report.model_copy(update={"fills":(evidence.model_copy(update={"observed_at":utcnow()}),)})
    await manager.reconcile(placed.id)
    assert len(manager.recovered_fills(placed.id))==1
    assert await (await storage.connection.execute("SELECT COUNT(*) FROM native_fill_evidence")).fetchone()==(1,)
    changed=evidence.model_copy(update={"fee_usd":D(".002")})
    adapter.report=adapter.report.model_copy(update={"fills":(changed,)})
    updated=await manager.reconcile(placed.id)
    assert updated.status==OrderStatus.UNKNOWN and not updated.reconciliation_complete and risk.kill_switch
    with pytest.raises(ValueError,match="Conflicting durable"):
        manager.recovered_fills(placed.id)
    await manager.start()
    with pytest.raises(ValueError,match="Conflicting durable"):
        manager.recovered_fills(placed.id)
    # Original evidence is retained; conflicting report remains separately in the audit.
    data=await (await storage.connection.execute("SELECT data FROM native_fill_evidence")).fetchone()
    assert NativeFill.model_validate_json(data[0])==evidence


async def test_missing_intervening_fill_is_preserved_without_inventing_zero_or_cumulative_cash(system,market):
    manager,risk,storage,adapter=system
    placed=await manager.execute(order(market))
    later=fill(placed,quantity="20",watermark="60")
    adapter.report=NativeOrderReport(native_order_id=placed.venue_order_id,client_order_id=placed.client_order_id,
        status="partial",financial_complete=False,fills=(later,))
    await manager.reconcile(placed.id)
    await manager.start()
    assert manager.recovered_fills(placed.id)==(later,)
    assert risk.native_orders[placed.id].filled_quantity==0


async def test_restart_migrates_exact_fills_from_older_complete_audit_reports(system,market):
    manager,risk,storage,adapter=system
    placed=await manager.execute(order(market))
    evidence=fill(placed)
    prior=report(placed).model_copy(update={"fills":(evidence,)})
    await storage.write("INSERT INTO native_reconciliations VALUES(NULL,?,?)",(placed.id,prior.model_dump_json()),True)
    await manager.start()
    assert manager.recovered_fills(placed.id)==(evidence,)
    assert await (await storage.connection.execute("SELECT COUNT(*) FROM native_fill_evidence")).fetchone()==(1,)


async def test_different_execution_ids_cannot_overlap_the_same_native_quantity_interval(system,market):
    manager,risk,storage,adapter=system
    placed=await manager.execute(order(market))
    first=fill(placed)
    second=fill(placed,fill_id="different-id",quantity="20",watermark="40")
    adapter.report=report(placed).model_copy(update={"fills":(first,second)})
    updated=await manager.reconcile(placed.id)
    assert updated.status==OrderStatus.UNKNOWN
    with pytest.raises(ValueError): manager.recovered_fills(placed.id)


async def test_full_watermarkless_native_history_remains_compatible_on_restart(system,market):
    manager,risk,storage,adapter=system
    placed=await manager.execute(order(market))
    evidence=fill(placed).model_copy(update={"cumulative_quantity_after":None})
    adapter.report=report(placed,status="cancelled",terminal=True).model_copy(update={"fills":(evidence,)})
    updated=await manager.reconcile(placed.id)
    assert updated.reconciliation_complete and updated.status==OrderStatus.CANCELLED
    await manager.start()
    assert risk.native_orders[placed.id].reconciliation_complete
    assert manager.recovered_fills(placed.id)==(evidence,)


async def test_exact_execution_cash_cannot_be_overridden_by_conflicting_aggregate(system,market):
    manager,risk,storage,adapter=system
    placed=await manager.execute(order(market))
    evidence=fill(placed,price=".40")
    adapter.report=report(placed).model_copy(update={"fills":(evidence,)})
    updated=await manager.reconcile(placed.id)
    assert updated.status==OrderStatus.UNKNOWN and not updated.reconciliation_complete
    assert manager.recovered_fills(placed.id)==(evidence,)
    assert risk.exposures()>=D(".47")


async def test_crash_after_fill_journal_before_aggregate_update_requires_reconciliation(system,market):
    manager,risk,storage,adapter=system
    placed=await manager.execute(order(market))
    adapter.report=report(placed,status="cancelled",terminal=True)
    terminal=await manager.reconcile(placed.id)
    assert terminal.reconciliation_complete
    later=fill(placed,quantity="60",watermark="60")
    await manager._persist_fill_evidence(terminal,[later])
    await manager.start()
    assert risk.native_orders[placed.id].status==OrderStatus.UNKNOWN
    assert not risk.native_orders[placed.id].reconciliation_complete and risk.kill_switch


async def test_private_execution_is_durable_before_any_order_reconciliation(system,market):
    manager,risk,storage,adapter=system
    placed=await manager.execute(order(market))
    evidence=fill(placed)
    await manager.record_private_fills(placed.id,[evidence],adapter=adapter)
    assert manager.recovered_fills(placed.id)==(evidence,)
    assert risk.native_orders[placed.id].filled_quantity==0
    assert await (await storage.connection.execute("SELECT COUNT(*) FROM native_reconciliations")).fetchone()==(0,)
    await manager.start()
    assert manager.recovered_fills(placed.id)==(evidence,)


async def test_early_execution_buffer_is_flushed_immediately_after_ack_identity_is_durable(system,market):
    manager,risk,storage,adapter=system
    captured=[]
    def execution_fills(native_id,intent):
        placed=risk.native_orders[intent.id]
        assert placed.venue_order_id==native_id
        evidence=fill(placed)
        captured.append(evidence)
        return (evidence,)
    adapter.execution_fills=execution_fills
    placed=await manager.execute(order(market))
    assert manager.recovered_fills(placed.id)==tuple(captured)
    assert placed.filled_quantity==0 and not placed.reconciliation_complete


async def test_other_account_private_stream_cannot_journal_into_owned_order(system,market):
    manager,risk,storage,adapter=system
    placed=await manager.execute(order(market))
    adapter.bound_account_fingerprint="b"*64
    await manager.record_private_fills(placed.id,[fill(placed)],adapter=adapter)
    assert not manager.recovered_fills(placed.id) and risk.kill_switch


async def test_late_private_fill_reopens_settled_exposure_without_lock_deadlock(system,market):
    import asyncio
    manager,risk,storage,adapter=system
    placed=await manager.execute(order(market))
    adapter.report=report(placed,status="cancelled",terminal=True)
    closed=await manager.reconcile(placed.id)
    await manager.settle(NativeSettlement(order_id=placed.id,venue=placed.venue,account_fingerprint=placed.account_fingerprint,
        environment=placed.mode,market_fingerprint=placed.market_fingerprint,settlement_id="OFFLINE settlement",
        payout_usd=closed.gross_payout_usd,settled_at=utcnow(),evidence="OFFLINE actual cash fixture"))
    assert placed.id in risk.settled
    later=fill(placed,quantity="60",watermark="60")
    updated=await asyncio.wait_for(manager.record_private_fills(placed.id,[later],adapter=adapter),timeout=1)
    assert updated.status==OrderStatus.UNKNOWN and placed.id not in risk.settled
    assert placed.id in manager.invalidated_settlements and risk.kill_switch
