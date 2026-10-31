"""Persist the reviewed DEN-OKC identities and a disabled Kalshi pilot.

This reads the existing production monitor and writes configuration only. It
cannot create a native execution mapping, approve a model, arm, or place orders.
The recorded source observations keep their original timestamps in the review.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

import httpx

from evplatform.core.models import Market
from evplatform.core.native_runtime import build_fingerprint
from evplatform.core.paths import PROJECT_ROOT, runtime_data_dir
from evplatform.core.readiness import LaunchProfile, LiveLimits, digest_json
from evplatform.core.structural_review import StructuralPilotReview
from evplatform.integrations.nba_moneyline import source_moneyline_outcomes
from evplatform.integrations.normalization import MappingFile, VerifiedOfferMapping
from evplatform.venues.book_normalization import NativeBookMapper


REVIEW = PROJECT_ROOT / "config" / "nba_pilot_identity_review_20261022.json"


def _write_once(path: Path, payload: dict) -> None:
    serialized = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if path.exists():
        if json.loads(path.read_text(encoding="utf-8")) != payload:
            raise ValueError(f"Existing {path.name} differs; review it instead of overwriting")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                            prefix=path.name + ".", suffix=".tmp", delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(serialized)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _replace_reviewed_json(path: Path, payload: dict) -> None:
    """Atomically persist an already validated, append-only review update."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                            prefix=path.name + ".", suffix=".tmp", delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(payload, stream, indent=2, sort_keys=True)
        stream.write("\n")
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _append_reviewed_mappings(path: Path, mappings: MappingFile) -> None:
    payload = mappings.model_dump(mode="json")
    if not path.exists():
        _write_once(path, payload)
        return
    previous = MappingFile.model_validate_json(path.read_text(encoding="utf-8"))
    old = {item.signature: item for item in previous.mappings}
    new = {item.signature: item for item in mappings.mappings}
    if previous.version != mappings.version or len(new) != len(mappings.mappings) or any(
            signature not in new or new[signature] != item for signature, item in old.items()):
        raise ValueError("Reviewed source mappings changed or disappeared; inspect before replacing")
    if old != new:
        _replace_reviewed_json(path, payload)


def _extend_structural_review(path: Path, review: StructuralPilotReview) -> None:
    payload = review.model_dump(mode="json")
    if not path.exists():
        _write_once(path, payload)
        return
    previous = StructuralPilotReview.model_validate_json(path.read_text(encoding="utf-8"))
    old, new = previous.model_dump(mode="json"), dict(payload)
    old_signatures = old.pop("source_signatures")
    new_signatures = new.pop("source_signatures")
    old.pop("reviewed_at")
    new.pop("reviewed_at")
    old_provenance = old.pop("review_provenance")
    new_provenance = new.pop("review_provenance")
    if (old != new or any(new_signatures.get(key) != value for key, value in old_signatures.items())
            or review.reviewed_at < previous.reviewed_at
            or not new_provenance.startswith(old_provenance)):
        raise ValueError("Structural pilot scope or authorization changed; inspect before replacing")
    if payload != previous.model_dump(mode="json"):
        _replace_reviewed_json(path, payload)


def _refresh_disabled_pilot(path: Path, payload: dict, *, mutable_fields: tuple[str, ...]) -> None:
    """Refresh only build-bound metadata of this still-disabled reviewed pilot."""
    if not path.exists():
        _write_once(path, payload)
        return
    previous = json.loads(path.read_text(encoding="utf-8"))
    if previous == payload:
        return
    comparable = dict(previous)
    for field in mutable_fields:
        if field not in comparable:
            raise ValueError(f"Existing {path.name} has no {field}; review it manually")
        comparable[field] = payload[field]
    if comparable != payload:
        raise ValueError(f"Existing {path.name} has changed scope or permissions; review it manually")
    if path.name == "launch_profile.json":
        old = LaunchProfile.model_validate(previous)
        if not old.validation_pilot or old.evidence or old.limits.max_native_order_attempts != 1:
            raise ValueError("Existing pilot is not disabled and one-attempt")
    elif path.name == "nba_pilot_owner_approval_draft.json":
        if previous.get("approved") is not False or previous.get("execution_authorized") is not False:
            raise ValueError("Existing owner approval is no longer a disabled draft")
    with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                            prefix=path.name + ".", suffix=".tmp", delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(payload, stream, indent=2, sort_keys=True)
        stream.write("\n")
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def validated_review(path: Path = REVIEW):
    raw = json.loads(path.read_text(encoding="utf-8"))
    if raw.get("schema_version") != 1:
        raise ValueError("Unsupported identity review version")
    market = Market.model_validate(raw["market"])
    structural = StructuralPilotReview.model_validate(raw["structural_review"])
    if (market.id != structural.canonical_market_id or market.event_id != structural.canonical_event_id
            or set(market.outcomes) != set(structural.native_outcomes)
            or market.start_time.isoformat() != "2026-10-23T01:30:00+00:00"
            or market.status != "scheduled"):
        raise ValueError("Reviewed canonical NBA event is inconsistent")
    mappings = MappingFile(mappings=[VerifiedOfferMapping.model_validate(row)
                                    for row in raw["source_mappings"]])
    if set(structural.source_signatures.values()) != {item.signature for item in mappings.mappings}:
        raise ValueError("Structural review and source signatures differ")
    for item in mappings.mappings:
        identity = raw["source_identities"].get(item.signature)
        observation = raw["captured_observations"].get(item.signature)
        if (item.market != market or item.settlement_equivalence_verified
                or not isinstance(identity, dict) or not isinstance(observation, dict)
                or digest_json(identity) != item.signature
                or source_moneyline_outcomes(identity, observation,
                    home="Oklahoma City Thunder", away="Denver Nuggets") != item.outcome_map):
            raise ValueError("Recorded outcome review no longer matches the exact source identity")
    return raw, mappings, structural


def bootstrap(data_dir: Path, *, url: str = "http://127.0.0.1:8002") -> dict:
    raw, mappings, structural = validated_review()
    with httpx.Client(base_url=url, timeout=25, trust_env=False, follow_redirects=False) as client:
        state = client.get("/api/state"); state.raise_for_status()
        before = state.json()
        if (before.get("mode") != "production" or before.get("sample_feed_enabled") is not False
                or before.get("auto_trade") is not False or before.get("kill_switch") is not True):
            raise ValueError("Production must remain stopped with synthetic feed disabled")
        probe = client.post("/api/launch/probe", json={"venue": "kalshi"},
                            headers={"X-EV-Control": "local"}); probe.raise_for_status()
        account = probe.json()["account"]
        if (account.get("environment") != "production" or account.get("identity_verified") is not True
                or account.get("inventory_reconciled") is not True
                or account.get("cash_available_verified") is not True
                or not account.get("account_fingerprint")):
            raise ValueError("Current read-only Kalshi account identity/inventory is incomplete")
        setup = client.get("/api/launch/setup"); setup.raise_for_status()
        pricing = setup.json()["pricing"]
        orders = client.get("/api/native/orders"); orders.raise_for_status()
        if orders.json().get("orders"):
            raise ValueError("Existing native order attempt requires reconciliation before pilot refresh")
    if (data_dir / "launch_portfolio.json").exists():
        raise ValueError("An existing launch portfolio must be reviewed separately")
    source_path = data_dir / "verified_mappings.json"
    if source_path.exists():
        existing = MappingFile.model_validate_json(source_path.read_text(encoding="utf-8"))
        combined = {item.signature: item for item in existing.mappings}
        for item in mappings.mappings:
            if item.signature in combined and combined[item.signature] != item:
                raise ValueError("An existing source mapping conflicts with the reviewed NBA identity")
            combined[item.signature] = item
        mappings = MappingFile(mappings=list(combined.values()))
    native_mapping_digest = NativeBookMapper.from_file(data_dir / "native_mappings.json").mapping_digest
    limit = LiveLimits(max_order_stake_usd="1", max_event_exposure_usd="1",
        max_player_exposure_usd="1", max_venue_exposure_usd="1",
        max_correlated_exposure_usd="1", max_total_exposure_usd="1",
        daily_loss_limit_usd="1", available_cash_only=True,
        block_overlapping_events=True, block_overlapping_players=True,
        stake_sizing="quarter_kelly", unit_size_usd="5", max_open_orders=1,
        max_orders_per_minute=1, max_native_order_attempts=1)
    profile = LaunchProfile(venue="kalshi", environment="production",
        account_fingerprint=account["account_fingerprint"], build_id=build_fingerprint(),
        market_ids=(structural.canonical_market_id,), strategy_ids=("native_positive_ev",),
        provider_ids=tuple(sorted({key.split("/", 1)[0] for key in structural.source_signatures})),
        mapping_digest=native_mapping_digest, pricing_digest=digest_json(pricing),
        limits=limit, min_reference_books=2, max_reference_age_ms=5000,
        max_orderbook_age_ms=2500, settlement_policy="kalshi_ioc_binary_halt_on_exception",
        validation_pilot=True, evidence=())
    # This is a proposed owner decision, not evidence or permission. The
    # research-only model remains research-only even if this draft is saved.
    approval = {"schema_version": 1, "approved": False, "owner_approved_at": None,
        "profile_digest": profile.digest(), "build_id": profile.build_id,
        "pricing_digest": profile.pricing_digest, "mapping_digest": profile.mapping_digest,
        "market_id": structural.canonical_market_id,
        "native_outcomes": structural.native_outcomes,
        "limits": limit.model_dump(mode="json"),
        "assumptions": ["Initial research weights are not backtested or statistically validated",
                        "Sportsbook exceptional settlement rules are unverified",
                        "Kalshi cancellation can resolve at fair price, not sportsbook refund",
                        "The $1 ceiling is a proposal, not prior stake approval"],
        "execution_authorized": False}
    _append_reviewed_mappings(source_path, mappings)
    _extend_structural_review(data_dir / "nba_pilot_structural_review.json", structural)
    _refresh_disabled_pilot(data_dir / "nba_pilot_owner_approval_draft.json", approval,
                            mutable_fields=("profile_digest", "build_id"))
    _refresh_disabled_pilot(data_dir / "launch_profile.json", profile.model_dump(mode="json"),
                            mutable_fields=("build_id",))
    return {"source_mappings": len(mappings.mappings), "native_execution_mappings": 0,
        "pilot_profile_saved": True, "owner_approval": False,
        "settlement_equivalence_verified": False, "execution_authorized": False,
        "restart_required": True}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=runtime_data_dir())
    parser.add_argument("--url", default="http://127.0.0.1:8002")
    args = parser.parse_args()
    try:
        print(json.dumps(bootstrap(args.data_dir.resolve(), url=args.url)))
        return 0
    except (ValueError, OSError, KeyError, TypeError, httpx.HTTPError) as exc:
        print(json.dumps({"ok": False, "error": str(exc) if isinstance(exc, ValueError) else
                          "Read-only account probe or local bootstrap failed"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
