"""Offline evidence classification only; fixtures never certify native execution."""
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal as D

import pytest

from evplatform.core.conflicts import reviewed_correlations, risk_family
from evplatform.core.lifecycle_evidence import verify_native_lifecycle
from evplatform.core.models import utcnow
from evplatform.core.native_accounting import NativeFill
from evplatform.core.native_oms import NativeOrder, NativeSettlement
from test_native_reconciliation import intent, novig_fill, novig_order, NOW


def capture(market, *, with_order=True):
    contract = intent("novig")
    contract = contract.model_copy(update={"instrument": contract.instrument.model_copy(update={
        "canonical_market_id": market.id, "settlement_rules_fingerprint": market.identity()})})
    order = NativeOrder(intent=contract, candidate_id="offline", strategy="native_positive_ev",
        account_fingerprint="a"*64, market_snapshot=market, market_fingerprint=market.identity(),
        risk_family_id=risk_family(market), reviewed_correlation_groups=reviewed_correlations(market),
        venue_order_id="native", filled_quantity=D(40), filled_cost_usd=D(".24"),
        gross_payout_usd=D(".40"), fees=D(".005"), submitted_at=NOW-timedelta(seconds=3),
        acknowledged_at=NOW-timedelta(seconds=2))
    fill = NativeFill(fill_id="fill1", order_id="native", client_order_id=contract.client_order_id,
        quantity="40", price=".6", cost_usd=".24", gross_payout_usd=".4", fee_usd=".005",
        filled_at=NOW, observed_at=NOW, source="offline fixture")
    tables = {name: {"available": True, "complete": True, "rows": []} for name in (
        "native_orders", "native_fill_evidence", "native_settlements", "native_fill_conflicts", "native_settlement_invalidations")}
    if with_order:
        tables["native_orders"]["rows"] = [{"id": order.id, "data": order.model_dump_json()}]
        tables["native_fill_evidence"]["rows"] = [{"order_id": order.id, "data": fill.model_dump_json()}]
    collections = {"orders_"+s: {"complete": True, "rows": []} for s in ("pending", "open", "filled", "canceled", "rejected")}
    collections["orders_open"]["rows"] = [novig_order()]
    collections["fills"] = {"complete": True, "rows": [novig_fill()]}
    return {"schema_version": 1, "collection_method": "authenticated_read_only", "build_id": "fixture-build",
        "captured_at": (max(utcnow(), NOW)+timedelta(seconds=1)).isoformat(),
        "local_oms": {"available": True, "tables": tables}, "venues": [{"venue": "novig", "environment": "sandbox",
            "account_fingerprint": "a"*64, "authenticated": True, "identity_verified": True, "collections": collections}]}


def test_empty_genuine_history_is_absent_lifecycle_not_certification(market):
    result = verify_native_lifecycle(capture(market, with_order=False), expected_build_id="fixture-build")
    assert result["status"] == "no_native_oms_orders_to_validate" and result["native_order_count"] == 0
    assert result["local_inventory_complete"]
    assert not result["certifies_current_oms"] and not result["launch_evidence_issued"] and not result["execution_enabled"]


def test_missing_local_inventory_is_not_an_empty_verified_database(market):
    evidence = capture(market, with_order=False)
    evidence["local_oms"]["tables"] = {}
    assert not verify_native_lifecycle(evidence)["local_inventory_complete"]


def test_matched_financial_fields_do_not_certify_our_lifecycle(market):
    result = verify_native_lifecycle(capture(market), expected_build_id="fixture-build")
    checks = result["orders"][0]["checks"]
    assert checks["authenticated_account_scope"]["status"] == "matched"
    assert checks["local_individual_fill_accounting"]["status"] == "matched"
    assert checks["native_financial_history_matches"]["status"] == "matched"
    for name in ("durable_intent_before_transmission", "native_settlement_cash_and_fees", "actual_restart_recovery",
                 "production_settlement_idempotency", "position_release", "partial_fill_cancel_race"):
        assert checks[name]["status"] == "missing"
    assert not result["orders"][0]["lifecycle_certified"] and not result["certifies_current_oms"]


@pytest.mark.parametrize("change,check,status", [
    ("account", "authenticated_account_scope", "missing"), ("environment", "authenticated_account_scope", "missing"),
    ("build", "build_scope", "mismatch"), ("client", "native_financial_history_matches", "mismatch"),
    ("fee", "native_financial_history_matches", "mismatch"), ("incomplete", "native_financial_history_matches", "missing"),
    ("conflict", "no_invalidated_financial_evidence", "mismatch"),
    ("local_fill", "local_individual_fill_accounting", "mismatch"),
])
def test_cross_scope_incomplete_and_conflicting_evidence_does_not_match(market, change, check, status):
    evidence = capture(market)
    record = evidence["venues"][0]
    if change == "account": record["account_fingerprint"] = "b"*64
    elif change == "environment": record["environment"] = "production"
    elif change == "build": evidence["build_id"] = "different-build"
    elif change == "client": record["collections"]["orders_open"]["rows"][0]["clientId"] = "unrelated"
    elif change == "fee": record["collections"]["fills"]["rows"][0]["fee"] = ".006"
    elif change == "incomplete": record["collections"]["orders_rejected"]["complete"] = False
    elif change == "conflict": evidence["local_oms"]["tables"]["native_fill_conflicts"]["rows"] = [{"order_id": "local-intent"}]
    elif change == "local_fill":
        row = evidence["local_oms"]["tables"]["native_fill_evidence"]["rows"][0]
        row["data"] = NativeFill.model_validate_json(row["data"]).model_copy(update={"fee_usd": D(".006")}).model_dump_json()
    result = verify_native_lifecycle(evidence, expected_build_id="fixture-build")
    assert result["orders"][0]["checks"][check]["status"] == status
    assert not result["certifies_current_oms"]


def test_two_read_snapshots_are_not_actual_restart_recovery(market):
    evidence = capture(market)
    previous = deepcopy(evidence)
    previous["captured_at"] = (NOW-timedelta(seconds=1)).isoformat()
    checks = verify_native_lifecycle(evidence, previous_capture=previous)["orders"][0]["checks"]
    assert checks["read_snapshot_identity_continuity"]["status"] == "matched"
    assert checks["actual_restart_recovery"]["status"] == "missing"


def test_settlement_payload_must_match_its_database_order_binding(market):
    evidence = capture(market)
    settlement = NativeSettlement(order_id="different-local-order", venue="novig", environment="sandbox",
        account_fingerprint="a"*64, market_fingerprint=market.identity(), settlement_id="offline-settlement",
        payout_usd=".4", settled_at=NOW, evidence="offline settlement fixture")
    evidence["local_oms"]["tables"]["native_settlements"]["rows"] = [{"order_id": "local-intent", "data": settlement.model_dump_json()}]
    result = verify_native_lifecycle(evidence)
    assert result["orders"][0]["checks"]["local_settlement_scope"]["status"] == "mismatch"


def test_malformed_raw_history_cannot_claim_complete_financial_evidence(market):
    evidence = capture(market)
    evidence["venues"][0]["collections"]["orders_open"]["rows"] = [None]
    checks = verify_native_lifecycle(evidence)["orders"][0]["checks"]
    assert checks["native_financial_history_matches"]["status"] == "missing"


@pytest.mark.parametrize("change", ["schema", "clock", "duplicate_account", "duplicate_order"])
def test_invalid_capture_does_not_emit_report(market, change):
    evidence = capture(market)
    if change == "schema": evidence["collection_method"] = "synthetic_fixture"
    elif change == "clock": evidence["captured_at"] = "2026-10-21T00:00:00"
    elif change == "duplicate_account": evidence["venues"].append(deepcopy(evidence["venues"][0]))
    else: evidence["local_oms"]["tables"]["native_orders"]["rows"] *= 2
    with pytest.raises(ValueError): verify_native_lifecycle(evidence)
