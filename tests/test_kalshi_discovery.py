"""Kalshi public series discovery rotates book scope without placing orders."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from evplatform.integrations.kalshi_discovery import balanced_open_markets, current_open_markets
from evplatform.plugins.external import KalshiMonitorConfig, NativeMonitor
from evplatform.venues.kalshi import KalshiClient, KalshiSettings


NOW = datetime(2026, 10, 22, 4, tzinfo=timezone.utc)


def row(series, suffix, starts, *, status="active", market_type="binary"):
    return {"ticker": f"{series}-{suffix}", "event_ticker": f"{series}-EVENT",
            "status": status, "market_type": market_type,
            "occurrence_datetime": starts.isoformat()}


def test_current_binary_books_balanced_without_claiming_mapping():
    nba = {"markets": [row("KXNBAGAME", "OLD", NOW - timedelta(days=1)),
                       row("KXNBAGAME", "NEXT", NOW + timedelta(hours=1)),
                       row("KXNBAGAME", "LATER", NOW + timedelta(hours=3)),
                       row("KXNBAGAME", "CLOSED", NOW, status="closed")], "cursor": ""}
    nhl = {"markets": [row("KXNHLGAME", "NEXT", NOW + timedelta(hours=2))], "cursor": ""}
    selected, truncated = balanced_open_markets([nba, nhl],
        series=["KXNBAGAME", "KXNHLGAME"], now=NOW, max_markets=2)
    assert selected == ["KXNBAGAME-NEXT", "KXNHLGAME-NEXT"] and truncated
    assert current_open_markets(nba, series="KXNBAGAME", now=NOW,
                                max_markets=8)[0][0][1] == "KXNBAGAME-NEXT"


@pytest.mark.parametrize("payload", [
    {}, {"markets": [], "cursor": 3},
    {"markets": [row("KXNBAGAME", "A", NOW)] * 2, "cursor": ""},
    {"markets": [row("KXNHLGAME", "A", NOW)], "cursor": ""},
    {"markets": [row("KXNBAGAME", "A", NOW.replace(tzinfo=None))], "cursor": ""},
])
def test_invalid_catalog_is_rejected(payload):
    with pytest.raises(ValueError):
        current_open_markets(payload, series="KXNBAGAME", now=NOW, max_markets=8)


def test_config_is_bounded_and_backward_compatible():
    assert KalshiMonitorConfig().discovery_series == []
    assert KalshiMonitorConfig(discovery_series=["KXNBAGAME"]).max_discovered_markets == 8
    for config in ({"discovery_series": ["kxnbagame"]},
                   {"discovery_series": ["KXNBAGAME", "KXNBAGAME"]},
                   {"discovery_series": ["KXNBAGAME"], "market_ids": ["X"],
                    "max_discovered_markets": 100}):
        with pytest.raises(ValidationError):
            KalshiMonitorConfig.model_validate(config)


@pytest.mark.asyncio
async def test_public_catalog_request_needs_no_secret_and_is_scoped():
    def handle(request):
        assert request.url.host == "external-api.kalshi.com"
        assert "kalshi-access-key" not in request.headers
        assert request.url.params["series_ticker"] == "KXNBAGAME"
        assert request.url.params["status"] == "open"
        return httpx.Response(200, json={"markets": [row("KXNBAGAME", "NEXT", NOW)], "cursor": ""})

    client = KalshiClient(KalshiSettings(environment="production"),
                          client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    payload = await client.discover_markets(series_ticker="KXNBAGAME",
        status="open", limit=1000, mve_filter="exclude")
    assert payload["markets"][0]["ticker"] == "KXNBAGAME-NEXT"
    assert not client.authenticated
    await client.disconnect()


@pytest.mark.asyncio
async def test_monitor_keeps_previous_scope_on_catalog_failure(monkeypatch):
    monkeypatch.setattr("evplatform.plugins.external.utcnow", lambda: NOW)

    class Client:
        fail = False

        async def discover_markets(self, **params):
            if self.fail:
                raise RuntimeError("provider unavailable")
            series = params["series_ticker"]
            return {"markets": [row(series, "NEXT", NOW + timedelta(hours=1))], "cursor": ""}

    client = Client()
    monitor = SimpleNamespace(client=client, context=SimpleNamespace(config={
        "discovery_series": ["KXNBAGAME", "KXNHLGAME"], "max_discovered_markets": 2}),
        discovery_status="disabled", discovered_market_ids=[])
    await NativeMonitor._refresh_kalshi_discovery(monitor)
    assert monitor.discovered_market_ids == ["KXNBAGAME-NEXT", "KXNHLGAME-NEXT"]
    client.fail = True
    await NativeMonitor._refresh_kalshi_discovery(monitor)
    assert monitor.discovery_status == "read_failed"
    assert monitor.discovered_market_ids == ["KXNBAGAME-NEXT", "KXNHLGAME-NEXT"]


@pytest.mark.asyncio
async def test_rotation_closes_existing_socket_for_new_snapshots():
    class Socket:
        closes = 0

        async def close(self):
            self.closes += 1

    client = KalshiClient()
    client._ws = Socket()
    with pytest.raises(ValueError):
        await client.replace_monitor_markets(["../bad"])
    assert client._ws.closes == 0
    await client.replace_monitor_markets(["KXNBAGAME-NEXT"])
    assert client._ws.closes == 1 and client.tickers == ["KXNBAGAME-NEXT"]
    assert client.request_count == 0
    client._ws = None
    await client.disconnect()


@pytest.mark.asyncio
async def test_periodic_rotation_only_when_scope_changes(monkeypatch):
    cycles = 0

    async def one_cycle(_):
        nonlocal cycles
        cycles += 1
        if cycles > 1:
            raise asyncio.CancelledError

    monkeypatch.setattr("evplatform.plugins.external.asyncio.sleep", one_cycle)

    class Client:
        selections = []

        async def replace_monitor_markets(self, ids):
            self.selections.append(ids)

    async def refresh():
        monitor.discovered_market_ids = ["KXNBAGAME-NEXT"]
        monitor.discovery_status = "current"

    monitor = SimpleNamespace(client=Client(), context=SimpleNamespace(config={
        "discovery_interval_seconds": 60, "market_ids": []}),
        _refresh_kalshi_discovery=refresh, discovery_status="current",
        discovered_market_ids=["KXNBAGAME-OLD"], _subscribed_market_ids=["KXNBAGAME-OLD"])
    with pytest.raises(asyncio.CancelledError):
        await NativeMonitor._discover_kalshi_periodically(monitor)
    assert monitor.client.selections == [["KXNBAGAME-NEXT"]]
