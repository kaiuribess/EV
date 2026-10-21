"""Capture protected native account history without submitting or cancelling orders."""
import argparse
import asyncio
import json
import os
from pathlib import Path

from dotenv import dotenv_values

from evplatform.core.lifecycle_capture import (build_fingerprint, capture_summary, capture_venue,
    local_oms_snapshot, protected_json)
from evplatform.core.models import utcnow


async def run(args):
    if args.output.exists():
        raise ValueError("Evidence directory already exists")
    values = {**dotenv_values(args.env_file), **os.environ}
    artifact = {"schema_version": 1, "captured_at": utcnow().isoformat(), "build_id": build_fingerprint(),
        "collection_method": "authenticated_read_only", "venues": [],
        "local_oms": local_oms_snapshot(args.database)}
    for venue in args.venue:
        artifact["venues"].append(await capture_venue(venue, args.environment, values, max_pages=args.max_pages))
    summary = capture_summary(artifact)
    protected_json(args.output / "account_history.json", artifact)
    protected_json(args.output / "coverage.json", summary)
    print(json.dumps({"ok": True, **summary}))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--venue", nargs="+", choices=("kalshi", "novig", "prophetx"), required=True)
    parser.add_argument("--environment", choices=("sandbox", "production"), required=True)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--database", type=Path, default=Path("data/evplatform.db"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-pages", type=int, choices=range(1, 21), default=4)
    args = parser.parse_args(argv)
    try:
        asyncio.run(run(args))
    except (ValueError, OSError, TypeError):
        print(json.dumps({"ok": False, "error": "capture_or_protected_storage_failed"}))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
