"""Prepare a paused, one-order Kalshi production pilot without sending an order.

Run from the repository root with .venv/Scripts/python.exe. This command uses
only the local control API and a read-only authenticated account inventory.
It does not create a market mapping, launch profile, or execution certificate.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from evplatform.core.readiness import LiveLimits


ROOT = Path(__file__).resolve().parents[1]
PILOT_LIMITS = {
    "max_order_stake_usd": "1",
    "max_event_exposure_usd": "1",
    "max_player_exposure_usd": "1",
    "max_venue_exposure_usd": "1",
    "max_correlated_exposure_usd": "1",
    "max_total_exposure_usd": "1",
    "daily_loss_limit_usd": None,
    "available_cash_only": True,
    "block_overlapping_events": True,
    "block_overlapping_players": True,
    "stake_sizing": "quarter_kelly",
    "unit_size_usd": "5",
    "max_open_orders": 1,
    "max_orders_per_minute": 1,
    "max_native_order_attempts": 1,
}
RUNTIME_LIMITS = {
    **{key.removesuffix("_usd") if key != "unit_size_usd" else key: value
       for key, value in PILOT_LIMITS.items()},
}


def _get(client: httpx.Client, path: str) -> dict:
    response = client.get(path)
    response.raise_for_status()
    return response.json()


def _post(client: httpx.Client, path: str, body: dict) -> dict:
    response = client.post(path, json=body, headers={"X-EV-Control": "local"})
    response.raise_for_status()
    return response.json()


def _paused_production(client: httpx.Client) -> tuple[dict, dict]:
    state = _get(client, "/api/state")
    orders = _get(client, "/api/native/orders")
    if state.get("mode") != "production" or state.get("sample_feed_enabled") is not False:
        raise ValueError("A sample-free production workspace is required")
    if state.get("auto_trade") is not False or state.get("kill_switch") is True:
        raise ValueError("Stop or resolve the current trading state before preparing a pilot")
    if orders.get("orders"):
        raise ValueError("This ledger already contains a native order attempt")
    records = [item for item in state.get("plugins", []) if item.get("manifest", {}).get("name") == "kalshi"]
    if len(records) != 1 or not records[0].get("enabled"):
        raise ValueError("Enable the Kalshi production monitor before preparing a pilot")
    monitor = records[0]
    if (monitor.get("config", {}).get("environment") != "production"
            or monitor.get("health", {}).get("authenticated") is not True
            or monitor.get("health", {}).get("streaming") is not True):
        raise ValueError("Kalshi production market data is not authenticated and streaming")
    return state, orders


def _write_limits_draft() -> None:
    limits = LiveLimits.model_validate(PILOT_LIMITS)
    destination = ROOT / "data" / "operator_launch_limits.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="kalshi-limits-", suffix=".tmp", dir=destination.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(limits.model_dump_json(indent=2) + "\n")
        os.replace(name, destination)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def prepare(url: str) -> dict:
    target = urlsplit(url)
    if target.scheme != "http" or target.hostname not in {"127.0.0.1", "localhost", "::1"} or target.path not in {"", "/"}:
        raise ValueError("The pilot setup requires a loopback HTTP dashboard URL")
    LiveLimits.model_validate(PILOT_LIMITS)
    with httpx.Client(base_url=url.rstrip("/"), timeout=25) as client:
        _paused_production(client)
        existing_setup = _get(client, "/api/launch/setup")
        if existing_setup.get("scope", {}).get("profile_count") != 0:
            raise ValueError("A reviewed launch profile already exists; do not replace its limits with a pilot draft")
        probe = _post(client, "/api/launch/probe", {"venue": "kalshi"})
        account = probe.get("account") or {}
        if (probe.get("read_only") is not True or account.get("environment") != "production"
                or account.get("identity_verified") is not True
                or not account.get("account_fingerprint")
                or account.get("cash_available_verified") is not True
                or account.get("inventory_reconciled") is not True
                or account.get("trading_permission_verified") is not True
                or not account.get("geographic_attestation_expires_at")):
            raise ValueError("Kalshi account inventory or permissions are not verified")
        expires_at = datetime.fromisoformat(account["geographic_attestation_expires_at"])
        cash = Decimal(str(account.get("balance_usd")))
        if (expires_at.tzinfo is None or expires_at <= datetime.now(timezone.utc)
                or not cash.is_finite() or cash < Decimal("1")):
            raise ValueError("Kalshi pilot requires current location attestation and $1 available cash")
        _paused_production(client)
        state = _post(client, "/api/settings", RUNTIME_LIMITS)
        risk = state.get("risk") or {}
        for key, value in RUNTIME_LIMITS.items():
            if risk.get(key) != value:
                raise ValueError("The local runtime did not retain the pilot risk settings")
        if state.get("auto_trade") is not False:
            raise ValueError("Automatic trading changed during pilot preparation")
        _write_limits_draft()
        setup = _get(client, "/api/launch/setup")
        if setup.get("risk_policy", {}).get("values") != LiveLimits.model_validate(PILOT_LIMITS).model_dump(mode="json"):
            raise ValueError("The dashboard did not load the saved pilot limits draft")
        return {
            "ok": True,
            "selected_first_venue": "kalshi",
            "environment": "production",
            "account_cash_usd": account.get("balance_usd"),
            "account_inventory_reconciled": True,
            "risk_cap_per_order_usd": "1",
            "lifetime_native_order_attempts": 1,
            "quarter_kelly": True,
            "unit_size_usd": "5",
            "auto_trade": state.get("auto_trade"),
            "safe_to_arm": setup.get("safe_to_arm"),
            "verified_native_markets": setup.get("scope", {}).get("verified_native_markets"),
            "real_reference_markets": setup.get("scope", {}).get("real_reference_markets"),
            "note": "Account monitoring and pilot limits are prepared. No order or launch evidence was created.",
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8002")
    args = parser.parse_args()
    try:
        result = prepare(args.url)
    except (ValueError, OSError, httpx.HTTPError):
        print(json.dumps({"ok": False, "error": "kalshi_pilot_preparation_failed"}))
        return 2
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
