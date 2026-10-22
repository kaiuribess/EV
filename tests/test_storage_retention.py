"""Raw-detail pruning must preserve the audit and signal accounting contract."""
from datetime import timedelta

import pytest

from evplatform.core.models import utcnow
from evplatform.core.signals import SignalLedger
from evplatform.core.storage import Storage
from scripts.maintain_storage import _fast_offline_prune


@pytest.mark.asyncio
async def test_retention_preserves_financial_events_and_transition_totals(tmp_path):
    storage = Storage(str(tmp_path / "retention.db"))
    await storage.open()
    ledger = SignalLedger(storage)
    await ledger.start()
    assert (await (await storage.connection.execute("PRAGMA auto_vacuum")).fetchone())[0] == 1
    page_size = (await (await storage.connection.execute("PRAGMA page_size")).fetchone())[0]
    pages = (await (await storage.connection.execute("PRAGMA max_page_count")).fetchone())[0]
    assert pages * page_size <= Storage.MAX_DATABASE_BYTES
    old = (utcnow() - timedelta(hours=8)).isoformat()
    fresh = utcnow().isoformat()
    try:
        await storage.write([
            ("INSERT INTO events(id,schema_name,emitted_at,data) VALUES(?,?,?,?)",
             ("old-quote", "market.quote", old, "{}")),
            ("INSERT INTO events(id,schema_name,emitted_at,data) VALUES(?,?,?,?)",
             ("old-order", "trade.order", old, "{}")),
            ("INSERT INTO events(id,schema_name,emitted_at,data) VALUES(?,?,?,?)",
             ("new-quote", "market.quote", fresh, "{}")),
            ("INSERT INTO signal_observations(episode_id,observed_at,candidate_id,snapshot_json) "
             "VALUES(?,?,?,?)", ("ep", old, "c", "{}")),
            ("INSERT INTO signal_observations(episode_id,observed_at,candidate_id,snapshot_json) "
             "VALUES(?,?,?,?)", ("ep", fresh, "c", "{}")),
            ("INSERT INTO signal_transitions(identity,market_id,strategy,synthetic,observed_at,status,reason) "
             "VALUES(?,?,?,?,?,?,?)", ("id", "m", "positive_ev", 1, old, "NO SIGNAL", "stale")),
            ("INSERT INTO signal_transitions(identity,market_id,strategy,synthetic,observed_at,status,reason) "
             "VALUES(?,?,?,?,?,?,?)", ("id", "m", "positive_ev", 0, old, "NEGATIVE EV", "edge")),
            ("INSERT INTO signal_transitions(identity,market_id,strategy,synthetic,observed_at,status,reason) "
             "VALUES(?,?,?,?,?,?,?)", ("id", "m", "positive_ev", 1, fresh, "POSITIVE EV", "edge")),
        ], (), durable=True)
        before = await ledger.summary()
        assert before["transition_count"] == 3
        assert before["no_signal_count"] == 1

        scanned = await storage.retention_pass(max_batches=2)
        assert scanned == {"events": 2, "signal_observations": 1, "signal_transitions": 2}
        assert await storage.retention_pass(max_batches=2) == {
            "events": 0, "signal_observations": 0, "signal_transitions": 0}
        rows = await (await storage.connection.execute("SELECT id FROM events ORDER BY seq")).fetchall()
        assert [row[0] for row in rows] == ["old-order", "new-quote"]
        assert (await (await storage.connection.execute("SELECT COUNT(*) FROM signal_observations")).fetchone())[0] == 1
        after = await ledger.summary()
        assert after["transition_count"] == before["transition_count"]
        assert after["no_signal_count"] == before["no_signal_count"]
        assert (await ledger.summary(synthetic=True))["no_signal_count"] == 1
        assert (await ledger.summary(synthetic=False))["transition_count"] == 1
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_retention_does_not_prune_future_or_malformed_clocks(tmp_path):
    storage = Storage(str(tmp_path / "bad-clock.db"))
    await storage.open()
    try:
        await storage.write("INSERT INTO events(id,schema_name,emitted_at,data) VALUES(?,?,?,?)",
                            ("invalid", "market.quote", "not-a-timestamp", "{}"), durable=True)
        with pytest.raises(ValueError, match="Invalid events retention clock"):
            await storage.retention_pass()
        assert (await (await storage.connection.execute("SELECT COUNT(*) FROM events")).fetchone())[0] == 1
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_offline_bulk_prune_keeps_permanent_events_and_counts_once(tmp_path):
    path = tmp_path / "bulk.db"
    storage = Storage(str(path))
    await storage.open()
    ledger = SignalLedger(storage)
    await ledger.start()
    old = (utcnow() - timedelta(hours=9)).isoformat()
    recent = utcnow().isoformat()
    try:
        await storage.write([
            ("INSERT INTO events(id,schema_name,emitted_at,data) VALUES(?,?,?,?)",
             ("old", "odds.quote", old, "{}")),
            ("INSERT INTO events(id,schema_name,emitted_at,data) VALUES(?,?,?,?)",
             ("permanent", "trade.fill", old, "{}")),
            ("INSERT INTO events(id,schema_name,emitted_at,data) VALUES(?,?,?,?)",
             ("recent", "odds.quote", recent, "{}")),
            ("INSERT INTO signal_transitions(identity,market_id,strategy,synthetic,observed_at,status,reason) "
             "VALUES(?,?,?,?,?,?,?)", ("id", "m", "positive_ev", 1, old, "NO SIGNAL", "stale")),
        ], (), durable=True)
    finally:
        await storage.close()
    assert _fast_offline_prune(path, chunk_size=2) == {
        "events": 1, "signal_observations": 0, "signal_transitions": 1}
    assert _fast_offline_prune(path, chunk_size=2) == {
        "events": 0, "signal_observations": 0, "signal_transitions": 0}
    storage = Storage(str(path))
    await storage.open()
    try:
        assert [row[0] for row in await (await storage.connection.execute(
            "SELECT id FROM events ORDER BY seq")).fetchall()] == ["permanent", "recent"]
        row = await (await storage.connection.execute(
            "SELECT total FROM signal_transition_totals WHERE synthetic=1 AND status='NO SIGNAL'")).fetchone()
        assert row == (1,)
    finally:
        await storage.close()
