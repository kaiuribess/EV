"""Export one exact, read-only native discovery lead for human mapping review.

This cannot write native_mappings.json or any approval/launch/attempt record.
The existing verified mapping loader remains the sole execution path.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

import httpx

from evplatform.core.paths import runtime_data_dir


def review_payload(state: dict, *, source_market_id: str, venue: str,
                   native_market_id: str, native_outcome_id: str) -> dict:
    if (state.get("mode") != "production" or state.get("auto_trade") is not False
            or state.get("kill_switch") is not True):
        raise ValueError("Export only from disarmed production")
    source = [row for row in state.get("reference_estimates", [])
              if row.get("market_id") == source_market_id]
    if len(source) != 1:
        raise ValueError("Select exactly one current sportsbook estimate")
    row = source[0]
    matches = [item for item in row.get("native_research_estimates", [])
               if item.get("quality") == "provisional_native_research"
               and item.get("venue") == venue and item.get("native_market_id") == native_market_id
               and item.get("native_outcome_id") == native_outcome_id]
    if len(matches) != 1:
        raise ValueError("Select exactly one current native market and outcome")
    lead = matches[0]
    if lead.get("execution_eligible") is not False or lead.get("mapping_reviewed") is not False:
        raise ValueError("This export is for unreviewed discovery leads only")
    return {"schema_version": 1, "status": "unapproved_research_draft",
            "execution_authorized": False, "model_approved": False,
            "source_market_id": source_market_id, "venue": venue,
            "native_market_id": native_market_id, "native_outcome_id": native_outcome_id,
            "runtime_started_at": state.get("started_at"), "captured_at": state.get("timestamp"),
            "source": {"event_name": row.get("event_name"), "start_time": row.get("start_time"),
                       "market_type": row.get("market_type"), "statistic": row.get("statistic"),
                       "player": row.get("player"), "line": row.get("line"),
                       "probabilities": row.get("probabilities"), "references": row.get("references"),
                       "quality_mode": row.get("quality_mode"), "limitations": row.get("limitations")},
            "native_discovery": lead,
            "review_required": ["Confirm normal WIN/LOSS condition, including period and overtime",
                                "Confirm player/team identity, threshold and source rule provenance",
                                "Verify fresh native status, depth, fee metadata and venue account",
                                "Persist a separately reviewed native mapping through the existing mapping contract",
                                "Obtain exact owner pilot approval; this draft grants no order permission"]}


def save_review(payload: dict, destination: Path) -> Path:
    content = json.dumps(payload, sort_keys=True, indent=2) + "\n"
    digest = hashlib.sha256(content.encode()).hexdigest()[:20]
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / f"native-research-{digest}.json"
    if target.exists():
        if target.read_text(encoding="utf-8") != content:
            raise ValueError("Existing review draft conflicts with this capture")
        return target
    with NamedTemporaryFile("w", encoding="utf-8", dir=destination,
                            prefix="native-research-", suffix=".tmp", delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(content)
    try:
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-market-id", required=True)
    parser.add_argument("--venue", choices=("kalshi", "novig"), required=True)
    parser.add_argument("--native-market-id", required=True)
    parser.add_argument("--native-outcome-id", required=True)
    parser.add_argument("--url", default="http://127.0.0.1:8002")
    parser.add_argument("--data-dir", type=Path)
    args = parser.parse_args()
    with httpx.Client(base_url=args.url, timeout=15, trust_env=False) as client:
        response = client.get("/api/state")
        response.raise_for_status()
        payload = review_payload(response.json(), source_market_id=args.source_market_id,
            venue=args.venue, native_market_id=args.native_market_id,
            native_outcome_id=args.native_outcome_id)
    target = save_review(payload, runtime_data_dir(args.data_dir) / "native_review_drafts")
    print(json.dumps({"saved": str(target), "status": payload["status"],
                      "execution_authorized": False}))


if __name__ == "__main__":
    main()
