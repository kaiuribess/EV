from dataclasses import replace
from datetime import UTC, datetime, timedelta
import importlib.util
import json
from pathlib import Path

import pytest

from evplatform.integrations.base import ProviderEnvelope
from evplatform.integrations.freshness_evidence import FreshnessEvidence, evidence_digest


NOW = datetime(2026, 10, 20, 19, tzinfo=UTC)


def raw(i=0, **updates):
    observed = NOW+timedelta(seconds=i)
    row = {"bookmaker": "fanduel", "event_id": "event", "market_key": "player_points", "player": "Example",
           "line": 24.5, "over_price": -110, "under_price": -110,
           "last_update": int((observed-timedelta(hours=1)).timestamp()*1000),
           "last_observed": observed.isoformat(), "last_update_type": "collector_observation", **updates}
    return ProviderEnvelope("parlay_ws", "odds_update", {"data": [row]}, observed+timedelta(milliseconds=100), i,
        published_at=observed+timedelta(milliseconds=50), metadata={"sport": "basketball_nba"})


def test_clock_measurements_remain_separate_and_small_samples_have_no_tail_estimate():
    collector = FreshnessEvidence()
    collector.observe(raw())
    group, = collector.report()["groups"]
    assert group["observation_to_receipt_ms"]["median"] == 100
    assert group["legacy_clock_age_ms_not_publication_latency"]["median"] == 3600100
    assert group["provider_send_to_receipt_ms"]["median"] == 50
    assert group["provider_send_to_receipt_ms"]["p95"] is None
    assert group["ttl_proposal"]["seconds"] is None
    assert not group["ttl_proposal"]["validated"] and not group["ttl_proposal"]["installable"]


def test_repeated_delivery_does_not_inflate_unique_observation_counts_or_cadence():
    collector = FreshnessEvidence()
    for _ in range(200):
        collector.observe(raw())
    group, = collector.report()["groups"]
    assert group["counts"]["deliveries"] == 200
    assert group["counts"]["distinct_observations"] == 1
    assert group["counts"]["repeated_same_observation"] == 199
    assert group["same_offer_observation_interval_ms"]["count"] == 0
    assert group["ttl_proposal"]["seconds"] is None


def test_measurement_candidate_requires_series_and_never_marks_validated():
    collector = FreshnessEvidence()
    for i in range(101):
        collector.observe(raw(i))
    group, = collector.report()["groups"]
    assert group["observation_to_receipt_ms"]["p99"] == 100
    assert group["same_offer_observation_interval_ms"]["p95"] == 1000
    assert group["ttl_proposal"]["seconds"] == 2
    assert group["ttl_proposal"]["validated"] is False
    assert group["counts"]["market_status_verified"] == 0


def test_provider_delivery_percentiles_count_frames_not_prices():
    collector = FreshnessEvidence()
    envelope = raw()
    envelope.payload["data"] = [{**envelope.payload["data"][0], "line": i+.5} for i in range(150)]
    collector.observe(envelope)
    group, = collector.report()["groups"]
    assert group["counts"]["distinct_observations"] == 150
    assert group["provider_send_to_receipt_ms"]["count"] == 1
    assert group["provider_send_to_receipt_ms"]["p99"] is None


def test_unknown_observation_clock_is_not_replaced_by_legacy_or_delivery():
    collector = FreshnessEvidence()
    collector.observe(raw(last_observed=None))
    report = collector.report()
    group, = report["groups"]
    assert group["observation_to_receipt_ms"]["count"] == 0
    assert group["legacy_clock_age_ms_not_publication_latency"]["count"] == 1
    assert report["field_types"][1]["fields"]["last_observed"] == {"null": 1}


def test_no_raw_credentials_names_or_arbitrary_strings_enter_report():
    collector = FreshnessEvidence()
    envelope = raw(player="Private display name", api_key="never-output-secret", status="never-output-secret")
    envelope.payload["token"] = "never-output-secret"
    collector.observe(envelope)
    report = collector.report()
    encoded = json.dumps(report)
    assert "never-output-secret" not in encoded and "Private display name" not in encoded
    assert len(evidence_digest(report)) == 64


def test_capture_bounds_and_bad_payloads_are_visible():
    collector = FreshnessEvidence(max_offers=1)
    collector.observe(raw())
    collector.observe(raw(1))
    collector.observe(raw(2, over_price="NaN"))
    report = collector.report()
    assert report["observations_omitted_by_bounds"] == 1
    assert report["extraction_errors"] == 1
    with pytest.raises(ValueError):
        FreshnessEvidence(max_offers=0)


def test_synthetic_evidence_is_explicit():
    collector = FreshnessEvidence()
    collector.observe(replace(raw(), synthetic=True))
    assert collector.report()["synthetic"] is True
    assert collector.report()["execution_enabled"] is False


async def test_live_capture_script_uses_existing_adapters_with_bounded_read_only_calls(monkeypatch):
    path = Path(__file__).resolve().parents[1]/"scripts"/"collect_freshness_evidence.py"
    spec = importlib.util.spec_from_file_location("freshness_capture_cli", path)
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    calls = []
    class Socket:
        def __init__(self, config):
            calls.append(("socket_config", config))
        async def stream_updates(self):
            yield raw()
        async def health_check(self):
            return {"authenticated": True, "credential_configured": True, "errors": 0}
        async def disconnect(self):
            calls.append(("socket_closed",))
    class Rest:
        def __init__(self, config):
            self.quota = type("Quota", (), {"provider_remaining": 100, "calls": 2, "billable_requests": 1})()
            self.last_http_latency_ms = 15
        async def connect(self):
            return True
        async def fetch_account(self):
            calls.append(("free_usage",))
        async def fetch_snapshot(self, sport):
            calls.append(("one_snapshot", sport))
            return ProviderEnvelope("oddize_rest", "snapshot", {"events": []}, NOW, 5)
        async def disconnect(self):
            calls.append(("rest_closed",))
    monkeypatch.setattr(script, "ParlayWebSocket", Socket)
    monkeypatch.setattr(script, "OddizeRest", Rest)
    report = await script.collect(seconds=5, parlay_sport="basketball_nba", oddize_sport="nba",
                                  parlay_books=["fanduel"], oddize_books=["fd"])
    assert calls.count(("one_snapshot", "nba")) == 1
    assert calls.index(("free_usage",)) < calls.index(("one_snapshot", "nba"))
    assert report["capture"]["diagnostics"]["parlay"]["authenticated"] is True
    assert report["capture"]["diagnostics"]["oddize"]["http_roundtrip_ms"] == 15
    assert ("socket_closed",) in calls and ("rest_closed",) in calls
