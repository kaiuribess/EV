"""Offline, bounded retention and SQLite compaction for an operational database.

Run only after stopping the corresponding local service. This keeps financial
records, compact signal episodes/results, and all configuration intact.
"""
from __future__ import annotations

import argparse
import asyncio
import ctypes
import json
import sqlite3
import sys
from contextlib import closing
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
sys.path.insert(0, str(ROOT / "backend"))

from evplatform.core.models import utcnow  # noqa: E402
from evplatform.core.storage import Storage  # noqa: E402


class MaintenanceStorage(Storage):
    # A pre-existing oversized database needs room for cursor/aggregate writes
    # while old detail is removed. The normal 5 GiB cap is set after VACUUM.
    MAX_DATABASE_BYTES = 100 * 1024**3


def _process_alive(pid: int) -> bool:
    if sys.platform != "win32":
        try:
            import os
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
    if handle:
        ctypes.windll.kernel32.CloseHandle(handle)
    return bool(handle)


def _check_path(raw: str) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        path = ROOT / path
    path = path.resolve(strict=True)
    if path.suffix.lower() != ".db" or DATA.resolve() not in path.parents:
        raise ValueError("Database must be an existing .db file inside this workspace's data folder")
    for runtime_file in DATA.glob("runtime-*.json"):
        try:
            runtime = json.loads(runtime_file.read_text(encoding="utf-8"))
            runtime_path = Path(runtime["database_path"]).resolve()
            pid = int(runtime["pid"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if runtime_path == path and pid > 0 and _process_alive(pid):
            raise RuntimeError(f"Stop the managed service for {path.name} before maintenance")
    return path


async def _prune(path: Path) -> dict[str, int]:
    storage = MaintenanceStorage(str(path))
    await storage.open()
    totals = {"events": 0, "signal_observations": 0, "signal_transitions": 0}
    try:
        await storage.write("""CREATE TABLE IF NOT EXISTS signal_transition_totals (
            synthetic INTEGER NOT NULL, status TEXT NOT NULL, total INTEGER NOT NULL,
            PRIMARY KEY(synthetic,status))""", (), durable=True)
        while True:
            result = await storage.retention_pass(max_batches=100)
            for table, scanned in result.items():
                totals[table] += scanned
            print(json.dumps({"retention_progress": totals}), flush=True)
            if not any(result.values()):
                break
    finally:
        await storage.close()
    return totals


def _fast_offline_prune(path: Path, *, chunk_size: int = 250_000) -> dict[str, int]:
    """Prune by SQLite rowid ranges while the service is stopped.

    SQLite compares aware ISO timestamps by actual instant. Malformed or naive
    timestamps remain untouched. Each range and its transition aggregates commit
    atomically, so an interrupted run can be repeated without double counting.
    """
    cutoff = (utcnow() - timedelta(hours=Storage.RAW_DETAIL_HOURS)).isoformat()
    totals = {"events": 0, "signal_observations": 0, "signal_transitions": 0}
    with closing(sqlite3.connect(path, timeout=30)) as conn:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("""CREATE TABLE IF NOT EXISTS signal_transition_totals (
            synthetic INTEGER NOT NULL, status TEXT NOT NULL, total INTEGER NOT NULL,
            PRIMARY KEY(synthetic,status))""")
        conn.commit()
        for table, clock in (("events", "emitted_at"),
                             ("signal_observations", "observed_at"),
                             ("signal_transitions", "observed_at")):
            exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
            if not exists:
                continue
            maximum = conn.execute(f"SELECT COALESCE(MAX(seq),0) FROM {table}").fetchone()[0]
            clock_guard = (f"julianday({clock}) < julianday(?) AND "
                           f"(substr({clock},-6,1) IN ('+','-') OR substr({clock},-1,1)='Z')")
            for lower in range(0, maximum, chunk_size):
                upper = min(maximum, lower + chunk_size)
                where = f"seq>? AND seq<=? AND {clock_guard}"
                params = (lower, upper, cutoff)
                with conn:
                    if table == "signal_transitions":
                        groups = conn.execute(
                            "SELECT synthetic,status,COUNT(*) FROM signal_transitions "
                            f"WHERE {where} GROUP BY synthetic,status", params).fetchall()
                        for synthetic, status, count in groups:
                            conn.execute(
                                "INSERT INTO signal_transition_totals(synthetic,status,total) VALUES(?,?,?) "
                                "ON CONFLICT(synthetic,status) DO UPDATE SET total=total+excluded.total",
                                (synthetic, status, count))
                    if table == "events":
                        placeholders = ",".join("?" for _ in Storage.PERMANENT_EVENT_SCHEMAS)
                        conn.execute(f"DELETE FROM events WHERE {where} AND schema_name NOT IN ({placeholders})",
                                     (*params, *Storage.PERMANENT_EVENT_SCHEMAS))
                    else:
                        conn.execute(f"DELETE FROM {table} WHERE {where}", params)
                    deleted = conn.execute("SELECT changes()").fetchone()[0]
                    totals[table] += deleted
                if upper % 1_000_000 == 0 or upper == maximum:
                    print(json.dumps({"table": table, "through_seq": upper,
                                      "deleted_old_detail": totals}), flush=True)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    return totals


def _compact(path: Path) -> dict:
    # VACUUM needs a closed operational connection and enough temporary disk.
    with closing(sqlite3.connect(path, timeout=30)) as conn:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        conn.execute("PRAGMA journal_mode=DELETE").fetchone()
        conn.execute("PRAGMA auto_vacuum=FULL")
        conn.execute("VACUUM")
        conn.execute("PRAGMA journal_mode=WAL").fetchone()
        conn.execute("PRAGMA journal_size_limit=67108864")
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        requested = Storage.MAX_DATABASE_BYTES // page_size
        applied = conn.execute(f"PRAGMA max_page_count={requested}").fetchone()[0]
        if applied > requested:
            raise RuntimeError("Database remains above the 5 GiB cap after compaction")
        check = conn.execute("PRAGMA quick_check").fetchone()[0]
        if check != "ok":
            raise RuntimeError(f"SQLite integrity check failed: {check}")
        return {"auto_vacuum": conn.execute("PRAGMA auto_vacuum").fetchone()[0],
                "max_database_bytes": applied * page_size, "quick_check": check}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    parser.add_argument("--compact", action="store_true", help="Reclaim disk after pruning")
    parser.add_argument("--fast-offline", action="store_true",
                        help="Use large SQLite batches for a stopped historical database")
    parser.add_argument("--compact-only", action="store_true",
                        help="Compact a stopped database after an already completed pruning pass")
    args = parser.parse_args()
    path = _check_path(args.database)
    before = path.stat().st_size
    if args.compact_only and (args.fast_offline or args.compact):
        parser.error("--compact-only cannot be combined with other maintenance modes")
    pruned = ({"events": 0, "signal_observations": 0, "signal_transitions": 0}
              if args.compact_only else _fast_offline_prune(path) if args.fast_offline
              else asyncio.run(_prune(path)))
    result = _compact(path) if args.compact or args.compact_only else {}
    print(json.dumps({"database": str(path), "before_bytes": before,
                      "after_bytes": path.stat().st_size, "scanned_old_detail": pruned,
                      **result}), flush=True)


if __name__ == "__main__":
    main()
