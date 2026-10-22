"""Offline private-state and actual-cash shaped fixtures, not live certification."""
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal as D

import httpx
import pytest

from evplatform.core.conflicts import reviewed_correlations, risk_family
from evplatform.core.models import OrderStatus, utcnow
from evplatform.core.native_accounting import NativeOrderIntent
from evplatform.core.native_oms import NativeOrder
from evplatform.venues.account_probe import kalshi_account_fingerprint, probe_native_account
from evplatform.venues.kalshi import KalshiClient, KalshiSettings, KalshiPrivateState
from evplatform.venues.kalshi_settlement import reconcile_kalshi_settlement
from evplatform.venues.reconciliation import ReconciliationError
from test_native_book_normalization import mapping


KEY = "OFFLINE-kalshi-primary-key"


def acknowledge(private):
    private.begin()
    for command, channel in enumerate(sorted(private.CHANNELS), 1):
        private.commands[command] = channel
        private.apply({"id": command, "type": "subscribed", "msg": {"channel": channel, "sid": command + 10}})


def private_message(private, kind="user_order"):
    return {"type": kind, "sid": private.channels[private.KINDS[kind]], "msg": {"order_id": "OFFLINE"}}


def test_all_private_acks_bound_to_our_unfiltered_commands_required():
    private = KalshiPrivateState()
    private.begin()
    private.apply({"type": "subscribed", "msg": {"channel": "fill", "sid": 1}})
    assert not private.ready and private.fence() is None
    acknowledge(private)
    assert private.ready and private.fence() == (2, 0)
    prior = private.fence()
    private.apply(private_message(private, "fill"))
    assert private.fence() != prior and private.revision == 1
    private.invalidate()
    assert not private.ready and private.fence() is None
    acknowledge(private)
    assert private.generation == 3 and private.revision == 0


@pytest.mark.parametrize("kind", ["fill", "user_order", "market_position"])
def test_every_private_event_invalidates_previous_read_before_publication(kind):
    private = KalshiPrivateState()
    acknowledge(private)
    barrier = private.barrier
    private.apply(private_message(private, kind))
    assert private.revision == 1 and private.barrier >= barrier
    with pytest.raises(ValueError, match="Unbound"):
        private.apply({**private_message(private, kind), "sid": 999})
    assert private.revision == 2


@pytest.fixture
def kalshi_case(market):
    now = utcnow()
    instrument = mapping(market).instrument
    intent = NativeOrderIntent(id="local", client_order_id="client", instrument=instrument,
        quantity="2.50", limit_price=".60", max_fee_usd=".10", fee_source="OFFLINE fixture", fees_verified=True,
        mode="sandbox", created_at=now-timedelta(minutes=10))
    order = NativeOrder(intent=intent, candidate_id="candidate", strategy="native_positive_ev",
        account_fingerprint=kalshi_account_fingerprint("sandbox", KEY), market_snapshot=market,
        market_fingerprint=market.identity(), risk_family_id=risk_family(market),
        reviewed_correlation_groups=reviewed_correlations(market), status=OrderStatus.FILLED,
        filled_quantity="2.50", filled_cost_usd="1.50", gross_payout_usd="2.50", fees=".042000",
        venue_order_id="native-order", reconciliation_complete=True)
    settlement = {"ticker": "TICKER", "exchange_index": 0, "event_ticker": "EVENT", "market_result": "yes",
        "yes_count_fp": "2.50", "yes_total_cost_dollars": "1.50", "no_count_fp": "0.00", "no_total_cost_dollars": "0",
        "revenue": 250, "fee_cost": ".042000", "settled_time": (now-timedelta(seconds=30)).isoformat(), "value": 100}
    fill = {"fill_id": "fill1", "trade_id": "fill1", "order_id": "native-order", "ticker": "TICKER",
        "market_ticker": "TICKER", "exchange_index": 0, "outcome_side": "yes", "book_side": "bid",
        "count_fp": "2.50", "yes_price_dollars": ".60", "no_price_dollars": ".40", "is_taker": True,
        "subaccount_number": 0, "fee_cost": ".042000", "created_time": (now-timedelta(minutes=5)).isoformat()}
    responses = {"/api_keys": {"api_keys": [{"api_key_id": KEY, "scopes": ["read", "write::trade"]}]},
        "/portfolio/settlements": {"settlements": [settlement]},
        "/portfolio/fills": {"fills": [fill], "cursor": ""}, "/historical/fills": {"fills": [], "cursor": ""},
        "/portfolio/balance": {"balance_dollars": "50.1234"}, "/portfolio/orders": {"orders": [], "cursor": ""},
        "/portfolio/positions": {"market_positions": [{"ticker": "TICKER", "exchange_index": 0,
            "position_fp": "2.50", "market_exposure_dollars": "1.50", "total_traded_dollars": "1.50",
            "fees_paid_dollars": ".042000", "realized_pnl_dollars": "0", "last_updated_ts": now.isoformat()}], "cursor": ""}}
    return order, responses


async def call(case, *, probe=False, managed=None, mutate=None, private=True):
    order, responses = case
    calls = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: pytest.fail("No live request"))) as http:
        client = KalshiClient(KalshiSettings(api_key_id=KEY), http)
        if private:
            acknowledge(client.private)
        async def request(method, path, **kwargs):
            assert method == "GET"
            calls.append((path, kwargs))
            if mutate:
                mutate(client, path)
            if path == "/exchange/user_data_timestamp":
                return deepcopy(responses.get(path, {"as_of_time": utcnow().isoformat()}))
            value = responses[path]
            return deepcopy(value(kwargs.get("params", {})) if callable(value) else value)
        client.request = request
        inventory = [order] if managed is None else managed
        result = await (probe_native_account(client, inventory) if probe else reconcile_kalshi_settlement(client, order, inventory))
        assert not client.settings.gate.enabled
        matches = client.private.matches(result) if probe else None
        return result, calls, matches


async def test_exact_fractional_paid_settlement_and_complete_history(kalshi_case):
    result, calls, _ = await call(kalshi_case)
    assert result.payout_usd == D("2.50") and result.additional_fee_usd == result.fee_refund_usd == 0
    assert result.settled_at.isoformat() == kalshi_case[1]["/portfolio/settlements"]["settlements"][0]["settled_time"]
    assert '"revenue_cents": 250' in result.evidence
    for path, kwargs in calls[1:]:
        assert kwargs["params"]["ticker"] == "TICKER" and kwargs["params"]["subaccount"] == 0
        assert "order_id" not in kwargs["params"]


async def test_loss_and_partial_cancel_use_only_filled_quantity(kalshi_case):
    order, rows = kalshi_case
    rows["/portfolio/settlements"]["settlements"][0].update(market_result="no", revenue=0, value=0)
    order = order.model_copy(update={"status": OrderStatus.CANCELLED,
                                   "intent": order.intent.model_copy(update={"quantity": D("4")})})
    result, _, _ = await call((order, rows))
    assert result.payout_usd == 0


async def test_no_side_preserves_no_cost_and_actual_payout(kalshi_case):
    order, rows = kalshi_case
    inst = order.intent.instrument.model_copy(update={"native_outcome": "no"})
    order = order.model_copy(update={"intent": order.intent.model_copy(update={"instrument": inst})})
    rows["/portfolio/settlements"]["settlements"][0].update(market_result="no", value=0,
        yes_count_fp="0", yes_total_cost_dollars="0", no_count_fp="2.50", no_total_cost_dollars="1.50")
    rows["/portfolio/fills"]["fills"][0].update(outcome_side="no", book_side="ask", yes_price_dollars=".40", no_price_dollars=".60")
    result, _, _ = await call((order, rows))
    assert result.payout_usd == D("2.50")


@pytest.mark.parametrize("field,value", [("market_result", "scalar"), ("revenue", "250"), ("revenue", True),
    ("revenue", 249), ("yes_count_fp", "2.49"), ("no_count_fp", ".01"), ("yes_total_cost_dollars", "1.49"),
    ("fee_cost", ".0419"), ("value", 99), ("exchange_index", True), ("ticker", "foreign")])
async def test_ambiguous_settlement_never_releases_exposure(kalshi_case, field, value):
    kalshi_case[1]["/portfolio/settlements"]["settlements"][0][field] = value
    with pytest.raises(ReconciliationError):
        await call(kalshi_case)


@pytest.mark.parametrize("field,value", [("order_id", "foreign"), ("count_fp", "2.49"), ("fee_cost", "0"),
    ("yes_price_dollars", ".61"), ("subaccount_number", 1), ("subaccount_number", None),
    ("is_taker", False), ("action", "sell"), ("exchange_index", 1), ("trade_id", "other"), ("book_side", "ask")])
async def test_unknown_or_contradictory_full_market_fill_is_rejected(kalshi_case, field, value):
    kalshi_case[1]["/portfolio/fills"]["fills"][0][field] = value
    with pytest.raises((ReconciliationError, ValueError)):
        await call(kalshi_case)


async def test_pagination_duplicate_and_history_handoff(kalshi_case):
    rows = kalshi_case[1]
    fill = rows["/portfolio/fills"]["fills"][0]
    rows["/historical/fills"]["fills"] = [deepcopy(fill)]
    result, _, _ = await call(kalshi_case)
    assert result.payout_usd == D("2.50")  # overlap is deduplicated economically
    rows["/portfolio/fills"]["fills"] = []
    result, _, _ = await call(kalshi_case)
    assert result.payout_usd == D("2.50")
    rows["/portfolio/fills"]["fills"] = [fill]
    rows["/historical/fills"]["fills"][0]["fee_cost"] = ".041"
    with pytest.raises(ReconciliationError, match="Conflicting"):
        await call(kalshi_case)
    rows["/historical/fills"] = {"fills": [], "cursor": "repeated"}
    with pytest.raises(ReconciliationError, match="pagination"):
        await call(kalshi_case)


async def test_missing_cash_record_unknown_and_shared_orders_not_assumed_paid(kalshi_case):
    order, rows = kalshi_case
    rows["/portfolio/settlements"]["settlements"] = []
    assert (await call(kalshi_case))[0] is None
    with pytest.raises(ReconciliationError, match="Terminal"):
        await call((order.model_copy(update={"status": OrderStatus.UNKNOWN}), rows))
    other = order.model_copy(update={"intent": order.intent.model_copy(update={"id": "other"})})
    with pytest.raises(ReconciliationError, match="Shared"):
        await call(kalshi_case, managed=[order, other])


async def test_private_inventory_fence_requires_rest_after_subscription(kalshi_case):
    snapshot, calls, synchronized = await call(kalshi_case, probe=True)
    assert snapshot.reconciled and synchronized
    assert snapshot.cash_available_verified
    assert snapshot.private_connection_generation == 1 and snapshot.private_revision == 0
    assert snapshot.order_sequence is snapshot.position_sequence is None
    kalshi_case[1]["/exchange/user_data_timestamp"] = {"as_of_time": (utcnow()-timedelta(minutes=1)).isoformat()}
    snapshot, _, synchronized = await call(kalshi_case, probe=True)
    assert snapshot.reconciled and not synchronized and snapshot.private_as_of is None


@pytest.mark.parametrize("change", ["private_update", "reconnect", "disconnect"])
async def test_state_changes_during_rest_cannot_install_baseline(kalshi_case, change):
    def mutate(client, path):
        if path != "/portfolio/positions": return
        if change == "private_update": client.private.apply(private_message(client.private))
        elif change == "reconnect": acknowledge(client.private)
        else: client.private.invalidate()
    snapshot, _, synchronized = await call(kalshi_case, probe=True, mutate=mutate)
    assert snapshot.reconciled and not synchronized and snapshot.private_as_of is None


@pytest.mark.parametrize("field,value", [("position_fp", "2.49"), ("market_exposure_dollars", "1.49"),
    ("fees_paid_dollars", "0"), ("total_traded_dollars", "3.00"), ("realized_pnl_dollars", ".001")])
async def test_position_financials_detect_offsetting_external_activity(kalshi_case, field, value):
    kalshi_case[1]["/portfolio/positions"]["market_positions"][0][field] = value
    snapshot, _, synchronized = await call(kalshi_case, probe=True)
    assert not snapshot.reconciled and not synchronized
    assert not snapshot.cash_available_verified


async def test_rest_only_account_can_be_inspected_but_never_claim_private_sync(kalshi_case):
    snapshot, calls, synchronized = await call(kalshi_case, probe=True, private=False)
    assert snapshot.reconciled and snapshot.identity_verified and not synchronized
    assert snapshot.private_connection_generation is None
    assert all(path != "/exchange/user_data_timestamp" for path, _ in calls)


async def test_private_subscriptions_are_unfiltered_and_not_duplicated_by_market_change():
    async with httpx.AsyncClient() as http:
        client = KalshiClient(KalshiSettings(), http)
        client.private.begin()
        sent = []
        async def send(payload): sent.append(payload)
        client._send_ws = send
        await client._subscribe_current()
        assert {p["params"]["channels"][0] for p in sent} == {"fill", "user_orders", "market_positions"}
        assert all(set(p["params"]) == {"channels"} for p in sent)
        for payload in sent:
            client.private.apply({"type": "subscribed", "id": payload["id"], "msg": {
                "channel": payload["params"]["channels"][0], "sid": payload["id"]+20}})
        assert client.private.ready
        sent.clear()
        client.tickers = ["TICKER"]
        await client._subscribe_current()
        assert len(sent) == 1 and sent[0]["params"]["channels"] == ["orderbook_delta"]
        assert sent[0]["params"]["use_yes_price"] is True


async def test_capability_requires_explicit_policy_exact_mapping_and_entire_order_history(market, kalshi_case):
    from evplatform.core.config import RiskConfig
    from evplatform.core.risk import RiskEngine
    from evplatform.venues.bridge import NativeExecutionVenue, VerifiedVenueMapping
    from evplatform.venues.kalshi_settlement import POLICY, supports_kalshi_settlement_scope
    from test_launch_readiness import ready_fixture
    item = mapping(market)
    inst = item.instrument
    execution = VerifiedVenueMapping(canonical_market_id=market.id, canonical_outcome=inst.outcome,
        native_instrument_id=inst.instrument_id, native_outcome=inst.native_outcome,
        settlement_fingerprint=market.identity(), mapping_evidence="OFFLINE exact review",
        verified=True, fee_schedule_verified=True, native_payout_usd=inst.payout_per_unit_usd,
        native_quantity_unit=inst.quantity_unit)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: pytest.fail("No network"))) as http:
        client = KalshiClient(KalshiSettings(), http)
        adapter = NativeExecutionVenue(client, RiskEngine(RiskConfig()).authority, mappings=[execution])
        profile, _, _ = ready_fixture("sandbox")
        profile = profile.model_copy(update={"venue": "kalshi", "market_ids": (market.id,), "settlement_policy": POLICY,
            "account_fingerprint": kalshi_case[0].account_fingerprint, "evidence": ()})
        assert supports_kalshi_settlement_scope(profile, adapter, (item,), (kalshi_case[0],))
        assert not profile.evidence  # Still requires real lifecycle certification elsewhere.
        assert not supports_kalshi_settlement_scope(profile.model_copy(update={"settlement_policy": "full"}), adapter, (item,), ())
        assert not supports_kalshi_settlement_scope(profile, adapter, (), ())
        second = kalshi_case[0].model_copy(update={"intent": kalshi_case[0].intent.model_copy(update={"id": "second"})})
        assert not supports_kalshi_settlement_scope(profile, adapter, (item,), (kalshi_case[0], second))
        bad = item.model_copy(update={"fee_bound": item.fee_bound.model_copy(update={"valid_until": utcnow()-timedelta(seconds=1)})})
        assert not supports_kalshi_settlement_scope(profile, adapter, (bad,), ())
        adapter.mappings.clear()
        assert not supports_kalshi_settlement_scope(profile, adapter, (item,), ())
