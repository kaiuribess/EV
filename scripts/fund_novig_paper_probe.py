"""Fund the existing Novig Paper trading wallet with $2 of play money once.

This tool is pinned to api.paper.novig.com. It never uses production keys,
places orders, or treats an accepted transfer as completed. The durable local
journal is created before transmission and reuses the same idempotency key on
an uncertain retry. No secret is written to the journal or printed.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import httpx
from dotenv import load_dotenv

from evplatform.venues.common import json_body
from evplatform.venues.novig import NovigClient, NovigSettings


ROOT = Path(__file__).resolve().parents[1]
JOURNAL = ROOT / "data" / "novig_paper_probe_transfer.json"
HOST = "https://api.paper.novig.com"
AMOUNT = "2.00000"


def _write_new_journal(target: str, management: str) -> dict:
    JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    record = {"version": 1, "environment": "sandbox",
              "target_fingerprint": hashlib.sha256(target.encode()).hexdigest(),
              "management_fingerprint": hashlib.sha256(management.encode()).hexdigest(),
              "amount": AMOUNT, "clientTransferId": "ev-" + uuid4().hex,
              "transferId": None, "status": "prepared"}
    try:
        with JOURNAL.open("x", encoding="utf-8") as file:
            file.write(json.dumps(record, sort_keys=True) + "\n")
            file.flush()
            os.fsync(file.fileno())
        return record
    except FileExistsError:
        record = json.loads(JOURNAL.read_text(encoding="utf-8"))
        if (record.get("version") != 1 or record.get("environment") != "sandbox"
                or record.get("target_fingerprint") != hashlib.sha256(target.encode()).hexdigest()
                or record.get("management_fingerprint") != hashlib.sha256(management.encode()).hexdigest()
                or record.get("amount") != AMOUNT
                or not isinstance(record.get("clientTransferId"), str)):
            raise ValueError("Paper transfer journal belongs to another configuration")
        return record


def _update_journal(record: dict) -> None:
    staging = JOURNAL.with_name(JOURNAL.name + "." + uuid4().hex + ".pending")
    with staging.open("x", encoding="utf-8") as file:
        file.write(json.dumps(record, sort_keys=True) + "\n")
        file.flush()
        os.fsync(file.fileno())
    os.replace(staging, JOURNAL)


async def signed(client: NovigClient, method: str, path: str, body: dict | None = None,
                 *, expected: int = 200):
    if client.settings.environment != "sandbox" or not path.startswith("/v3/account/"):
        raise ValueError("Paper account route required")
    if method == "POST" and not path.endswith("/transfer"):
        raise ValueError("Only Paper transfer submission is supported")
    raw = json_body(body)
    request = client.http.build_request(method, HOST + path, content=raw,
                                        headers=client.headers(method, path, body=raw))
    if request.url.host != "api.paper.novig.com" or request.url.query:
        raise ValueError("Unexpected Paper host or query")
    response = await client.http.send(request, follow_redirects=False)
    if response.status_code != expected:
        raise ValueError(f"Paper account HTTP {response.status_code}")
    result = response.json(parse_float=Decimal)
    if not isinstance(result, (dict, list)):
        raise ValueError("Unexpected Paper account response")
    return result


async def fund() -> dict:
    load_dotenv(ROOT / ".env", override=False)
    management_id = os.getenv("NOVIG_MANAGEMENT_API_KEY", "")
    management_path = os.getenv("NOVIG_MANAGEMENT_PRIVATE_KEY_PATH", "")
    trading_id = os.getenv("NOVIG_PAPER_TRADING_API_KEY", "")
    trading_path = os.getenv("NOVIG_PAPER_TRADING_PRIVATE_KEY_PATH", "")
    if not all((management_id, management_path, trading_id, trading_path)):
        raise ValueError("Configured Novig Paper management and trading keys are required")
    UUID(management_id)
    UUID(trading_id)
    management = NovigClient(NovigSettings(environment="sandbox", api_key=management_id,
                                          private_key_path=management_path))
    trading = NovigClient(NovigSettings(environment="sandbox", api_key=trading_id,
                                       private_key_path=trading_path))
    target_path = f"/v3/account/subaccounts/{trading_id}"
    try:
        await management.connect()
        await trading.connect()
        cash = await signed(management, "GET", "/v3/account/balance")
        wallets = await signed(management, "GET", "/v3/account/subaccounts")
        wallet = await signed(trading, "GET", target_path + "/balance")
        if (not isinstance(cash, dict) or not isinstance(wallets, list)
                or sum(isinstance(item, dict) and item.get("keyId") == trading_id for item in wallets) != 1
                or not isinstance(wallet, dict) or wallet.get("keyId") != trading_id):
            raise ValueError("Paper wallet identity is not verified")
        record = _write_new_journal(trading_id, management_id)
        if record.get("status") not in {"prepared", "Requested", "Applied", "Rejected"}:
            raise ValueError("Unknown Paper transfer journal state")
        if Decimal(str(cash.get("balance", "0"))) < Decimal(AMOUNT) and not record.get("transferId"):
            raise ValueError("Paper cash wallet lacks play funds")
        newly_submitted = record.get("transferId") is None
        if newly_submitted:
            reply = await signed(management, "POST", target_path + "/transfer",
                                 {"direction": "fund", "amount": AMOUNT,
                                  "clientTransferId": record["clientTransferId"]}, expected=202)
            if (not isinstance(reply, dict) or reply.get("clientTransferId") != record["clientTransferId"]
                    or reply.get("direction") != "fund" or reply.get("amount") != AMOUNT
                    or reply.get("status") not in {"Requested", "Applied"}):
                raise ValueError("Paper transfer acknowledgement did not match the durable intent")
            UUID(reply["transferId"])
            record.update(transferId=reply["transferId"], status=reply["status"])
            _update_journal(record)
        for _ in range(15):
            latest = await signed(management, "GET", target_path + "/transfers/" + record["transferId"])
            if (not isinstance(latest, dict) or latest.get("transferId") != record["transferId"]
                    or latest.get("clientTransferId") != record["clientTransferId"]
                    or latest.get("amount") != AMOUNT or latest.get("direction") != "fund"):
                raise ValueError("Paper transfer status changed identity")
            status = latest.get("status")
            if status in {"Applied", "Rejected"}:
                record["status"] = status
                _update_journal(record)
                final_wallet = await signed(trading, "GET", target_path + "/balance")
                return {"ok": status == "Applied", "paper_only": True, "status": status,
                        "wallet_usd": final_wallet.get("balance") if isinstance(final_wallet, dict) else None,
                        "new_transfer": newly_submitted}
            if status != "Requested":
                raise ValueError("Unknown Paper transfer status")
            await asyncio.sleep(1)
        return {"ok": False, "paper_only": True, "status": "Requested",
                "note": "Transfer pending; rerun to check the same durable transfer"}
    finally:
        await management.disconnect()
        await trading.disconnect()


if __name__ == "__main__":
    try:
        print(json.dumps(asyncio.run(fund()), sort_keys=True))
    except Exception as exc:
        print(json.dumps({"ok": False, "paper_only": True, "error_type": type(exc).__name__},
                         sort_keys=True))
        raise SystemExit(1)
