"""Read-only native history collection. Never issues a trading authorization.

The HTTP boundary allows documented account reads and authentication/subscription
handshakes only. Raw account evidence stays in protected local files.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from decimal import Decimal
from urllib.parse import quote

import httpx

from .models import utcnow
from .native_runtime import build_fingerprint
from ..venues.account_probe import probe_native_account
from ..venues.kalshi import KalshiClient, KalshiSettings
from ..venues.novig import NovigClient, NovigSettings
from ..venues.prophetx import ProphetXClient, ProphetXSettings
from ..venues.prophetx_financial import authenticated_identity


_HOSTS = {"kalshi": set(KalshiClient.HOSTS.values()), "novig": set(NovigClient.HOSTS.values()),
          "prophetx": set(ProphetXClient.HOSTS.values())}
_GET = {
    "kalshi": r"/trade-api/v2/(?:api_keys|portfolio/(?:balance|orders|fills|settlements|positions))",
    "novig": r"/v3/(?:orders|portfolio/fills|history/positions|account/(?:orders|positions|subaccounts/[0-9a-fA-F-]+/(?:balance|transactions)))",
    "prophetx": r"/partner/(?:websocket/connection-config|v4/mm/(?:get_balance|get_order_history|get_transactions|get_trades))",
}
_POST = {"kalshi": set(), "novig": {"/v3/echo"},
         "prophetx": {"/partner/auth/login", "/partner/auth/refresh", "/partner/v4/mm/websocket"}}


def read_guard(venue):
    async def guard(request):
        origin = f"{request.url.scheme}://{request.url.host}"
        allowed_origins = {value.split("/partner")[0] for value in _HOSTS[venue]}
        allowed = request.url.port in {None, 443} and not request.url.userinfo and origin in allowed_origins and (
            request.method == "GET" and re.fullmatch(_GET[venue], request.url.path)
            or request.method == "POST" and request.url.path in _POST[venue])
        if not allowed:
            raise ValueError("Read-only lifecycle capture rejected a non-allowlisted request")
    return guard


def sanitize(value):
    """Never retain credentials or auth handshake material in evidence exports."""
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            normalized = re.sub(r"[^a-z]", "", key.lower())
            if normalized in {"keyid", "apikeyid", "apikey", "accesskey", "secretkey", "privatekey",
                              "accesstoken", "refreshtoken", "authorization", "password", "signature", "auth",
                              "secret", "token", "bearertoken", "sessiontoken", "authentication", "jwt"}:
                result[key] = "[redacted]"
            else:
                result[key] = sanitize(item)
        return result
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    if isinstance(value, Decimal):
        return str(value)
    return value


def protected_json(path: Path, value):
    from ..venues.novig_setup import protect_private_path
    path.parent.mkdir(parents=True, exist_ok=True)
    protect_private_path(path.parent)
    with path.open("x", encoding="utf-8") as handle:
        protect_private_path(path)
        json.dump(value, handle, indent=2, allow_nan=False)
        handle.write("\n")


def local_oms_snapshot(database: Path):
    """An SQLite read transaction; absence is never replaced with sample data."""
    if not database.is_file():
        return {"available": False, "reason": "database_missing", "tables": {}}
    allowed = ("native_orders", "native_fill_evidence", "native_reconciliations", "native_settlements",
               "native_fill_conflicts", "native_settlement_invalidations")
    result = {}
    with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=5) as db:
        db.execute("BEGIN")
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in allowed:
            if table not in tables:
                result[table] = {"available": False, "rows": []}
                continue
            cursor = db.execute(f"SELECT * FROM {table} LIMIT 10001")
            columns = [item[0] for item in cursor.description]
            rows = [dict(zip(columns, row)) for row in cursor]
            result[table] = {"available": True, "complete": len(rows) <= 10000, "rows": rows[:10000]}
    return {"available": True, "captured_at": utcnow().isoformat(), "tables": result}


async def _pages(client, path, collection, *, params=None, envelope=None, cursor_key, cursor_param,
                 missing_cursor_ends=True, max_pages=4):
    pages, cursors, cursor = [], set(), None
    for _ in range(max_pages):
        raw = await client.request("GET", path, params={"limit": 100, **(params or {}),
            **({cursor_param: cursor} if cursor else {})})
        pages.append({"received_at": utcnow().isoformat(), "response": sanitize(raw)})
        body = raw.get(envelope) if envelope and isinstance(raw, dict) else raw
        if (not isinstance(body, dict) or not isinstance(body.get(collection), list)
                or any(not isinstance(row, dict) for row in body[collection])):
            return {"pages": pages, "rows": [], "complete": False, "error": "unexpected_history_shape"}
        cursor = body.get(cursor_key)
        rows = [row for page in pages for row in
                ((page["response"].get(envelope) if envelope else page["response"])[collection])]
        if cursor == "" or cursor is None and missing_cursor_ends and cursor_key not in body:
            return {"pages": pages, "rows": rows, "complete": True}
        if not isinstance(cursor, str) or not cursor.strip() or cursor in cursors:
            return {"pages": pages, "rows": rows, "complete": False, "error": "invalid_history_cursor"}
        cursors.add(cursor)
    return {"pages": pages, "rows": rows, "complete": False, "error": "history_page_bound"}


def configured_client(venue, environment, values, http):
    if venue == "kalshi":
        # No production credentials are silently substituted into a sandbox.
        prefix = "KALSHI_" if environment == "production" else "KALSHI_SANDBOX_"
        return KalshiClient(KalshiSettings(environment=environment,
            api_key_id=values.get(prefix+"API_KEY_ID", ""), private_key_path=values.get(prefix+"PRIVATE_KEY_PATH", "")), http)
    if venue == "novig":
        prefix = "NOVIG_PRODUCTION_TRADING_" if environment == "production" else "NOVIG_PAPER_TRADING_"
        return NovigClient(NovigSettings(environment=environment,
            api_key=values.get(prefix+"API_KEY", ""), private_key_path=values.get(prefix+"PRIVATE_KEY_PATH", "")), http)
    if venue == "prophetx":
        prefix = "PROPHETX_" if environment == "production" else "PROPHETX_SANDBOX_"
        return ProphetXClient(ProphetXSettings(environment=environment,
            api_key=values.get(prefix+"API_KEY", ""), secret_key=values.get(prefix+"SECRET_KEY", "")), http)
    raise ValueError("Unsupported capture venue")


async def capture_venue(venue, environment, values, *, max_pages=4):
    if environment not in {"sandbox", "production"} or not 1 <= max_pages <= 20:
        raise ValueError("Explicit environment and bounded history are required")
    result = {"venue": venue, "environment": environment, "captured_at": utcnow().isoformat(),
              "authenticated": False, "identity_verified": False, "account_fingerprint": None,
              "orders_submitted": 0, "financial_mutations": 0, "collections": {}, "errors": []}
    stream = None
    async with httpx.AsyncClient(timeout=15, trust_env=False, follow_redirects=False,
                                event_hooks={"request": [read_guard(venue)]}) as http:
        client = configured_client(venue, environment, values, http)
        try:
            await client.connect()
            result["authenticated"] = True
            if venue in {"kalshi", "novig"}:
                account = await probe_native_account(client)
                result.update(identity_verified=account.identity_verified, account_fingerprint=account.account_fingerprint)
                result["account_snapshot"] = account.model_dump(mode="json")
            else:
                async def consume():
                    async for _ in client.stream_updates():
                        pass
                stream = asyncio.create_task(consume())
                async with asyncio.timeout(20):
                    while not client._signin_confirmed:
                        if stream.done():
                            await stream
                            raise ValueError("Private identity unavailable")
                        await asyncio.sleep(.1)
                _, fingerprint = authenticated_identity(client)
                result.update(identity_verified=True, account_fingerprint=fingerprint)
                result["account_snapshot"] = {"observed_at": utcnow().isoformat(),
                    "balance_response": sanitize(await client.fetch_balance()), "identity_verified": True}
            specs = []
            if venue == "kalshi":
                specs = [(name, f"/portfolio/{name}", name, {}, None, "cursor", "cursor", False)
                         for name in ("orders", "fills", "settlements")]
                result["history_scope"] = "Current portfolio endpoints only; archived records and retention are not inferred"
            elif venue == "novig":
                specs = [("orders_"+status.lower(), "/v3/orders", "items", {"status": status}, None, "next", "after", True)
                         for status in ("PENDING", "OPEN", "FILLED", "CANCELED", "REJECTED")]
                specs += [("fills", "/v3/portfolio/fills", "items", {}, None, "next", "after", True),
                    ("transactions", f"/v3/account/subaccounts/{quote(client.settings.api_key, safe='')}/transactions", "items", {}, None, "next", "after", True),
                    ("settled_positions", "/v3/history/positions", "items", {"status": "SETTLED"}, None, "next", "after", True)]
                result["history_scope"] = "Personal trading wallet; all five order statuses; served history only"
            else:
                end = int(utcnow().timestamp())
                window = {"from": end-7*86400, "to": end}
                specs = [(name, "/v4/mm/get_"+endpoint, collection, window, "data", "next_cursor", "next_cursor", True)
                         for name, endpoint, collection in (("orders", "order_history", "orders"),
                             ("fills", "trades", "trades"), ("transactions", "transactions", "transactions"))]
                result["history_scope"] = {"last_days": 7, "from": window["from"], "to": window["to"],
                    "note": "Bounded reporting history; eventual consistency and older history are not certified"}
            for name, path, collection, params, envelope, cursor_key, cursor_param, missing_ends in specs:
                try:
                    result["collections"][name] = await _pages(client, path, collection, params=params,
                        envelope=envelope, cursor_key=cursor_key, cursor_param=cursor_param,
                        missing_cursor_ends=missing_ends, max_pages=max_pages)
                except Exception as exc:
                    result["collections"][name] = {"complete": False, "rows": [], "error": type(exc).__name__,
                        "http_status": getattr(exc, "status_code", None)}
        except Exception as exc:
            result["errors"].append({"kind": type(exc).__name__, "http_status": getattr(exc, "status_code", None)})
        finally:
            if stream:
                stream.cancel()
                await asyncio.gather(stream, return_exceptions=True)
            await client.disconnect()
    return result


def capture_summary(artifact):
    return {"schema_version": 1, "captured_at": artifact["captured_at"], "build_id": artifact["build_id"],
        "artifact_sha256": hashlib.sha256(json.dumps(artifact, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        "execution_enabled": False, "certifies_current_oms": False,
        "coverage": [{"venue": record["venue"], "environment": record["environment"],
            "authenticated": record["authenticated"], "identity_verified": record["identity_verified"],
            "account_fingerprint": record["account_fingerprint"], "errors": record["errors"],
            "history_scope": record.get("history_scope"),
            "collections": {name: {"rows": len(value.get("rows", [])), "complete": value["complete"],
                "error": value.get("error"), "http_status": value.get("http_status")}
                for name, value in record["collections"].items()}} for record in artifact["venues"]],
        "local_oms_counts": {name: {"available": table["available"], "rows": len(table["rows"]),
            "complete": table.get("complete", False)} for name, table in artifact["local_oms"]["tables"].items()},
        "missing_evidence": ["Our durable intent before transmission and native acknowledgement for the same client ID",
            "Our partial-fill and cancellation race handling on genuine native orders",
            "Restart with a durable live order and exact post-restart reconciliation",
            "Verified final settlement, cash ledger reconciliation and idempotent position release"],
        "note": "Authenticated history is genuine read access, not proof this application executed an order lifecycle. No LaunchEvidence or permissions are issued."}
