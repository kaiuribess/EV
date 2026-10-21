"""Trade-ID metadata enrichment is durable and cannot alter fill economics."""
from datetime import timedelta
from decimal import Decimal as D

import pytest

from evplatform.core.native_accounting import NativeAccounting, NativeFill, ReconciliationRequired, merge_native_fill_metadata
from evplatform.core.models import utcnow
from test_native_accounting import fill, intent
from test_native_oms import order, system


def test_missing_trade_id_can_be_enriched_without_adding_fill_twice():
    first = fill()
    enriched = first.model_copy(update={"trade_id": "trade-1", "observed_at": first.observed_at+timedelta(seconds=1)})
    ledger = NativeAccounting(intent(), "native-order")
    ledger.apply_fill(first)
    before = ledger.totals
    assert ledger.apply_fill(enriched).duplicate and ledger.totals == before
    assert ledger._fills[first.fill_id].trade_id == "trade-1"
    assert ledger.apply_fill(first).duplicate
    assert ledger._fills[first.fill_id].trade_id == "trade-1"
    assert ledger._fills[first.fill_id].observed_at == first.observed_at


@pytest.mark.parametrize("change", [{"trade_id": "trade-2"}, {"fee_usd": D(".02")},
    {"client_order_id": "other-client"}, {"filled_at": utcnow()}, {"cumulative_quantity_after": D(10)}])
def test_nonnull_trade_conflicts_and_any_economic_change_still_reject(change):
    first = fill(trade_id="trade-1")
    with pytest.raises(ReconciliationRequired):
        merge_native_fill_metadata(first, first.model_copy(update=change))


async def test_metadata_is_persisted_before_memory_change_and_survives_restart(system, market):
    manager, risk, storage, _ = system
    original = order(market).model_copy(update={"venue_order_id": "native-order"})
    await manager._save(original)
    first = NativeFill(fill_id="fill-1", order_id="native-order", client_order_id=original.client_order_id,
        quantity="40", price=".45", cost_usd=".18", gross_payout_usd=".40", fee_usd=".001",
        filled_at=utcnow(), source="offline metadata test")
    await manager._persist_fill_evidence(original, (first,))
    enriched = first.model_copy(update={"trade_id": "trade-1"})
    await manager._persist_fill_evidence(original, (enriched, first))
    rows = await (await storage.connection.execute("SELECT data FROM native_fill_evidence")).fetchall()
    assert len(rows) == 1 and NativeFill.model_validate_json(rows[0][0]).trade_id == "trade-1"
    manager._fill_evidence.clear()
    await manager.start()
    assert manager.recovered_fills(original.id)[0].trade_id == "trade-1"
    with pytest.raises(ValueError):
        await manager._persist_fill_evidence(original, (enriched.model_copy(update={"trade_id": "trade-2"}),))
    assert manager.recovered_fills(original.id)[0].trade_id == "trade-1"


async def test_failed_metadata_write_does_not_replace_memory(system, market):
    manager, _, storage, _ = system
    original = order(market).model_copy(update={"venue_order_id": "native-order"})
    first = NativeFill(fill_id="fill-1", order_id="native-order", client_order_id=original.client_order_id,
        quantity="40", price=".45", cost_usd=".18", gross_payout_usd=".4", fee_usd=".001",
        filled_at=utcnow(), source="offline metadata test")
    await manager._persist_fill_evidence(original, (first,))
    original_write = storage.write
    async def fail(*args, **kwargs): raise OSError("offline simulated persistence failure")
    storage.write = fail
    try:
        with pytest.raises(OSError):
            await manager._persist_fill_evidence(original, (first.model_copy(update={"trade_id": "trade-1"}),))
        assert manager.recovered_fills(original.id)[0].trade_id is None
    finally:
        storage.write = original_write
