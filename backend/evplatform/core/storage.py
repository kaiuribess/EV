"""Single asynchronous SQLite writer. Pricing never waits on disk.

Order intents use a separate durable barrier *before* transmission; this unavoidable
barrier is measured independently from internal pricing latency.
"""
import asyncio
import json
from pathlib import Path

import aiosqlite

from .models import Envelope, Order


class Storage:
    def __init__(self, path: str):
        self.path = path
        self.connection = None
        self.queue = asyncio.Queue(8192)
        self.task = None
        self.failure = None
        self.on_failure = None

    async def open(self):
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = await aiosqlite.connect(self.path)
        await self.connection.execute("PRAGMA journal_mode=WAL")
        await self.connection.execute("PRAGMA synchronous=FULL")
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
                await self.connection.execute("BEGIN IMMEDIATE")
                for sql, params, future in batch:
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

    async def event(self, event: Envelope):
        await self.write("INSERT OR IGNORE INTO events VALUES(NULL,?,?,?,?)",
                         (event.id, event.schema_name, event.emitted_at.isoformat(), event.model_dump_json()))

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

    async def close(self):
        if self.task:
            await self.queue.join()
            await self.queue.put(None)
            await self.task
        if self.connection:
            await self.connection.close()
            self.connection = None

