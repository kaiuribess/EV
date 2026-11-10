"""Documented native shapes only; these tests never connect to a venue."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
import base64
import json
import time

import httpx
import pytest

from evplatform.core.native_accounting import NativeInstrument, NativeOrderIntent, NativePriceBand
from evplatform.core.risk import RiskAuthority
from evplatform.venues.bridge import NativeExecutionVenue, NativePreflight, VerifiedVenueMapping
from evplatform.venues.common import NativeSettings, NativeTransport, PreflightRequired, UnknownOrderOutcome, VenueHTTPError
from evplatform.venues.kalshi import KalshiClient
from evplatform.venues.novig import NovigClient
from evplatform.venues.prophetx import ProphetXClient, decode_talaria
from evplatform.venues.reconciliation import (
    ReconciliationError, match_client_order, normalize_ack, normalize_kalshi_fill,
    normalize_kalshi_order, normalize_novig_fill, normalize_novig_order,
    normalize_prophetx_cancel, normalize_prophetx_fill, normalize_prophetx_order,
)

NOW = datetime(2026, 10, 21, 1, tzinfo=timezone.utc)
CLIENT = "11111111-1111-4111-8111-111111111111"
OUTCOME = "22222222-2222-4222-8222-222222222222"


def intent(venue="kalshi"):
    options = dict(venue=venue, instrument_id="instrument", canonical_market_id="canonical",
                   outcome="home", native_outcome="yes", quantity_unit="contracts",
                   price_format="probability", payout_per_unit_usd=D(1), quantity_step=D(".01"),
                   minimum_quantity=D(".01"), cash_quantum_usd=D(".0001"), verified=True,
                   settlement_rules_fingerprint="reviewed", source="offline reviewed fixture",
                   price_bands=(NativePriceBand(minimum=".0001", maximum=".9999", step=".0001"),))
    if venue == "novig":
        options.update(native_outcome=OUTCOME, quantity_unit="whole_cent_contracts", payout_per_unit_usd=D(".01"),
                       quantity_step=D(1), minimum_quantity=D(1), cash_quantum_usd=D(".00001"),
                       price_bands=(NativePriceBand(minimum=".055", maximum=".945", step=".005"),))
    if venue == "prophetx":
        options.update(quantity_unit="usd_stake", price_format="american", payout_per_unit_usd=None,
                       quantity_step=D(".01"), minimum_quantity=D(1), cash_quantum_usd=D(".01"), price_bands=())
    return NativeOrderIntent(id="local-intent", client_order_id=CLIENT, instrument=NativeInstrument(**options),
                             quantity=D(110) if venue == "novig" else D(10),
                             limit_price=D(120) if venue == "prophetx" else D(".665"),
                             max_fee_usd=D(1), fee_source="fixture", fees_verified=True,
                             mode="sandbox", created_at=NOW-timedelta(seconds=5))


def mapping(order):
    i = order.instrument
    return VerifiedVenueMapping(canonical_market_id=i.canonical_market_id, canonical_outcome=i.outcome,
                                native_instrument_id=i.instrument_id, native_outcome=i.native_outcome,
                                native_exchange_index=i.exchange_index,
                                settlement_fingerprint=i.settlement_rules_fingerprint, mapping_evidence="fixture",
                                verified=True, fee_schedule_verified=True, native_quantity_unit=i.quantity_unit,
                                native_payout_usd=i.payout_per_unit_usd)


def kalshi_order(**updates):
    return {"order": dict(order_id="native", client_order_id=CLIENT, ticker="instrument",
                           outcome_side="yes", action="buy", fill_count_fp="2.50", remaining_count_fp="7.50",
                           initial_count_fp="10.00", status="resting", taker_fill_cost_dollars="1.50",
                           yes_price_dollars=".665", no_price_dollars=".335",
                           maker_fill_cost_dollars="0", taker_fees_dollars=".01", maker_fees_dollars="0",
                           last_update_time=NOW.isoformat(), **updates)}


def novig_order(status="OPEN", remaining=70):
    return dict(orderId="native", clientId=CLIENT, outcomeId=OUTCOME, marketId="instrument", price=".665",
                qty=110, remaining=remaining, status=status)


def novig_fill(**updates):
    row = dict(orderId="native", clientId=CLIENT, outcomeId=OUTCOME, marketId="instrument", fillId="fill1", qty=40,
               cost=".24", fee=".005", ts=int(NOW.timestamp()*1000))
    row.update(updates)
    return row


def prophetx_order(**updates):
    row = dict(order_id="native", external_id=CLIENT, strike_id="instrument", original_quantity="10",
               filled_quantity="4", open_quantity="0", quantity="4", status="open",
               matching_status="fully_matched", price=120, updated_at=NOW.isoformat())
    row.update(updates)
    return {"data": row}


@pytest.mark.parametrize("venue,payload", [
    ("kalshi", {"order_id": "native", "fill_count_fp": "10.00"}),
    ("novig", {"orderId": "native", "clientId": CLIENT}),
    ("prophetx", {"data": {"success": True, "order": {"order_id": "native", "external_id": CLIENT,
                                                              "filled_quantity": 10}}}),
])
def test_placement_identity_never_implies_filled(venue, payload):
    result = normalize_ack(venue, payload, intent(venue))
    assert result.status == "acknowledged"
    assert not result.terminal and not result.financial_complete and result.snapshot is None


@pytest.mark.parametrize("payload", [{}, {"orderId": "native", "clientId": "wrong"}])
def test_malformed_success_is_unknown_not_rejection(payload):
    with pytest.raises(UnknownOrderOutcome):
        normalize_ack("novig", payload, intent("novig"))


def test_kalshi_fractional_partial_and_cancel_keep_exact_filled_money():
    order = intent()
    result = normalize_kalshi_order(order, kalshi_order(), expected_order_id="native", observed_at=NOW)
    assert result.status == "partial"
    assert result.snapshot.cumulative_quantity == D("2.50")
    assert result.snapshot.cumulative_cost_usd == D("1.50")
    raw = kalshi_order()
    raw["order"].update(status="canceled", remaining_count_fp="0")
    cancelled = normalize_kalshi_order(order, raw, expected_order_id="native", observed_at=NOW)
    assert cancelled.terminal and cancelled.status == "cancelled"
    assert cancelled.snapshot.cumulative_quantity == D("2.50")


async def test_kalshi_index_three_intent_survives_recovery_and_scopes_each_supported_route():
    original = intent()
    order = original.model_copy(update={"mode": "production", "instrument":
        original.instrument.model_copy(update={"exchange_index": 3})})
    order = NativeOrderIntent.model_validate_json(order.model_dump_json())
    assert order.instrument.exchange_index == 3
    calls = []
    raw = kalshi_order()
    raw["order"].update(exchange_index=3, subaccount_number=0)
    async with httpx.AsyncClient() as http:
        client = KalshiClient(client=http)
        client.settings.environment = "production"
        async def request(method, path, **kwargs):
            assert method != "POST"  # No fixture path may place an order.
            calls.append((method, path, kwargs.get("params")))
            if method == "DELETE":
                return {"order_id": "native", "client_order_id": CLIENT, "reduced_by": "7.50"}
            if path == "/portfolio/orders":
                return {"orders": [raw["order"]], "cursor": ""}
            if path == "/portfolio/fills":
                return {"fills": [], "cursor": ""}
            return raw
        client.request = request
        bridge = NativeExecutionVenue(client, RiskAuthority(), mappings=[mapping(order)])
        body = bridge.build_native_request(order)
        assert body["ticker"] == "instrument" and "exchange_index" not in body
        assert await bridge.resolve_client_order(order) == "native"
        assert (await bridge.reconcile_native("native", order)).status == "partial"
        await bridge.cancel_native("native", order)
        await client.fetch_fills(ticker="instrument", subaccount=0, exchange_index=3)
        assert calls == [
            ("GET", "/portfolio/orders", {"limit": 200, "ticker": "instrument", "subaccount": 0,
                                           "exchange_index": 3}),
            ("GET", "/portfolio/orders/native", None),
            ("DELETE", "/portfolio/events/orders/native", {"market_ticker": "instrument",
                                                            "exchange_index": 3}),
            ("GET", "/portfolio/fills", {"ticker": "instrument", "subaccount": 0,
                                         "exchange_index": 3}),
        ]
        bridge.preflight = NativePreflight(environment="production", account_authorized=True,
            geographic_eligibility_verified=True, native_trading_enabled=True,
            evidence="fixture", valid_until=time.time()+60)
        bridge.bound_exchange_index = 0
        with pytest.raises(PreflightRequired, match="shard"):
            bridge._native_preflight(order)
        bridge.bound_exchange_index = 3
        bridge._native_preflight(order)


async def test_kalshi_index_three_wrong_shard_or_404_stays_unknown():
    original = intent()
    order = original.model_copy(update={"mode": "production", "instrument":
        original.instrument.model_copy(update={"exchange_index": 3})})
    raw = kalshi_order()
    raw["order"].update(exchange_index=0, subaccount_number=0)
    with pytest.raises(ReconciliationError, match="shard"):
        normalize_kalshi_order(order, raw, expected_order_id="native", observed_at=NOW)
    with pytest.raises(ReconciliationError, match="shard"):
        match_client_order("kalshi", order, [raw["order"]])
    async with httpx.AsyncClient() as http:
        client = KalshiClient(client=http)
        async def missing(order_id):
            raise VenueHTTPError("kalshi", 404)
        client.reconcile_order = missing
        bridge = NativeExecutionVenue(client, RiskAuthority(), mappings=[mapping(order)])
        report = await bridge.reconcile_native("native", order)
        assert report.status == "unknown" and not report.financial_complete


@pytest.mark.parametrize("field,value", [("client_order_id", "other"), ("ticker", "wrong"), ("action", "sell"),
                                          ("initial_count_fp", "20"), ("fill_count_fp", "11"),
                                          ("taker_fees_dollars", None), ("status", "settled")])
def test_inconsistent_kalshi_evidence_fails_closed(field, value):
    raw = kalshi_order()
    raw["order"][field] = value
    with pytest.raises(ReconciliationError):
        normalize_kalshi_order(intent(), raw, expected_order_id="native", observed_at=NOW)


def test_private_kalshi_no_fill_uses_complement_and_source_match_time():
    order = intent().model_copy(update={"instrument": intent().instrument.model_copy(update={"native_outcome": "no"})})
    frame = {"type": "fill", "msg": dict(order_id="native", client_order_id=CLIENT, market_ticker="instrument",
             action="buy", purchased_side="no", yes_price_dollars=".60", count_fp=".25", fee_cost=".01",
             trade_id="fill-id", ts_ms=int(NOW.timestamp()*1000))}
    fill = normalize_kalshi_fill(order, frame, expected_order_id="native")
    assert fill.price == D(".40") and fill.cost_usd == D(".10") and fill.gross_payout_usd == D(".25")
    assert fill.filled_at == NOW


def test_novig_cent_units_cost_and_explicit_fee():
    order = intent("novig")
    fill = normalize_novig_fill(order, novig_fill(), expected_order_id="native")
    assert fill.quantity == 40 and fill.gross_payout_usd == D(".40") and fill.price == D(".6")
    report = normalize_novig_order(order, novig_order(), expected_order_id="native", observed_at=NOW,
                                   fills=(fill,), history_complete=True)
    assert report.status == "partial" and report.financial_complete
    assert report.snapshot.cumulative_fee_usd == D(".005")


def test_novig_documented_uncharged_fill_omits_fee_but_null_is_invalid():
    raw = novig_fill()
    del raw["fee"]
    assert normalize_novig_fill(intent("novig"), raw, expected_order_id="native").fee_usd == 0
    raw["fee"] = None
    with pytest.raises(ReconciliationError):
        normalize_novig_fill(intent("novig"), raw, expected_order_id="native")


def test_novig_partial_pagination_and_cancel_missing_watermark_stay_reserved():
    order = intent("novig")
    fill = normalize_novig_fill(order, novig_fill(), expected_order_id="native")
    pending = normalize_novig_order(order, novig_order(), expected_order_id="native", observed_at=NOW,
                                    fills=(fill,), history_complete=False)
    assert not pending.financial_complete
    cancelled = normalize_novig_order(order, novig_order("CANCELED", 0), expected_order_id="native", observed_at=NOW,
                                      fills=(fill,), history_complete=True)
    assert cancelled.terminal and not cancelled.financial_complete
    confirmed = normalize_novig_order(order, novig_order("CANCELED", 0), expected_order_id="native", observed_at=NOW,
                                      fills=(fill,), history_complete=True, confirmed_filled_quantity=D(40))
    assert confirmed.financial_complete and confirmed.snapshot.cumulative_quantity == 40


def test_novig_duplicate_fill_page_deduplicated_but_conflict_rejected():
    order = intent("novig")
    fill = normalize_novig_fill(order, novig_fill(), expected_order_id="native")
    reread = fill.model_copy(update={"observed_at": fill.observed_at + timedelta(seconds=1)})
    result = normalize_novig_order(order, novig_order(), expected_order_id="native", observed_at=NOW,
                                  fills=(fill, reread), history_complete=True)
    assert result.snapshot.cumulative_quantity == 40
    with pytest.raises(ReconciliationError):
        normalize_novig_order(order, novig_order(), expected_order_id="native", observed_at=NOW,
                              fills=(fill, fill.model_copy(update={"fee_usd": D(".006")})), history_complete=True)


def test_prophetx_partial_remainder_killed_is_terminal_with_position():
    order = intent("prophetx")
    report = normalize_prophetx_order(order, prophetx_order(), expected_order_id="native", observed_at=NOW)
    assert report.terminal and report.status == "cancelled"
    assert not report.financial_complete and report.snapshot is None


def test_prophetx_private_fill_clock_and_cumulative_are_separate():
    order = intent("prophetx")
    payload = {"info": dict(id="native", external_id=CLIENT, strike_id="instrument", update_type="matching",
                            filled_quantity="4", total_filled_quantity="4", fill_price=150,
                            fee=".02", matching_id=124)}
    fill = normalize_prophetx_fill(order, payload, expected_order_id="native", published_at=NOW)
    assert fill.quantity == 4 and fill.gross_payout_usd == 10
    assert fill.filled_at is None and fill.timestamp_kind == "publication" and fill.observed_at == NOW
    report = normalize_prophetx_order(order, prophetx_order(), expected_order_id="native", observed_at=NOW,
                                      fills=(fill,), history_complete=True)
    assert report.financial_complete and report.snapshot.cumulative_cost_usd == 4
    assert report.snapshot.cumulative_gross_payout_usd == 10


def test_prophetx_ambiguous_payout_and_missing_fees_fail_closed():
    order = intent("prophetx")
    payload = {"info": dict(id="native", external_id=CLIENT, strike_id="instrument", update_type="matching",
                            filled_quantity="1", total_filled_quantity="1", fill_price=-132,
                            fee=".02", matching_id=124)}
    with pytest.raises(ReconciliationError, match="profit evidence"):
        normalize_prophetx_fill(order, payload, expected_order_id="native", published_at=NOW)
    payload["info"].update(fill_price=150)
    del payload["info"]["fee"]
    with pytest.raises(ReconciliationError, match="fee"):
        normalize_prophetx_fill(order, payload, expected_order_id="native", published_at=NOW)


@pytest.mark.parametrize("code,status,stopped,retry", [(None, 200, True, False), ("order_already_filled", 400, True, False),
    ("order_already_cancelled", 400, True, False), ("order_is_placing", 409, False, True),
    ("order_is_matching", 409, False, True), ("data_not_found", 404, False, False)])
def test_prophetx_cancel_races_never_claim_fill_quantities(code, status, stopped, retry):
    result = normalize_prophetx_cancel("native", intent("prophetx"), status_code=status, error_code=code)
    assert result.matching_stopped == stopped and result.retry_cancel_allowed == retry
    assert not result.financial_complete


def test_recovery_client_id_absence_never_proves_rejection_and_duplicates_block():
    order = intent("novig")
    assert match_client_order("novig", order, []) is None
    with pytest.raises(ReconciliationError, match="Multiple"):
        match_client_order("novig", order, [{"clientId": CLIENT, "orderId": "first"},
                                            {"clientId": CLIENT, "orderId": "second"}])


@pytest.mark.parametrize("venue,client_type", [("kalshi", KalshiClient), ("novig", NovigClient), ("prophetx", ProphetXClient)])
async def test_extended_bridge_native_request_units_and_one_use_authority(venue, client_type):
    order = intent(venue)
    authority, seen = RiskAuthority(), []
    async with httpx.AsyncClient() as http:
        client = client_type(client=http)
        if venue == "prophetx":
            client.set_verified_price_ladder([120, 150])
        async def submit(body, *, before_send):
            before_send()
            seen.append(body)
            client.settings.gate.require(CLIENT, "sandbox")
            return ({"order_id": "native"} if venue == "kalshi" else {"orderId": "native", "clientId": CLIENT}
                    if venue == "novig" else {"data": {"success": True, "order": {"order_id": "native", "external_id": CLIENT}}})
        client.submit_order = submit
        bridge = NativeExecutionVenue(client, authority, mappings=[mapping(order)],
            preflight=NativePreflight(account_authorized=True, native_trading_enabled=True,
                                      evidence="offline fixture only", valid_until=time.time()+60))
        result = await bridge.submit_intent(order, authority.issue(order.id))
        assert result.status == "acknowledged" and not client.settings.gate.enabled
        with pytest.raises(UnknownOrderOutcome):
            await bridge.submit_intent(order, authority.issue(order.id))
        assert len(seen) == 1
        if venue == "novig":
            assert seen[0]["qty"] == 110 and seen[0]["price"] == "0.665" and seen[0]["tif"] == "IOC"
        if venue == "prophetx": assert seen[0]["quantity"] == 10 and seen[0]["price"] == 120


async def test_unknown_native_transmission_never_retries_and_gate_resets():
    order, authority = intent("novig"), RiskAuthority()
    async with httpx.AsyncClient() as http:
        client, calls = NovigClient(client=http), []
        async def unknown(body, *, before_send):
            before_send()
            calls.append(body)
            raise UnknownOrderOutcome(CLIENT)
        client.submit_order = unknown
        bridge = NativeExecutionVenue(client, authority, mappings=[mapping(order)], preflight=NativePreflight(
            account_authorized=True, native_trading_enabled=True, evidence="fixture", valid_until=time.time()+60))
        for _ in range(2):
            with pytest.raises(UnknownOrderOutcome):
                await bridge.submit_intent(order, authority.issue(order.id))
        assert len(calls) == 1 and not client.settings.gate.enabled


async def test_rest_404_stays_unknown_not_cancelled():
    order = intent("prophetx")
    async with httpx.AsyncClient() as http:
        client = ProphetXClient(client=http)
        async def missing(order_id): raise VenueHTTPError("prophetx", 404, "data_not_found")
        client.reconcile_order = missing
        bridge = NativeExecutionVenue(client, RiskAuthority(), mappings=[mapping(order)])
        report = await bridge.reconcile_native("native", order)
        assert report.status == "unknown" and not report.financial_complete


async def test_novig_fill_cursor_loop_and_identity_conflict_fail_closed():
    order = intent("novig")
    async with httpx.AsyncClient() as http:
        client = NovigClient(client=http)
        async def read_order(order_id): return novig_order()
        async def fills(**kwargs): return {"items": [novig_fill()], "next": "loop"}
        client.reconcile_order, client.fetch_fills = read_order, fills
        bridge = NativeExecutionVenue(client, RiskAuthority(), mappings=[mapping(order)])
        assert (await bridge.reconcile_native("native", order)).status == "unknown"


async def test_unknown_identity_recovery_scans_all_pages_before_unique_match():
    order = intent("novig")
    async with httpx.AsyncClient() as http:
        client, calls = NovigClient(client=http), []
        async def pages(**kwargs):
            calls.append(kwargs)
            return ({"items": [{"clientId": CLIENT, "orderId": "native"}], "next": "second"}
                    if "after" not in kwargs else {"items": []})
        client.reconcile_account = pages
        bridge = NativeExecutionVenue(client, RiskAuthority(), mappings=[mapping(order)])
        assert await bridge.resolve_client_order(order) == "native"
        assert len(calls) == 2


@pytest.mark.parametrize("cursor", [None, False, 0, ""])
async def test_malformed_next_cursor_is_not_exhaustive_history(cursor):
    order = intent("novig")
    async with httpx.AsyncClient() as http:
        client = NovigClient(client=http)
        async def pages(**kwargs):
            return {"items": [{"clientId": CLIENT, "orderId": "native"}], "next": cursor}
        client.reconcile_account = pages
        bridge = NativeExecutionVenue(client, RiskAuthority(), mappings=[mapping(order)])
        with pytest.raises(ReconciliationError, match="cursor"):
            await bridge.resolve_client_order(order)


async def test_private_contiguous_watermark_proves_cancelled_fills_without_guessing_fees():
    order = intent("novig")
    async with httpx.AsyncClient() as http:
        client = NovigClient(client=http)
        async def read_order(order_id): return novig_order("CANCELED", 0)
        async def fills(**kwargs): return {"items": [novig_fill()]}
        client.reconcile_order, client.fetch_fills = read_order, fills
        bridge = NativeExecutionVenue(client, RiskAuthority(), mappings=[mapping(order)])
        bridge.bind_native_intent("native", order)
        bridge.observe_private_frame({"snapshot": {}, "orders": {"seq": 10, "open": [
            {"orderId": "native", "clientId": CLIENT, "outcomeId": OUTCOME, "qty": 110}]}})
        bridge.observe_private_frame({"delta": {}, "orders": {"seq": 11, "deltas": [
            {"kind": "fill", "orderId": "native", "clientId": CLIENT, "outcomeId": OUTCOME,
             "qty": 40, "remaining": 70}, {"kind": "cancel", "orderId": "native"}]}})
        report = await bridge.reconcile_native("native", order)
        assert report.financial_complete and report.snapshot.cumulative_quantity == 40
        bridge.observe_private_frame({"type": "transport_invalidated"})
        assert not (await bridge.reconcile_native("native", order)).financial_complete


async def test_private_sequence_gap_or_regression_invalidates_cancel_watermark():
    from evplatform.venues.common import SequenceGap
    order = intent("novig")
    async with httpx.AsyncClient() as http:
        bridge = NativeExecutionVenue(NovigClient(client=http), RiskAuthority(), mappings=[mapping(order)])
        bridge.bind_native_intent("native", order)
        snapshot = {"snapshot": {}, "orders": {"seq": 1, "open": [
            {"orderId": "native", "clientId": CLIENT, "outcomeId": OUTCOME, "qty": 110}]}}
        bridge.observe_private_frame(snapshot)
        with pytest.raises(SequenceGap):
            bridge.observe_private_frame({"delta": {}, "orders": {"seq": 3, "deltas": []}})
        assert bridge._confirmed_filled == {}
        bridge.observe_private_frame(snapshot)
        with pytest.raises(ReconciliationError):
            bridge.observe_private_frame({"delta": {}, "orders": {"seq": 2, "deltas": [
                {"kind": "fill", "orderId": "native", "clientId": CLIENT, "outcomeId": OUTCOME,
                 "qty": 30, "remaining": 70}]}})
        assert bridge._confirmed_filled == {}


async def test_native_rest_original_decimal_text_is_never_rounded_through_float():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(
        200, content=b'{"data":{"quantity":123456789012345.67,"fee":0.00000000000000001234}}'))) as http:
        native = NativeTransport(NativeSettings(), client=http)
        response = await native._request(http.build_request("GET", "https://test.invalid/read"))
    assert response["data"]["quantity"] == D("123456789012345.67")
    assert response["data"]["fee"] == D("0.00000000000000001234")
    assert isinstance(response["data"]["quantity"], D)


def test_double_encoded_talaria_preserves_native_stake_cash_decimal():
    payload = base64.b64encode(b'{"info":{"filled_quantity":123456789012345.67,"fee":0.00000000000000001234}}').decode()
    frame = json.dumps({"event": "orders", "data": json.dumps({"payload": payload})})
    parsed = decode_talaria(frame)
    assert parsed["data"]["info"]["filled_quantity"] == D("123456789012345.67")
    assert parsed["data"]["info"]["fee"] == D("0.00000000000000001234")
