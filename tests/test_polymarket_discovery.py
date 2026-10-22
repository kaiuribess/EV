"""Current public US sports discovery only changes read-only monitor scope."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from evplatform.integrations.polymarket_us_discovery import balanced_open_moneylines, current_open_moneylines
from evplatform.plugins.external import NativeMonitor
from evplatform.venues.polymarket_us import PolymarketUSClient, PolymarketUSMonitorConfig


NOW = datetime(2026, 10, 22, 4, tzinfo=timezone.utc)


def page(league, rows, *, bounded=False):
    return {"league": {"slug": league}, "events": rows, "bounded": bounded}


def event(slug, starts, *, status="MARKET_STATUS_OPEN", market_type="SPORTS_MARKET_TYPE_MONEYLINE"):
    return {"slug": slug, "active": True, "closed": False, "markets": [{
        "slug": f"aec-{slug}", "sportsMarketTypeV2": market_type,
        "status": status, "active": True, "closed": False, "hidden": False,
        "gameStartTime": starts.isoformat()}]}


def test_open_moneylines_are_bounded_balanced_and_not_futures():
    nba = [page("nba", [event("nba-old", NOW - timedelta(days=1)),
                        event("nba-next", NOW + timedelta(hours=2)),
                        event("nba-later", NOW + timedelta(hours=6)),
                        event("nba-future", NOW + timedelta(hours=1),
                              market_type="SPORTS_MARKET_TYPE_FUTURE")])]
    nhl = [page("nhl", [event("nhl-next", NOW + timedelta(hours=3)),
                        event("nhl-halted", NOW + timedelta(hours=1), status="MARKET_STATUS_HALTED")])]
    assert current_open_moneylines(nba, league="nba", now=NOW, max_markets=8)[0] == [
        (NOW + timedelta(hours=2), "aec-nba-next"),
        (NOW + timedelta(hours=6), "aec-nba-later")]
    selected, truncated = balanced_open_moneylines([nba, nhl], leagues=["nba", "nhl"],
                                                    now=NOW, max_markets=2)
    assert selected == ["aec-nba-next", "aec-nhl-next"] and truncated


@pytest.mark.parametrize("bad", [
    page("nfl", [event("nba-next", NOW)]),
    page("nba", [event("same", NOW), event("same", NOW)]),
    page("nba", [{"slug": "missing", "active": True, "closed": False}]),
    page("nba", [event("naive-clock", NOW.replace(tzinfo=None))]),
])
def test_uncertain_catalog_is_rejected(bad):
    with pytest.raises(ValueError):
        current_open_moneylines([bad], league="nba", now=NOW, max_markets=8)


def test_config_requires_bounded_distinct_leagues_and_scope():
    assert PolymarketUSMonitorConfig().discovery_leagues == []
    assert PolymarketUSMonitorConfig(discovery_leagues=["nba", "nhl"]).max_discovered_markets == 8
    for value in ({"discovery_leagues": ["NBA"]}, {"discovery_leagues": ["nba", "nba"]},
                  {"discovery_leagues": ["nba"], "market_slugs": ["x"],
                   "max_discovered_markets": 50}):
        with pytest.raises(ValidationError):
            PolymarketUSMonitorConfig.model_validate(value)


@pytest.mark.asyncio
async def test_public_league_discovery_is_unsigned_and_bounded():
    calls = []

    def handle(request):
        calls.append(request)
        assert request.url.host == "gateway.polymarket.us"
        assert "x-pm-access-key" not in request.headers
        assert request.url.path == "/v2/leagues/nba/events"
        assert request.url.params["type"] == "sport"
        offset = int(request.url.params["offset"])
        rows = [event(f"nba-{offset}", NOW)] if offset == 0 else []
        return httpx.Response(200, json=page("nba", rows))

    client = PolymarketUSClient(client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    pages = await client.discover_league_events("nba", limit=1, max_pages=2)
    assert len(pages) == len(calls) == 2
    assert pages[-1]["bounded"] is False
    assert client.market_slugs == [] and client.request_count == 2
    with pytest.raises(ValueError):
        await client.discover_league_events("../keys")
    await client.disconnect()


@pytest.mark.asyncio
async def test_monitor_keeps_previous_scope_on_discovery_failure(monkeypatch):
    monkeypatch.setattr("evplatform.plugins.external.utcnow", lambda: NOW)

    class Client:
        fail = False

        async def discover_league_events(self, league):
            if self.fail:
                raise RuntimeError("provider unavailable")
            return [page(league, [event(f"{league}-next", NOW + timedelta(hours=2))])]

    client = Client()
    monitor = SimpleNamespace(client=client, context=SimpleNamespace(config={
        "discovery_leagues": ["nba", "nhl"], "max_discovered_markets": 2}),
        discovery_status="disabled", discovered_market_ids=[])
    await NativeMonitor._refresh_polymarket_discovery(monitor)
    assert monitor.discovered_market_ids == ["aec-nba-next", "aec-nhl-next"]
    client.fail = True
    await NativeMonitor._refresh_polymarket_discovery(monitor)
    assert monitor.discovered_market_ids == ["aec-nba-next", "aec-nhl-next"]
    assert monitor.discovery_status == "read_failed"


@pytest.mark.asyncio
async def test_rotation_restarts_read_only_stream_with_new_baseline(monkeypatch):
    cycles = 0

    async def one_cycle(_):
        nonlocal cycles
        cycles += 1
        if cycles > 1:
            raise asyncio.CancelledError

    monkeypatch.setattr("evplatform.plugins.external.asyncio.sleep", one_cycle)

    class Client:
        configured = []

        async def configure_subscriptions(self, config):
            self.configured.append(config["market_slugs"])

    async def run():
        await asyncio.Event().wait()

    async def refresh():
        monitor.discovered_market_ids = ["aec-nhl-next"]
        monitor.discovery_status = "current"

    monitor = SimpleNamespace(client=Client(), context=SimpleNamespace(config={
        "environment": "production", "market_slugs": [], "private_updates": True,
        "discovery_leagues": ["nhl"], "max_discovered_markets": 8,
        "discovery_interval_seconds": 60}), discovery_status="current",
        discovered_market_ids=["aec-nba-old"], _subscribed_market_ids=["aec-nba-old"],
        _refresh_polymarket_discovery=refresh, _run=run, task=asyncio.create_task(run()))
    try:
        with pytest.raises(asyncio.CancelledError):
            await NativeMonitor._discover_polymarket_periodically(monitor)
        assert monitor.client.configured == [["aec-nhl-next"]]
        assert monitor._subscribed_market_ids == ["aec-nhl-next"]
        assert not monitor.task.done()
    finally:
        monitor.task.cancel()
        await asyncio.gather(monitor.task, return_exceptions=True)
