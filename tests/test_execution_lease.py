"""Actual OS lock tests; these do not connect to a venue or send orders."""
import subprocess
import sys

import pytest

from evplatform.core.execution_lease import ExecutionLease


def test_one_writer_across_objects_and_release(tmp_path):
    first = ExecutionLease("kalshi", "production", directory=tmp_path)
    other = ExecutionLease("kalshi", "production", directory=tmp_path)
    try:
        assert first.acquire() and first.acquire() and first.held
        assert not other.acquire() and not other.held
        first.release()
        assert not first.held and other.acquire()
        assert first.path.exists()  # Do not unlink and create a split-lock race.
    finally:
        first.release()
        other.release()


def test_environment_is_separate_but_venues_share_one_controller(tmp_path):
    leases = [ExecutionLease(v, e, directory=tmp_path) for v, e in (
        ("kalshi", "production"), ("kalshi", "sandbox"), ("novig", "production"))]
    try:
        assert leases[0].acquire() and leases[1].acquire()
        assert not leases[2].acquire()
        assert len({lease.path for lease in leases}) == 2
    finally:
        for lease in leases:
            lease.release()


def test_restart_cannot_switch_ledgers_and_forget_other_venue_positions(tmp_path):
    first = ExecutionLease("kalshi", "production", directory=tmp_path, ledger_path=tmp_path / "native.db")
    changed = ExecutionLease("novig", "production", directory=tmp_path, ledger_path=tmp_path / "empty.db")
    restored = ExecutionLease("novig", "production", directory=tmp_path, ledger_path=tmp_path / "native.db")
    try:
        assert first.acquire()
        first.release()
        assert not changed.acquire()
        assert restored.acquire()
    finally:
        for lease in (first, changed, restored):
            lease.release()


def test_cross_process_contention_and_abrupt_exit_release(tmp_path):
    code = """
import os, sys
from pathlib import Path
from evplatform.core.execution_lease import ExecutionLease
lease = ExecutionLease('kalshi', 'production', directory=Path(sys.argv[1]))
print('held' if lease.acquire() else 'blocked', flush=True)
sys.stdin.read(1)
os._exit(0)
"""
    child = subprocess.Popen([sys.executable, "-c", code, str(tmp_path)],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    contender = ExecutionLease("kalshi", "production", directory=tmp_path)
    try:
        assert child.stdout.readline().strip() == "held"
        assert not contender.acquire()
        child.communicate("x", timeout=10)
        assert child.returncode == 0
        assert contender.acquire()
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=10)
        contender.release()


@pytest.mark.parametrize("venue,environment", [("", "production"), ("x", "demo"), (" ", "sandbox")])
def test_invalid_scope_is_rejected(venue, environment, tmp_path):
    with pytest.raises(ValueError):
        ExecutionLease(venue, environment, directory=tmp_path)
