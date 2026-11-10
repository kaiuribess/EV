import httpx
import pytest
import hashlib
from decimal import Decimal
from types import SimpleNamespace

from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives import serialization

from evplatform.core.models import OrderStatus, utcnow

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


@pytest.mark.parametrize("scope,matching_fingerprint,verified", [
    ("trading", True, True), ("trading::read", True, False), ("trading", False, False),
])
async def test_novig_management_witness_verifies_actual_trading_signer(scope, matching_fingerprint, verified):
    private = ed25519.Ed25519PrivateKey.generate()
    der = private.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    fingerprint = "sha256:" + hashlib.sha256(der).hexdigest()
    trading = NovigClient(NovigSettings(api_key=KEY))
    management = NovigClient(NovigSettings(api_key="22222222-2222-4222-8222-222222222222"))
    trading._load_key = lambda: private
    async def trading_request(method, path):
        return fixtures()[path]
    async def management_request(method, path):
        assert (method, path) == ("GET", f"/v3/keys/{KEY}")
        return {"keyId": KEY, "scope": scope, "restriction": "subaccount",
                "expiresAt": None, "fingerprint": fingerprint if matching_fingerprint else "sha256:"+"0"*64}
    trading.request = trading_request
    management.request = management_request
    try:
        result = await probe_native_account(trading, management_client=management)
        assert result.identity_verified and result.reconciled
        assert result.trading_permission_verified is verified
        assert result.cash_available_verified is verified
        assert result.api_key_scopes == (("trading",) if verified else ())
    finally:
        await trading.disconnect()
        await management.disconnect()


async def test_novig_management_witness_must_match_environment():
    trading = NovigClient(NovigSettings(api_key=KEY, environment="sandbox"))
    management = NovigClient(NovigSettings(api_key="22222222-2222-4222-8222-222222222222",
                                            environment="production"))
    async def trading_request(method, path):
        return fixtures()[path]
    async def no_management_read(*_):
        pytest.fail("A production key must never witness a Paper trading key")
    trading.request = trading_request
    management.request = no_management_read
    try:
        result = await probe_native_account(trading, management_client=management)
        assert result.reconciled and not result.trading_permission_verified
        assert not result.cash_available_verified
    finally:
        await trading.disconnect()
        await management.disconnect()


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


async def test_exact_reviewed_external_position_reconciles_without_debiting_cash_twice():
    data = fixtures()
    data[f"/v3/account/subaccounts/{KEY}/balance"]["balance"] = "24.02825"
    data["/v3/account/positions"]["positions"] = [
        {"marketId": "market", "outcomeId": "outcome", "qty": 169, "cost": ".97175"}]
    external = SimpleNamespace(venue="novig", environment="sandbox",
        account_fingerprint=novig_wallet_fingerprint("sandbox", KEY),
        native_market_id="market", native_outcome_id="outcome",
        quantity=Decimal(169), cost_usd=Decimal(".97175"))
    async with httpx.AsyncClient() as http:
        client = NovigClient(NovigSettings(api_key=KEY), client=http)
        async def request(method, path):
            assert method == "GET"
            return data[path]
        client.request = request
        result = await probe_native_account(client, external_positions=(external,))
    assert result.reconciled and result.external_position_count == 1
    assert result.external_cost_usd == Decimal(".97175")
    assert result.balance_usd == Decimal("24.02825")
    data["/v3/account/positions"]["positions"][0]["cost"] = ".97176"
    async with httpx.AsyncClient() as http:
        client = NovigClient(NovigSettings(api_key=KEY), client=http)
        client.request = request
        changed = await probe_native_account(client, external_positions=(external,))
    assert not changed.reconciled


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
    assert snapshot.exchange_index is None and not snapshot.cash_available_verified


async def test_kalshi_scoped_index_cash_and_paginated_inventory_are_independent_of_aggregate():
    calls = []
    async with httpx.AsyncClient() as http:
        client = KalshiClient(KalshiSettings(api_key_id=KEY), client=http)
        async def request(method, path, **kwargs):
            assert method == "GET"
            calls.append((path, kwargs.get("params")))
            if path == "/api_keys":
                return {"api_keys": [{"api_key_id": KEY, "scopes": ["read", "write"]}]}
            if path == "/portfolio/balance":
                assert kwargs["params"] == {"subaccount": 0, "exchange_index": 3}
                return {"balance_dollars": "25.0000", "balance_breakdown": [
                    {"exchange_index": 3, "balance": "25.0000"}]}
            if path == "/portfolio/orders": return {"orders": [], "cursor": ""}
            if path == "/portfolio/positions": return {"market_positions": [], "cursor": ""}
            pytest.fail("Unexpected endpoint")
        client.request = request
        snapshot = await probe_native_account(client, exchange_index=3)
    assert snapshot.exchange_index == 3 and snapshot.balance_usd == Decimal("25")
    assert snapshot.reconciled and snapshot.cash_available_verified
    assert all(params["exchange_index"] == 3 for path, params in calls if path.startswith("/portfolio/"))


async def test_kalshi_scoped_balance_breakdown_mismatch_is_rejected():
    async with httpx.AsyncClient() as http:
        client = KalshiClient(KalshiSettings(api_key_id=KEY), client=http)
        async def request(method, path, **kwargs):
            if path == "/api_keys":
                return {"api_keys": [{"api_key_id": KEY, "scopes": ["read"]}]}
            if path == "/portfolio/balance":
                return {"balance_dollars": "25.0000", "balance_breakdown": [
                    {"exchange_index": 0, "balance": "25.0000"}]}
            pytest.fail("Inventory must not be consulted after balance conflict")
        client.request = request
        with pytest.raises(ReconciliationError, match="breakdown"):
            await probe_native_account(client, exchange_index=3)


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
        client.health_check = lambda: {"authenticated": True}
        async def request(method, path):
            assert method == "GET" and path == "/v4/mm/get_balance"
            return {"data": {"balance": "50.23", "gec_balance": "0",
                "matched_order_balance": "0", "unmatched_order_balance": "0",
                "unmatched_order_balance_status": "succeed",
                "unmatched_order_last_synced_at": utcnow().isoformat()}}
        client.request = request
        snapshot = await probe_native_account(client)
    assert snapshot.identity_verified and str(snapshot.balance_usd) == "50.23"
    assert not snapshot.reconciled and snapshot.cash_available_verified
