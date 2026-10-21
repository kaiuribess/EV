import httpx
import pytest
from decimal import Decimal
from types import SimpleNamespace

from evplatform.core.models import OrderStatus

from evplatform.venues.account_probe import probe_native_account, novig_wallet_fingerprint
from evplatform.venues.novig import NovigClient, NovigSettings
from evplatform.venues.kalshi import KalshiClient, KalshiSettings
from evplatform.venues.prophetx import ProphetXClient
from evplatform.venues.reconciliation import ReconciliationError


KEY = "11111111-1111-4111-8111-111111111111"


async def probe_with(responses):
    async with httpx.AsyncClient() as http:
        client = NovigClient(NovigSettings(api_key=KEY), client=http)
        calls = []
        async def request(method, path):
            assert method == "GET"
            calls.append(path)
            return responses[path]
        client.request = request
        result = await probe_native_account(client)
        return result, calls


def fixtures():
    return {f"/v3/account/subaccounts/{KEY}/balance": {"balance": "100.00000", "keyId": KEY},
            "/v3/account/orders": {"open": [], "seq": 0}, "/v3/account/positions": {"positions": [], "seq": 0}}


async def test_authenticated_echoed_wallet_identity_and_empty_inventory():
    result, calls = await probe_with(fixtures())
    assert result.account_fingerprint == novig_wallet_fingerprint("sandbox", KEY)
    assert result.identity_verified and result.reconciled and result.balance_usd == 100
    assert not result.trading_permission_verified
    assert len(calls) == 3


async def test_server_wallet_mismatch_rejected():
    data = fixtures()
    data[f"/v3/account/subaccounts/{KEY}/balance"]["keyId"] = "other"
    with pytest.raises(ReconciliationError, match="wallet key"):
        await probe_with(data)


async def test_unmanaged_position_blocks_inventory_reconciliation():
    data = fixtures()
    data["/v3/account/positions"]["positions"] = [{"marketId": "m", "outcomeId": "o", "qty": 100, "cost": ".6"}]
    result, _ = await probe_with(data)
    assert result.identity_verified and not result.reconciled and result.position_count == 1


async def test_unmanaged_resting_order_blocks_inventory_reconciliation():
    data = fixtures()
    data["/v3/account/orders"]["open"] = [{"orderId": "external", "qty": 100, "price": ".6"}]
    result, _ = await probe_with(data)
    assert not result.reconciled and result.open_order_count == 1


async def test_multisubaccount_envelope_is_not_empty_inventory():
    data = fixtures()
    data["/v3/account/orders"] = {"wallet1": {"open": [], "seq": 0}}
    with pytest.raises(ReconciliationError, match="complete personal"):
        await probe_with(data)


@pytest.mark.parametrize("position_cost,expected", [(".60000", True), (".61000", False)])
async def test_same_quantity_but_changed_native_cash_cost_blocks_account(position_cost, expected):
    order = SimpleNamespace(venue="novig", mode="sandbox", id="local", venue_order_id="native",
        account_fingerprint=novig_wallet_fingerprint("sandbox", KEY), reconciliation_complete=True,
        status=OrderStatus.FILLED, filled_quantity=Decimal(100), filled_cost_usd=Decimal(".6"),
        intent=SimpleNamespace(instrument=SimpleNamespace(instrument_id="market", native_outcome="outcome")))
    data = fixtures()
    data["/v3/account/positions"]["positions"] = [
        {"marketId": "market", "outcomeId": "outcome", "qty": 100, "cost": position_cost}]
    async with httpx.AsyncClient() as http:
        client = NovigClient(NovigSettings(api_key=KEY), client=http)
        async def request(method, path):
            assert method == "GET"
            return data[path]
        client.request = request
        result = await probe_native_account(client, [order])
    assert result.reconciled is expected
    if not expected:
        assert "position cost" in result.reason


async def test_no_automatic_prophetx_identity_claim_before_private_signin():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: pytest.fail("No request expected"))) as http:
        snapshot = await probe_native_account(ProphetXClient(client=http))
    assert not snapshot.identity_verified and not snapshot.reconciled and not snapshot.account_fingerprint


async def test_kalshi_authenticated_key_scope_primary_account_and_fractional_cash():
    calls = []
    async with httpx.AsyncClient() as http:
        client = KalshiClient(KalshiSettings(api_key_id=KEY), client=http)
        async def request(method, path, **kwargs):
            calls.append((method, path, kwargs))
            if path == "/api_keys": return {"api_keys": [{"api_key_id": KEY, "scopes": ["read"]}]}
            if path == "/portfolio/balance": return {"balance_dollars": "100.1234"}
            if path == "/portfolio/orders": return {"orders": [], "cursor": ""}
            if path == "/portfolio/positions": return {"market_positions": [], "cursor": ""}
            pytest.fail("Unexpected endpoint")
        client.request = request
        snapshot = await probe_native_account(client)
    assert snapshot.identity_verified and snapshot.reconciled
    assert str(snapshot.balance_usd) == "100.1234" and not snapshot.trading_permission_verified
    assert all(method == "GET" for method, _, _ in calls)
    assert all(kwargs["params"]["subaccount"] == 0 for _, path, kwargs in calls if path != "/api_keys")


async def test_kalshi_wrong_key_and_nonprimary_binding_not_accepted():
    async with httpx.AsyncClient() as http:
        client = KalshiClient(KalshiSettings(api_key_id=KEY), client=http)
        async def request(*args, **kwargs): return {"api_keys": [{"api_key_id": KEY, "subaccount": 4}]}
        client.request = request
        snapshot = await probe_native_account(client)
        assert not snapshot.identity_verified and not snapshot.reconciled


async def test_prophetx_signed_registration_identity_does_not_claim_inventory_ready():
    assert ProphetXClient.registration_user_id({"user_data": '{"id":"account"}'}) == "account"
    assert ProphetXClient.registration_user_id({"user_data": '{"user_id":"account"}'}) == "account"
    with pytest.raises(ValueError, match="Conflicting"):
        ProphetXClient.registration_user_id({"user_data": '{"id":"one","user_id":"two"}'})
    async with httpx.AsyncClient() as http:
        client = ProphetXClient(client=http)
        client._signin_confirmed = True
        client._authenticated_user_id = "account"
        async def request(method, path):
            assert method == "GET" and path == "/v4/mm/get_balance"
            return {"data": {"balance": "50.23"}}
        client.request = request
        snapshot = await probe_native_account(client)
    assert snapshot.identity_verified and str(snapshot.balance_usd) == "50.23"
    assert not snapshot.reconciled
