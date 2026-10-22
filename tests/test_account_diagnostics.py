"""Read-only account diagnostics use mocked HTTP, never real credentials."""
import importlib.util
import json
import hashlib
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives import serialization

from evplatform.core.models import utcnow
from evplatform.venues.account_probe import NativeAccountSnapshot
from evplatform.venues.common import VenueHTTPError
from evplatform.venues.novig import NovigClient


spec = importlib.util.spec_from_file_location("account_diagnostic_script", Path(__file__).parents[1] / "scripts/check_accounts.py")
diagnostic = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diagnostic)
KEY = "00000000-0000-4000-8000-000000000001"


async def test_kalshi_demo_diagnostic_never_uses_production_key(monkeypatch):
    monkeypatch.setattr(diagnostic, "build_client", lambda *args: pytest.fail("No fallback connection allowed"))
    result = await diagnostic.check_account("kalshi", "sandbox", environ={
        "KALSHI_API_KEY_ID": KEY, "KALSHI_PRIVATE_KEY_PATH": "production-only"})
    assert not result["credentials_present"] and not result["checks"]


async def test_kalshi_demo_diagnostic_selects_only_demo_pair(monkeypatch):
    selected = []
    def capture(venue, environment, values, http):
        selected.append((venue, environment, values))
        raise RuntimeError("Stop before network")
    monkeypatch.setattr(diagnostic, "build_client", capture)
    with pytest.raises(RuntimeError, match="Stop before network"):
        await diagnostic.check_account("kalshi", "sandbox", environ={
            "KALSHI_API_KEY_ID": "production", "KALSHI_PRIVATE_KEY_PATH": "production-path",
            "KALSHI_SANDBOX_API_KEY_ID": "demo", "KALSHI_SANDBOX_PRIVATE_KEY_PATH": "demo-path"})
    assert selected == [("kalshi", "sandbox", ["demo", "demo-path"])]


async def test_scoped_production_diagnostic_never_falls_back_to_legacy_pair(monkeypatch):
    monkeypatch.setattr(diagnostic, "build_client", lambda *args: pytest.fail("No fallback connection allowed"))
    result = await diagnostic.check_account("novig", novig_credential_profile="environment",
        environ={"NOVIG_API_KEY": KEY, "NOVIG_PRIVATE_KEY_PATH": "legacy-only",
                 "NOVIG_PRODUCTION_TRADING_API_KEY": KEY})
    assert not result["credentials_present"] and not result["checks"]


async def test_scoped_production_diagnostic_passes_only_matching_pair(monkeypatch):
    selected = []
    def capture(venue, environment, values, http):
        selected.append((venue, environment, values))
        raise RuntimeError("Stop before network")
    monkeypatch.setattr(diagnostic, "build_client", capture)
    with pytest.raises(RuntimeError, match="Stop before network"):
        await diagnostic.check_account("novig", novig_credential_profile="environment",
            environ={"NOVIG_API_KEY": "legacy", "NOVIG_PRIVATE_KEY_PATH": "legacy-path",
                "NOVIG_PRODUCTION_TRADING_API_KEY": "production", "NOVIG_PRODUCTION_TRADING_PRIVATE_KEY_PATH": "production-path"})
    assert selected == [("novig", "production", ["production", "production-path"])]


@pytest.mark.asyncio
async def test_production_failure_never_falls_back_to_sandbox_or_exposes_error_body(monkeypatch):
    monkeypatch.setattr(NovigClient, "headers", lambda *args, **kwargs: {})
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(401, json={"code": "secret-from-server", "private_key": "do-not-echo"})
    result = await diagnostic.check_account("novig", environ={"NOVIG_API_KEY": KEY, "NOVIG_PRIVATE_KEY_PATH": "secret-path"},
                                            transport=httpx.MockTransport(handler))
    assert len(requests) == 1
    assert requests[0].url.host == "api.novig.com" and requests[0].method == "GET"
    assert result["environment"] == "production" and result["money_type"] == "real"
    assert result["checks"]["balance_read"]["http_status"] == 401
    assert not result["live_readiness_verified"]
    assert all(value not in json.dumps(result) for value in (KEY, "secret-path", "do-not-echo", "secret-from-server"))


@pytest.mark.asyncio
async def test_sandbox_cash_and_empty_inventory_never_become_production_authorization(monkeypatch):
    monkeypatch.setattr(NovigClient, "headers", lambda *args, **kwargs: {})
    requests = []
    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/balance"):
            return httpx.Response(200, json={"keyId": KEY, "balance": "50.00"})
        if request.url.path.endswith("/orders"):
            return httpx.Response(200, json={"open": [], "seq": 2})
        return httpx.Response(200, json={"positions": [], "seq": 4})
    result = await diagnostic.check_account("novig", "sandbox",
        environ={"NOVIG_API_KEY": KEY, "NOVIG_PRIVATE_KEY_PATH": "never-read"}, transport=httpx.MockTransport(handler))
    assert all(r.url.host == "api.paper.novig.com" and r.method == "GET" for r in requests)
    assert result["money_type"] == "play"
    snapshot = result["checks"]["account_inventory"]
    assert snapshot["balance_usd"] == "50.00" and snapshot["reconciled"]
    assert not snapshot["trading_permission_verified"]
    assert not snapshot["live_readiness_verified"]
    assert KEY not in json.dumps(result)


@pytest.mark.asyncio
async def test_novig_production_management_witness_verifies_scoped_trading_signer(monkeypatch):
    trading_key = ed25519.Ed25519PrivateKey.generate()
    public = trading_key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    fingerprint = "sha256:" + hashlib.sha256(public).hexdigest()
    manager_id = "00000000-0000-4000-8000-000000000002"
    monkeypatch.setattr(NovigClient, "headers", lambda *args, **kwargs: {})
    monkeypatch.setattr(NovigClient, "_load_key", lambda self: trading_key)
    requests = []
    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/balance"):
            return httpx.Response(200, json={"keyId": KEY, "balance": "25.00000"})
        if request.url.path.endswith("/orders"):
            return httpx.Response(200, json={"open": [], "seq": 1})
        if request.url.path.endswith("/positions"):
            return httpx.Response(200, json={"positions": [], "seq": 1})
        assert request.url.path == f"/v3/keys/{KEY}"
        return httpx.Response(200, json={"keyId": KEY, "scope": "trading",
            "restriction": "subaccount", "expiresAt": None, "fingerprint": fingerprint})
    result = await diagnostic.check_account("novig", "production",
        novig_credential_profile="environment", environ={
            "NOVIG_PRODUCTION_TRADING_API_KEY": KEY,
            "NOVIG_PRODUCTION_TRADING_PRIVATE_KEY_PATH": "trading-secret-path",
            "NOVIG_PRODUCTION_MANAGEMENT_API_KEY": manager_id,
            "NOVIG_PRODUCTION_MANAGEMENT_PRIVATE_KEY_PATH": "management-secret-path",
            "NOVIG_MANAGEMENT_API_KEY": "paper-only",
            "NOVIG_MANAGEMENT_PRIVATE_KEY_PATH": "paper-secret-path",
        }, transport=httpx.MockTransport(handler))
    assert result["management_credentials_present"]
    inventory = result["checks"]["account_inventory"]
    assert inventory["identity_verified"] and inventory["reconciled"]
    assert inventory["trading_permission_verified"] and inventory["cash_available_verified"]
    assert not result["live_readiness_verified"]
    assert len([r for r in requests if r.url.path == f"/v3/keys/{KEY}"]) == 1
    assert all(r.method == "GET" and r.url.host == "api.novig.com" for r in requests)
    assert all(secret not in json.dumps(result) for secret in (
        KEY, manager_id, "trading-secret-path", "management-secret-path", "paper-only"))


@pytest.mark.asyncio
async def test_novig_missing_management_pair_does_not_use_wrong_environment(monkeypatch):
    monkeypatch.setattr(NovigClient, "headers", lambda *args, **kwargs: {})
    requests = []
    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/balance"):
            return httpx.Response(200, json={"keyId": KEY, "balance": "25.00000"})
        if request.url.path.endswith("/orders"):
            return httpx.Response(200, json={"open": [], "seq": 1})
        if request.url.path.endswith("/positions"):
            return httpx.Response(200, json={"positions": [], "seq": 1})
        pytest.fail("An unrelated management key was used")
    result = await diagnostic.check_account("novig", "production",
        novig_credential_profile="environment", environ={
            "NOVIG_PRODUCTION_TRADING_API_KEY": KEY,
            "NOVIG_PRODUCTION_TRADING_PRIVATE_KEY_PATH": "trading-secret-path",
            "NOVIG_MANAGEMENT_API_KEY": "paper-only",
            "NOVIG_MANAGEMENT_PRIVATE_KEY_PATH": "paper-secret-path",
        }, transport=httpx.MockTransport(handler))
    inventory = result["checks"]["account_inventory"]
    assert not result["management_credentials_present"]
    assert inventory["reconciled"] and not inventory["trading_permission_verified"]
    assert not inventory["cash_available_verified"]
    assert all(r.method == "GET" and r.url.host == "api.novig.com" for r in requests)


@pytest.mark.asyncio
@pytest.mark.parametrize("venue,path,method", [
    ("novig", "/v3/orders", "POST"), ("novig", "/v3/orders/native", "DELETE"),
    ("kalshi", "/trade-api/v2/portfolio/orders", "POST"),
    ("prophetx", "/partner/v4/mm/place_order", "POST"),
    ("polymarket_us", "/v1/order", "POST"),
    ("prophetx", "/partner/auth/login", "DELETE"),
])
async def test_transport_guard_blocks_writes_before_network(venue, path, method):
    hosts = {"novig": "api.novig.com", "kalshi": "external-api.kalshi.com",
             "prophetx": "cash.api.prophetx.co", "polymarket_us": "api.polymarket.us"}
    called = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: called.append(request)),
        event_hooks={"request": [diagnostic.read_guard(venue, "production")]}) as client:
        with pytest.raises(RuntimeError, match="mutations"):
            await client.request(method, "https://" + hosts[venue] + path)
    assert not called


@pytest.mark.asyncio
async def test_guard_allows_only_prophet_auth_and_same_host_reads():
    guard = diagnostic.read_guard("prophetx", "production")
    await guard(httpx.Request("POST", "https://cash.api.prophetx.co/partner/auth/login"))
    await guard(httpx.Request("GET", "https://cash.api.prophetx.co/partner/v4/mm/get_balance"))
    for url in ("http://cash.api.prophetx.co/partner/v4/mm/get_balance", "https://example.com/partner/auth/login"):
        with pytest.raises(RuntimeError):
            await guard(httpx.Request("GET", url))


@pytest.mark.asyncio
async def test_missing_credentials_do_not_attempt_network(monkeypatch):
    monkeypatch.setattr(diagnostic, "build_client", lambda *args: pytest.fail("Built an unauthenticated client"))
    result = await diagnostic.check_account("novig", environ={})
    assert not result["credentials_present"] and result["checks"] == {}


def test_projection_never_copies_unknown_fields_or_pending_withdrawal_details():
    raw = {"balances": [{"currency": "USD", "currentBalance": Decimal("50"), "buyingPower": Decimal("50"),
        "balanceReservation": Decimal("50"), "api_key": "secret", "pendingWithdrawals": [{"bankId": "secret"}]}]}
    result = diagnostic.balance_summary("polymarket_us", raw)
    assert result == {"reported_balance_usd": "50", "reported_buying_power_usd": "50",
        "balance_reservation_usd": "50", "source_clock_status": "missing"}
    assert "secret" not in json.dumps(result)


@pytest.mark.parametrize("value", [True, 1.25, "NaN", "Infinity", "-1", None])
def test_malformed_money_is_never_reported_as_valid_cash(value):
    with pytest.raises((ValueError, ArithmeticError)):
        diagnostic.money(value)


def test_missing_and_future_clocks_are_not_relabelled_current():
    assert diagnostic.clock_summary(None)["source_clock_status"] == "missing"
    assert diagnostic.clock_summary("bad-private-response")["source_clock_status"] == "invalid"
    assert diagnostic.clock_summary((utcnow()+timedelta(seconds=60)).isoformat())["source_clock_status"] == "future"


def test_error_projection_omits_messages_and_unknown_server_codes():
    result = diagnostic.error_summary(VenueHTTPError("novig", 401, "sensitive-error-text"))
    assert result == {"ok": False, "error_type": "VenueHTTPError", "http_status": 401}


def test_snapshot_does_not_echo_unrecognized_scope_or_assert_readiness():
    snapshot = NativeAccountSnapshot(venue="kalshi", environment="production", observed_at=utcnow(),
        identity_verified=True, cash_available_verified=True, reconciled=True,
        api_key_scopes=("read", "write::trade", "malformed-secret-scope"), reason="arbitrary-data")
    summary = diagnostic.snapshot_summary(snapshot)
    assert summary["api_key_scopes"] == ["read", "write::trade"]
    assert not summary["live_readiness_verified"]
    assert "malformed-secret-scope" not in json.dumps(summary) and "arbitrary-data" not in json.dumps(summary)
