"""Operator-run production Novig setup. Defaults to read-only status.

Opening/funding requires typed confirmation in a real terminal. This tool does
not place orders, enable live trading, switch running monitors, or certify a
launch profile. Secrets and raw account responses are never printed.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from dotenv import dotenv_values

from evplatform.venues.novig import NovigSettings
from evplatform.venues.novig_setup import NovigSetup, NovigSetupTransport, SetupError


async def run(args):
    values = dotenv_values(args.env_file)
    key = values.get("NOVIG_PRODUCTION_MANAGEMENT_API_KEY", "")
    private_path = values.get("NOVIG_PRODUCTION_MANAGEMENT_PRIVATE_KEY_PATH", "")
    if not key or not private_path:
        raise SetupError("production_management_credentials_missing")
    transport = NovigSetupTransport(NovigSettings(environment="production", api_key=key, private_key_path=private_path))
    setup = NovigSetup(transport, Path(args.setup_dir))
    try:
        if args.command in {None, "status"}:
            result = await setup.status()
        elif args.command == "verify-trading":
            trading_key = values.get("NOVIG_PRODUCTION_TRADING_API_KEY", "")
            trading_private = values.get("NOVIG_PRODUCTION_TRADING_PRIVATE_KEY_PATH", "")
            if not trading_key or not trading_private:
                raise SetupError("production_trading_credentials_missing")
            result = await setup.verify_trading(trading_key, trading_private)
        elif args.command == "prepare":
            journal = await setup.prepare(args.label)
            result = {"prepared": True, "journal": str(journal), "remote_wallet_created": False, "trading_enabled": False}
        elif args.command == "open":
            result = await setup.open_wallet(args.journal)
        elif args.command == "reconcile":
            result = await setup.reconcile(args.journal)
        elif args.command == "fund":
            result = await setup.fund(args.journal, args.amount)
        elif args.command == "recover-funding":
            result = await setup.recover_funding(args.journal, args.transfer)
        else:
            result = await setup.export(args.journal, Path(args.env_file))
        print(json.dumps(result, indent=2))
    finally:
        await transport.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--setup-dir", default="data/novig_setup")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("status", help="Read management identity, cash and existing wallets")
    sub.add_parser("verify-trading", help="Read-only verification of configured trading signer, scope and wallet")
    prepare = sub.add_parser("prepare", help="Prepare a protected local signer and journal; no remote mutation")
    prepare.add_argument("--label", required=True)
    for name in ("open", "reconcile", "fund", "recover-funding", "export"):
        command = sub.add_parser(name)
        command.add_argument("--journal", required=True)
        if name == "fund":
            command.add_argument("--amount", required=True, help="Explicit USD decimal amount, at most five decimal places")
        if name == "recover-funding":
            command.add_argument("--transfer", required=True, help="client_transfer_id from this journal's sanitized output")
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print(json.dumps({"ok": False, "error": "interrupted_reconcile_saved_journal_before_any_retry"}))
        return 2
    except SetupError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 2
    except Exception:
        print(json.dumps({"ok": False, "error": "setup_failed_details_redacted_reconcile_before_retry"}))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
