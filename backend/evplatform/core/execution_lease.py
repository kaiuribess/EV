"""OS-owned single-writer lease for local native execution.

The lease spans all checkouts/venues for this OS user and native environment.
Several keys can represent one account, and separate venue controllers could
overlap bets. The durable ledger binding survives process exits. Separate
machines still require an external coordinator. A PID never proves ownership.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path


def lease_directory() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".local" / "state")
    return base / "EVPlatform" / "execution-locks"


class ExecutionLease:
    def __init__(self, venue: str, environment: str, *, directory: Path | None = None,
                 ledger_path: Path | None = None):
        if not venue.strip() or environment not in {"sandbox", "production"}:
            raise ValueError("An explicit native venue and environment are required")
        self.scope = (venue, environment)
        # A second venue must not obtain an independent risk book for the same
        # event. One native controller owns ALL venues for this environment.
        digest = hashlib.sha256(f"evplatform:v2:all-venues:{environment}".encode()).hexdigest()
        self.path = (directory or lease_directory()) / (digest + ".lock")
        self.ledger_path = os.path.normcase(str(ledger_path.resolve())) if ledger_path is not None else None
        self._file = None
        self._pid = None

    @property
    def held(self) -> bool:
        return self._file is not None and not self._file.closed and self._pid == os.getpid()

    def acquire(self) -> bool:
        if self.held:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+b")
        try:
            if stream.seek(0, os.SEEK_END) == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            stream.close()
            return False
        self._file, self._pid = stream, os.getpid()
        if self.ledger_path is not None:
            binding = self.path.with_suffix(".json")
            try:
                if binding.exists():
                    saved = json.loads(binding.read_text(encoding="utf-8"))
                    if saved != {"schema_version": 1, "database_path": self.ledger_path}:
                        raise ValueError("A different durable native risk ledger owns this environment")
                else:
                    with binding.open("x", encoding="utf-8") as output:
                        json.dump({"schema_version": 1, "database_path": self.ledger_path}, output)
                        output.flush()
                        os.fsync(output.fileno())
            except (OSError, ValueError):
                self.release()
                return False
        return True

    def release(self) -> None:
        stream = self._file
        if stream is None:
            return
        try:
            if self.held:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()
            self._file, self._pid = None, None
        # Keep the inode/path: deleting a lock file introduces a split-lock race.

    def __del__(self):
        if getattr(self, "_file", None) is not None:
            self._file.close()
