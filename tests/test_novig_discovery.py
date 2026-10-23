"""Read-only Novig discovery rotates native book scope without order access."""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from decimal import Decimal

import pytest

from evplatform.integrations.novig_discovery import balanced_open_market_ids, open_market_ids
from evplatform.plugins.external import NativeMonitor, NovigMonitorConfig
from evplatform.venues.novig import NovigBook, NovigClient
from evplatform.venues.common import SequenceGap
from evplatform.core.models import Market, Settlement


NOW = datetime(2026, 10, 22, 3, tzinfo=timezone.utc)
EVENT = "11111111-1111-4111-8111-111111111111"
LIVE = "22222222-2222-4222-8222-222222222222"
NEXT = "33333333-3333-4333-8333-333333333333"
CLOSED = "44444444-4444-4444-8444-444444444444"


def market(market_id, minutes, status="OPEN", event_id=EVENT):
    return {"marketId": market_id, "eventId": event_id,
            "startsTs": int(NOW.timestamp() * 1000) + minutes * 60_000,
            "status": status}


def test_catalog_selects_current_and_nearby_open_markets_without_trading_claim():
    rows = {"items": [market(NEXT, 40), market(LIVE, -10), market(CLOSED, 1, "CLOSED")]}
    assert open_market_ids(rows, now=NOW, max_markets=10) == ([LIVE, NEXT], False)
    assert open_market_ids(rows, now=NOW, max_markets=1) == ([LIVE], True)
    assert open_market_ids({**rows, "next": "opaque"}, now=NOW, max_markets=10) == ([LIVE, NEXT], True)


def test_selection_reserves_coverage_across_requested_leagues():
    nba = {"items": [market(LIVE, -10), market(CLOSED, 20)]}
    nfl = {"items": [market(NEXT, 40)]}
    assert balanced_open_market_ids([nba, nfl], now=NOW, max_markets=2) == ([LIVE, NEXT], True)
    assert balanced_open_market_ids([nba, nfl], now=NOW, max_markets=3) == ([LIVE, NEXT, CLOSED], False)


@pytest.mark.parametrize("payload", [
    {}, {"items": None}, {"items": [market(LIVE, 1), market(LIVE, 2)]},
    {"items": [market("bad", 1)]}, {"items": [market(LIVE, 1)], "next": 17},
])
def test_uncertain_catalog_is_rejected_without_guessing(payload):
    with pytest.raises(ValueError):
        open_market_ids(payload, now=NOW, max_markets=10)


def test_discovery_config_is_bounded_and_backward_compatible():
    assert NovigMonitorConfig().discovery_leagues == []
    assert NovigMonitorConfig(discovery_leagues=["NBA"],
                              discovery_market_types=["MONEY"]).max_discovered_markets == 50
    for config in (
        {"discovery_leagues": ["NBA"]},
        {"discovery_market_types": ["MONEY"]},
        {"discovery_leagues": ["nba"], "discovery_market_types": ["MONEY"]},
        {"discovery_leagues": ["NBA", "NBA"], "discovery_market_types": ["MONEY"]},
        {"discovery_leagues": ["NBA"], "discovery_market_types": ["MONEY"],
         "market_ids": [LIVE] * 51},
    ):
        with pytest.raises(ValueError):
            NovigMonitorConfig.model_validate(config)


@pytest.mark.asyncio
async def test_monitor_read_failure_retains_last_discovered_scope(monkeypatch):
    monkeypatch.setattr("evplatform.plugins.external.utcnow", lambda: NOW)

    class Client:
        fail = False

        async def discover_markets(self, **params):
            assert params == {"league": "NBA" if self.calls % 2 == 0 else "NFL",
                              "marketType": "MONEY", "limit": 500}
            self.calls += 1
            if self.fail:
                raise RuntimeError("secret provider response")
            return {"items": [market(NEXT if params["league"] == "NFL" else LIVE,
                                     40 if params["league"] == "NFL" else -10)]}

    client = Client()
    client.calls = 0
    async def no_research_matches():
        return []
    monitor = SimpleNamespace(client=client, context=SimpleNamespace(config={
        "discovery_leagues": ["NBA", "NFL"], "discovery_market_types": ["MONEY"],
        "max_discovered_markets": 1}), discovery_status="disabled", discovered_market_ids=[],
        _refresh_research_matches=no_research_matches)
    await NativeMonitor._refresh_novig_discovery(monitor)
    assert monitor.discovered_market_ids == [LIVE] and monitor.discovery_status == "truncated"
    client.fail = True
    await NativeMonitor._refresh_novig_discovery(monitor)
    assert monitor.discovered_market_ids == [LIVE] and monitor.discovery_status == "read_failed"


@pytest.mark.asyncio
async def test_market_rotation_uses_reconnect_snapshot_only_after_success(monkeypatch):
    cycles = 0

    async def one_cycle(_):
        nonlocal cycles
        cycles += 1
        if cycles > 12:
            raise asyncio.CancelledError

    monkeypatch.setattr("evplatform.plugins.external.asyncio.sleep", one_cycle)

    class Client:
        def __init__(self):
            self.selections = []

        async def replace_monitor_markets(self, ids):
            self.selections.append(ids)

    client = Client()

    async def refresh():
        monitor.discovered_market_ids = [NEXT]
        monitor.discovery_status = "current"

    async def no_research_matches():
        return []

    monitor = SimpleNamespace(client=client, context=SimpleNamespace(config={
        "discovery_interval_seconds": 60, "market_ids": [LIVE]}),
        discovered_market_ids=[LIVE], _subscribed_market_ids=[LIVE],
        discovery_status="current", _refresh_novig_discovery=refresh,
        _refresh_research_matches=no_research_matches, research_matches={})
    with pytest.raises(asyncio.CancelledError):
        await NativeMonitor._discover_novig_periodically(monitor)
    assert client.selections == [[LIVE, NEXT]]
    assert monitor._subscribed_market_ids == [LIVE, NEXT]


@pytest.mark.asyncio
async def test_client_rotation_never_sends_an_order_and_validates_before_closing():
    class Socket:
        closes = 0

        async def close(self):
            self.closes += 1

    client = NovigClient()
    socket = Socket()
    client._ws = socket
    with pytest.raises(ValueError):
        await client.replace_monitor_markets(["invalid-id"])
    assert socket.closes == 0
    await client.replace_monitor_markets([LIVE])
    assert socket.closes == 1 and client._configured_market_ids == (LIVE,)
    assert client.request_count == 0
    client._ws = None
    await client.disconnect()


def test_gap_health_identifies_channel_without_exposing_market_or_account_id():
    client = NovigClient()
    client._record_sequence_gap(SequenceGap(f"{LIVE}:book", 12, 14))
    client._record_sequence_gap(SequenceGap("PRIVATE:orders", 8, 10))
    health = client.health_check()
    assert health["sequence_gap_counts"] == {"book": 1, "private_orders": 1}
    assert health["last_sequence_gap_channel"] == "private_orders"
    assert health["last_sequence_gap_kind"] == "skipped_sequence"
    assert LIVE not in str(health)
    client._record_sequence_gap(SequenceGap(f"{LIVE}:book", None, 15))
    assert client.health_check()["last_sequence_gap_kind"] == "missing_book_order"


@pytest.mark.asyncio
@pytest.mark.parametrize("already_fair", [False, True])
async def test_incomplete_nfl_prop_prewarms_only_exact_native_research_scope(already_fair):
    start = datetime(2026, 10, 25, 17, tzinfo=timezone.utc)
    source = Market(id="research:parlay:nfl-prop", event_id="research:parlay:old-event",
        event_name="Indianapolis Colts at Minnesota Vikings", sport="football", league="NFL",
        participants=("Minnesota Vikings", "Indianapolis Colts"), start_time=start,
        market_type="player_prop", player="Aaron Jones", player_id="Aaron Jones",
        statistic="receptions", line=Decimal("2.5"), outcomes=("over", "under"),
        settlement=Settlement(period="FULL"), extensions={
            "research_only": True, "source_market_key": "player_receptions"})
    native = {"marketId": LIVE, "eventId": EVENT, "marketType": "RECEPTIONS",
              "status": "OPEN", "voids": "FMV", "strike": "2.5",
              "startsTs": int(start.timestamp()*1000),
              "description": "Aaron Jones 2.5 RECEPTIONS",
              "outcomes": [{"outcomeId": NEXT, "name": "Over 2.5", "status": "TBD"},
                           {"outcomeId": CLOSED, "name": "Under 2.5", "status": "TBD"}]}
    event = {"eventId": EVENT, "league": "NFL", "status": "OPEN_PREGAME",
             "startsTs": native["startsTs"],
             "description": "Indianapolis Colts @ Minnesota Vikings"}
    calls = []

    async def request(method, path):
        calls.append((method, path))
        return event

    monitor = SimpleNamespace(manifest=SimpleNamespace(name="novig"),
        engine=SimpleNamespace(markets={source.id: source}, quotes={source.id: {
            "f": SimpleNamespace(provider="parlay_rest", bookmaker="fanduel", status="unknown",
                verification_reason="truncated_response", decimal_odds={"over": 2, "under": 2},
                source_observed_at=None, observation_verified=False, source_stored_at=NOW),
            "d": SimpleNamespace(provider="parlay_rest", bookmaker="draftkings", status="unknown",
                verification_reason="truncated_response", decimal_odds={"over": 2, "under": 2},
                source_observed_at=None, observation_verified=False, source_stored_at=NOW)}},
            reference_estimates={}, consensus_estimates=(
                {source.id: {"independent_book_count": 2}} if already_fair else {})),
        context=SimpleNamespace(config={"discovery_leagues": ["NFL"], "max_discovered_markets": 16}),
        client=SimpleNamespace(request=request), research_catalog_at=NOW,
        research_catalog_rows=[native], research_events={}, research_fee_metadata={},
        research_matches={}, focused_prop_matches={}, focused_prop_source_ids=[],
        _last_research_source_ids=frozenset())
    priority = await NativeMonitor._refresh_research_matches(monitor)
    assert priority == ([LIVE] if already_fair else [])
    assert bool(monitor.research_matches) is already_fair
    assert monitor.focused_prop_source_ids == [source.id]
    assert {item.native_market_id for item in monitor.focused_prop_matches[source.id]} == {LIVE}
    assert calls == [("GET", f"/v3/catalog/events/{EVENT}")]


def test_missing_remove_diagnostic_distinguishes_zero_update_from_unseen_order():
    book = NovigBook()
    book.apply({"snapshot": {LIVE: {"lifecycle": {"seq": 1, "status": "OPEN"},
        "book": {"seq": 1, "orders": {NEXT: [{"orderId": "resting-order",
            "price": "0.4", "qty": 3}]}}}}})
    book.apply({"delta": {LIVE: {"book": {"seq": 2, "deltas": [
        {"kind": "update", "orderId": "resting-order", "remaining": 0}]}}}})
    with pytest.raises(SequenceGap):
        book.apply({"delta": {LIVE: {"book": {"seq": 3, "deltas": [
            {"kind": "remove", "orderId": "resting-order"}]}}}})
    assert book.last_gap_context == {"prior_zero_update": True,
        "last_lifecycle": None, "market_status": "OPEN", "book_size": 0}
    book.reset()
    assert book.last_gap_context is None
