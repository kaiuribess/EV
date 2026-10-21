"""Read personal US account activities; preserve missing settlement cash evidence."""
import argparse
import asyncio
from collections import Counter
import hashlib
import json
import os
from pathlib import Path

from dotenv import dotenv_values
import httpx

from evplatform.core.lifecycle_capture import build_fingerprint, protected_json, sanitize
from evplatform.core.lifecycle_evidence import artifact_digest
from evplatform.core.models import utcnow
from evplatform.venues.polymarket_us import PolymarketUSClient, PolymarketUSSettings
from evplatform.venues.polymarket_us_activities import activity_read_guard, capture_activities
from evplatform.venues.polymarket_us_private_evidence import capture_private_evidence


async def run(args):
    if args.output.exists():
        raise ValueError("Use a new evidence directory")
    values = {**dotenv_values(args.env_file), **os.environ}
    settings = PolymarketUSSettings(api_key_id=values.get("POLYMARKET_US_API_KEY_ID", ""),
        secret_key=values.get("POLYMARKET_US_SECRET_KEY", ""))
    artifact = {"schema_version": 1, "venue": "polymarket_us", "environment": "production",
        "collection_method": "authenticated_read_only", "captured_at": utcnow().isoformat(),
        "build_id": build_fingerprint(), "authenticated": False, "account_identity_verified": False,
        "credential_scope_fingerprint": hashlib.sha256(f"polymarket_us:production:credential:{settings.api_key_id}".encode()).hexdigest(),
        "participant_fingerprint": None, "cash_posting_join_verified": False,
        "orders_submitted": 0, "financial_mutations": 0, "errors": [], "activities": None}
    async with httpx.AsyncClient(timeout=15, trust_env=False, follow_redirects=False,
            event_hooks={"request": [activity_read_guard]}) as http:
        client = PolymarketUSClient(settings, http)
        try:
            await client.connect()
            artifact["authenticated"] = True
            artifact["account_balance_response"] = client.account_snapshot
            if getattr(args, "stream_seconds", 0):
                try:
                    artifact["private_wire_evidence"] = await capture_private_evidence(client, seconds=args.stream_seconds)
                except Exception as exc:
                    artifact["errors"].append({"operation": "private_wire", "error": type(exc).__name__})
            try:
                participant = await client.authenticated_participant()
                identity = participant.get("rfqUserId") if isinstance(participant, dict) else None
                if not isinstance(identity, str) or not identity.strip():
                    raise ValueError("Participant identity unavailable")
                artifact["participant_fingerprint"] = hashlib.sha256(f"polymarket_us:production:rfq-participant:{identity}".encode()).hexdigest()
                artifact["participant_note"] = "Authenticated RFQ participant, not a verified cash-account identity"
            except Exception as exc:
                artifact["errors"].append({"operation": "participant", "error": type(exc).__name__, "http_status": getattr(exc, "status_code", None)})
            artifact["activities"] = await capture_activities(client, max_pages=args.max_pages)
        except Exception as exc:
            artifact["errors"].append({"operation": "account_capture", "error": type(exc).__name__, "http_status": getattr(exc, "status_code", None)})
        finally:
            await client.disconnect()
    artifact["finished_at"] = utcnow().isoformat()
    artifact = sanitize(artifact)
    activities = artifact["activities"] or {}
    rows = activities.get("parsed", [])
    summary = {"schema_version": 1, "captured_at": artifact["captured_at"], "build_id": artifact["build_id"],
        "artifact_sha256": artifact_digest(artifact), "authenticated": artifact["authenticated"],
        "account_identity_verified": False, "participant_observed": bool(artifact["participant_fingerprint"]),
        "activity_count": len(rows), "counts_by_kind": dict(Counter(row["kind"] for row in rows)),
        "trade_states": dict(Counter(row["state"] for row in rows if row["kind"] == "trade")),
        "pagination_complete": activities.get("pagination_complete", False), "parse_complete": activities.get("complete", False),
        "errors": artifact["errors"] + activities.get("errors", []), "cash_posting_join_verified": False,
        "settlements_emitted": 0, "certifies_current_oms": False, "execution_enabled": False}
    if "private_wire_evidence" in artifact:
        summary["private_wire"] = artifact["private_wire_evidence"]["summary"]
    protected_json(args.output / "activities.json", artifact)
    protected_json(args.output / "coverage.json", summary)
    print(json.dumps({"ok": True, **summary}))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-pages", type=int, choices=range(1, 21), default=4)
    parser.add_argument("--stream-seconds", type=int, choices=range(0, 61), default=0,
                        help="Optional bounded private read-only subscription capture; zero skips")
    args = parser.parse_args(argv)
    try:
        asyncio.run(run(args))
    except (ValueError, OSError, TypeError):
        print(json.dumps({"ok": False, "error": "capture_or_protected_storage_failed"}))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
