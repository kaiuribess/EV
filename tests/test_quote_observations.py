"""Offline evidence-contract tests; these do not certify any live provider."""
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from evplatform.core.models import Market, Settlement
from evplatform.integrations.base import ProviderEnvelope
from evplatform.integrations.normalization import VerifiedOfferMapping, VerifiedProviderMapper, extract_offers
from evplatform.integrations import normalization
from evplatform.integrations.oddize import OddizeRest


NOW = datetime(2026, 10, 20, 19, tzinfo=UTC)


def parlay_row(**updates):
    return {"bookmaker": "fanduel", "event_id": "native-event", "market_key": "player_points",
            "player": "Example Player", "line": 24.5, "over_price": -110, "under_price": -110,
            "last_update": int((NOW - timedelta(hours=1)).timestamp() * 1000),
            "last_observed": (NOW - timedelta(seconds=1)).isoformat(),
            "last_update_type": "collector_observation", **updates}


def parlay(*rows, **body):
    return ProviderEnvelope("parlay_ws", "odds_update", {"data": list(rows or [parlay_row()]), **body}, NOW, 1234)


def mapper_for(raw):
    mapper = VerifiedProviderMapper()
    mapper(raw)
    signature = next(iter(mapper.quarantine))
    identity = mapper.quarantine[signature]["identity"]
    market = Market(id="canonical-market", event_id="canonical-event", event_name="Away vs Home",
                    sport="basketball", league="NBA", participants=("Away", "Home"),
                    start_time=NOW + timedelta(hours=1), market_type="player_prop", player="Example Player",
                    player_id="verified-player", statistic="points", line=Decimal("24.5"),
                    outcomes=("over", "under"), settlement=Settlement(scope="player", participation="must_play"))
    sides = list(identity["outcome_points"])
    mapper.register(VerifiedOfferMapping(signature=signature, market=market,
        outcome_map={side: "over" if side.lower().endswith("over") else "under" for side in sides},
        bookmaker="fanduel", verified=True, evidence="Offline only: exact player, event, rule and threshold fixture"))
    mapper(replace(raw, kind="feed_invalidated", payload={}))
    return mapper


def test_observation_is_distinct_from_retained_clock_price_change_and_delivery():
    changed = NOW - timedelta(hours=2)
    raw = parlay(parlay_row(line_changed_at_ms=int(changed.timestamp() * 1000)))
    offer, = extract_offers(raw)
    assert offer.source_observed_at == NOW - timedelta(seconds=1)
    assert offer.observed_at == NOW - timedelta(hours=1)
    assert offer.source_price_changed_at == changed
    assert offer.observation_verified and offer.observation_complete
    assert not offer.market_status_verified  # Observed does not mean tradable.
    (_, quote), = mapper_for(raw)(raw)
    assert quote.source_observed_at == offer.source_observed_at
    assert quote.source_time == offer.observed_at
    assert quote.source_price_changed_at == changed
    assert quote.received_at == NOW
    assert quote.observation_verified and quote.observation_complete
    assert not quote.market_status_verified


def test_parlay_fanduel_bilateral_native_links_certify_only_that_rest_prop_observation():
    row = parlay_row(over_link="https://sportsbook.fanduel.com/over",
                     under_link="https://sportsbook.fanduel.com/under")
    rest = ProviderEnvelope("parlay_rest", "props_snapshot", [row], NOW, 1)
    offer, = extract_offers(rest)
    assert offer.observation_verified and offer.observation_complete and offer.market_status_verified
    assert not extract_offers(parlay(row))[0].market_status_verified  # WS has no documented link contract.
    for change in ({"under_link": None}, {"over_link": "not-a-url"},
                   {"over_link": "https://"}, {"under_link": "https://user:pass@example.com/"},
                   {"last_observed": None}, {"last_update_type": "price_change"},
                   {"bookmaker": "draftkings"}, {"under_price": None}):
        candidate, = extract_offers(ProviderEnvelope("parlay_rest", "props_snapshot",
            [{**row, **change}], NOW, 1))
        assert not candidate.market_status_verified
    partial, = extract_offers(ProviderEnvelope("parlay_rest", "props_snapshot",
        [row], NOW, 1, metadata={"headers": {"x-result-truncated": "true"}}))
    assert not partial.market_status_verified


@pytest.mark.parametrize("extra", [
    {"last_observed": None}, {"last_update_type": None}, {"last_update_type": "price_change"},
    {"last_observed": "invalid"}, {"last_observed": NOW + timedelta(seconds=1)},
    {"last_observed": int((NOW - timedelta(seconds=1)).timestamp() * 1000)},
    {"last_observed": int((NOW - timedelta(seconds=1)).timestamp())},
    {"last_observed": (NOW + timedelta(seconds=1)).isoformat()},
    {"last_observed": int((NOW + timedelta(seconds=1)).timestamp() * 1000)},
])
def test_missing_invalid_or_future_observation_is_never_certified(extra):
    offer, = extract_offers(parlay(parlay_row(**extra)))
    assert not offer.observation_verified
    assert not offer.observation_complete


@pytest.mark.parametrize("body,reason", [
    ({"truncated": True}, "truncated_response"),
    ({"truncated_books": {"fanduel": {"omitted": 1}}}, "truncated_response"),
    ({"partial": True}, "partial_response"),
    ({"snapshot_complete": False, "truncated": False}, "incomplete_snapshot"),
    ({"snapshot_partial_reasons": ["read_cap"]}, "partial_response"),
    ({"snapshot_in_progress": True}, "partial_response"),
])
def test_transport_completeness_flags_do_not_get_discarded(body, reason):
    offer, = extract_offers(parlay(**body))
    assert offer.observation_verified
    assert not offer.observation_complete
    assert offer.verification_reason == reason


@pytest.mark.parametrize("headers,reason", [
    ({"x-result-truncated": "true"}, "truncated_response"),
    ({"x-result-has-more": "true", "x-next-offset": "500"}, "paginated_response_not_atomic"),
    ({"x-result-degraded": "fanduel,draftkings"}, "degraded_source"),
    ({"x-parlay-stale": "props_scan_busy", "x-parlay-stale-age-seconds": "10"},
     "stale_cached_board"),
])
def test_rest_headers_survive_into_quote_completeness(headers, reason):
    raw = ProviderEnvelope("parlay_rest", "props_snapshot", [parlay_row()], NOW, 1,
                           metadata={"headers": headers})
    offer, = extract_offers(raw)
    assert not offer.observation_complete
    assert offer.verification_reason == reason


def test_fixture_heartbeat_and_topups_never_verify_main_prices():
    event = {"id": "event", "bookmakers": [{"key": "fanduel", "last_update_ms": int(NOW.timestamp()*1000),
             "verified_at": NOW.isoformat(), "is_current": True, "topped_up": True,
             "markets": [{"key": "h2h", "outcomes": [{"name": "Home", "price": 2}, {"name": "Away", "price": 2}]}]}]}
    raw = ProviderEnvelope("parlay_rest", "snapshot", [event], NOW, 1)
    offer, = extract_offers(raw)
    assert offer.observed_at is None and offer.source_observed_at is None
    assert not offer.observation_verified and not offer.observation_complete
    assert offer.verification_reason == "retained_top_up"


def paired_rows():
    row = parlay_row()
    row.pop("over_price")
    row.pop("under_price")
    return [{**row, "outcome": f"Example Player {side}", "price": -110} for side in ("Over", "Under")]


def test_cross_frame_or_unsynchronized_sides_remain_research_only():
    over, under = paired_rows()
    mapper = mapper_for(parlay(over, under))
    assert mapper(parlay(over)) == []
    (_, quote), = mapper(parlay(under))
    assert quote.observation_verified and not quote.observation_complete
    assert quote.verification_reason == "incomplete_or_cross_frame_outcomes"
    (_, quote), = mapper(parlay(over, under))
    assert quote.observation_complete
    newer_under = {**under, "last_observed": NOW.isoformat()}
    (_, quote), = mapper(parlay(over, newer_under))
    assert quote.source_observed_at == NOW - timedelta(seconds=1)
    assert not quote.observation_complete
    assert quote.verification_reason == "unsynchronized_outcomes"


def test_clock_refresh_keeps_source_identity_and_accepts_new_observation():
    raw = parlay()
    mapper = mapper_for(raw)
    mapper(raw)
    newer = parlay(parlay_row(last_update=int((NOW-timedelta(hours=2)).timestamp()*1000),
                             last_observed=NOW.isoformat()))
    assert extract_offers(newer)[0].signature == extract_offers(raw)[0].signature
    (_, quote), = mapper(newer)
    assert quote.source_observed_at == NOW
    assert not mapper(raw)  # A new delivery cannot restore an older observation.


def test_missing_observation_evidence_revokes_eligibility_even_with_old_legacy_clock():
    raw = parlay()
    mapper = mapper_for(raw)
    (_, good), = mapper(raw)
    assert good.observation_verified and good.observation_complete
    degraded = parlay(parlay_row(last_observed=None))
    (_, bad), = mapper(degraded)
    assert not bad.observation_verified and not bad.observation_complete
    assert bad.source_time == good.source_time


def test_missing_partial_side_clock_does_not_keep_cached_verified_pair():
    over, under = paired_rows()
    mapper = mapper_for(parlay(over, under))
    mapper(parlay(over, under))
    (_, downgraded), = mapper(parlay({**over, "last_observed": None}, under))
    assert not downgraded.observation_verified and not downgraded.observation_complete


def test_delayed_verified_replay_cannot_clear_downgrade_or_reconnect_latch(monkeypatch):
    # This fixture supplies an independently verified active status to exercise
    # recovery. The production Parlay/Oddize parsers never assert this status.
    real_extract = normalization.extract_offers
    monkeypatch.setattr(normalization, "extract_offers", lambda raw: [
        replace(offer, market_status_verified=True) for offer in real_extract(raw)])
    raw = parlay()
    mapper = mapper_for(raw)
    (_, initial), = mapper(raw)
    assert initial.observation_complete and initial.market_status_verified
    (_, degraded), = mapper(parlay(parlay_row(last_observed=None)))
    assert not degraded.observation_complete
    (_, replayed), = mapper(raw)
    assert not replayed.observation_complete
    assert replayed.verification_reason == "new_complete_verified_observation_required"
    newer = parlay(parlay_row(last_observed=NOW.isoformat()))
    (_, recovered), = mapper(newer)
    assert recovered.observation_complete
    mapper(replace(newer, kind="feed_invalidated", payload={}))
    result = mapper(newer)
    assert all(not quote.observation_complete for _, quote in result)


def test_explicit_null_price_invalidates_previous_usable_quote():
    raw = parlay()
    mapper = mapper_for(raw)
    mapper(raw)
    missing = parlay(parlay_row(over_price=None, under_price=None))
    result = mapper(missing)
    assert result and all(quote.status == "suspended" for _, quote in result)


def oddize_props(*, older_under=False):
    rows = []
    for side in ("Over", "Under"):
        observed = NOW - timedelta(seconds=10 if older_under and side == "Under" else 1)
        rows.append({"market": "player_pts", "offer_type": "player_pts", "subject": "Example Player",
                     "subject_type": "player", "side": side, "line": 24.5, "book": "fd",
                     "american_odds": "-110", "ts": int(observed.timestamp()), "timestamp": observed.isoformat()})
    return ProviderEnvelope("oddize_rest", "props_snapshot", {"event_id": "event", "sport": "nba", "props": rows}, NOW, 1)


def test_oddize_observation_does_not_invent_market_status_or_price_change():
    offer, = extract_offers(oddize_props())
    assert offer.observation_verified and offer.observation_complete
    assert not offer.market_status_verified and offer.source_price_changed_at is None
    assert offer.source_observed_at == NOW - timedelta(seconds=1)
    older, = extract_offers(oddize_props(older_under=True))
    assert older.source_observed_at == NOW - timedelta(seconds=10)
    assert older.observation_verified and not older.observation_complete
    assert older.verification_reason == "unsynchronized_outcomes"


async def test_oddize_sport_prop_discovery_requires_type_and_keeps_alternates_unpriced():
    seen = []
    nested = {"sport": "nba", "prop_type": "player_pts", "events": [{"event_id": "event",
              "players": [{"player_name": "Example", "main_line": 24.5, "odds": [],
                           "alt_lines": {"25.5": []}, "all_odds": []}]}]}
    def handle(request):
        seen.append(request)
        return httpx.Response(200, json=nested)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        provider = OddizeRest({"enabled": True}, credential="offline", client=client)
        for invalid in (None, "", "player pts", "player_pts&sport=nfl"):
            with pytest.raises(ValueError):
                await provider.discover_prop_events("nba", prop_type=invalid)
        assert not seen
        result = await provider.discover_prop_events("nba", prop_type="player_pts")
    assert seen[0].url.path == "/api/v1/props/nba"
    assert seen[0].url.params["prop_type"] == "player_pts"
    assert result.payload == nested and result.kind == "prop_discovery"
    assert extract_offers(result) == []
    assert provider.quota.billable_requests == 2
