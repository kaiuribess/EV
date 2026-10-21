"""Offline checks for documented stream limits and server-reported entitlement."""
from __future__ import annotations

import asyncio
import json
from urllib.parse import parse_qs, urlsplit

import pytest
from pydantic import ValidationError
from websockets.exceptions import ConnectionClosed
from websockets.frames import Close

from evplatform.integrations.base import ProviderState
from evplatform.integrations.parlay import ParlayConfig, ParlayWebSocket


@pytest.mark.parametrize("limit", [0, 1001, 10000])
def test_stream_snapshot_limit_rejects_provider_refused_values(limit):
    with pytest.raises(ValidationError):
        ParlayConfig(stream_limit=limit)


@pytest.mark.parametrize("limit", [1, 1000])
def test_stream_snapshot_limit_preserves_supported_boundaries(limit):
    provider = ParlayWebSocket({"stream_limit": limit}, credential="")
    assert parse_qs(urlsplit(provider.stream_url("basketball_nba")).query)["limit"] == [str(limit)]


@pytest.mark.parametrize("ending", ["disconnect", "server_close"])
async def test_health_reports_actual_server_entitlement_only_for_open_socket(ending):
    close_socket = asyncio.Event()
    allow_snapshot = asyncio.Event()
    messages = [
        {"type": "connected", "tier": "scale", "push_mode": "raw", "min_push_interval_s": 0.0,
         "api_key": "fixture-secret", "note": "fixture-secret"},
        {"type": "initial_state", "data": []},
    ]

    class Socket:
        async def recv(self):
            if messages:
                if messages[0]["type"] == "initial_state":
                    await allow_snapshot.wait()
                return json.dumps(messages.pop(0))
            await close_socket.wait()
            raise ConnectionClosed(Close(4001, "Trial ended"), None)

        async def close(self):
            close_socket.set()

    class Connection:
        async def __aenter__(self):
            return Socket()

        async def __aexit__(self, *ignored):
            return False

    provider = ParlayWebSocket(
        {"enabled": True, "sports": ["basketball_nba"], "account_tier": "unknown"},
        credential="fixture-secret", ws_factory=lambda *args, **kwargs: Connection(),
    )
    updates = provider.stream_updates()
    try:
        # REST state or configured credentials do not establish a live WS session.
        provider.state = ProviderState.CONNECTED
        health = await provider.health_check()
        assert health["authenticated"] is False
        assert health["streaming"] is False
        assert (await anext(updates)).kind == "connected"
        health = await provider.health_check()
        assert health["authenticated"] is True
        assert health["streaming"] is False
        assert health["ready_sports"] == []
        assert health["awaiting_snapshot_sports"] == ["basketball_nba"]
        allow_snapshot.set()
        assert (await anext(updates)).kind == "initial_state"
        health = await provider.health_check()
        assert health["authenticated"] is True
        assert health["streaming"] is True
        assert health["ready_sports"] == ["basketball_nba"]
        assert health["awaiting_snapshot_sports"] == []
        assert health["configured_account_tier"] == "unknown"
        assert health["server_connections"] == {
            "basketball_nba": {"tier": "scale", "push_mode": "raw", "min_push_interval_s": 0.0},
        }
        assert "fixture-secret" not in json.dumps(health)
        health["server_connections"]["basketball_nba"]["tier"] = "free"
        assert (await provider.health_check())["server_connections"]["basketball_nba"]["tier"] == "scale"

        if ending == "disconnect":
            await provider.disconnect()
        else:
            close_socket.set()
            assert (await asyncio.wait_for(anext(updates), timeout=2)).kind == "feed_invalidated"
        health = await provider.health_check()
        assert health["server_connections"] == {}
        assert health["authenticated"] is False
        assert health["streaming"] is False
        assert health["ready_sports"] == []
        assert health["awaiting_snapshot_sports"] == []
    finally:
        await updates.aclose()
        await provider.disconnect()
