"""Read-only account access diagnostic; never a launch certificate.

Run from the repository root with the project's Python interpreter. Credentials
stay in memory. Only allowlisted cash/identity facts and error classifications
are emitted. ProphetX requires one authentication session before its GET; this
tool never retries a failed login or changes an account, key, order or balance.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import traceback
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import httpx
from dotenv import load_dotenv

from evplatform.venues.account_probe import probe_native_account
from evplatform.venues.common import VenueHTTPError
from evplatform.venues.kalshi import KalshiClient, KalshiSettings
from evplatform.venues.novig import NovigClient, NovigSettings
from evplatform.venues.polymarket_us import PolymarketUSClient, PolymarketUSSettings
from evplatform.venues.prophetx import ProphetXClient, ProphetXSettings


VENUES = ("kalshi", "novig", "prophetx", "polymarket_us")
CREDS = {
    "kalshi": ("KALSHI_API_KEY_ID", "KALSHI_PRIVATE_KEY_PATH"),
    "novig": ("NOVIG_API_KEY", "NOVIG_PRIVATE_KEY_PATH"),
    "prophetx": ("PROPHETX_API_KEY", "PROPHETX_SECRET_KEY"),
    "polymarket_us": ("POLYMARKET_US_API_KEY_ID", "POLYMARKET_US_SECRET_KEY"),
}
KNOWN_SCOPES = {"read", "write", "write::trade", "write::transfer", "trading", "trading::read",
                "management", "management::read"}


def money(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ValueError("Exact decimal amount required")
    number = Decimal(value)
    if not number.is_finite() or number < 0:
        raise ValueError("Finite nonnegative amount required")
    return str(number)


def error_summary(exc):
    # Exception messages and server error codes may contain arbitrary response
    # text; neither is safe for a report written beside user-visible artifacts.
    result = {"ok": False, "error_type": type(exc).__name__}
    if isinstance(exc, VenueHTTPError):
        result["http_status"] = exc.status_code
    frames = traceback.extract_tb(exc.__traceback__)
    if frames:
        frame = frames[-1]
        result["source"] = {"file": Path(frame.filename).name, "function": frame.name, "line": frame.lineno}
    return result


def snapshot_summary(snapshot):
    raw = snapshot.model_dump(mode="json")
    keep = ("venue", "environment", "account_fingerprint", "identity_verified", "observed_at",
            "balance_usd", "cash_available_verified", "open_order_count", "position_count",
            "reconciled", "trading_permission_verified", "geographic_attestation_expires_at")
    result = {key: raw[key] for key in keep}
    result["api_key_scopes"] = [value for value in raw["api_key_scopes"] if value in KNOWN_SCOPES]
    # REST inventory agreement alone is neither a live private-stream baseline
    # nor lifecycle/settlement certification. Never promote it to launch proof.
    result["live_readiness_verified"] = False
    return result


def clock_summary(value):
    if not isinstance(value, str) or not value:
        return {"source_clock_status": "missing"}
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError("Timezone required")
    except ValueError:
        return {"source_clock_status": "invalid"}
    age = (datetime.now(timezone.utc) - stamp).total_seconds()
    return {"source_clock_status": "future" if age < 0 else "valid",
            "source_clock": stamp.isoformat(), "source_age_seconds": age}


def balance_summary(venue, raw):
    if venue == "kalshi":
        return {"reported_balance_usd": money(raw["balance_dollars"])}
    if venue == "novig":
        return {"reported_balance_usd": money(raw["balance"])}
    if venue == "prophetx":
        row = raw["data"]
        money(row["balance"])
        fields = {"balance": "reported_balance_usd", "gec_balance": "global_exposure_credit_usd",
                  "matched_order_balance": "matched_order_balance_usd",
                  "unmatched_order_balance": "unmatched_order_balance_usd"}
        result = {label: money(row[key]) for key, label in fields.items() if key in row}
        result["unmatched_balance_synchronized"] = row.get("unmatched_order_balance_status") == "succeed"
        result.update(clock_summary(row.get("unmatched_order_last_synced_at")))
        return result
    rows = [row for row in raw["balances"] if row.get("currency") == "USD"]
    if len(rows) != 1:
        raise ValueError("Exactly one USD balance required")
    row = rows[0]
    money(row["currentBalance"])
    fields = {"currentBalance": "reported_balance_usd", "buyingPower": "reported_buying_power_usd",
              "assetAvailable": "reported_asset_available_usd", "pendingCredit": "pending_credit_usd",
              "unsettledFunds": "unsettled_funds_usd", "balanceReservation": "balance_reservation_usd"}
    return {**{label: money(row[key]) for key, label in fields.items() if key in row},
            **clock_summary(row.get("lastUpdated"))}


def read_guard(venue, environment):
    hosts = {
        "kalshi": {httpx.URL(KalshiClient.HOSTS[environment]).host},
        "novig": {httpx.URL(NovigClient.HOSTS[environment]).host},
        "prophetx": {httpx.URL(ProphetXClient.HOSTS[environment]).host},
        "polymarket_us": {httpx.URL(PolymarketUSClient.PRIVATE_HOST).host},
    }

    async def guard(request):
        permitted_login = (venue == "prophetx" and request.method == "POST"
                           and request.url.path in {"/partner/auth/login", "/partner/auth/refresh"})
        if (request.url.scheme != "https" or request.url.host not in hosts[venue]
                or (request.method != "GET" and not permitted_login)):
            raise RuntimeError("Account diagnostic cannot transmit account mutations")
    return guard


def build_client(venue, environment, values, http):
    common = {"environment": environment, "timeout_seconds": 10}
    if venue == "kalshi":
        return KalshiClient(KalshiSettings(**common, api_key_id=values[0], private_key_path=values[1]), http)
    if venue == "novig":
        return NovigClient(NovigSettings(**common, api_key=values[0], private_key_path=values[1]), http)
    if venue == "prophetx":
        return ProphetXClient(ProphetXSettings(**common, api_key=values[0], secret_key=values[1]), http)
    return PolymarketUSClient(PolymarketUSSettings(**common, api_key_id=values[0], secret_key=values[1]), http)


async def check_account(venue, environment="production", *, environ=None, transport=None,
                        novig_credential_profile="legacy"):
    environ = os.environ if environ is None else environ
    names = CREDS[venue]
    if novig_credential_profile not in {"legacy", "environment"}:
        raise ValueError("Invalid Novig credential profile")
    if venue == "novig" and novig_credential_profile == "environment":
        prefix = "NOVIG_PRODUCTION_TRADING" if environment == "production" else "NOVIG_PAPER_TRADING"
        names = (prefix + "_API_KEY", prefix + "_PRIVATE_KEY_PATH")
    values = [environ.get(key, "") for key in names]
    result = {"venue": venue, "environment": environment, "money_type": "real" if environment == "production" else "play",
              "credentials_present": all(values), "read_only": True, "live_readiness_verified": False,
              "observed_at": datetime.now(timezone.utc).isoformat(), "checks": {}}
    if not all(values):
        result["blocker"] = "Required backend credentials are missing"
        return result
    if venue == "polymarket_us" and environment != "production":
        result["blocker"] = "No documented personal US sandbox configured"
        return result
    async with httpx.AsyncClient(timeout=10, transport=transport,
                                 event_hooks={"request": [read_guard(venue, environment)]}) as http:
        client = build_client(venue, environment, values, http)
        try:
            async with asyncio.timeout(35):
                try:
                    if venue == "kalshi":
                        raw = await client.request("GET", "/portfolio/balance", params={"subaccount": 0})
                    elif venue == "novig":
                        from urllib.parse import quote
                        raw = await client.request("GET", f"/v3/account/subaccounts/{quote(values[0], safe='')}/balance")
                    elif venue == "prophetx":
                        raw = await client.fetch_balance()
                    else:
                        raw = await client.account_balances()
                        client.authenticated = True  # This exact signed read just succeeded.
                    result["checks"]["balance_read"] = {"ok": True, **balance_summary(venue, raw)}
                except Exception as exc:
                    result["checks"]["balance_read"] = error_summary(exc)
                    return result
                if venue == "prophetx":
                    result["blocker"] = ("Balance read does not verify full history, stable private identity, "
                        "no exposure credits, trading permissions, or settlement; existing monitor is unchanged")
                else:
                    try:
                        snapshot = await probe_native_account(client)
                        result["checks"]["account_inventory"] = {"ok": True, **snapshot_summary(snapshot)}
                    except Exception as exc:
                        result["checks"]["account_inventory"] = error_summary(exc)
        except Exception as exc:
            result["checks"]["completion"] = error_summary(exc)
        finally:
            await client.disconnect()
    return result


async def monitor_scope(url):
    address = httpx.URL(url)
    if address.host not in {"127.0.0.1", "localhost", "::1"} or address.scheme != "http":
        raise ValueError("Only the local loopback dashboard may supply monitor configuration")
    async with httpx.AsyncClient(timeout=5) as client:
        response = await client.get(str(address).rstrip("/") + "/api/state")
        response.raise_for_status()
        state = response.json()
    return {"auto_trade": state.get("auto_trade"), "kill_switch": state.get("kill_switch"),
        "monitors": {row["manifest"]["name"]: {"enabled": row["enabled"],
            "environment": row["config"].get("environment"),
            "authenticated": row["health"].get("authenticated") is True}
            for row in state.get("plugins", []) if row.get("manifest", {}).get("name") in VENUES}}


async def run(args):
    load_dotenv(".env", override=False)
    report = {"schema_version": 1, "purpose": "Read-only account access; not trading authorization",
              "observed_at": datetime.now(timezone.utc).isoformat(), "live_readiness_verified": False}
    try:
        report["local_monitor_scope"] = await monitor_scope(args.dashboard)
    except Exception as exc:
        report["local_monitor_scope"] = error_summary(exc)
    profile = getattr(args, "novig_credential_profile", "legacy")
    report["accounts"] = await asyncio.gather(*(check_account(venue, args.environment,
        novig_credential_profile=profile) for venue in args.venues))
    if args.compare_novig_sandbox and args.environment == "production" and "novig" in args.venues:
        report["accounts"].append(await check_account("novig", "sandbox", novig_credential_profile=profile))
    text = json.dumps(report, indent=2)
    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n", encoding="utf-8")
    print(text)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", choices=("sandbox", "production"), default="production")
    parser.add_argument("--venues", nargs="+", choices=VENUES, default=list(VENUES))
    parser.add_argument("--compare-novig-sandbox", action="store_true")
    parser.add_argument("--novig-credential-profile", choices=("legacy", "environment"), default="legacy")
    parser.add_argument("--dashboard", default="http://127.0.0.1:8001")
    parser.add_argument("--output", default="data/account-access-diagnostic.json")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
