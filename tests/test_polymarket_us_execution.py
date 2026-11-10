"""Official retail schema fixtures; never evidence of real order connectivity."""
import asyncio
import base64
import json
import time
from datetime import timedelta
from decimal import Decimal as D
from types import SimpleNamespace

import httpx
import pytest

from evplatform.core.models import utcnow
from evplatform.core.manual_native import ManualNativeService
from evplatform.core.native_accounting import NativeInstrument, NativeOrderIntent, NativePriceBand
from evplatform.core.risk import RiskRejected
from evplatform.venues.bridge import NativePreflight, VerifiedVenueMapping
from evplatform.venues.common import PreflightRequired, UnknownOrderOutcome
from evplatform.venues.polymarket_us import PolymarketUSClient, PolymarketUSSettings
from evplatform.venues.polymarket_us_execution import (
    PolymarketUSExecutionVenue, conservative_taker_fee_bound, encode_exact_json,
    normalize_execution, normalize_order, order_body, sdk_preview_request,
    standard_taker_fee, validate_preview,
)
from evplatform.venues.reconciliation import ReconciliationError


def intent(side="yes", quantity="3"):
    instrument = NativeInstrument(venue="polymarket_us", instrument_id="nba-example",
        canonical_market_id="canonical", outcome=side, native_outcome=side, quantity_unit="contracts",
        price_format="probability", payout_per_unit_usd="1", quantity_step="0.01", minimum_quantity="0.01",
        cash_quantum_usd="0.00001", price_bands=(NativePriceBand(minimum="0.001",maximum="0.999",step="0.001"),),
        settlement_rules_fingerprint="verified-test-rule", verified=True, source="offline fixture")
    return NativeOrderIntent(id="local-intent", client_order_id="local-client-order", instrument=instrument,
        quantity=quantity, limit_price="0.45", max_fee_usd="1", fee_source="offline fixture", fees_verified=True, mode="production")


def native_order(order_intent, *, state="ORDER_STATE_FILLED", filled="3", remaining="0"):
    side = order_intent.instrument.native_outcome
    return {"id": "exchange-order-1", "marketSlug": "nba-example", "side": "ORDER_SIDE_BUY" if side=="yes" else "ORDER_SIDE_SELL",
        "intent": "ORDER_INTENT_BUY_LONG" if side=="yes" else "ORDER_INTENT_BUY_SHORT", "type": "ORDER_TYPE_LIMIT",
        "price": {"value": "0.45" if side=="yes" else "0.55", "currency": "USD"}, "quantity": order_intent.quantity,
        "cumQuantity": D(filled), "leavesQuantity": D(remaining), "tif": "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL", "state": state,
        "commissionNotionalTotalCollected": {"value": "0.05", "currency": "USD"},
        "avgPx": {"value": "0.433333", "currency": "USD"}}


def execution(order_intent, *, identity="execution-1", quantity="3", cumulative="3", price="0.4", fee="0.05"):
    native = native_order(order_intent, filled=cumulative)
    return {"id": identity, "order": native, "lastShares": quantity,
        "lastPx": {"value": price if order_intent.instrument.native_outcome=="yes" else str(1-D(price)), "currency": "USD"},
        "type": "EXECUTION_TYPE_FILL", "transactTime": (utcnow()-timedelta(seconds=1)).isoformat(),
        "tradeId": "trade-1", "aggressor": True, "commissionNotionalCollected": {"value": fee, "currency": "USD"}}


def adapter_for(order_intent, handler, *, enabled=True):
    client = PolymarketUSClient(PolymarketUSSettings(api_key_id="00000000-0000-4000-8000-000000000001",
        secret_key=base64.b64encode(bytes(range(32))).decode(), requests_per_second=10),
        httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    client.authenticated = True
    mapping = VerifiedVenueMapping(canonical_market_id="canonical",canonical_outcome=order_intent.instrument.outcome,
        native_instrument_id="nba-example",native_outcome=order_intent.instrument.native_outcome,
        settlement_fingerprint="verified-test-rule",mapping_evidence="offline fixture",verified=True,
        fee_schedule_verified=True,native_payout_usd="1",native_quantity_unit="contracts")
    authority = SimpleNamespace(consume=lambda authorization, identity: authorization=="one-use-test-token" and identity==order_intent.id)
    preflight = NativePreflight(environment="production", account_authorized=True, geographic_eligibility_verified=True,
        valid_until=time.time()+60,evidence="offline fixture",native_trading_enabled=enabled)
    return PolymarketUSExecutionVenue(client, authority, mappings=[mapping], preflight=preflight)


@pytest.mark.parametrize("side,wire_price,wire_intent", [("yes","0.45","ORDER_INTENT_BUY_LONG"), ("no","0.55","ORDER_INTENT_BUY_SHORT")])
def test_yes_no_limit_and_fractional_quantity_encode_exactly(side,wire_price,wire_intent):
    original = intent(side, quantity="0.29")
    body = order_body(original)
    assert body["price"]["value"] == wire_price and body["intent"] == wire_intent
    assert body["tif"] == "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL"
    assert body["manualOrderIndicator"] == "MANUAL_ORDER_INDICATOR_AUTOMATIC"
    encoded = encode_exact_json(body)
    assert b'"quantity":0.29' in encoded
    assert original.client_order_id.encode() not in encoded
    assert json.loads(encoded, parse_float=D)["quantity"] == original.quantity


def test_cash_json_never_silently_accepts_binary_float():
    with pytest.raises(ValueError):
        encode_exact_json({"quantity": 0.29})


@pytest.mark.parametrize("side,expected_side,expected_price", [
    ("yes", "ORDER_SIDE_BUY", "0.45"), ("no", "ORDER_SIDE_SELL", "0.55")])
def test_sdk_241_preview_shape_preserves_fractional_long_and_short(side, expected_side, expected_price):
    original = intent(side, quantity="0.29")
    request = sdk_preview_request(original)
    assert request["quantity"] == 0.29
    assert request["price"] == {"value": expected_price, "currency": "USD"}
    assert request["tif"] == "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL"
    response = {"order": {"marketSlug": "nba-example", "intent": request["intent"],
        "side": expected_side, "outcomeSide": f"OUTCOME_SIDE_{side.upper()}",
        "type": "ORDER_TYPE_LIMIT", "price": request["price"], "quantity": 0.29,
        "tif": request["tif"], "state": "ORDER_STATE_PENDING_NEW"}}
    checked = validate_preview(original, response)
    assert checked["parameter_check_only"] and checked["market_slug"] == "nba-example"
    assert not hasattr(original, "order_id")


@pytest.mark.parametrize("field,value", [
    ("marketSlug", "other-market"), ("intent", "ORDER_INTENT_SELL_LONG"),
    ("quantity", 0.3), ("state", "ORDER_STATE_REJECTED")])
def test_preview_never_accepts_changed_identity_size_or_rejection(field, value):
    original = intent(quantity="0.29")
    row = {"marketSlug": "nba-example", "intent": "ORDER_INTENT_BUY_LONG",
        "side": "ORDER_SIDE_BUY", "outcomeSide": "OUTCOME_SIDE_YES",
        "type": "ORDER_TYPE_LIMIT", "price": {"value": "0.45", "currency": "USD"},
        "quantity": 0.29, "tif": "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL",
        "state": "ORDER_STATE_PENDING_NEW"}
    row[field] = value
    with pytest.raises(PreflightRequired):
        validate_preview(original, {"order": row})


async def test_sdk_preview_invocation_is_preview_only_and_never_places():
    original = intent(quantity="0.29")
    calls = []
    adapter = adapter_for(original, lambda request: calls.append(request) or httpx.Response(500))
    class Orders:
        async def preview(self, params):
            calls.append(("preview", params))
            request = params["request"]
            return {"order": {**request, "side": "ORDER_SIDE_BUY",
                "outcomeSide": "OUTCOME_SIDE_YES", "state": "ORDER_STATE_PENDING_NEW"}}
        async def create(self, params):
            raise AssertionError("production placement must never occur in preview")
    class SDK:
        orders = Orders()
        async def close(self):
            pass
    adapter.client._private_sdk = SDK()
    adapter.client.authenticated = True
    try:
        result = await adapter.preview_native_request(original)
        assert result["parameter_check_only"]
        assert calls == [("preview", {"request": sdk_preview_request(original)})]
        assert not adapter._transmitted
    finally:
        await adapter.client.disconnect()


async def test_owner_preview_uses_existing_native_adapter_without_dispatch():
    calls = []
    class Adapter:
        async def preview_native_request(self, native_intent):
            calls.append(native_intent.client_order_id)
            return {"parameter_check_only": True}
        async def submit_intent(self, *args):
            raise AssertionError("owner preview cannot dispatch")
    service = ManualNativeService(SimpleNamespace(native=SimpleNamespace(
        manager=SimpleNamespace(adapters={"polymarket_us": Adapter()}))))
    order = SimpleNamespace(intent=intent())
    assert await service._verify_polymarket_us_request("polymarket_us", order) == {"parameter_check_only": True}
    assert calls == [order.intent.client_order_id]
    assert await service._verify_polymarket_us_request("kalshi", order) is None
    service.engine.native.manager.adapters.clear()
    with pytest.raises(RiskRejected):
        await service._verify_polymarket_us_request("polymarket_us", order)


@pytest.mark.parametrize("quantity,price,theta,fee", [("1000","0.1","0.0695","6.26"),
    ("1000","0.65","0.0695","15.81"),("1000","0.5","0.1","25.00")])
def test_current_official_fee_examples(quantity,price,theta,fee):
    assert standard_taker_fee(quantity,price,theta=theta)==D(fee)


def test_fee_bound_includes_improvement_toward_half_and_excludes_weekly_rebate():
    bound=conservative_taker_fee_bound("100","0.9",theta="0.0695")
    assert bound==D("1.74")
    assert bound>=standard_taker_fee("100","0.5",theta="0.0695")
    assert conservative_taker_fee_bound("100","0.1",theta="0.0695")==D("0.63")


@pytest.mark.parametrize("side,wire_intent,wire_price", [
    ("yes", "ORDER_INTENT_BUY_LONG", "0.45"),
    ("no", "ORDER_INTENT_BUY_SHORT", "0.55")])
def test_one_dollar_long_and_short_collateral_boundary(side, wire_intent, wire_price):
    template = intent(side, quantity="1")
    fee = conservative_taker_fee_bound("1", "0.45", theta="0.0695")
    bounded = NativeOrderIntent(id=template.id, client_order_id=template.client_order_id,
        instrument=template.instrument, quantity="1", limit_price="0.45",
        max_fee_usd=fee, fee_source="OFFLINE standard taker fee fixture",
        fees_verified=True, mode="production")
    assert fee == D("0.02")
    assert bounded.reserved_cost_usd == D("0.47") <= D("1")
    request = order_body(bounded)
    assert request["intent"] == wire_intent and request["price"]["value"] == wire_price
    if side == "no":
        assert D("1")-D(wire_price) == D("0.45")  # full $1 collateral less YES proceeds
    too_large = NativeOrderIntent(id=template.id, client_order_id=template.client_order_id,
        instrument=template.instrument, quantity="3", limit_price="0.45",
        max_fee_usd=conservative_taker_fee_bound("3", "0.45", theta="0.0695"),
        fee_source="OFFLINE standard taker fee fixture", fees_verified=True, mode="production")
    assert too_large.reserved_cost_usd > D("1")


@pytest.mark.parametrize("side", ["yes","no"])
def test_exact_private_execution_normalizes_outcome_price_and_actual_commission(side):
    original=intent(side)
    fill=normalize_execution(original,execution(original),expected_order_id="exchange-order-1",observed_at=utcnow())
    assert fill.price==D("0.4") and fill.cost_usd==D("1.2") and fill.gross_payout_usd==D(3)
    assert fill.fee_usd==D("0.05") and fill.timestamp_kind=="matched"
    assert fill.fill_id == "execution-1" and fill.trade_id == "trade-1"


@pytest.mark.parametrize("trade_id", [None, "", "bad/id", 1, True])
def test_present_malformed_execution_trade_identity_is_rejected(trade_id):
    original = intent()
    raw = execution(original)
    raw["tradeId"] = trade_id
    with pytest.raises(ReconciliationError):
        normalize_execution(original, raw, expected_order_id="exchange-order-1", observed_at=utcnow())


def test_missing_legacy_trade_identity_remains_absent_never_inferred():
    original = intent()
    raw = execution(original)
    raw.pop("tradeId")
    fill = normalize_execution(original, raw, expected_order_id="exchange-order-1", observed_at=utcnow())
    assert fill.trade_id is None


async def test_adapter_retains_documented_trade_binding_across_legacy_replay():
    original = intent()
    adapter = adapter_for(original, lambda request: httpx.Response(200, json={}))
    raw = execution(original)
    fill = normalize_execution(original, raw, expected_order_id="exchange-order-1", observed_at=utcnow())
    try:
        adapter.seed_fills("exchange-order-1", original, (fill.model_copy(update={"trade_id": None}),))
        adapter.seed_fills("exchange-order-1", original, (fill,))
        adapter.seed_fills("exchange-order-1", original, (fill.model_copy(update={"trade_id": None}),))
        assert adapter.execution_fills("exchange-order-1", original)[0].trade_id == "trade-1"
        with pytest.raises(ReconciliationError):
            adapter.seed_fills("exchange-order-1", original, (fill.model_copy(update={"trade_id": "trade-2"}),))
    finally:
        await adapter.client.disconnect()


@pytest.mark.parametrize("change", ["missing_fee","maker","combo","wrong_side","wrong_id","wrong_price","bad_clock","changed_quantity"])
def test_unsafe_execution_evidence_is_rejected(change):
    original=intent()
    raw=execution(original)
    if change=="missing_fee": raw.pop("commissionNotionalCollected")
    elif change=="maker": raw["aggressor"]=False
    elif change=="combo": raw["legPrices"]=[{"marketSlug":"leg"}]
    elif change=="wrong_side": raw["order"]["side"]="ORDER_SIDE_SELL"
    elif change=="wrong_id": raw["order"]["id"]="unrelated"
    elif change=="wrong_price": raw["lastPx"]["value"]="0.46"
    elif change=="bad_clock": raw["transactTime"]=(utcnow()+timedelta(days=1)).isoformat()
    else: raw["order"]["quantity"]=D(4)
    with pytest.raises((ReconciliationError,ValueError)):
        normalize_execution(original,raw,expected_order_id="exchange-order-1",observed_at=utcnow())


def test_terminal_order_requires_exact_complete_executions_not_rounded_average():
    original=intent()
    first=normalize_execution(original,execution(original,identity="e1",quantity="1",cumulative="1",price="0.4",fee="0.02"),expected_order_id="exchange-order-1",observed_at=utcnow())
    second=normalize_execution(original,execution(original,identity="e2",quantity="2",cumulative="3",price="0.45",fee="0.03"),expected_order_id="exchange-order-1",observed_at=utcnow())
    raw={"order":native_order(original)}
    missing=normalize_order(original,raw,expected_order_id="exchange-order-1",observed_at=utcnow(),fills=[first])
    assert missing.terminal and not missing.financial_complete and missing.snapshot is None
    complete=normalize_order(original,raw,expected_order_id="exchange-order-1",observed_at=utcnow(),fills=[second,first])
    assert complete.financial_complete and complete.snapshot.cumulative_cost_usd==D("1.30")
    assert complete.snapshot.cumulative_cost_usd!=D("0.433333")*3


def test_zero_fill_cancel_can_reconcile_with_explicit_zero_commission():
    original=intent()
    raw=native_order(original,state="ORDER_STATE_CANCELED",filled="0")
    raw["commissionNotionalTotalCollected"]["value"]="0"
    report=normalize_order(original,{"order":raw},expected_order_id="exchange-order-1",observed_at=utcnow())
    assert report.financial_complete and report.terminal and report.status=="cancelled"
    assert report.snapshot.cumulative_cost_usd==0


def test_cancellation_racing_fill_stays_incomplete_until_execution_arrives():
    original=intent()
    raw=native_order(original,state="ORDER_STATE_CANCELED",filled="1")
    raw["commissionNotionalTotalCollected"]["value"]="0.02"
    report=normalize_order(original,{"order":raw},expected_order_id="exchange-order-1",observed_at=utcnow())
    assert report.status=="cancelled" and not report.financial_complete
    fill=normalize_execution(original,execution(original,quantity="1",cumulative="1",fee="0.02"),expected_order_id="exchange-order-1",observed_at=utcnow())
    report=normalize_order(original,{"order":raw},expected_order_id="exchange-order-1",observed_at=utcnow(),fills=[fill])
    assert report.financial_complete and report.snapshot.cumulative_quantity==1


def test_gapped_execution_history_and_actual_commission_mismatch_block_completeness():
    original=intent()
    gap=normalize_execution(original,execution(original,quantity="1",cumulative="2"),expected_order_id="exchange-order-1",observed_at=utcnow())
    with pytest.raises(ReconciliationError):
        normalize_order(original,{"order":native_order(original)},expected_order_id="exchange-order-1",observed_at=utcnow(),fills=[gap])
    fill=normalize_execution(original,execution(original,fee="0.04"),expected_order_id="exchange-order-1",observed_at=utcnow())
    report=normalize_order(original,{"order":native_order(original)},expected_order_id="exchange-order-1",observed_at=utcnow(),fills=[fill])
    assert not report.financial_complete


async def test_submit_requires_central_authority_and_current_preflight():
    calls=[]
    adapter=adapter_for(intent(),lambda request: calls.append(request) or httpx.Response(200,json={"id":"exchange-order-1"}),enabled=False)
    with pytest.raises(PermissionError): await adapter.submit_intent(intent(),"bad-token")
    with pytest.raises(PreflightRequired): await adapter.submit_intent(intent(),"one-use-test-token")
    assert calls==[]
    await adapter.client.disconnect()


async def test_ack_is_not_fill_and_duplicate_order_never_resends():
    calls=[]
    def handle(request):
        calls.append(request)
        assert request.method=="POST" and request.url.path=="/v1/orders"
        assert request.headers["X-PM-Signature"]
        return httpx.Response(200,json={"id":"exchange-order-1"})
    adapter=adapter_for(intent(),handle)
    report=await adapter.submit_intent(intent(),"one-use-test-token")
    assert report.status=="acknowledged" and not report.financial_complete and not report.fills
    assert not adapter.client.settings.gate.enabled
    with pytest.raises(UnknownOrderOutcome): await adapter.submit_intent(intent(),"one-use-test-token")
    assert len(calls)==1
    await adapter.client.disconnect()


@pytest.mark.parametrize("mode", ["timeout","missing_id","cancelled","server_error"])
async def test_unknown_submission_preserves_no_retry_and_no_heuristic_identity(mode):
    calls=[]
    def handle(request):
        calls.append(request)
        if mode=="timeout": raise httpx.ReadTimeout("offline",request=request)
        if mode=="cancelled": raise asyncio.CancelledError()
        return httpx.Response(500 if mode=="server_error" else 200,json={})
    adapter=adapter_for(intent(),handle)
    with pytest.raises(UnknownOrderOutcome): await adapter.submit_intent(intent(),"one-use-test-token")
    with pytest.raises(UnknownOrderOutcome): await adapter.submit_intent(intent(),"one-use-test-token")
    assert await adapter.resolve_client_order(intent()) is None
    assert len(calls)==1 and not adapter.client.settings.gate.enabled
    await adapter.client.disconnect()


async def test_cancel_only_owned_order_ack_is_pending_and_failed_cancel_is_unknown():
    responses=[200,409]
    def handle(request):
        assert request.method=="POST" and request.url.path=="/v1/order/exchange-order-1/cancel"
        assert json.loads(request.content)=={"marketSlug":"nba-example"}
        return httpx.Response(responses.pop(0),json={})
    original=intent()
    adapter=adapter_for(original,handle)
    with pytest.raises(PreflightRequired): await adapter.cancel_native("exchange-order-1",original)
    adapter.bind_native_intent("exchange-order-1",original)
    report=await adapter.cancel_native("exchange-order-1",original)
    assert report.status=="cancel_pending" and not report.financial_complete
    report=await adapter.cancel_native("exchange-order-1",original)
    assert report.status=="unknown" and not report.retry_cancel_allowed
    await adapter.client.disconnect()


async def test_restart_requires_durable_binding_and_fill_evidence():
    original=intent()
    raw=native_order(original)
    adapter=adapter_for(original,lambda request:httpx.Response(200,content=encode_exact_json({"order":raw})))
    with pytest.raises(PreflightRequired): await adapter.reconcile_native("exchange-order-1",original)
    adapter.bind_native_intent("exchange-order-1",original)
    assert not (await adapter.reconcile_native("exchange-order-1",original)).financial_complete
    fill=normalize_execution(original,execution(original),expected_order_id="exchange-order-1",observed_at=utcnow())
    adapter.seed_fills("exchange-order-1",original,[fill])
    assert (await adapter.reconcile_native("exchange-order-1",original)).financial_complete
    await adapter.client.disconnect()


async def test_private_snapshot_eof_and_disconnect_reset_without_erasing_exact_fill_evidence():
    original=intent()
    adapter=adapter_for(original,lambda request:httpx.Response(200,json={}))
    adapter.bind_native_intent("exchange-order-1",original)
    def frame(payload): return {"type":"polymarket_us_monitor","channel":"private","kind":"data","payload":payload}
    adapter.observe_private_frame(frame({"orderSubscriptionSnapshot":{"orders":[],"eof":False}}))
    assert not adapter.private_snapshot_complete
    adapter.observe_private_frame(frame({"orderSubscriptionSnapshot":{"orders":[],"eof":True}}))
    assert adapter.private_snapshot_complete
    raw=execution(original)
    adapter.observe_private_frame(frame({"orderSubscriptionUpdate":{"execution":raw}}))
    adapter.observe_private_frame(frame({"orderSubscriptionUpdate":{"execution":raw}}))
    assert len(adapter._fills["exchange-order-1"])==1
    adapter.observe_private_frame({"type":"transport_invalidated"})
    assert not adapter.private_snapshot_complete and not adapter.private_orders and adapter.private_generation==1
    assert len(adapter._fills["exchange-order-1"])==1
    await adapter.client.disconnect()


async def test_private_fill_before_http_ack_is_retained_and_bound_only_by_returned_id():
    original=intent()
    async def handle(request):
        if request.method=="POST":
            raw=execution(original)
            adapter.observe_private_frame({"type":"polymarket_us_monitor","channel":"private","kind":"data",
                "payload":{"orderSubscriptionUpdate":{"execution":raw}}})
            assert not adapter._fills
            assert await adapter.resolve_client_order(original) is None
            return httpx.Response(200,json={"id":"exchange-order-1"})
        return httpx.Response(200,content=encode_exact_json({"order":native_order(original)}))
    adapter=adapter_for(original,handle)
    report=await adapter.submit_intent(original,"one-use-test-token")
    assert report.status=="acknowledged" and not report.fills
    assert len(adapter._fills["exchange-order-1"])==1 and not adapter._unbound_executions
    report=await adapter.reconcile_native("exchange-order-1",original)
    assert report.financial_complete and report.snapshot.cumulative_cost_usd==D("1.2")
    await adapter.client.disconnect()


async def test_unbound_fill_conflict_or_buffer_limit_invalidates_private_readiness():
    original=intent();adapter=adapter_for(original,lambda request:httpx.Response(200,json={}))
    def send(raw):
        adapter.observe_private_frame({"type":"polymarket_us_monitor","channel":"private","kind":"data",
            "payload":{"orderSubscriptionUpdate":{"execution":raw}}})
    raw=execution(original); send(raw)
    adapter.private_snapshot_complete=True
    raw["commissionNotionalCollected"]["value"]="0.06"
    with pytest.raises(ReconciliationError): send(raw)
    assert not adapter.private_snapshot_complete
    adapter._unbound_executions={"other-order":{str(n):({},utcnow()) for n in range(512)}}
    with pytest.raises(ReconciliationError): send(execution(original,identity="new-execution"))
    assert not adapter.private_snapshot_complete
    await adapter.client.disconnect()


async def test_rejected_private_snapshot_cannot_leave_previous_eof_ready():
    adapter=adapter_for(intent(),lambda request:httpx.Response(200,json={}))
    raw={"type":"polymarket_us_monitor","channel":"private","kind":"data",
         "payload":{"orderSubscriptionSnapshot":{"orders":[],"eof":True}}}
    adapter.observe_private_frame(raw)
    assert adapter.private_snapshot_complete
    with pytest.raises(ReconciliationError): adapter.observe_private_frame(raw)
    assert not adapter.private_snapshot_complete
    await adapter.client.disconnect()


async def test_key_rotation_during_bound_adapter_cannot_reuse_cached_signer():
    adapter=adapter_for(intent(),lambda request:httpx.Response(200,json={}))
    adapter.client.settings.secret_key=base64.b64encode(bytes(reversed(range(32)))).decode()
    with pytest.raises(PreflightRequired): await adapter.submit_intent(intent(),"one-use-test-token")
    await adapter.client.disconnect()


async def test_expired_preflight_after_transport_pacing_never_sends():
    calls=[]
    adapter=adapter_for(intent(),lambda request:calls.append(request) or httpx.Response(200,json={"id":"exchange-order-1"}))
    adapter.preflight=adapter.preflight.model_copy(update={"valid_until":time.time()+0.03})
    adapter.client._next_request=time.monotonic()+0.08
    with pytest.raises(PreflightRequired):
        await adapter.submit_intent(intent(),"one-use-test-token")
    assert not calls and not adapter.client.settings.gate.enabled
    await adapter.client.disconnect()


async def test_central_dispatch_guard_runs_after_wait_and_does_not_block_owned_cancel():
    calls=[]; guarded=[]
    original = intent()
    adapter=adapter_for(original,lambda request:calls.append(request) or httpx.Response(200,json={}))
    def changed_risk(original):
        guarded.append(time.monotonic())
        raise PreflightRequired("OFFLINE kill switch changed")
    adapter.dispatch_guard=changed_risk
    started=time.monotonic()
    deadline=started+0.08
    adapter.client._next_request=deadline
    with pytest.raises(PreflightRequired):
        await adapter.submit_intent(original,"one-use-test-token")
    assert not calls and guarded[0]-started>0.04
    adapter.bind_native_intent("exchange-order-1",original)
    adapter.preflight=adapter.preflight.model_copy(update={"valid_until":0})
    assert (await adapter.cancel_native("exchange-order-1",original)).status=="cancel_pending"
    assert len(calls)==1 and len(guarded)==1
    await adapter.client.disconnect()
