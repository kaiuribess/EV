"""The dashboard explains the first observed native matching stop."""
from datetime import timedelta
from types import SimpleNamespace

from evplatform.core.models import Market, Settlement, utcnow
from evplatform.core.native_runtime import NativeRuntime


def source_market(*, prop=False):
    return Market(id="m", event_id="e", event_name="Away at Home", sport="basketball",
        league="NBA", participants=("Home", "Away"), start_time=utcnow()+timedelta(hours=3),
        market_type="player_prop" if prop else "moneyline",
        outcomes=("over", "under") if prop else ("Home", "Away"),
        player="Player A" if prop else None, player_id="Player A" if prop else None,
        statistic="points" if prop else None, line="24.5" if prop else None,
        settlement=Settlement(period="FULL"), extensions={"research_only": True})


def test_funnel_distinguishes_missing_identity_subscription_depth_and_pricing(monkeypatch):
    monkeypatch.setattr(NativeRuntime, "research_identity_rejections", lambda *_: [])
    current = utcnow()
    def monitor(matches=(), subscribed=(), status="current", stream=True):
        return SimpleNamespace(research_catalog_at=current, discovery_status=status,
            context=SimpleNamespace(config={"discovery_interval_seconds": 300}),
            research_matches={"m": matches} if matches else {},
            _subscribed_market_ids=subscribed,
            client=SimpleNamespace(health_check=lambda: {"streaming": stream}),
            task=SimpleNamespace(done=lambda: False))

    matched = SimpleNamespace(native_market_id="native", native_event_id="event")
    records = {
        "kalshi": SimpleNamespace(enabled=True, plugin=monitor()),
        "novig": SimpleNamespace(enabled=True, plugin=monitor([matched])),
        "polymarket_us": SimpleNamespace(enabled=True, plugin=monitor([matched], ["native"])),
        "prophetx": SimpleNamespace(enabled=False, plugin=None),
    }
    runtime = object.__new__(NativeRuntime)
    runtime.engine = SimpleNamespace(registry=SimpleNamespace(records=records))
    funnel = runtime.research_venue_funnel(source_market(), [
        {"venue": "polymarket_us", "price": None, "rejection": "No current depth"}])
    assert funnel["kalshi"]["stage"] == "not_discovered"
    assert funnel["novig"]["stage"] == "not_subscribed"
    assert funnel["polymarket_us"]["stage"] == "depth_or_fee_rejected"
    assert funnel["prophetx"]["stage"] == "disconnected"
    records["polymarket_us"].plugin.client.rest_books = {"native": {}}
    funnel = runtime.research_venue_funnel(source_market(), [
        {"venue": "polymarket_us", "price": "0.44", "rejection": None}])
    assert funnel["polymarket_us"]["stage"] == "priced_research"
    assert runtime.research_venue_funnel(source_market(prop=True), [])["polymarket_us"]["stage"] == "unsupported_family"


def test_funnel_rejects_expired_catalog_and_disconnected_stream(monkeypatch):
    monkeypatch.setattr(NativeRuntime, "research_identity_rejections", lambda *_: [])
    match = SimpleNamespace(native_market_id="native", native_event_id="event")
    record = SimpleNamespace(enabled=True, plugin=SimpleNamespace(
        research_catalog_at=utcnow()-timedelta(hours=1), discovery_status="current",
        context=SimpleNamespace(config={"discovery_interval_seconds": 60}),
        research_matches={"m": [match]}, _subscribed_market_ids=["native"],
        client=SimpleNamespace(health_check=lambda: {"streaming": False}),
        task=SimpleNamespace(done=lambda: False)))
    runtime = object.__new__(NativeRuntime)
    runtime.engine = SimpleNamespace(registry=SimpleNamespace(records={"kalshi": record}))
    assert runtime.research_venue_funnel(source_market(), [])["kalshi"]["stage"] == "catalog_expired"
    record.plugin.research_catalog_at = utcnow()
    assert runtime.research_venue_funnel(source_market(), [])["kalshi"]["stage"] == "stream_unavailable"
