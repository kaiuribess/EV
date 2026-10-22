"""Wait for a reviewed $1 Kalshi pilot, arm it once, and stop after one intent.

This helper never creates mappings, evidence, profiles, signals or orders. The
backend's current launch gate and per-order preflight retain final authority.
Run it only against the sample-free local production workspace.
"""
from __future__ import annotations

import argparse
import json
import msvcrt
import os
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit

import httpx


ROOT = Path(__file__).resolve().parents[1]
STATUS_PATH = ROOT / "data" / "kalshi_one_shot_status.json"
LOCK_PATH = ROOT / "data" / "kalshi_one_shot.lock"
CONTROL_HEADER = {"X-EV-Control": "local"}


@contextmanager
def _single_watcher():
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise ValueError("kalshi_one_shot_watcher_already_running") from exc
        try:
            yield
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def _local_url(value: str) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in {"", "/"}):
        raise ValueError("A loopback production dashboard URL is required")
    return value.rstrip("/")


def _money(value) -> Decimal | None:
    if isinstance(value, (bool, float)) or value is None:
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return amount if amount.is_finite() else None


def _one_dollar_risk(state: dict, *, applied: bool) -> bool:
    risk = state.get("risk") or {}
    cap_names = ("max_order_stake", "max_event_exposure", "max_player_exposure",
                 "max_venue_exposure", "max_correlated_exposure", "max_total_exposure")
    if any((cap := _money(risk.get(name))) is None or cap <= 0 or cap > 1 for name in cap_names):
        return False
    return (risk.get("max_native_order_attempts") == 1
        and risk.get("max_open_orders") == 1 and risk.get("max_orders_per_minute") == 1
        and risk.get("available_cash_only") is True
        and risk.get("block_overlapping_events") is True
        and risk.get("block_overlapping_players") is True
        and risk.get("stake_sizing") == "quarter_kelly"
        and _money(risk.get("unit_size_usd")) == Decimal("5")
        and (not applied or (_money(risk.get("live_max_order_stake")) == Decimal("1")
                            and risk.get("live_stake_authorized") is True)))


def _base_state(state: dict) -> str | None:
    if (state.get("mode") != "production" or state.get("sample_feed_enabled") is not False
            or state.get("live_enabled") is not True):
        return "production_workspace_unavailable"
    if not _one_dollar_risk(state, applied=False):
        return "one_dollar_risk_scope_changed"
    return None


def _account_fingerprint(client: httpx.Client) -> str:
    response = client.post("/api/launch/probe", json={"venue": "kalshi"}, headers=CONTROL_HEADER)
    response.raise_for_status()
    result = response.json()
    account = result.get("account") or {}
    expires = account.get("geographic_attestation_expires_at")
    try:
        expires_at = datetime.fromisoformat(expires)
    except (ValueError, TypeError):
        expires_at = None
    fingerprint = account.get("account_fingerprint")
    if (result.get("read_only") is not True or account.get("venue") != "kalshi"
            or account.get("environment") != "production"
            or account.get("identity_verified") is not True
            or account.get("cash_available_verified") is not True
            or account.get("inventory_reconciled") is not True
            or account.get("trading_permission_verified") is not True
            or account.get("open_order_count") != 0 or account.get("position_count") != 0
            or not isinstance(fingerprint, str) or len(fingerprint) != 64
            or _money(account.get("balance_usd")) is None
            or _money(account.get("balance_usd")) < 1
            or expires_at is None or expires_at.tzinfo is None
            or expires_at <= datetime.now(timezone.utc)):
        raise ValueError("kalshi_account_probe_not_ready")
    return fingerprint


def _scope_status(setup: dict, fingerprint: str) -> tuple[str, str | None]:
    count = (setup.get("scope") or {}).get("profile_count")
    if count == 0:
        return "waiting_for_reviewed_profile", None
    scope = setup.get("launch_scope") or []
    if count != 1 or len(scope) != 1:
        return "wrong_profile_scope", None
    profile = scope[0]
    if (profile.get("venue") != "kalshi" or profile.get("environment") != "production"
            or profile.get("account_fingerprint") != fingerprint
            or profile.get("validation_pilot") is not True
            or profile.get("settlement_policy") != "kalshi_ioc_binary_halt_on_exception"
            or profile.get("strategy_ids") != ["native_positive_ev"]
            or not profile.get("markets")):
        return "wrong_profile_scope", None
    risk_policy = setup.get("risk_policy") or {}
    if risk_policy.get("source") != "launch_profile" or risk_policy.get("applied") is not True:
        return "waiting_for_applied_profile", None
    values = risk_policy.get("values") or {}
    if (values.get("max_native_order_attempts") != 1
            or _money(values.get("max_order_stake_usd")) != Decimal("1")):
        return "wrong_profile_limits", None
    accounts = [item for item in setup.get("accounts", ()) if item.get("venue") == "kalshi"]
    if len(accounts) != 1:
        return "kalshi_account_unavailable", None
    account = accounts[0].get("account_snapshot") or {}
    if (account.get("current") is not True or account.get("identity_verified") is not True
            or account.get("reconciled") is not True or account.get("cash_available_verified") is not True
            or (cash := _money(account.get("balance_usd"))) is None or cash < 1):
        return "waiting_for_reconciled_cash", None
    activation = setup.get("activation") or {}
    digest = activation.get("scope_digest")
    if (setup.get("safe_to_arm") is not True or activation.get("available") is not True
            or not isinstance(digest, str) or len(digest) != 64):
        return "waiting_for_live_checks", None
    return "ready", digest


def _get(client: httpx.Client, path: str) -> dict:
    response = client.get(path)
    response.raise_for_status()
    return response.json()


def _post_control(client: httpx.Client, *, enabled: bool, digest: str | None = None) -> dict:
    body = {"auto_trade": enabled}
    if digest is not None:
        body["expected_launch_digest"] = digest
    response = client.post("/api/control", json=body, headers=CONTROL_HEADER)
    response.raise_for_status()
    return response.json()


def _status(phase: str, detail: str, *, armed: bool = False) -> None:
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"phase": phase, "detail": detail, "auto_trade_armed_by_watcher": armed,
               "updated_at": datetime.now(timezone.utc).isoformat()}
    fd, name = tempfile.mkstemp(prefix="kalshi-one-shot-", suffix=".tmp", dir=STATUS_PATH.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream)
            stream.write("\n")
        os.replace(name, STATUS_PATH)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def watch(url: str, *, poll_seconds: float = 5, check_only: bool = False) -> str:
    if poll_seconds < 1 or poll_seconds > 60:
        raise ValueError("Polling interval must be 1 to 60 seconds")
    with httpx.Client(base_url=_local_url(url), timeout=25) as client:
        state = _get(client, "/api/state")
        if reason := _base_state(state):
            raise ValueError(reason)
        if state.get("auto_trade") is True or state.get("kill_switch") is True:
            raise ValueError("production_workspace_already_armed_or_stopped")
        if (_get(client, "/api/native/orders").get("orders") or []):
            raise ValueError("one_order_pilot_already_attempted")
        fingerprint = _account_fingerprint(client)
        armed = False
        while True:
            try:
                state = _get(client, "/api/state")
                setup = _get(client, "/api/launch/setup")
                orders = _get(client, "/api/native/orders").get("orders") or []
                if orders:
                    if armed and state.get("auto_trade") is True:
                        _post_control(client, enabled=False)
                    _status("attempted", "A native intent exists; no further attempts are authorized")
                    return "attempted"
                if reason := _base_state(state):
                    if armed and state.get("auto_trade") is True:
                        _post_control(client, enabled=False)
                    raise ValueError(reason)
                phase, digest = _scope_status(setup, fingerprint)
                if phase in {"wrong_profile_scope", "wrong_profile_limits"}:
                    if armed and state.get("auto_trade") is True:
                        _post_control(client, enabled=False)
                    raise ValueError(phase)
                if armed:
                    if phase != "ready" or not _one_dollar_risk(state, applied=True):
                        if state.get("auto_trade") is True:
                            _post_control(client, enabled=False)
                        raise ValueError("armed_scope_or_live_checks_changed")
                    if state.get("auto_trade") is not True:
                        raise ValueError("watcher_did_not_rearm_after_external_pause")
                    _status("armed", "Waiting for one qualifying Kalshi order", armed=True)
                elif phase == "ready" and not state.get("kill_switch") and _one_dollar_risk(state, applied=True):
                    if state.get("auto_trade") is True:
                        raise ValueError("production_armed_outside_watcher")
                    if check_only:
                        _status("ready", "Live gate passed; check-only made no change")
                        return "ready"
                    try:
                        updated = _post_control(client, enabled=True, digest=digest)
                    except httpx.HTTPStatusError as exc:
                        if exc.response.status_code != 409:
                            raise
                        _status("pending", "Live conditions changed before activation")
                    else:
                        if updated.get("auto_trade") is not True:
                            raise ValueError("activation_not_acknowledged")
                        armed = True
                        _status("armed", "Waiting for one qualifying Kalshi order", armed=True)
                else:
                    _status("pending", phase)
                    if check_only:
                        return phase
            except httpx.RequestError:
                if armed:
                    # The core's one-attempt cap survives a client failure;
                    # a reconnect must verify the same profile before any action.
                    _status("unknown", "Local dashboard connection lost after activation", armed=True)
                else:
                    _status("pending", "Local dashboard temporarily unavailable")
            time.sleep(poll_seconds)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8002")
    parser.add_argument("--poll-seconds", type=float, default=5)
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    try:
        if args.check_only:
            result = watch(args.url, poll_seconds=args.poll_seconds, check_only=True)
        else:
            with _single_watcher():
                result = watch(args.url, poll_seconds=args.poll_seconds)
    except ValueError as exc:
        _status("blocked", str(exc))
        return 2
    except (httpx.HTTPError, OSError):
        _status("blocked", "One-shot watcher stopped; inspect launch setup and current runtime")
        return 2
    return 0 if result in {"ready", "attempted"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
