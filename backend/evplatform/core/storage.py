"""Single asynchronous SQLite writer. Pricing never waits on disk.

Order intents use a separate durable barrier *before* transmission; this unavoidable
barrier is measured independently from internal pricing latency.
"""
import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import aiosqlite

from .models import Envelope, Order, utcnow
from .audit_codec import decode_audit_data, encode_audit_data


@dataclass(frozen=True)
class _PendingAuditData:
    text: str
    threshold: int | None = None


def _prepare_audit_batch(batch):
    """CPU work runs in a worker thread before opening the SQLite transaction."""
    def prepared(params):
        return tuple((encode_audit_data(value.text) if value.threshold is None
                      else encode_audit_data(value.text, threshold=value.threshold))
                     if isinstance(value, _PendingAuditData) else value for value in params)

    return [([(statement, prepared(values)) for statement, values in sql] if isinstance(sql, list) else sql,
             prepared(params), future) for sql, params, future in batch]


def _has_pending_audit_data(sql, params):
    return (any(isinstance(value, _PendingAuditData) for value in params)
            or isinstance(sql, list) and any(
                isinstance(value, _PendingAuditData)
                for _, values in sql for value in values))


class Storage:
    # Full market and quote streams are useful for short replay windows, but
    # keeping every repeated tick in the operational database indefinitely can
    # exhaust a workstation. Financial events and compact signal episodes are
    # retained separately and are never selected by this raw-detail policy.
    RAW_DETAIL_HOURS = 6
    RETENTION_BATCH = 2000
    MAX_DATABASE_BYTES = 3 * 1024**3
    PERMANENT_EVENT_SCHEMAS = (
        "trade.order", "trade.fill", "trade.settlement", "native.order",
        "native.settlement", "signal.result", "system.notice",
    )

    @staticmethod
    def compact_signal_json(data: str) -> _PendingAuditData:
        # Signal snapshots are normally ~2 KB and repeat across first/last/best.
        # Encode them on the writer thread; market_json stays TEXT for SQLite
        # JSON queries and historical TEXT snapshots remain readable.
        return _PendingAuditData(data, threshold=512)

    def __init__(self, path: str):
        self.path = path
        self.connection = None
        self.queue = asyncio.Queue(8192)
        self.task = None
        self.failure = None
        self.on_failure = None
        self._retention_lock = asyncio.Lock()

    async def open(self):
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        new_database = not Path(self.path).exists() or Path(self.path).stat().st_size == 0
        self.connection = await aiosqlite.connect(self.path)
        if new_database:
            # SQLite can only enable FULL auto-vacuum before tables exist.
            # Existing installations are converted by scripts/maintain_storage.py.
            await self.connection.execute("PRAGMA auto_vacuum=FULL")
        await self.connection.execute("PRAGMA journal_mode=WAL")
        await self.connection.execute("PRAGMA synchronous=FULL")
        await self.connection.execute("PRAGMA journal_size_limit=67108864")
        page_size = (await (await self.connection.execute("PRAGMA page_size")).fetchone())[0]
        limit_pages = self.MAX_DATABASE_BYTES // page_size
        applied_pages = (await (await self.connection.execute(
            f"PRAGMA max_page_count={limit_pages}")).fetchone())[0]
        if applied_pages > limit_pages:
            await self.connection.close()
            self.connection = None
            raise RuntimeError("Database exceeds disk cap; stop its service and run scripts/maintain_storage.py --compact")
        await self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS schema_version(version INTEGER NOT NULL);
            INSERT INTO schema_version SELECT 1 WHERE NOT EXISTS(SELECT 1 FROM schema_version);
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                schema_name TEXT NOT NULL, emitted_at TEXT NOT NULL, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS orders (
                id TEXT PRIMARY KEY, client_order_id TEXT UNIQUE NOT NULL,
                status TEXT NOT NULL, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS fills (id TEXT PRIMARY KEY, data TEXT NOT NULL);
        """)
        version = await (await self.connection.execute("SELECT version FROM schema_version")).fetchone()
        if version[0] != 1:
            raise RuntimeError("Unsupported storage schema version; migration required")
        await self.connection.commit()
        self.task = asyncio.create_task(self._writer(), name="sqlite-writer")

    async def _writer(self):
        while True:
            item = await self.queue.get()
            if item is None:
                self.queue.task_done()
                return
            batch = [item]
            # One fsync per batch, while every durable caller waits for that commit.
            # The queue owns disk IO; the event processing loop only enqueues.
            while len(batch) < 256 and not self.queue.empty():
                batch.append(self.queue.get_nowait())
            try:
                if any(_has_pending_audit_data(sql, params) for sql, params, _ in batch):
                    prepared = await asyncio.to_thread(_prepare_audit_batch, batch)
                else:
                    prepared = batch
                await self.connection.execute("BEGIN IMMEDIATE")
                for sql, params, future in prepared:
                    if isinstance(sql, list):
                        for statement, values in sql:
                            await self.connection.execute(statement, values)
                    else:
                        await self.connection.execute(sql, params)
                await self.connection.commit()
                for _, _, future in batch:
                    if future and not future.done():
                        future.set_result(None)
            except Exception as exc:
                await self.connection.rollback()
                self.failure = type(exc).__name__
                if self.on_failure:
                    self.on_failure("Audit storage failure")
                for _, _, future in batch:
                    if future and not future.done():
                        future.set_exception(exc)
            finally:
                for _ in batch:
                    self.queue.task_done()

    async def write(self, sql, params, durable=False):
        if self.failure:
            raise RuntimeError("Persistence unavailable")
        future = asyncio.get_running_loop().create_future() if durable else None
        self.queue.put_nowait((sql, params, future))
        if future:
            await future

    async def event(self, event: Envelope, *, durable: bool = False):
        await self.write("INSERT OR IGNORE INTO events VALUES(NULL,?,?,?,?)",
                         (event.id, event.schema_name, event.emitted_at.isoformat(), _PendingAuditData(event.model_dump_json())),
                         durable=durable)

    @staticmethod
    async def decode_event_data(data: str | bytes) -> str:
        """Read both old JSON TEXT and new compressed audit BLOB rows losslessly."""
        return await asyncio.to_thread(decode_audit_data, data)

    async def save_order(self, order: Order):
        await self.write("INSERT INTO orders VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,data=excluded.data",
                         (order.id, order.client_order_id, order.status.value, order.model_dump_json()), True)

    async def save_fill(self, fill):
        await self.write("INSERT OR IGNORE INTO fills VALUES(?,?)", (fill.id, fill.model_dump_json()), True)

    async def save_fill_and_order(self, fill, order):
        await self.write([
            ("INSERT OR IGNORE INTO fills VALUES(?,?)", (fill.id, fill.model_dump_json())),
            ("UPDATE orders SET status=?,data=? WHERE id=?",
             (order.status.value, order.model_dump_json(), order.id)),
        ], (), True)

    async def save_config(self, key, value):
        await self.write("INSERT INTO config VALUES(?,?) ON CONFLICT(key) DO UPDATE SET data=excluded.data",
                         (key, json.dumps(value)), True)

    def enqueue_config(self, key, value):
        """Synchronous enqueue only, for fail-closed callbacks; no disk IO here."""
        self.queue.put_nowait((
            "INSERT INTO config VALUES(?,?) ON CONFLICT(key) DO UPDATE SET data=excluded.data",
            (key, json.dumps(value)), None,
        ))

    async def load_config(self):
        rows = await (await self.connection.execute("SELECT key,data FROM config")).fetchall()
        return {key: json.loads(value) for key, value in rows}

    async def load_orders(self):
        rows = await (await self.connection.execute("SELECT data FROM orders")).fetchall()
        return [Order.model_validate_json(row[0]) for row in rows]

    async def load_fill_ids(self):
        rows = await (await self.connection.execute("SELECT id FROM fills")).fetchall()
        return {row[0] for row in rows}

    async def retention_pass(self, *, now=None, max_batches=24):
        """Bounded, off-path pruning of repetitive raw observations.

        The scan advances only through a chronological old prefix. A newer
        record stops it, so a row cannot be deleted before its retention age.
        Durable financial events and episode/result/order tables are untouched.
        The cursors make repeated passes restart-safe without large OFFSET scans.
        """
        if not isinstance(max_batches, int) or isinstance(max_batches, bool) or not 1 <= max_batches <= 100000:
            raise ValueError("Invalid retention batch budget")
        cutoff = (now or utcnow()) - timedelta(hours=self.RAW_DETAIL_HOURS)
        if cutoff.tzinfo is None:
            raise ValueError("Retention clock must be timezone-aware")
        scanned = {"events": 0, "signal_observations": 0, "signal_transitions": 0}
        async with self._retention_lock:
            async with aiosqlite.connect(self.path) as reader:
                await reader.execute("PRAGMA query_only=ON")
                for table, clock in (("events", "emitted_at"),
                                     ("signal_observations", "observed_at"),
                                     ("signal_transitions", "observed_at")):
                    async with reader.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)) as query:
                        exists = await query.fetchone()
                    if not exists:
                        continue
                    key = f"retention_cursor:{table}"
                    async with reader.execute("SELECT data FROM config WHERE key=?", (key,)) as query:
                        saved = await query.fetchone()
                    cursor = int(json.loads(saved[0])) if saved else 0
                    for _ in range(max_batches):
                        columns = f"seq,{clock},synthetic,status" if table == "signal_transitions" else f"seq,{clock}"
                        async with reader.execute(
                            f"SELECT {columns} FROM {table} WHERE seq>? ORDER BY seq LIMIT ?",
                            (cursor, self.RETENTION_BATCH)) as query:
                            rows = await query.fetchall()
                        if not rows:
                            break
                        old = []
                        for row in rows:
                            seq, raw_time = row[:2]
                            # Parse aware timestamps; malformed clocks stop
                            # pruning rather than risk deleting recent data.
                            try:
                                stamp = datetime.fromisoformat(raw_time)
                            except (TypeError, ValueError):
                                raise ValueError(f"Invalid {table} retention clock") from None
                            if stamp.tzinfo is None:
                                raise ValueError(f"Naive {table} retention clock")
                            if stamp >= cutoff:
                                break
                            old.append(seq)
                        if not old:
                            break
                        last = old[-1]
                        statements = []
                        if table == "events":
                            placeholders = ",".join("?" for _ in self.PERMANENT_EVENT_SCHEMAS)
                            sql = (f"DELETE FROM events WHERE seq>? AND seq<=? "
                                   f"AND schema_name NOT IN ({placeholders})")
                            params = (cursor, last, *self.PERMANENT_EVENT_SCHEMAS)
                        elif table == "signal_transitions":
                            totals = {}
                            for _, _, synthetic, status in rows[:len(old)]:
                                group_key = (synthetic, status)
                                totals[group_key] = totals.get(group_key, 0) + 1
                            for (synthetic, status), total in totals.items():
                                statements.append((
                                    "INSERT INTO signal_transition_totals(synthetic,status,total) VALUES(?,?,?) "
                                    "ON CONFLICT(synthetic,status) DO UPDATE SET total=total+excluded.total",
                                    (synthetic, status, total)))
                            sql = "DELETE FROM signal_transitions WHERE seq>? AND seq<=?"
                            params = (cursor, last)
                        else:
                            sql = "DELETE FROM signal_observations WHERE seq>? AND seq<=?"
                            params = (cursor, last)
                        # The delete and cursor advance are one committed batch.
                        await self.write([*statements, (sql, params),
                            ("INSERT INTO config(key,data) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET data=excluded.data",
                             (key, json.dumps(last)))], (), durable=True)
                        scanned[table] += len(old)
                        cursor = last
                        if len(old) < len(rows):
                            break
        return scanned

    async def close(self):
        if self.task:
            await self.queue.join()
            await self.queue.put(None)
            await self.task
        if self.connection:
            await self.connection.close()
            self.connection = None

