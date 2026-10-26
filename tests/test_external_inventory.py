import hashlib
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from evplatform.core.external_inventory import (ExternalInventoryFile, ExternalPosition,
    OwnerTrackedProphetXPosition, PendingExternalOrder, load_external_inventory,
    load_external_inventory_review)
from evplatform.core.risk import RiskEngine
from evplatform.core.config import RiskConfig


def review(artifact, catalog):
    return ExternalPosition(venue="novig", environment="production",
        account_fingerprint="a" * 64, native_order_id="order",
        native_event_id="event", native_market_id="market", native_outcome_id="outcome",
        sport="basketball", quantity=Decimal(169), cost_usd=Decimal(".97175"),
        recorded_at=datetime.now(timezone.utc), source_artifact=artifact.name,
        source_sha256=hashlib.sha256(artifact.read_bytes()).hexdigest(),
        catalog_artifact=catalog.name,
        catalog_sha256=hashlib.sha256(catalog.read_bytes()).hexdigest())


def evidence(tmp_path):
    artifact = tmp_path / "smoke_receipt.json"
    catalog = tmp_path / "market_catalog.json"
    artifact.write_text("{}", encoding="utf-8")
    catalog.write_text('{"event":{"eventId":"event","sport":"BASKETBALL"},'
        '"market":{"eventId":"event","marketId":"market",'
        '"outcomes":[{"outcomeId":"outcome"}]}}', encoding="utf-8")
    row = review(artifact, catalog)
    artifact.write_text('{"venue":"novig","account_fingerprint":"' + "a" * 64 + '",'
        '"order_id":"order","market_id":"market","outcome_id":"outcome",'
        '"native_status":"FILLED","filled_qty":169,"filled_cost_usd":"0.97175",'
        '"recorded_at":"' + row.recorded_at.isoformat() + '"}', encoding="utf-8")
    return artifact, catalog, row.model_copy(update={"source_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest()})


def test_external_receipt_is_bound_to_immutable_local_artifact(tmp_path):
    artifact, catalog, row = evidence(tmp_path)
    path = tmp_path / "external_native_positions.json"
    path.write_text(ExternalInventoryFile(positions=(row,)).model_dump_json(), encoding="utf-8")
    assert load_external_inventory(path) == (row,)
    artifact.write_text('{"order":"changed"}', encoding="utf-8")
    with pytest.raises(ValueError, match="artifact changed"):
        load_external_inventory(path)


def test_external_identity_cannot_be_relabelled_to_avoid_sport_conflict(tmp_path):
    artifact, catalog, row = evidence(tmp_path)
    path = tmp_path / "external_native_positions.json"
    changed = row.model_copy(update={"sport": "football"})
    path.write_text(ExternalInventoryFile(positions=(changed,)).model_dump_json(), encoding="utf-8")
    with pytest.raises(ValueError, match="identity differs"):
        load_external_inventory(path)


def test_external_cost_is_exposure_but_never_subtracted_from_cash():
    risk = RiskEngine(RiskConfig())
    risk.external_positions = (type("External", (), {"cost_usd": Decimal(".97175")})(),)
    assert risk.exposures() == Decimal(".97175")
    assert risk.config.unit_size_usd == Decimal("5")


def test_duplicate_external_position_identity_is_rejected(tmp_path):
    _, _, row = evidence(tmp_path)
    with pytest.raises(ValueError, match="Duplicate external position"):
        ExternalInventoryFile(positions=(row, row))


def test_pending_raw_order_blocks_review_until_authoritative_inventory_exists(tmp_path):
    artifact = tmp_path / "kalshi_raw_smoke_attempt.json"
    artifact.write_text('{"order_id":"external-1","status":"filled_unsettled"}', encoding="utf-8")
    pending = PendingExternalOrder(venue="kalshi", native_order_id="external-1",
        source_artifact=artifact.name, source_sha256=hashlib.sha256(artifact.read_bytes()).hexdigest())
    path = tmp_path / "external_native_positions.json"
    path.write_text(ExternalInventoryFile(pending_orders=(pending,)).model_dump_json(), encoding="utf-8")
    assert load_external_inventory_review(path).pending_orders == (pending,)
    artifact.write_text('{"order_id":"external-2","status":"filled_unsettled"}', encoding="utf-8")
    with pytest.raises(ValueError, match="artifact changed"):
        load_external_inventory_review(path)


def test_owner_tracked_prophetx_fill_counts_exposure_without_becoming_oms_order(tmp_path):
    artifact = tmp_path / "prophetx_raw_fill.json"
    recorded = datetime.now(timezone.utc)
    receipt = {"venue": "prophetx", "environment": "production", "order_id": "native-1",
        "event_id": "event-1", "market_id": "market-1", "strike_id": "strike-1",
        "status": "filled_unsettled", "matching_status": "fully_matched",
        "open_stake_usd": "0", "filled_stake_usd": "1", "maximum_loss_usd": "1.00",
        "recorded_at": recorded.isoformat()}
    import json
    artifact.write_text(json.dumps(receipt), encoding="utf-8")
    row = OwnerTrackedProphetXPosition(account_fingerprint="b" * 64,
        native_order_id="native-1", native_event_id="event-1", native_market_id="market-1",
        native_outcome_id="strike-1", sport="basketball", quantity=Decimal("1"),
        cost_usd=Decimal("1"), recorded_at=recorded, source_artifact=artifact.name,
        source_sha256=hashlib.sha256(artifact.read_bytes()).hexdigest())
    with pytest.raises(ValueError, match="both pending and tracked"):
        ExternalInventoryFile(positions=(row,), pending_orders=(PendingExternalOrder(
            venue="prophetx", native_order_id="native-1", source_artifact=artifact.name,
            source_sha256=row.source_sha256),))
    path = tmp_path / "external_native_positions.json"
    path.write_text(ExternalInventoryFile(positions=(row,)).model_dump_json(), encoding="utf-8")
    assert load_external_inventory(path) == (row,)
    risk = RiskEngine(RiskConfig())
    risk.external_positions = (row,)
    assert risk.exposures() == Decimal("1")
    assert risk.native_orders == {}
    receipt["strike_id"] = "another-strike"
    artifact.write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(ValueError, match="artifact changed"):
        load_external_inventory(path)
