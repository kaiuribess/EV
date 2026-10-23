"""Persist read-only native discovery evidence, never an execution mapping."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile


def review_payload(state: dict, *, source_market_id: str, venue: str,
                   native_market_id: str, native_outcome_id: str) -> dict:
    if (state.get("mode") != "production" or state.get("auto_trade") is not False
            or state.get("kill_switch") is not True):
        raise ValueError("Export only from disarmed production")
    source = [row for row in state.get("reference_estimates", [])
              if row.get("market_id") == source_market_id and not row.get("historical")]
    if len(source) != 1:
        raise ValueError("Select exactly one current sportsbook estimate")
    row = source[0]
    matches = [item for item in row.get("native_research_estimates", [])
               if item.get("quality") == "provisional_native_research"
               and item.get("venue") == venue and item.get("native_market_id") == native_market_id
               and item.get("native_outcome_id") == native_outcome_id and not item.get("historical")]
    if len(matches) != 1:
        raise ValueError("Select exactly one current native market and outcome")
    lead = matches[0]
    if lead.get("execution_eligible") is not False or lead.get("mapping_reviewed") is not False:
        raise ValueError("This export is for unreviewed discovery leads only")
    for key in ("source_event_id", "native_event_id", "source_market_fingerprint",
                "rules_sha256", "price_denomination_usd", "catalog_observed_at"):
        if not lead.get(key):
            raise ValueError("Native discovery lacks required review evidence")
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


def read_reviews(destination: Path, *, limit: int = 30) -> list[dict]:
    if not destination.exists():
        return []
    result = []
    for path in sorted(destination.glob("native-research-*.json"),
                       key=lambda item: item.stat().st_mtime, reverse=True)[:limit]:
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (item.get("schema_version") == 1 and item.get("status") == "unapproved_research_draft"
                and item.get("execution_authorized") is False and item.get("model_approved") is False):
            result.append({**item, "draft_id": path.stem})
    return result
