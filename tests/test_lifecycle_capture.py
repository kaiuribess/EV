from decimal import Decimal
import sqlite3

import httpx
import pytest

from evplatform.core.lifecycle_capture import _pages, configured_client, local_oms_snapshot, read_guard, sanitize


@pytest.mark.parametrize("venue,url,method", [
    ("kalshi", "https://external-api.kalshi.com/trade-api/v2/portfolio/orders", "POST"),
    ("kalshi", "https://external-api.kalshi.com/trade-api/v2/portfolio/orders/123", "DELETE"),
    ("novig", "https://api.novig.com/v3/orders", "POST"),
    ("novig", "https://api.novig.com/v3/account/subaccounts/abc/fund", "POST"),
    ("prophetx", "https://cash.api.prophetx.co/partner/v4/mm/submit_order", "POST"),
    ("prophetx", "https://cash.api.prophetx.co/partner/v4/mm/cancel_order", "GET"),
    ("kalshi", "https://evil.example/trade-api/v2/portfolio/orders", "GET"),
    ("novig", "http://api.novig.com/v3/orders", "GET"),
    ("prophetx", "https://cash.api.prophetx.co/partner/auth/login/extra", "POST"),
    ("kalshi", "https://external-api.kalshi.com:444/trade-api/v2/portfolio/orders", "GET"),
])
async def test_capture_rejects_mutations_redirect_hosts_and_unknown_reads(venue, url, method):
    with pytest.raises(ValueError, match="non-allowlisted"):
        await read_guard(venue)(httpx.Request(method, url))


@pytest.mark.parametrize("venue,url,method", [
    ("kalshi", "https://external-api.kalshi.com/trade-api/v2/portfolio/settlements", "GET"),
    ("novig", "https://api.paper.novig.com/v3/echo", "POST"),
    ("novig", "https://api.novig.com/v3/account/subaccounts/abcdef-123/balance", "GET"),
    ("prophetx", "https://cash.api.prophetx.co/partner/auth/login", "POST"),
    ("prophetx", "https://cash.api.prophetx.co/partner/v4/mm/websocket", "POST"),
])
async def test_only_documented_read_auth_subscription_boundary_allowed(venue, url, method):
    await read_guard(venue)(httpx.Request(method, url))


class Pages:
    def __init__(self, pages):
        self.pages = iter(pages)
        self.calls = []

    async def request(self, method, path, *, params):
        self.calls.append((method, path, params))
        return next(self.pages)


async def test_pages_preserve_exact_money_and_follow_documented_cursor():
    client = Pages([{"items": [{"cost": Decimal(".12345")}], "next": "second"}, {"items": []}])
    result = await _pages(client, "/v3/portfolio/fills", "items", cursor_key="next", cursor_param="after")
    assert result["complete"] and result["rows"] == [{"cost": "0.12345"}]
    assert client.calls[1][2]["after"] == "second"
    assert all(call[0] == "GET" for call in client.calls)


@pytest.mark.parametrize("pages,error", [
    ([{"items": [], "next": None}], "invalid_history_cursor"),
    ([{"items": [], "next": "x"}, {"items": [], "next": "x"}], "invalid_history_cursor"),
    ([{"items": [], "next": "x"}, {"items": [], "next": "y"}], "history_page_bound"),
    ([{"wrong": []}], "unexpected_history_shape"),
    ([{"items": [None]}], "unexpected_history_shape"),
])
async def test_missing_malformed_looping_or_truncated_history_never_complete(pages, error):
    result = await _pages(Pages(pages), "/v3/orders", "items", cursor_key="next", cursor_param="after", max_pages=2)
    assert not result["complete"] and result["error"] == error


async def test_kalshi_missing_cursor_is_not_exhaustion():
    result = await _pages(Pages([{"orders": []}]), "/portfolio/orders", "orders",
                          cursor_key="cursor", cursor_param="cursor", missing_cursor_ends=False)
    assert not result["complete"]


def test_auth_material_redacted_but_order_linkage_retained():
    raw = {"api_key_id": "private", "data": [{"access_token": "private", "auth": "private", "clientId": "order-link"}]}
    assert sanitize(raw) == {"api_key_id": "[redacted]", "data": [{"access_token": "[redacted]", "auth": "[redacted]", "clientId": "order-link"}]}


async def test_credentials_do_not_cross_environments():
    values = {"KALSHI_API_KEY_ID": "production", "KALSHI_PRIVATE_KEY_PATH": "production.pem",
              "NOVIG_API_KEY": "management", "NOVIG_PRODUCTION_TRADING_API_KEY": "prod-trading"}
    async with httpx.AsyncClient() as http:
        sandbox = configured_client("kalshi", "sandbox", values, http)
        assert not sandbox.settings.api_key_id
        sandbox_novig = configured_client("novig", "sandbox", values, http)
        assert not sandbox_novig.settings.api_key
        production = configured_client("novig", "production", values, http)
        assert production.settings.api_key == "prod-trading"
        assert not production.settings.gate.enabled


def test_sqlite_capture_is_read_only_and_missing_tables_are_missing(tmp_path):
    path = tmp_path / "native.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE native_orders (id TEXT, data TEXT)")
        db.execute("INSERT INTO native_orders VALUES ('local-id','{}')")
    before = path.read_bytes()
    found = local_oms_snapshot(path)
    assert found["tables"]["native_orders"]["rows"] == [{"id": "local-id", "data": "{}"}]
    assert not found["tables"]["native_settlements"]["available"]
    assert path.read_bytes() == before
    assert not local_oms_snapshot(tmp_path / "absent.db")["available"]
