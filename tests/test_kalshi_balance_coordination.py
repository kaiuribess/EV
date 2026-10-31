"""Offline checks for Kalshi's separate, non-burst balance bucket."""
import asyncio
import time

import httpx
import pytest

from evplatform.venues.common import VenueError, VenueHTTPError
from evplatform.venues.kalshi import KalshiClient, KalshiSettings


def client(transport):
    value = KalshiClient(KalshiSettings(api_key_id="fixture-key", environment="production"),
                         httpx.AsyncClient(transport=httpx.MockTransport(transport)))
    value.headers = lambda *args, **kwargs: {}
    return value


async def test_balance_readers_share_one_local_nonburst_bucket(tmp_path, monkeypatch):
    monkeypatch.setattr(KalshiClient, "_balance_gate_directory", staticmethod(lambda: tmp_path))
    sent = []

    def response(request):
        assert request.url.path.endswith("/portfolio/balance")
        sent.append(time.monotonic())
        return httpx.Response(200, json={"balance_dollars": "1.00"})

    first, second = client(response), client(response)
    try:
        rows = await asyncio.gather(first.request("GET", "/portfolio/balance"),
                                    second.request("GET", "/portfolio/balance"))
        assert len(rows) == 2 and len(sent) == 2
        assert sent[1] - sent[0] >= .95
    finally:
        await first.disconnect()
        await second.disconnect()


async def test_balance_429_sets_shared_cooldown_without_retry_or_false_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(KalshiClient, "_balance_gate_directory", staticmethod(lambda: tmp_path))
    sent = []

    def response(request):
        sent.append(request.url.path)
        return httpx.Response(429, headers={"Retry-After": "30"}, json={"error": "limited"})

    first, second = client(response), client(response)
    try:
        with pytest.raises(VenueHTTPError) as error:
            await first.request("GET", "/portfolio/balance")
        assert error.value.status_code == 429
        with pytest.raises(VenueError, match="balance cooldown active"):
            await second.request("GET", "/portfolio/balance")
        assert len(sent) == 1
    finally:
        await first.disconnect()
        await second.disconnect()
