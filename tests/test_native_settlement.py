"""Offline official-shape fixtures, not evidence of an actual venue payment."""
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal as D

import httpx
import pytest

from evplatform.core.conflicts import reviewed_correlations, risk_family
from evplatform.core.models import OrderStatus, utcnow
from evplatform.core.native_accounting import NativeInstrument, NativeOrderIntent, NativePriceBand
from evplatform.core.native_oms import NativeOrder
from evplatform.venues.account_probe import novig_wallet_fingerprint
from evplatform.venues.novig import NovigClient, NovigSettings
from evplatform.venues.reconciliation import ReconciliationError
from evplatform.venues.settlement import reconcile_novig_settlement


KEY = "11111111-1111-4111-8111-111111111111"
NATIVE = "22222222-2222-4222-8222-222222222222"
OUTCOME = "33333333-3333-4333-8333-333333333333"
CLIENT = "44444444-4444-4444-8444-444444444444"


@pytest.fixture
def settlement_case(market):
    now = utcnow()
    instrument = NativeInstrument(venue="novig", instrument_id=NATIVE, canonical_market_id=market.id,
        outcome="home", native_outcome=OUTCOME, quantity_unit="whole_cent_contracts", price_format="probability",
        payout_per_unit_usd=".01", quantity_step="1", minimum_quantity="1", cash_quantum_usd=".00001",
        price_bands=(NativePriceBand(minimum=".055", maximum=".945", step=".005"),),
        settlement_rules_fingerprint=market.identity(), verified=True, source="OFFLINE documented fixture")
    intent = NativeOrderIntent(id="local", client_order_id=CLIENT, instrument=instrument, quantity="100",
        limit_price=".60", max_fee_usd=".02", fee_source="OFFLINE fee fixture", fees_verified=True,
        mode="sandbox", created_at=now-timedelta(minutes=10))
    order = NativeOrder(intent=intent, candidate_id="candidate", strategy="native_positive_ev",
        account_fingerprint=novig_wallet_fingerprint("sandbox", KEY), market_snapshot=market,
        market_fingerprint=market.identity(), risk_family_id=risk_family(market),
        reviewed_correlation_groups=reviewed_correlations(market), status=OrderStatus.FILLED,
        filled_quantity="100", filled_cost_usd=".6", gross_payout_usd="1", fees=".01440",
        venue_order_id="native-order", reconciliation_complete=True)
    settled_ms = int((now-timedelta(seconds=30)).timestamp()*1000)
    responses = {
        f"/v3/account/subaccounts/{KEY}/balance": {"keyId": KEY, "balance": "100"},
        f"/v3/history/markets/{NATIVE}": {"marketId": NATIVE, "status": "SETTLED", "settledTs": settled_ms,
            "outcomes": [{"outcomeId": OUTCOME, "status": "WIN"}]},
        "/v3/history/positions": {"items": [{"positionId": "native-position", "marketId": NATIVE,
            "outcomeId": OUTCOME, "status": "SETTLED", "result": "WIN", "resettled": False,
            "settledTs": settled_ms, "qty": 100, "cost": ".6", "payout": "1", "pnl": ".4"}]},
        "/v3/portfolio/fills": {"items": [{"fillId": "fill1", "orderId": "native-order", "clientId": CLIENT,
            "marketId": NATIVE, "outcomeId": OUTCOME, "qty": 100, "cost": ".6", "fee": ".01440",
            "taker": True, "ts": int((now-timedelta(minutes=5)).timestamp()*1000)}]},
    }
    return order, responses


async def run(case, *, managed=None):
    order, responses = case
    calls = []
    async with httpx.AsyncClient() as http:
        client = NovigClient(NovigSettings(api_key=KEY), client=http)
        async def request(method, path, **kwargs):
            assert method == "GET"
            calls.append((path, kwargs))
            response = responses[path]
            return response(kwargs.get("params", {})) if callable(response) else deepcopy(response)
        client.request = request
        result = await reconcile_novig_settlement(client, order, [order] if managed is None else managed)
    return result, calls


async def test_exact_cash_win_uses_source_settlement_timestamp(settlement_case):
    order, responses = settlement_case
    result, calls = await run(settlement_case)
    assert result.payout_usd == 1 and result.additional_fee_usd == result.fee_refund_usd == 0
    assert result.account_fingerprint == order.account_fingerprint
    assert int(result.settled_at.timestamp()*1000) == responses[f"/v3/history/markets/{NATIVE}"]["settledTs"]
    assert '"execution_fees_usd": "0.01440"' in result.evidence
    assert len(calls) == 4
    # All fills in the market are compared, not an order-only subset that could
    # hide an external offsetting trade.
    assert calls[-1][1]["params"]["market"] == NATIVE


async def test_actual_loss_cash_is_zero_not_inferred_profit(settlement_case):
    order, responses = settlement_case
    responses[f"/v3/history/markets/{NATIVE}"]["outcomes"][0]["status"] = "LOSS"
    responses["/v3/history/positions"]["items"][0].update(result="LOSS", payout="0", pnl="-.6")
    result, _ = await run(settlement_case)
    assert result.payout_usd == 0
    assert result.settlement_id.endswith(":LOSS")


async def test_ungraded_market_returns_no_settlement(settlement_case):
    order, responses = settlement_case
    responses[f"/v3/history/markets/{NATIVE}"]["status"] = "CLOSED"
    result, calls = await run(settlement_case)
    assert result is None and len(calls) == 2


@pytest.mark.parametrize("field,value", [("qty", 99), ("cost", ".61"), ("payout", "2"),
    ("pnl", None), ("pnl", ".41"), ("resettled", True), ("result", "PUSH"), ("outcomeId", "foreign")])
async def test_position_cash_identity_and_remediation_fail_closed(settlement_case, field, value):
    settlement_case[1]["/v3/history/positions"]["items"][0][field] = value
    with pytest.raises((ReconciliationError, ValueError)):
        await run(settlement_case)


@pytest.mark.parametrize("field,value", [("orderId", "foreign"), ("marketId", "foreign"),
    ("outcomeId", "foreign"), ("taker", False), ("taker", None), ("qty", 99), ("fee", ".02")])
async def test_unmanaged_or_maker_fills_never_allocated(settlement_case, field, value):
    settlement_case[1]["/v3/portfolio/fills"]["items"][0][field] = value
    with pytest.raises((ReconciliationError, ValueError)):
        await run(settlement_case)


async def test_wrong_authenticated_wallet_rejected(settlement_case):
    settlement_case[1][f"/v3/account/subaccounts/{KEY}/balance"]["keyId"] = "foreign"
    with pytest.raises(ReconciliationError, match="wallet"):
        await run(settlement_case)


async def test_multiple_managed_orders_even_already_settled_are_ambiguous(settlement_case):
    order, _ = settlement_case
    with pytest.raises(ReconciliationError, match="Shared"):
        await run(settlement_case, managed=[order, order.model_copy(update={"intent": order.intent.model_copy(update={"id": "other"})})])


@pytest.mark.parametrize("updates", [{"filled_quantity": D(0)}, {"reconciliation_complete": False}, {"status": OrderStatus.UNKNOWN}])
async def test_unknown_unfilled_and_incomplete_orders_cannot_settle(settlement_case, updates):
    with pytest.raises(ReconciliationError, match="terminal"):
        await run((settlement_case[0].model_copy(update=updates), settlement_case[1]))


async def test_history_pagination_must_finish_and_conflicting_fill_rejected(settlement_case):
    order, responses = settlement_case
    original = responses["/v3/portfolio/fills"]["items"][0]
    responses["/v3/portfolio/fills"] = lambda params: {"items": [original], "next": "loop"}
    with pytest.raises(ReconciliationError, match="cursor"):
        await run(settlement_case)
    responses["/v3/portfolio/fills"] = lambda params: ({"items": [original], "next": "second"} if "after" not in params else
                                                       {"items": [{**original, "fee": ".02"}]})
    with pytest.raises(ReconciliationError, match="Conflicting"):
        await run(settlement_case)
    responses["/v3/portfolio/fills"] = lambda params: ({"items": [original], "next": "second"} if "after" not in params else {"items": [original]})
    result, _ = await run(settlement_case)
    assert result.payout_usd == 1


async def test_source_clock_disagreement_and_future_timestamp_rejected(settlement_case):
    order, responses = settlement_case
    responses["/v3/history/positions"]["items"][0]["settledTs"] += 1
    with pytest.raises(ReconciliationError, match="grade"):
        await run(settlement_case)
    responses[f"/v3/history/markets/{NATIVE}"]["settledTs"] = int((utcnow()+timedelta(days=1)).timestamp()*1000)
    with pytest.raises(ReconciliationError, match="timestamp"):
        await run(settlement_case)


async def test_documented_uncharged_fill_does_not_require_fee_field(settlement_case):
    order, responses = settlement_case
    del responses["/v3/portfolio/fills"]["items"][0]["fee"]
    result, _ = await run((order.model_copy(update={"fees": D(0)}), responses))
    assert result.payout_usd == 1
