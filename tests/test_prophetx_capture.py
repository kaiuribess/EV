"""OFFLINE captured-wire rehearsal. No API credentials or native wagers."""
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal as D

import pytest

from evplatform.core.lifecycle_capture import read_guard
from evplatform.core.lifecycle_evidence import verify_native_lifecycle
from evplatform.venues.prophetx_capture import capture_owned_orders, normalize_captured_order
from evplatform.venues.reconciliation import ReconciliationError
from test_prophetx_financial import case, close_win, fee_only_in_history, NOW, OID, iso


@pytest.fixture(autouse=True)
def clocks(monkeypatch):
    monkeypatch.setattr("evplatform.venues.prophetx_capture.utcnow", lambda: NOW)
    monkeypatch.setattr("evplatform.core.lifecycle_capture.utcnow", lambda: NOW)


def record(order, bundles):
    return {"venue": "prophetx", "environment": "sandbox", "authenticated": True,
            "identity_verified": True, "account_fingerprint": order.account_fingerprint,
            "owned_order_evidence": bundles}


async def captured(case):
    order, client, _ = case
    bundles = await capture_owned_orders(client, [order])
    return order, client, record(order, bundles)


async def test_owned_get_order_matched_and_wallet_capture_replays_exact_finances(case):
    order, client, evidence = await captured(case)
    result = normalize_captured_order(order, evidence, observed_at=NOW)
    assert result.financial_complete and result.snapshot.cumulative_fee_usd == D(".2")
    assert result.snapshot.cumulative_cost_usd == 10 and result.snapshot.cumulative_gross_payout_usd == 22
    assert {path for path, _ in client.calls} == {
        "/v4/mm/get_order_history", "/v4/mm/get_order_matched_detail", "/v4/mm/get_transactions"}
    assert client.calls[-1][1]["trade_id"] == OID
    assert not result.fills  # Aggregate fees never become invented per-fill fees.


async def test_exact_history_fee_fallback_survives_protected_wire_replay(case):
    fee_only_in_history(case[1])
    order, _, evidence = await captured(case)
    assert "fee" not in evidence["owned_order_evidence"][0]["order"]["response"]["data"]
    assert normalize_captured_order(order, evidence, observed_at=NOW).snapshot.cumulative_fee_usd == D(".2")


async def test_offline_one_dollar_order_with_partial_fill_below_order_minimum(case):
    order, client, review = case
    order = order.model_copy(update={"intent": order.intent.model_copy(update={"quantity": D("1")}),
        "filled_quantity": D(".4"), "filled_cost_usd": D(".4"), "gross_payout_usd": D(".88"), "fees": D(".01")})
    client.row.update(original_quantity="1", quantity=".4", filled_quantity=".4", profit=".48", fee=".01")
    client.fills[0].update(quantity=".4", profit=".48")
    client.wallet[0].update(amount=".4", change="-.4", balance="99.6")
    client.wallet[1].update(amount=".01", change="-.01", balance_before="99.6", balance="99.59")
    _, _, evidence = await captured((order, client, review))
    result = normalize_captured_order(order, evidence, observed_at=NOW)
    assert result.status == "cancelled" and result.terminal
    assert result.snapshot.cumulative_quantity == D(".4") and result.snapshot.cumulative_gross_payout_usd == D(".88")
    assert order.quantity == 1 and order.intent.instrument.minimum_quantity == 1


@pytest.mark.parametrize("mutation", ["rows", "missing_clock", "future_clock", "stale_clock", "repeated_cursor",
    "unfinished_cursor", "extra_page", "receipt_before_start", "receipt_after_end", "wrong_order", "wrong_wallet",
    "fee", "duplicate_bundle", "wrong_account", "wrong_environment"])
async def test_tampered_incomplete_or_misbound_capture_fails_closed(case, mutation):
    order, _, evidence = await captured(case)
    bundle = evidence["owned_order_evidence"][0]
    matched = bundle["matched"]
    response = matched["pages"][0]["response"]
    if mutation == "rows": matched["rows"] = []
    elif mutation == "missing_clock": response.pop("last_synced_at")
    elif mutation == "future_clock": response["last_synced_at"] = iso(1)
    elif mutation == "stale_clock": response["last_synced_at"] = iso(-11)
    elif mutation == "unfinished_cursor": response["data"]["next_cursor"] = "more"
    elif mutation == "repeated_cursor":
        response["data"]["next_cursor"] = "more"
        matched["pages"] *= 2
    elif mutation == "extra_page": matched["pages"] *= 2
    elif mutation == "receipt_before_start": matched["pages"][0]["received_at"] = iso(-1)
    elif mutation == "receipt_after_end": matched["pages"][0]["received_at"] = iso(1)
    elif mutation == "wrong_order": bundle["native_order_id"] = "other"
    elif mutation == "wrong_wallet":
        bundle["wallet"]["rows"][0]["trade_id"] = "other"
    elif mutation == "fee": bundle["order"]["response"]["data"]["fee"] = "0"
    elif mutation == "duplicate_bundle": evidence["owned_order_evidence"].append(deepcopy(bundle))
    elif mutation == "wrong_account": evidence["account_fingerprint"] = "b"*64
    elif mutation == "wrong_environment": evidence["environment"] = "production"
    with pytest.raises((ReconciliationError, ValueError)):
        normalize_captured_order(order, evidence, observed_at=NOW)


async def test_pagination_failure_is_retained_and_never_replayed_as_complete(case):
    order, client, _ = case
    client.overrides["/v4/mm/get_order_matched_detail"] = lambda _: {
        "data": {"matching_details": [], "next_cursor": "loop"}, "last_synced_at": iso()}
    bundles = await capture_owned_orders(client, [order], max_pages=2)
    assert not bundles[0]["complete"] and bundles[0]["matched"]["error"] == "invalid_history_cursor"
    assert normalize_captured_order(order, record(order, bundles), observed_at=NOW) is None


@pytest.mark.parametrize("mutation", ["account", "identity", "bound", "duplicate"])
async def test_wrong_local_inventory_rejected_before_read(case, mutation):
    order, client, _ = case
    if mutation == "account": order = order.model_copy(update={"account_fingerprint": "b"*64})
    if mutation == "identity": order = order.model_copy(update={"venue_order_id": "../cancel_order"})
    with pytest.raises(ValueError):
        await capture_owned_orders(client, [order, order] if mutation == "duplicate" else [order],
            max_orders=0 if mutation == "bound" else 20)
    assert not client.calls


async def test_empty_owned_inventory_does_not_trigger_order_reads(case):
    client = case[1]
    assert await capture_owned_orders(client, []) == [] and not client.calls


async def test_capture_read_failure_preserves_error_without_credentials(case):
    order, client, _ = case
    async def failed(_):
        raise RuntimeError("secret authentication material must never be exported")
    client.reconcile_order = failed
    bundles = await capture_owned_orders(client, [order])
    assert bundles[0]["errors"] == [{"type": "RuntimeError", "http_status": None}]
    assert not bundles[0]["complete"] and "secret" not in str(bundles)


async def test_closed_order_cash_can_match_without_certifying_full_lifecycle(case):
    close_win(case)
    order, _, evidence = await captured(case)
    tables = {name: {"available": True, "complete": True, "rows": []} for name in (
        "native_orders", "native_fill_evidence", "native_settlements", "native_fill_conflicts", "native_settlement_invalidations")}
    tables["native_orders"]["rows"] = [{"id": order.id, "data": order.model_dump_json()}]
    artifact = {"schema_version": 1, "collection_method": "authenticated_read_only", "build_id": "OFFLINE",
        "captured_at": iso(-1), "finished_at": iso(), "local_oms": {"tables": tables}, "venues": [evidence]}
    result = verify_native_lifecycle(artifact, expected_build_id="OFFLINE")
    checks = result["orders"][0]["checks"]
    assert checks["native_financial_history_matches"]["status"] == "matched"
    assert checks["native_settlement_cash_and_fees"]["status"] == "missing"
    assert checks["actual_restart_recovery"]["status"] == "missing"
    assert not result["certifies_current_oms"] and not result["launch_evidence_issued"]


async def test_future_bundle_cannot_use_a_capture_start_as_its_completion(case):
    order, _, evidence = await captured(case)
    with pytest.raises(ReconciliationError):
        normalize_captured_order(order, evidence, observed_at=NOW-timedelta(seconds=1))


@pytest.mark.parametrize("path", ["get_order/partner-owned", "get_order_matched_detail"])
async def test_only_documented_owned_order_reads_are_added_to_guard(path):
    import httpx
    await read_guard("prophetx")(httpx.Request("GET", "https://cash.api.prophetx.co/partner/v4/mm/"+path))
    with pytest.raises(ValueError):
        await read_guard("prophetx")(httpx.Request("POST", "https://cash.api.prophetx.co/partner/v4/mm/"+path))
