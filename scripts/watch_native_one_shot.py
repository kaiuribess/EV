"""One bounded authorization for the existing production native OMS.

The Windows production supervisor calls one read-only tick each minute. This
module may release the stop only when the running application's launch gate
already passes; it never creates profiles, mappings, quotes, or orders.
"""
from __future__ import annotations

import argparse
import json
import msvcrt
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit

import httpx


ROOT = Path(__file__).resolve().parents[1]
AUTH_PATH = ROOT / "data" / "native_one_shot_authorization.json"
STATUS_PATH = ROOT / "data" / "native_one_shot_status.json"
LOCK_PATH = ROOT / "data" / "native_one_shot.lock"
CONTROL_HEADER = {"X-EV-Control": "local"}
VENUES = frozenset({"kalshi", "novig", "prophetx", "polymarket_us"})
SETTLEMENT = {
    "kalshi": "kalshi_ioc_binary_halt_on_exception",
    "novig": "novig_ioc_win_loss_halt_on_exception",
    "prophetx": "prophetx_funded_cash_win_loss_halt_on_exception",
}
CAP = Decimal("5")
MARGIN = Decimal("0.01")
MIN_EV = Decimal("0.015")


@contextmanager
def _single_controller():
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise ValueError("one_shot_controller_already_running") from exc
        try:
            yield
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def _local_url(value: str) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in {"", "/"} or parsed.port != 8002):
        raise ValueError("A loopback production dashboard on port 8002 is required")
    return value.rstrip("/")


def _money(value) -> Decimal | None:
    if value is None or isinstance(value, (bool, float)):
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return amount if amount.is_finite() else None


def _time(value) -> datetime | None:
    try:
        result = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return result if result.tzinfo is not None and result.utcoffset() is not None else None


def _authorization(data: dict, now: datetime) -> tuple[datetime, str]:
    issued, expires = _time(data.get("issued_at")), _time(data.get("expires_at"))
    identity = data.get("authorization_id")
    venues = data.get("venues")
    if (data.get("schema_version") != 1 or not isinstance(identity, str) or len(identity) < 16
            or issued is None or expires is None or issued > now
            or not issued < expires <= issued + timedelta(hours=24)
            or _money(data.get("max_upfront_debit_usd")) != CAP
            or _money(data.get("max_loss_usd")) != CAP
            or _money(data.get("minimum_ev")) != MIN_EV
            or _money(data.get("uncertainty_probability")) != MARGIN
            or data.get("maximum_native_attempts") != 1
            or not isinstance(venues, list) or len(venues) != len(VENUES)
            or set(venues) != VENUES):
        raise ValueError("one_shot_authorization_invalid")
    return expires, identity


def _risk_fits(state: dict) -> bool:
    risk = state.get("risk") or {}
    names = ("max_order_stake", "max_event_exposure", "max_player_exposure",
             "max_venue_exposure", "max_correlated_exposure", "max_total_exposure",
             "live_max_order_stake")
    if any((value := _money(risk.get(name))) is None or not 0 < value <= CAP for name in names):
        return False
    return (risk.get("max_native_order_attempts") == 1 and risk.get("max_open_orders") == 1
            and risk.get("max_orders_per_minute") == 1 and risk.get("available_cash_only") is True
            and risk.get("block_overlapping_events") is True
            and risk.get("block_overlapping_players") is True
            and risk.get("stake_sizing") == "quarter_kelly"
            and risk.get("live_stake_authorized") is True
            and _money(risk.get("unit_size_usd")) == CAP)


def _scope(setup: dict, state: dict) -> tuple[str, str | None]:
    if (state.get("mode") != "production" or state.get("sample_feed_enabled") is not False
            or state.get("live_enabled") is not True):
        return "production_workspace_unavailable", None
    if not _risk_fits(state):
        return "one_shot_risk_scope_unavailable", None
    profiles = setup.get("launch_scope") or []
    risk_policy = setup.get("risk_policy") or {}
    limits = risk_policy.get("values") or {}
    if (not profiles or len(profiles) != (setup.get("scope") or {}).get("profile_count")
            or risk_policy.get("source") not in {"launch_profile", "launch_portfolio"}
            or risk_policy.get("applied") is not True
            or limits.get("max_native_order_attempts") != 1
            or (limit := _money(limits.get("max_order_stake_usd"))) is None or not 0 < limit <= CAP):
        return "waiting_for_bounded_launch_profile", None
    if any(p.get("venue") not in VENUES or p.get("environment") != "production"
           or p.get("validation_pilot") is not True
           or p.get("strategy_ids") != ["native_positive_ev"]
           or p.get("settlement_policy") != SETTLEMENT.get(p.get("venue"))
           or not p.get("markets") for p in profiles):
        return "launch_scope_outside_one_shot_authorization", None
    # Only the currently implemented exact-market trusted source exception may
    # arm. Research consensus and an unreviewed dynamic mapping remain blocked.
    pricing = setup.get("pricing") or {}
    pilot = pricing.get("trusted_pilot_policy") or {}
    market_ids = {market.get("id") for p in profiles for market in p["markets"]}
    if (pricing.get("reference_quality_mode") != "trusted_provider"
            or pricing.get("normal_outcome_policy") != "winner_only_binary"
            or pricing.get("reference_profile_mode") != "strict"
            or (configured_ev := _money(pricing.get("min_ev"))) is None or configured_ev < MIN_EV
            or pilot.get("enabled") is not True or pilot.get("owner_approved") is not True
            or market_ids != {pilot.get("exact_market_id")}
            or _money(pilot.get("uncertainty_margin_probability")) != MARGIN
            or (debit := _money(pilot.get("max_debit_usd"))) is None or debit > CAP
            or (loss := _money(pilot.get("max_loss_usd"))) is None or loss > CAP):
        return "waiting_for_approved_source_and_market_policy", None
    overrides = pricing.get("market_overrides") or {}
    if any(not isinstance(override, dict) or any(key in override for key in (
            "min_ev", "trusted_pilot_policy", "reference_quality_mode",
            "normal_outcome_policy", "reference_profile_mode")) for override in overrides.values()):
        return "market_pricing_override_needs_one_shot_review", None
    if setup.get("safe_to_arm") is not True or (setup.get("activation") or {}).get("available") is not True:
        return "waiting_for_live_checks", None
    digest = (setup.get("activation") or {}).get("scope_digest")
    if not isinstance(digest, str) or len(digest) != 64:
        return "launch_digest_unavailable", None
    return "ready", digest


def _was_armed(identity: str) -> bool:
    try:
        saved = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return saved.get("authorization_id") == identity and saved.get("arm_attempted") is True


def _status(phase: str, detail: str, identity: str | None = None,
            *, arm_attempted: bool = False) -> None:
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    if identity is None:
        try:
            previous = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
            if previous.get("arm_attempted") is True:
                identity = previous.get("authorization_id")
        except (OSError, ValueError):
            pass
    payload = {"phase": phase, "detail": detail, "authorization_id": identity,
               "arm_attempted": arm_attempted or bool(identity and _was_armed(identity)),
               "updated_at": datetime.now(timezone.utc).isoformat()}
    fd, name = tempfile.mkstemp(prefix="native-one-shot-", suffix=".tmp", dir=STATUS_PATH.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream)
            stream.write("\n")
        os.replace(name, STATUS_PATH)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _get(client: httpx.Client, path: str) -> dict:
    response = client.get(path)
    response.raise_for_status()
    return response.json()


def _control(client: httpx.Client, *, arm: bool, digest: str | None = None,
             expires: datetime | None = None) -> dict:
    body = {"auto_trade": arm, "kill_switch": not arm}
    if arm:
        body.update(expected_launch_digest=digest, authorization_expires_at=expires.isoformat())
    response = client.post("/api/control", json=body, headers=CONTROL_HEADER)
    response.raise_for_status()
    return response.json()


def tick(url: str = "http://127.0.0.1:8002", *, check_only: bool = False,
         now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    with _single_controller(), httpx.Client(base_url=_local_url(url), timeout=8) as client:
        state = _get(client, "/api/state")
        if state.get("mode") != "production":
            raise ValueError("Port 8002 is not the production scanner")
        if not AUTH_PATH.exists():
            if (state.get("auto_trade") is True or state.get("kill_switch") is not True) and not check_only:
                _control(client, arm=False)
            _status("disabled", "No bounded one-shot authorization is saved")
            return "disabled"
        try:
            expires, identity = _authorization(json.loads(AUTH_PATH.read_text(encoding="utf-8")), now)
        except (OSError, json.JSONDecodeError, ValueError):
            if (state.get("auto_trade") is True or state.get("kill_switch") is not True) and not check_only:
                _control(client, arm=False)
            _status("blocked", "Saved one-shot authorization is invalid")
            return "blocked"
        try:
            orders = _get(client, "/api/native/orders").get("orders")
        except (httpx.HTTPError, ValueError):
            if (state.get("auto_trade") is True or state.get("kill_switch") is not True) and not check_only:
                _control(client, arm=False)
            raise
        if not isinstance(orders, list):
            raise ValueError("Native attempt inventory is unavailable")
        if orders:
            if (state.get("auto_trade") is True or state.get("kill_switch") is not True) and not check_only:
                _control(client, arm=False)
            _status("consumed", "A durable native attempt exists; no further attempt is authorized", identity)
            return "consumed"
        if now >= expires:
            if (state.get("auto_trade") is True or state.get("kill_switch") is not True) and not check_only:
                _control(client, arm=False)
            _status("expired", "Authorization expired; research scanning continues", identity)
            return "expired"
        try:
            setup = _get(client, "/api/launch/setup")
        except (httpx.HTTPError, ValueError):
            if (state.get("auto_trade") is True or state.get("kill_switch") is not True) and not check_only:
                _control(client, arm=False)
            raise
        phase, digest = _scope(setup, state)
        if phase != "ready" and not (phase == "waiting_for_live_checks"
                                     and state.get("auto_trade") is True
                                     and state.get("kill_switch") is False
                                     and _time(state.get("native_authorization_expires_at")) == expires):
            if (state.get("auto_trade") is True or state.get("kill_switch") is not True) and not check_only:
                _control(client, arm=False)
            _status("pending", phase, identity)
            return phase
        if state.get("auto_trade") is True:
            running_expiry = _time(state.get("native_authorization_expires_at"))
            if (state.get("kill_switch") is True or running_expiry != expires):
                if not check_only:
                    _control(client, arm=False)
                _status("blocked", "Running authorization differs from the saved one-shot scope", identity)
                return "blocked"
            _status("armed", "Waiting for one qualifying native candidate", identity,
                    arm_attempted=True)
            return "armed"
        if _was_armed(identity):
            if state.get("kill_switch") is not True and not check_only:
                _control(client, arm=False)
            _status("interrupted", "A prior arming ended; emergency stop remains in force", identity)
            return "interrupted"
        if not check_only:
            # Durable local latch precedes the control POST. A lost response or
            # restart must never cause the supervisor to release the stop again.
            _status("arming", "One bounded activation attempted", identity, arm_attempted=True)
            try:
                updated = _control(client, arm=True, digest=digest, expires=expires)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 409:
                    raise
                _status("pending", "Readiness changed before activation", identity)
                return "pending"
            if updated.get("auto_trade") is not True or updated.get("kill_switch") is not False:
                raise ValueError("Live activation was not acknowledged")
            _status("armed", "Waiting for one qualifying native candidate", identity)
            return "armed"
        _status("ready", "Launch gate passed; check-only made no change", identity)
        return "ready"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8002")
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    try:
        tick(args.url, check_only=args.check_only)
    except (ValueError, OSError, httpx.HTTPError) as exc:
        _status("error", type(exc).__name__)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
