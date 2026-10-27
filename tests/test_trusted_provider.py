"""Provider-trusted probabilities are real-source research, never order permission."""
from dataclasses import replace
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import json

import pytest
from fastapi.testclient import TestClient

from evplatform.api import create_app
from evplatform.core.config import PricingConfig, TrustedPilotMarketScope, TrustedPilotPolicy
from evplatform.core.config import Settings
from evplatform.core.models import Envelope, utcnow
from evplatform.core.native_accounting import NativeInstrument, NativePriceBand
from evplatform.core.native_pricing import (NativeBook, NativeFeeBound, NativeLevel,
    instrument_fingerprint, native_opportunity, research_native_estimate)
from evplatform.core.pricing import NoSignal, fair_value, validate_fair_references
from evplatform.core.reference_profiles import ProfileSelector, ReferenceProfile
from evplatform.engine import Engine
from evplatform.integrations.base import ProviderEnvelope
from evplatform.integrations.normalization import SourceOffer, TrustedSourceProjector, VerifiedProviderMapper


NOW = datetime(2026, 10, 22, 20, tzinfo=UTC)


def config(mode="trusted_provider"):
    profile = ReferenceProfile(selectors=(ProfileSelector(
        sport="basketball", league="NBA", market_type="moneyline"),),
        weights_percent={"pinnacle": Decimal(60), "fanduel": Decimal(40)},
        required_any_anchor=("pinnacle",), min_distinct_books=2,
        status="research_prior", live_execution_allowed=False)
    return PricingConfig(reference_profile_mode="strict", reference_quality_mode=mode,
        normal_outcome_policy="winner_only_binary", reference_profiles={"nba": profile},
        enabled_reference_profiles=["nba"], max_quote_age_ms=5000,
        trusted_max_delivery_age_ms=30000, trusted_max_indicator_age_ms=30000)


def frame(*, suspended=False, pinnacle=True, fanduel=True, incomplete=False,
          poll_age=1, received=NOW, start=None):
    start = start or NOW + timedelta(hours=1)
    books = []
    if pinnacle:
        books.append({"key": "pinnacle", "verified_at": (received-timedelta(seconds=poll_age)).isoformat(),
            "is_current": True, "markets": [{"key": "h2h", "last_update": (NOW-timedelta(hours=1)).isoformat(),
                "outcomes": [{"name": "Home", "price": 1.85}, {"name": "Away", "price": 2.05}]}]})
    if fanduel:
        books.append({"key": "fanduel", "markets": [{"key": "h2h", "suspended": suspended,
            "last_update": (NOW-timedelta(hours=1)).isoformat(),
            "outcomes": ([{"name": "Home", "price": 1.91}] if incomplete else
                         [{"name": "Home", "price": 1.91}, {"name": "Away", "price": 1.91}])}]})
    return ProviderEnvelope("parlay_rest", "snapshot", [{"id": "event-1", "sport_key": "basketball_nba",
        "home_team": "Home", "away_team": "Away", "commence_time": start.isoformat(),
        "bookmakers": books}], received, 1, published_at=received,
        metadata={"sport": "basketball_nba"})


def projected(envelope, policy=None):
    mapper = VerifiedProviderMapper()
    mapper.research_projector = TrustedSourceProjector(lambda: policy or config())
    assert mapper(envelope) == []  # No reviewed source-to-native identity.
    return mapper, mapper.research_updates


def test_trusted_two_book_probability_keeps_unknown_quote_age_and_status():
    _, updates = projected(frame())
    assert len(updates) == 2
    market = updates[0][0]
    assert market.id.startswith("research:parlay:") and market.extensions["native_mapping_reviewed"] is False
    assert all(m.id == market.id for m, _ in updates)
    fair = fair_value(market, [q for _, q in updates], config(), NOW)
    assert set(fair.probabilities) == {"Home", "Away"}
    assert fair.probabilities["Home"] > Decimal("0.50")
    assert [r["effective_weight_percent"] for r in fair.references] == ["40.0", "60.0"]
    assert all(r["source_observed_at"] is None and r["age_ms"] is None for r in fair.references)
    assert any(r["status"] == "unknown" for r in fair.references)
    assert not fair.execution_eligible and not fair.live_execution_allowed
    assert any("Exact selection observation age unknown" in reason for reason in fair.ineligibility_reasons)
    assert all(r["source_price_changed_at"] is None or r["source_price_changed_at"] != r["source_observed_at"]
               for r in fair.references)


def test_parlay_stored_row_clock_expires_even_with_fresh_book_heartbeat():
    fresh = frame()
    payload = deepcopy(fresh.payload)
    for book in payload[0]["bookmakers"]:
        book["verified_at"] = NOW.isoformat()
        book["is_current"] = True
        book["line_changed_at_ms"] = int((NOW-timedelta(seconds=2)).timestamp()*1000)
    current = replace(fresh, payload=payload)
    _, updates = projected(current)
    market = updates[0][0]
    quotes = [quote for _, quote in updates]
    fair = fair_value(market, quotes, config(), NOW)
    assert all(ref["source_stored_at"] == (NOW-timedelta(seconds=2)).isoformat()
               for ref in fair.references)
    assert all(ref["source_price_changed_at"] is None for ref in fair.references)
    assert all(ref["source_scope_poll_at"] is None or ref["source_scope_poll_at"] == NOW.isoformat()
               for ref in fair.references)
    stale_payload = deepcopy(payload)
    stale_payload[0]["bookmakers"][0]["line_changed_at_ms"] = int(
        (NOW-timedelta(seconds=31)).timestamp()*1000)
    _, stale_updates = projected(replace(fresh, payload=stale_payload))
    with pytest.raises(NoSignal, match="Insufficient"):
        fair_value(market, [quote for _, quote in stale_updates], config(), NOW)
    with pytest.raises(NoSignal, match="stored price-row indicator expired"):
        validate_fair_references(market, fair, config(), NOW+timedelta(seconds=29))


def test_newer_fanduel_stream_row_beats_rest_heartbeat_without_changing_weights():
    original = frame()
    payload = deepcopy(original.payload)
    for book in payload[0]["bookmakers"]:
        book["line_changed_at_ms"] = int((NOW-timedelta(seconds=20)).timestamp()*1000)
        book["verified_at"] = NOW.isoformat()
        book["is_current"] = True
    _, updates = projected(replace(original, payload=payload))
    market = updates[0][0]
    rest_quotes = [quote for _, quote in updates]
    fd_rest = next(q for q in rest_quotes if q.bookmaker == "fanduel")
    fd_ws = fd_rest.model_copy(update={"provider": "parlay_game_ws",
        "source_stored_at": None, "source_time": NOW-timedelta(seconds=2),
        "source_scope_poll_at": None, "source_scope_is_current": None,
        "received_at": NOW-timedelta(seconds=1)})
    fair = fair_value(market, [*rest_quotes, fd_ws], config(), NOW)
    selected = next(ref for ref in fair.references if ref["bookmaker"] == "fanduel")
    assert selected["provider"] == "parlay_game_ws"
    assert selected["effective_weight_percent"] == "40.0"
    older_ws = fd_ws.model_copy(update={"source_time": NOW-timedelta(seconds=25)})
    fair = fair_value(market, [*rest_quotes, older_ws], config(), NOW)
    assert next(ref for ref in fair.references if ref["bookmaker"] == "fanduel")["provider"] == "parlay_rest"


@pytest.mark.parametrize("adverse", ["degraded", "cached", "topped"])
def test_parlay_adverse_response_or_source_role_blocks_trusted_research(adverse):
    original = frame()
    payload = deepcopy(original.payload)
    headers = {}
    if adverse == "degraded":
        headers["x-provider-state"] = json.dumps({"src": {"pinnacle": {"role": "degraded"},
            "fanduel": {"role": "primary"}}})
    elif adverse == "cached":
        headers["x-parlay-stale"] = "board_query_timeout"
    else:
        payload[0]["bookmakers"][0]["topped_up"] = True
    bad = replace(original, payload=payload, metadata={**original.metadata, "headers": headers})
    _, updates = projected(bad)
    assert any(quote.verification_reason in {
        "degraded_source", "stale_cached_board", "retained_top_up"} for _, quote in updates)
    with pytest.raises(NoSignal, match="Insufficient"):
        fair_value(updates[0][0], [quote for _, quote in updates], config(), NOW)


def test_trusted_delivery_expires_without_relabeling_cached_price():
    _, updates = projected(frame())
    market = updates[0][0]
    with pytest.raises(NoSignal, match="Insufficient"):
        fair_value(market, [q for _, q in updates], config(), NOW+timedelta(seconds=31))


def test_cached_trusted_fair_expires_when_verified_source_indicator_ages_out():
    _, updates = projected(frame())
    market = updates[0][0]
    quotes = [quote.model_copy(update={"source_scope_poll_at": None,
              "source_observed_at": NOW-timedelta(seconds=1) if index == 0 else None,
              "observation_verified": index == 0})
              for index, (_, quote) in enumerate(updates)]
    scoped = config().model_copy(update={"trusted_max_indicator_age_ms": 5000})
    fair = fair_value(market, quotes, scoped, NOW)
    validate_fair_references(market, fair, scoped, NOW+timedelta(seconds=2))
    with pytest.raises(NoSignal, match="observation indicator expired"):
        validate_fair_references(market, fair, scoped, NOW+timedelta(seconds=6))


def test_research_game_delivery_window_can_be_scoped_without_changing_strict_ttl():
    _, updates = projected(frame())
    market = updates[0][0]
    quotes = [q.model_copy(update={"source_scope_poll_at": None,
        "source_scope_is_current": None}) for _, q in updates]
    scoped = config().model_copy(update={"reference_quality_mode": "strict_verified",
        "market_overrides": {"basketball:moneyline": {
            "reference_quality_mode": "trusted_provider",
            "trusted_max_delivery_age_ms": 300000}}})
    fair = fair_value(market, quotes, scoped, NOW+timedelta(minutes=2))
    assert fair.execution_eligible is False
    assert all(ref["source_observed_at"] is None for ref in fair.references)
    assert scoped.max_quote_age_ms == 5000
    with pytest.raises(NoSignal, match="Insufficient"):
        fair_value(market, quotes, scoped, NOW+timedelta(minutes=6))


def test_trusted_rejects_suspension_incomplete_pair_and_stale_poll():
    for case in (frame(suspended=True), frame(incomplete=True), frame(poll_age=31)):
        _, updates = projected(case)
        assert len(updates) >= 1
        market = updates[0][0]
        with pytest.raises(NoSignal):
            fair_value(market, [q for _, q in updates], config(), NOW)


@pytest.mark.parametrize("scope", ["paginated_response_not_atomic", "incomplete_snapshot",
    "truncated_response"])
def test_complete_parlay_pair_from_bounded_board_is_labeled_research(scope):
    envelope = frame()
    if scope in {"paginated_response_not_atomic", "truncated_response"}:
        envelope = replace(envelope, metadata={**envelope.metadata,
            "headers": ({"x-result-has-more": "true", "x-next-offset": "500"}
                        if scope == "paginated_response_not_atomic" else
                        {"x-result-truncated": "true"})})
    else:
        rows = [{"kind": "game", "bookmaker": book, "market_key": "h2h",
            "event_id": "event-1", "home_team": "Home", "away_team": "Away",
            "commence_time": (NOW + timedelta(hours=1)).isoformat(),
            "home_ml": -110, "away_ml": -110, "period": "FULL"}
            for book in ("pinnacle", "fanduel")]
        envelope = ProviderEnvelope("parlay_ws", "initial_state",
            {"data": rows, "snapshot_complete": False, "snapshot_scope": "recent_rows"},
            NOW, 1, published_at=NOW, metadata={"sport": "basketball_nba"})
    _, updates = projected(envelope)
    market = updates[0][0]
    fair = fair_value(market, [q for _, q in updates], config(), NOW)
    assert len(fair.references) == 2 and not fair.execution_eligible
    assert all(ref["verification_reason"] == scope for ref in fair.references)
    assert any("board coverage is bounded or paginated" in reason
               for reason in fair.ineligibility_reasons)
    with pytest.raises(NoSignal):
        fair_value(market, [q for _, q in updates], config("strict_verified"), NOW)


def test_truncation_never_masks_degraded_or_cached_parlay_board():
    for headers in ({"x-result-degraded": "pinnacle", "x-result-truncated": "true"},
                    {"x-parlay-stale": "props_scan_busy", "x-result-truncated": "true"}):
        envelope = replace(frame(), metadata={"sport": "basketball_nba", "headers": headers})
        _, updates = projected(envelope)
        with pytest.raises(NoSignal):
            fair_value(updates[0][0], [q for _, q in updates], config(), NOW)


def test_trusted_keeps_strict_mode_and_source_mapping_separate():
    _, updates = projected(frame(), config("strict_verified"))
    assert updates == []
    _, updates = projected(frame())
    market = updates[0][0]
    with pytest.raises(NoSignal, match="Insufficient"):
        fair_value(market, [q for _, q in updates], config("strict_verified"), NOW)


def test_duplicate_book_from_rest_and_websocket_counts_once():
    _, updates = projected(frame())
    market = updates[0][0]
    copies = [q for _, q in updates]
    copies.append(copies[0].model_copy(update={"provider": "parlay_ws"}))
    fair = fair_value(market, copies, config(), NOW)
    assert len(fair.references) == 2
    assert {r["bookmaker"] for r in fair.references} == {"pinnacle", "fanduel"}


def test_malformed_line_is_isolated_from_other_research_markets():
    projector = TrustedSourceProjector(config)
    envelope = frame()
    valid = SourceOffer(provider="parlay_rest", bookmaker="pinnacle", event_id="event-1",
        market_key="h2h", player_key=None, line=None, period="FULL", outcome_points={},
        prices={"home": Decimal("1.85"), "away": Decimal("2.05")}, observed_at=None,
        provider_time=NOW, context={"event_sport": "basketball_nba",
            "participants": ["Home", "Away"],
            "scheduled_start": (NOW + timedelta(hours=1)).isoformat()})
    broken = SourceOffer(provider="parlay_rest", bookmaker="pinnacle", event_id="event-1",
        market_key="spreads", player_key=None, line="bad", period="FULL",
        outcome_points={"Home": "bad", "Away": "-1.5"},
        prices={"Home": Decimal("1.9"), "Away": Decimal("1.9")}, observed_at=None,
        provider_time=NOW, context=valid.context)
    updates = projector.apply(envelope, [valid, broken])
    assert len(updates) == 1
    assert updates[0][0].market_type == "moneyline"


def test_full_game_periods_coalesce_but_quarter_market_does_not():
    from dataclasses import replace

    projector = TrustedSourceProjector(config)
    envelope = frame()
    base = SourceOffer(provider="parlay_rest", bookmaker="pinnacle", event_id="event-1",
        market_key="h2h", player_key=None, line=None, period=None, outcome_points={},
        prices={"home": Decimal("1.85"), "away": Decimal("2.05")}, observed_at=None,
        provider_time=NOW, context={"event_sport": "basketball_nba",
            "participants": ["Home", "Away"],
            "scheduled_start": (NOW + timedelta(hours=1)).isoformat()})
    rest = projector.apply(envelope, [base])[0][0]
    full = projector.apply(envelope, [replace(base, provider="parlay_game_ws", period="FULL")])[0][0]
    assert full.id == rest.id
    assert projector.apply(envelope, [replace(base, provider="parlay_game_ws", period="Q1")]) == []


def test_approved_game_websocket_unsuffixed_h2h_uses_documented_full_game_scope():
    scope = TrustedPilotMarketScope(venue="prophetx", sport="basketball", league="NBA",
        market_type="moneyline", reviewed_normal_rule_id="nba-full-winner-2026",
        source_normal_rule_evidence="Parlay game WebSocket h2h full-game market key review")
    policy = TrustedPilotPolicy(enabled=True, owner_approved=True, market_scope=scope,
        provider_id="parlay_game_ws", trusted_books=["fanduel", "draftkings", "caesars"],
        min_distinct_books=3, reference_weights_percent={"fanduel": Decimal(34),
            "draftkings": Decimal(33), "caesars": Decimal(33)},
        accept_unknown_active_status=True, accept_unknown_quote_observation_age=True,
        max_indicator_age_ms=30000, independent_crosscheck="two_distinct_books",
        uncertainty_margin_probability=Decimal("0.01"), max_debit_usd=Decimal(25),
        max_loss_usd=Decimal(25), recurring_authorization=True,
        maximum_native_attempts=None, automatic_followups=True,
        approval_evidence="OFFLINE approved-scope test fixture")
    pricing = PricingConfig.model_validate({**config().model_dump(),
        "trusted_pilot_policy": policy.model_dump()})
    projector = TrustedSourceProjector(lambda: pricing)
    envelope = ProviderEnvelope("parlay_game_ws", "initial_state", [], NOW, 1,
        published_at=NOW, metadata={"sport": "basketball_nba"})
    offer = SourceOffer(provider="parlay_game_ws", bookmaker="fanduel", event_id="event-1",
        market_key="h2h", player_key=None, line=None, period=None, outcome_points={},
        prices={"Home": Decimal("1.85"), "Away": Decimal("2.05")}, observed_at=None,
        provider_time=NOW, context={"event_sport": "basketball_nba",
            "participants": ["Home", "Away"],
            "scheduled_start": (NOW + timedelta(hours=1)).isoformat()})
    market = projector.apply(envelope, [offer])[0][0]
    assert market.id.startswith("reviewed:")
    assert market.extensions["normal_outcome_reviewed"] is True
    assert market.settlement.period == "FULL"
    assert TrustedSourceProjector(lambda: pricing).apply(envelope,
        [replace(offer, provider="parlay_rest")])[0][0].id.startswith("research:")
    assert TrustedSourceProjector(lambda: pricing).apply(envelope,
        [replace(offer, period="Q1")]) == []


def test_opposite_spread_orientation_has_a_distinct_research_identity():
    from dataclasses import replace

    projector = TrustedSourceProjector(config)
    envelope = frame()
    base = SourceOffer(provider="parlay_rest", bookmaker="pinnacle", event_id="event-1",
        market_key="spreads", player_key=None, line="3.5", period="FULL",
        outcome_points={"Home": "-3.5", "Away": "3.5"},
        prices={"Home": Decimal("1.9"), "Away": Decimal("1.9")}, observed_at=None,
        provider_time=NOW, context={"event_sport": "basketball_nba",
            "participants": ["Home", "Away"],
            "scheduled_start": (NOW + timedelta(hours=1)).isoformat()})
    home_favorite = projector._market_quote(envelope, base)[0]
    away_favorite = projector._market_quote(envelope, replace(base,
        outcome_points={"Home": "3.5", "Away": "-3.5"}))[0]
    assert home_favorite.id != away_favorite.id
    assert "Home -3.5" in home_favorite.outcomes
    assert "Home +3.5" in away_favorite.outcomes


def test_complete_game_websocket_frame_pairs_current_sides_without_quote_clock():
    rows = []
    for book in ("pinnacle", "fanduel"):
        for team in ("Home", "Away"):
            rows.append({"kind": "game", "bookmaker": book, "market_key": "h2h",
                "event_id": "event-1", "team": team, "outcome": team,
                "price": -110, "over_price": -110, "under_price": None,
                "home_team": "Home", "away_team": "Away", "period": "FULL",
                "commence_time": (NOW + timedelta(hours=1)).isoformat(),
                "last_update": int((NOW - timedelta(minutes=2)).timestamp() * 1000)})
    envelope = ProviderEnvelope("parlay_game_ws", "initial_state",
        {"data": rows, "snapshot_scope": "current_game_board",
         "snapshot_complete": True, "truncated": False}, NOW, 1,
        published_at=NOW, snapshot_complete=True, metadata={"sport": "basketball_nba"})
    mapper = VerifiedProviderMapper()
    mapper.research_projector = TrustedSourceProjector(config)
    mapper(envelope)
    assert len(mapper.research_updates) == 2
    market = mapper.research_updates[0][0]
    fair = fair_value(market, [quote for _, quote in mapper.research_updates], config(), NOW)
    assert fair.probabilities["Home"] == Decimal("0.5")
    assert all(ref["source_observed_at"] is None for ref in fair.references)
    assert not fair.execution_eligible


def test_game_websocket_one_sided_delta_preserves_older_side_age_for_research():
    start = (NOW + timedelta(hours=1)).isoformat()
    base = {"kind": "game", "market_key": "h2h", "event_id": "event-1",
        "home_team": "Home", "away_team": "Away", "period": "FULL",
        "commence_time": start, "last_update": int((NOW-timedelta(minutes=2)).timestamp()*1000)}
    baseline = ProviderEnvelope("parlay_game_ws", "initial_state", {
        "data": [{**base, "bookmaker": book, "home_ml": -110, "away_ml": -110}
                 for book in ("pinnacle", "fanduel")],
        "snapshot_scope": "current_game_board", "snapshot_complete": True,
        "truncated": False}, NOW, 1, published_at=NOW,
        snapshot_complete=True, metadata={"sport": "basketball_nba"})
    mapper = VerifiedProviderMapper()
    mapper.research_projector = TrustedSourceProjector(config)
    mapper(baseline)
    market = mapper.research_updates[0][0]
    assert len(mapper.research_updates) == 2
    later = NOW + timedelta(seconds=10)
    changed = {**base, "bookmaker": "pinnacle", "team": "Home",
        "outcome": "Home", "price": -125,
        "last_update": int(later.timestamp()*1000)}
    update = ProviderEnvelope("parlay_game_ws", "odds_update", {"data": [changed]},
        later, 2, published_at=later, metadata={"sport": "basketball_nba"})
    mapper(update)
    _, quote = mapper.research_updates[0]
    assert quote.decimal_odds["Home"] != quote.decimal_odds["Away"]
    assert quote.received_at == NOW  # The opposing side was not redelivered.
    assert quote.source_observed_at is None
    assert quote.verification_reason == "trusted_game_cross_frame_pair"
    quotes = [quote, next(q for m, q in mapper.research_projector.active.values()
                          if m.id == market.id and q.bookmaker == "fanduel")]
    fair = fair_value(market, quotes, config(), later)
    assert not fair.execution_eligible
    assert any("separate WebSocket frames" in text for text in fair.ineligibility_reasons)
    with pytest.raises(NoSignal, match="Insufficient"):
        fair_value(market, quotes, config(), NOW + timedelta(seconds=31))
    with pytest.raises(NoSignal):
        fair_value(market, quotes, config("strict_verified"), later)

    suspended = ProviderEnvelope("parlay_game_ws", "odds_update", {"data": [
        {**base, "bookmaker": "pinnacle", "team": "Away", "outcome": "Away",
         "price": -110, "suspended": True}]},
        later + timedelta(seconds=1), 3, metadata={"sport": "basketball_nba"})
    mapper(suspended)
    assert mapper.research_updates[0][1].status == "withdrawn"


@pytest.mark.asyncio
async def test_research_delta_does_not_trigger_strict_same_observation_latch(tmp_path):
    now = utcnow()
    _, updates = projected(frame(received=now, start=now + timedelta(hours=1)))
    market, first = updates[0]
    engine = Engine(Settings(_env_file=None, mode="production", live_enabled=False,
        auto_trade=False, sample_feed_enabled=False,
        database_path=str(tmp_path / "research-delta.db"), pricing=config()))
    engine.risk.kill_switch = True
    await engine.process_event(Envelope(schema_name="sports.market", source="test", payload=market))
    await engine.process_event(Envelope(schema_name="odds.quote", source="test", payload=first))
    updated = first.model_copy(update={"decimal_odds": {**first.decimal_odds, "Home": Decimal("1.7")},
        "verification_reason": "trusted_game_cross_frame_pair"})
    await engine.process_event(Envelope(schema_name="odds.quote", source="test", payload=updated))
    stored = engine.quotes[market.id][(first.provider, first.bookmaker)]
    assert stored.verification_reason == "trusted_game_cross_frame_pair"
    assert not engine.risk.orders and engine.risk.kill_switch


def test_pricing_settings_refresh_existing_research_without_order_attempt(tmp_path):
    now = utcnow()
    _, updates = projected(frame(received=now-timedelta(seconds=45),
        start=now+timedelta(hours=1)))
    market = updates[0][0]
    app = create_app(Settings(_env_file=None, mode="production", live_enabled=False,
        auto_trade=False, sample_feed_enabled=False,
        database_path=str(tmp_path / "settings-research.db"), pricing=config()))
    with TestClient(app) as client:
        engine = app.state.engine
        engine.risk.kill_switch = True
        engine.markets[market.id] = market
        engine.quotes[market.id] = {(q.provider, q.bookmaker): q.model_copy(update={
            "source_scope_poll_at": None, "source_scope_is_current": None}) for _, q in updates}
        assert not engine.reference_estimates
        response = client.post("/api/settings", headers={"X-EV-Control": "local"},
            json={"trusted_max_delivery_age_ms": 120000})
        assert response.status_code == 200
        assert any(item["market_id"] == market.id for item in response.json()["reference_estimates"])
        assert not engine.risk.native_orders and not engine.risk.orders
        assert engine.risk.kill_switch and not engine.risk.auto_trade


def test_trusted_pilot_policy_is_disabled_and_requires_scoped_owner_review():
    assert not PricingConfig().trusted_pilot_policy.enabled
    with pytest.raises(ValueError):
        TrustedPilotPolicy(enabled=True, exact_market_id="nba-1", provider_id="parlay_rest",
            trusted_books=["pinnacle", "fanduel"], max_indicator_age_ms=30000,
            independent_crosscheck="two_distinct_books",
            uncertainty_margin_probability=Decimal("0.02"), max_debit_usd=Decimal("1"),
            max_loss_usd=Decimal("1"), approval_evidence="owner-review")
    with pytest.raises(ValueError):
        TrustedPilotPolicy(enabled=True, owner_approved=True, exact_market_id="nba-1",
            provider_id="parlay_rest", trusted_books=["pinnacle", "fanduel"],
            max_indicator_age_ms=30000, independent_crosscheck="two_distinct_books",
            uncertainty_margin_probability=Decimal("0.02"), max_debit_usd=Decimal("5.01"),
            max_loss_usd=Decimal("1"), approval_evidence="owner-review")
    five = TrustedPilotPolicy(enabled=True, owner_approved=True, exact_market_id="nba-1",
        provider_id="parlay_rest", trusted_books=["pinnacle", "fanduel"],
        max_indicator_age_ms=30000, independent_crosscheck="two_distinct_books",
        uncertainty_margin_probability=Decimal("0.01"), max_debit_usd=Decimal("5"),
        max_loss_usd=Decimal("5"), approval_evidence="owner-review")
    assert five.max_debit_usd == Decimal("5")


def trusted_native_fixture(at=NOW):
    _, updates = projected(frame(received=at, start=at + timedelta(hours=1)))
    source_market = updates[0][0]
    market = source_market.model_copy(update={"id": "reviewed-fixture",
        "settlement": source_market.settlement.model_copy(update={"push_rule": "not_applicable"}),
        "extensions": {"reviewed_fixture": True}})
    quotes = [q.model_copy(update={"market_id": market.id,
        "market_fingerprint": market.identity()}) for _, q in updates]
    instrument = NativeInstrument(venue="kalshi", instrument_id="KX-REVIEWED-FIXTURE",
        exchange_index=3, canonical_market_id=market.id, outcome="Home",
        native_outcome="yes", quantity_unit="contracts", price_format="probability",
        payout_per_unit_usd="1", quantity_step="1", minimum_quantity="1",
        minimum_order_cost_usd="0", order_minimum_verified=True,
        order_minimum_source="fixture", cash_quantum_usd="0.0001",
        price_bands=(NativePriceBand(minimum="0.01", maximum="0.99", step="0.01"),),
        settlement_rules_fingerprint=market.identity(), verified=True, source="fixture")
    fee = NativeFeeBound(instrument_fingerprint=instrument_fingerprint(instrument),
        per_unit_usd="0.01", per_fill_usd="0", fixed_order_usd="0", verified=True,
        source="fixture", observed_at=at-timedelta(seconds=1),
        valid_until=at+timedelta(minutes=1))
    book = NativeBook(instrument=instrument, market_fingerprint=market.identity(),
        asks=(NativeLevel(price="0.40", quantity="10"),), fee_bound=fee,
        source_time=at-timedelta(milliseconds=100), received_at=at,
        revision=1, synchronized=True)
    return market, quotes, book


def test_synthetic_trusted_positive_ev_is_research_only_without_order_intent():
    market, quotes, book = trusted_native_fixture()
    fair = fair_value(market, quotes, config(), NOW)
    estimate = research_native_estimate(market, fair, book, config(), now=NOW)
    assert estimate["positive_estimate"] is True
    assert Decimal(estimate["expected_profit_usd"]) > 0
    assert estimate["execution_eligible"] is False
    with pytest.raises(NoSignal, match="research-only"):
        native_opportunity(market, fair, book, config(), max_stake_usd=Decimal("1"),
            mode="production", now=NOW)


def approved_pilot_case(at=NOW):
    market, quotes, book = trusted_native_fixture(at)
    quotes = [quote.model_copy(update={
        "normal_outcome_verified": True,
        "normal_outcome_evidence": "OFFLINE reviewed ordinary winner identity",
        "source_stored_at": at - timedelta(seconds=1),
        "source_scope_poll_at": at - timedelta(seconds=1),
        "source_scope_is_current": True,
    }) for quote in quotes]
    policy = TrustedPilotPolicy(enabled=True, owner_approved=True,
        exact_market_id=market.id, provider_id="parlay_rest",
        trusted_books=["pinnacle", "fanduel"],
        accept_unknown_active_status=True, accept_unknown_quote_observation_age=True,
        max_indicator_age_ms=30000, independent_crosscheck="two_distinct_books",
        uncertainty_margin_probability=Decimal("0.01"), max_debit_usd=Decimal("1"),
        max_loss_usd=Decimal("1"), approval_evidence="OFFLINE owner-policy fixture only")
    scoped = config().model_copy(update={"trusted_pilot_policy": policy})
    return market, quotes, book, scoped


def test_recurring_trusted_pricing_uses_cash_kelly_not_reporting_unit_as_cap():
    market, quotes, book, scoped = approved_pilot_case()
    reviewed = scoped.trusted_pilot_policy.model_dump()
    reviewed.update(recurring_authorization=True, maximum_native_attempts=None,
        automatic_followups=True, max_debit_usd=Decimal("25"), max_loss_usd=Decimal("25"))
    policy = TrustedPilotPolicy.model_validate(reviewed)
    assert policy.cost_ceiling() == Decimal("25")
    scoped = scoped.model_copy(update={"trusted_pilot_policy": policy})
    book = book.model_copy(update={"asks": (NativeLevel(price="0.40", quantity="100"),)})
    fair = fair_value(market, quotes, scoped, NOW)
    five = native_opportunity(market, fair, book, scoped, max_stake_usd=None,
        mode="production", stake_sizing="quarter_kelly",
        available_cash_usd=Decimal("1000"), unit_size_usd=Decimal("5"), now=NOW)
    ten = native_opportunity(market, fair, book, scoped, max_stake_usd=None,
        mode="production", stake_sizing="quarter_kelly",
        available_cash_usd=Decimal("1000"), unit_size_usd=Decimal("10"), now=NOW)
    assert five.intent.quantity == ten.intent.quantity
    assert five.intent.reserved_cost_usd > Decimal("5")
    assert five.sizing.unit_size_usd == Decimal("5")


def test_recurring_trusted_source_policy_requires_separate_money_caps():
    _, _, _, scoped = approved_pilot_case()
    reviewed = scoped.trusted_pilot_policy.model_dump()
    reviewed.update(recurring_authorization=True, maximum_native_attempts=None,
        automatic_followups=True, max_debit_usd=None, max_loss_usd=None)
    with pytest.raises(ValueError, match="exact, complete owner-reviewed source policy"):
        TrustedPilotPolicy.model_validate(reviewed)



def test_rule_bound_novig_nfl_pilot_pricing_accepts_only_reviewed_new_questions():
    """An approved source scope cannot itself review a new market's identity."""
    base, old_quotes, _ = trusted_native_fixture()
    rule_id = "OFFLINE-reviewed-nfl-full-prop-rule"
    market = base.model_copy(update={
        "id": "OFFLINE-new-nfl-receptions-3.5", "sport": "football", "league": "NFL",
        "market_type": "player_prop", "player": "Fixture Player", "player_id": "Fixture Player",
        "statistic": "receptions", "line": Decimal("3.5"), "outcomes": ("over", "under"),
        "settlement": base.settlement.model_copy(update={"period": "FULL", "overtime": "included"}),
        "extensions": {"reviewed_normal_rule_id": rule_id, "normal_outcome_reviewed": True},
    })
    books = ("fanduel", "draftkings", "caesars")
    quotes = [old_quotes[index % 2].model_copy(update={
        "market_id": market.id, "market_fingerprint": market.identity(), "bookmaker": book,
        "decimal_odds": {"over": old_quotes[index % 2].decimal_odds["Home"],
                         "under": old_quotes[index % 2].decimal_odds["Away"]},
        "normal_outcome_verified": True,
        "normal_outcome_evidence": f"{rule_id}: OFFLINE exact normal question review",
        "source_stored_at": NOW-timedelta(seconds=1),
        "source_scope_poll_at": NOW-timedelta(seconds=1),
        "source_scope_is_current": True,
    }) for index, book in enumerate(books)]
    profile = ReferenceProfile(selectors=(ProfileSelector(sport="football", league="NFL",
        market_type="player_prop", statistic="receptions"),),
        weights_percent={"fanduel": Decimal(34), "draftkings": Decimal(33),
                         "caesars": Decimal(33)},
        min_distinct_books=3, status="research_only", live_execution_allowed=False)
    policy = TrustedPilotPolicy(enabled=True, owner_approved=True,
        market_scope=TrustedPilotMarketScope(venue="novig", sport="football", league="NFL",
            market_type="player_prop", statistics=("receptions", "receiving_yards"),
            reviewed_normal_rule_id=rule_id),
        provider_id="parlay_rest", trusted_books=list(books), min_distinct_books=3,
        accept_unknown_active_status=True, accept_unknown_quote_observation_age=True,
        max_indicator_age_ms=30000, independent_crosscheck="two_distinct_books",
        uncertainty_margin_probability=Decimal("0.01"), max_debit_usd=Decimal("5"),
        max_loss_usd=Decimal("5"), approval_evidence="OFFLINE scoped approval fixture")
    scoped = PricingConfig(reference_profile_mode="strict", reference_quality_mode="trusted_provider",
        normal_outcome_policy="winner_only_binary", reference_profiles={"nfl": profile},
        enabled_reference_profiles=["nfl"], trusted_pilot_policy=policy,
        trusted_max_delivery_age_ms=30000, trusted_max_indicator_age_ms=30000)
    fair = fair_value(market, quotes, scoped, NOW)
    assert fair.live_execution_allowed and fair.execution_eligible
    assert fair.trusted_pilot_margin_probability == Decimal("0.01")
    assert {ref["bookmaker"] for ref in fair.references} == set(books)
    with pytest.raises(NoSignal, match="Insufficient"):
        fair_value(market, quotes[:2], scoped, NOW)
    for altered in (
        market.model_copy(update={"extensions": {"reviewed_normal_rule_id": rule_id}}),
        market.model_copy(update={"extensions": {"reviewed_normal_rule_id": rule_id,
            "normal_outcome_reviewed": True, "research_only": True}}),
        market.model_copy(update={"id": "OFFLINE-other-threshold", "line": Decimal("4.5")}),
    ):
        try:
            result = fair_value(altered, quotes, scoped, NOW)
        except NoSignal:
            continue
        assert not result.live_execution_allowed


def test_prophetx_recurring_scope_requires_reviewed_exact_nfl_moneyline():
    base, _, _, _ = approved_pilot_case()
    rule_id = "OFFLINE-reviewed-prophetx-nfl-winner-rule"
    market = base.model_copy(update={
        "sport": "football", "league": "NFL", "market_type": "moneyline",
        "participants": ("Team A", "Team B"), "outcomes": ("Team A", "Team B"),
        "player": None, "player_id": None, "statistic": None, "line": None,
        "settlement": base.settlement.model_copy(update={"period": "FULL", "overtime": "included"}),
        "extensions": {"reviewed_normal_rule_id": rule_id, "normal_outcome_reviewed": True},
    })
    scope = TrustedPilotMarketScope(venue="prophetx", sport="football", league="NFL",
        market_type="moneyline", reviewed_normal_rule_id=rule_id)
    assert scope.matches(market)
    for altered in (
        market.model_copy(update={"outcomes": ("Team A", "Draw")}),
        market.model_copy(update={"player": "Unknown Player"}),
        market.model_copy(update={"extensions": {"normal_outcome_reviewed": True}}),
        market.model_copy(update={"extensions": {"reviewed_normal_rule_id": rule_id,
            "normal_outcome_reviewed": True, "research_only": True}}),
        market.model_copy(update={"settlement": market.settlement.model_copy(update={"period": "1H"})}),
    ):
        assert not scope.matches(altered)
    prop_scope = TrustedPilotMarketScope(venue="prophetx", sport="football", league="NFL",
        market_type="player_prop", statistics=("receptions",), reviewed_normal_rule_id=rule_id)
    prop = market.model_copy(update={"market_type": "player_prop", "player": "Fixture Player",
        "player_id": "fixture-player-id", "statistic": "receptions", "line": Decimal("3.5"),
        "outcomes": ("over", "under")})
    assert prop_scope.matches(prop)
    assert not prop_scope.matches(prop.model_copy(update={"player_id": None}))
    assert not prop_scope.matches(prop.model_copy(update={"settlement": prop.settlement.model_copy(
        update={"period": "1H"})}))
    nba_scope = TrustedPilotMarketScope(venue="prophetx", sport="basketball", league="NBA",
        market_type="player_prop", statistics=("points", "rebounds", "assists"),
        reviewed_normal_rule_id=rule_id)
    nba_prop = prop.model_copy(update={"sport": "basketball", "league": "NBA",
        "statistic": "points", "line": Decimal("24.5")})
    assert nba_scope.matches(nba_prop)
    assert not nba_scope.matches(nba_prop.model_copy(update={"statistic": "shots_on_goal"}))
    with pytest.raises(ValueError, match="ProphetX pilot scope"):
        TrustedPilotMarketScope(venue="prophetx", sport="football", league="NFL",
            market_type="player_prop", statistics=("shots_on_goal",), reviewed_normal_rule_id=rule_id)
    with pytest.raises(ValueError, match="sport and league"):
        TrustedPilotMarketScope(venue="prophetx", sport="basketball", league="NFL",
            market_type="moneyline", reviewed_normal_rule_id=rule_id)
    with pytest.raises(ValueError, match="Novig pilot scope"):
        TrustedPilotMarketScope(venue="novig", sport="football", league="NFL",
            market_type="moneyline", reviewed_normal_rule_id=rule_id)


@pytest.mark.asyncio
async def test_approved_trusted_pilot_emits_one_automatic_native_candidate_only_while_armed():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from evplatform.core.native_runtime import NativeRuntime
    from evplatform.core.risk import RiskEngine
    from evplatform.core.config import RiskConfig
    from evplatform.plugins.native_positive_ev import NativePositiveEV

    market, quotes, book, scoped = approved_pilot_case(utcnow())
    risk = RiskEngine(RiskConfig(max_order_stake=Decimal("1"), live_max_order_stake=Decimal("1"),
        live_stake_authorized=True, stake_sizing="quarter_kelly"))
    risk.auto_trade, risk.kill_switch = True, False
    risk.native_enabled_venues = frozenset({"kalshi"})
    publish = AsyncMock()
    engine = SimpleNamespace(risk=risk, settings=SimpleNamespace(pricing=scoped, mode="production"),
        markets={market.id: market}, quotes={market.id: {q.bookmaker: q for q in quotes}},
        registry=SimpleNamespace(records={"native_positive_ev": SimpleNamespace(
            enabled=True, plugin=NativePositiveEV())}), bus=SimpleNamespace(publish=publish))
    runtime = SimpleNamespace(engine=engine, books={(market.id, "kalshi", "Home"): book},
        candidates={}, signals={}, sizing_cash=lambda venue: Decimal("50"))

    await NativeRuntime.recalculate(runtime, market.id)
    assert len(runtime.candidates) == 1
    assert runtime.candidates[(market.id, "kalshi", "Home")].qualified
    assert [call.args[0].schema_name for call in publish.call_args_list] == [
        "native.signal", "trade.native_candidate"]

    risk.kill_switch = True
    publish.reset_mock()
    await NativeRuntime.recalculate(runtime, market.id)
    assert [call.args[0].schema_name for call in publish.call_args_list] == ["native.signal"]


def test_approved_fixture_pilot_reaches_fee_and_kelly_bounded_native_candidate():
    market, quotes, book, scoped = approved_pilot_case()
    fair = fair_value(market, quotes, scoped, NOW)
    assert fair.execution_eligible and fair.live_execution_allowed
    assert fair.trusted_pilot_margin_probability == Decimal("0.01")
    assert all(ref["age_ms"] is None for ref in fair.references)
    assert all(ref["source_scope_poll_at"] for ref in fair.references)
    candidate = native_opportunity(market, fair, book, scoped,
        max_stake_usd=Decimal("1"), mode="production", stake_sizing="quarter_kelly",
        available_cash_usd=Decimal("50"), now=NOW)
    assert candidate.qualified and candidate.execution_eligible
    assert candidate.intent.reserved_cost_usd <= Decimal("1")
    assert candidate.fair_probability == fair.probabilities["Home"] - Decimal("0.01")
    assert candidate.quote_age_ms is None  # No measured selection observation age.
    assert candidate.native_book_age_ms is not None


@pytest.mark.asyncio
async def test_offline_novig_trusted_pilot_reaches_unsigned_review_without_placing():
    """The approved one-market route uses the shared native calculator and review."""
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    from evplatform.core.manual_native import ManualNativeService
    from evplatform.core.risk import RiskEngine
    from evplatform.core.config import RiskConfig

    market, quotes, _, scoped = approved_pilot_case()
    instrument = NativeInstrument(venue="novig", instrument_id="OFFLINE-novig-market",
        canonical_market_id=market.id, outcome="Home", native_outcome="OFFLINE-outcome",
        quantity_unit="whole_cent_contracts", price_format="probability",
        payout_per_unit_usd="0.01", quantity_step="1", minimum_quantity="1",
        minimum_order_cost_usd="0", order_minimum_verified=True,
        order_minimum_source="OFFLINE whole-contract minimum fixture",
        cash_quantum_usd="0.00001", price_bands=(
            NativePriceBand(minimum="0.001", maximum="0.050", step="0.001"),
            NativePriceBand(minimum="0.055", maximum="0.945", step="0.005"),
            NativePriceBand(minimum="0.950", maximum="0.999", step="0.001")),
        settlement_rules_fingerprint=market.identity(), verified=True, source="OFFLINE exact mapping")
    fee = NativeFeeBound(instrument_fingerprint=instrument_fingerprint(instrument),
        per_unit_usd="0", per_fill_usd="0", fixed_order_usd="0", verified=True,
        source="OFFLINE pregame WHEN_LIVE market fee fixture",
        observed_at=NOW-timedelta(seconds=1), valid_until=NOW+timedelta(minutes=1))
    book = NativeBook(instrument=instrument, market_fingerprint=market.identity(),
        asks=(NativeLevel(price="0.400", quantity="1000"),), fee_bound=fee,
        source_time=NOW-timedelta(milliseconds=100), received_at=NOW,
        revision=1, synchronized=True)
    fair = fair_value(market, quotes, scoped, NOW)
    candidate = native_opportunity(market, fair, book, scoped, max_stake_usd=Decimal("1"),
        mode="production", stake_sizing="quarter_kelly", available_cash_usd=Decimal("25"), now=NOW)
    assert candidate.qualified and candidate.execution_eligible
    assert candidate.intent.instrument.payout_per_unit_usd == Decimal("0.01")
    assert candidate.intent.quantity == candidate.intent.quantity.to_integral_value()
    assert 0 < candidate.intent.reserved_cost_usd <= Decimal("1")
    risk = RiskEngine(RiskConfig())
    risk.kill_switch = True
    preview_check, place, arm = Mock(), AsyncMock(), Mock()
    native = SimpleNamespace(books={(market.id, "novig", "Home"): book},
        build_id="OFFLINE-build", preview_check=preview_check,
        build_order=lambda found, **_: SimpleNamespace(id=found.intent.id,
            account_fingerprint="a" * 64), execute=place)
    engine = SimpleNamespace(native=native, risk=risk, oms=SimpleNamespace(lock=asyncio.Lock()),
        markets={market.id: market}, settings=SimpleNamespace(mode="production", pricing=scoped),
        control=arm)
    service = ManualNativeService(engine)
    service._gate = lambda **_: "OFFLINE-scoped-profile"
    service._fresh_candidate = AsyncMock(return_value=candidate)
    review = await service.preview(market.id, "novig", "Home", Decimal("1"))
    assert review["unsigned"] and review["candidate"]["intent"]["quantity"] == str(candidate.intent.quantity)
    assert (
        Decimal(review["candidate"]["intent"]["limit_price"])
        * Decimal(review["candidate"]["intent"]["quantity"])
        * Decimal("0.01")
    ) <= Decimal("1")
    assert review["source_policy"]["trusted_pilot"]["uncertainty_margin_probability"] == "0.01"
    preview_check.assert_called_once()
    place.assert_not_awaited()
    arm.assert_not_called()
    assert not risk.auto_trade and risk.kill_switch


@pytest.mark.parametrize("mutation", [
    "unknown_status_unapproved", "unknown_age_unapproved", "stale_poll",
    "suspended", "withdrawn", "truncated", "missing_normal_review", "wrong_provider", "wrong_market",
])
def test_pilot_policy_rejects_unreviewed_or_unavailable_source(mutation):
    market, quotes, book, scoped = approved_pilot_case()
    policy = scoped.trusted_pilot_policy
    if mutation == "unknown_status_unapproved":
        policy = policy.model_copy(update={"accept_unknown_active_status": False})
    elif mutation == "unknown_age_unapproved":
        policy = policy.model_copy(update={"accept_unknown_quote_observation_age": False})
    elif mutation == "stale_poll":
        quotes[0] = quotes[0].model_copy(update={"source_scope_poll_at": NOW-timedelta(minutes=1)})
    elif mutation in {"suspended", "withdrawn"}:
        quotes[0] = quotes[0].model_copy(update={"status": mutation})
    elif mutation == "truncated":
        quotes[0] = quotes[0].model_copy(update={"verification_reason": "truncated_response"})
    elif mutation == "missing_normal_review":
        quotes[0] = quotes[0].model_copy(update={"normal_outcome_verified": False})
    elif mutation == "wrong_provider":
        quotes[0] = quotes[0].model_copy(update={"provider": "oddize"})
    else:
        policy = policy.model_copy(update={"exact_market_id": "another-market"})
    scoped = scoped.model_copy(update={"trusted_pilot_policy": policy})
    try:
        fair = fair_value(market, quotes, scoped, NOW)
    except NoSignal:
        return
    assert not fair.execution_eligible and not fair.live_execution_allowed
    with pytest.raises(NoSignal):
        native_opportunity(market, fair, book, scoped, max_stake_usd=Decimal("1"),
            mode="production", now=NOW)


@pytest.mark.asyncio
async def test_approved_fixture_pilot_can_be_previewed_without_placement_or_arming():
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock
    from evplatform.core.manual_native import ManualNativeService
    from evplatform.core.risk import RiskEngine
    from evplatform.core.config import RiskConfig

    market, quotes, book, scoped = approved_pilot_case()
    candidate = native_opportunity(market, fair_value(market, quotes, scoped, NOW),
        book, scoped, max_stake_usd=Decimal("1"), mode="production",
        stake_sizing="quarter_kelly", available_cash_usd=Decimal("50"), now=NOW)
    risk = RiskEngine(RiskConfig())
    risk.kill_switch = True
    preview_check, place = Mock(), AsyncMock()
    native = SimpleNamespace(books={(market.id, "kalshi", "Home"): book},
        build_id="OFFLINE-build", preview_check=preview_check,
        build_order=lambda found, **_: SimpleNamespace(id=found.intent.id,
            account_fingerprint="a" * 64), execute=place)
    engine = SimpleNamespace(native=native, risk=risk, oms=SimpleNamespace(lock=asyncio.Lock()),
        markets={market.id: market}, settings=SimpleNamespace(mode="production", pricing=scoped))
    service = ManualNativeService(engine)
    service._gate = lambda **_: "OFFLINE-scoped-profile"
    service._fresh_candidate = AsyncMock(return_value=candidate)
    proposal = await service.preview(market.id, "kalshi", "Home", Decimal("1"))
    assert proposal["unsigned"] and proposal["candidate"]["fair_probability"] == str(candidate.fair_probability)
    assert proposal["candidate"]["quote_age_ms"] is None
    assert proposal["source_policy"]["quality_mode"] == "trusted_provider"
    assert proposal["source_policy"]["trusted_pilot"]["max_debit_usd"] == "1"
    assert proposal["source_policy"]["quote_observation_age_known"] is False
    preview_check.assert_called_once()
    place.assert_not_awaited()
    assert not risk.auto_trade and risk.kill_switch


def test_trusted_pilot_preflight_requires_armed_automatic_route_and_blocks_prior_attempt():
    from types import SimpleNamespace
    from evplatform.core.native_runtime import NativeRuntime
    from evplatform.core.risk import RiskEngine, RiskRejected
    from evplatform.core.config import RiskConfig

    market, _, _, scoped = approved_pilot_case()
    risk = RiskEngine(RiskConfig())
    risk.kill_switch = True
    account = "a" * 64
    profile = SimpleNamespace(venue="novig", environment="production",
        account_fingerprint=account, market_ids=(market.id,),
        strategy_ids=("native_positive_ev",), validation_pilot=True,
        settlement_policy="full", evidence=(SimpleNamespace(expires_at=utcnow()+timedelta(minutes=1)),),
        digest=lambda: "OFFLINE-profile",
        limits=SimpleNamespace(max_native_order_attempts=1))
    runtime = SimpleNamespace(
        report=lambda *args: SimpleNamespace(safe_to_arm=True, live_trading_ready=True,
                                       sandbox_trading_ready=False),
        profile_for=lambda venue: profile,
        account_snapshots={"novig": SimpleNamespace(cash_available_verified=True,
                                                      balance_usd=Decimal("25"),
                                                      identity_verified=True,
                                                      account_fingerprint=account,
                                                      environment="production",
                                                      geographic_attestation_expires_at=utcnow()+timedelta(minutes=1))},
        manager=SimpleNamespace(adapters={"novig": SimpleNamespace(client=object(), preflight=None)}),
        engine=SimpleNamespace(settings=SimpleNamespace(live_enabled=True, pricing=scoped),
                               markets={market.id: market}, risk=risk))
    base = dict(id="current", venue="novig", mode="production",
        account_fingerprint=account, market_id=market.id,
        strategy="native_positive_ev", reserved_cost=Decimal("0.40"))
    with pytest.raises(RiskRejected, match="authorized attempt only"):
        NativeRuntime.require_preflight(runtime, SimpleNamespace(**base, execution_origin="automatic"))
    risk.auto_trade, risk.kill_switch = True, False
    NativeRuntime.require_preflight(runtime, SimpleNamespace(**base, execution_origin="automatic"))
    assert runtime.manager.adapters["novig"].preflight.native_trading_enabled is True
    runtime.account_snapshots["novig"].geographic_attestation_expires_at = None
    with pytest.raises(RiskRejected, match="geographic eligibility"):
        NativeRuntime.require_preflight(runtime, SimpleNamespace(**base, execution_origin="automatic"))
    runtime.account_snapshots["novig"].geographic_attestation_expires_at = utcnow()+timedelta(minutes=1)
    risk.kill_switch = True
    with pytest.raises(RiskRejected, match="authorized attempt only"):
        NativeRuntime.require_preflight(runtime, SimpleNamespace(**base, execution_origin="automatic"))
    risk.native_orders["prior"] = SimpleNamespace(**(base | {"id": "prior"}),
                                                    execution_origin="manual")
    with pytest.raises(RiskRejected, match="already exists"):
        NativeRuntime.require_preflight(runtime, SimpleNamespace(**base, execution_origin="manual"),
                                        preview_only=True)


def test_recurring_trusted_preflight_accepts_cash_sized_order_above_one_unit_only_when_armed():
    from types import SimpleNamespace
    from evplatform.core.native_runtime import NativeRuntime
    from evplatform.core.risk import RiskEngine, RiskRejected
    from evplatform.core.config import RiskConfig

    market, _, _, scoped = approved_pilot_case()
    reviewed = scoped.trusted_pilot_policy.model_dump()
    reviewed.update(recurring_authorization=True, maximum_native_attempts=None,
        automatic_followups=True, max_debit_usd=Decimal("25"), max_loss_usd=Decimal("25"))
    policy = TrustedPilotPolicy.model_validate(reviewed)
    scoped = scoped.model_copy(update={"trusted_pilot_policy": policy})
    risk = RiskEngine(RiskConfig(available_cash_only=True, stake_sizing="quarter_kelly",
                                 max_order_stake=None, live_stake_authorized=True))
    account = "a" * 64
    profile = SimpleNamespace(venue="novig", environment="production",
        account_fingerprint=account, market_ids=(market.id,),
        strategy_ids=("native_positive_ev",), validation_pilot=False,
        settlement_policy="full", evidence=(SimpleNamespace(expires_at=utcnow()+timedelta(minutes=1)),),
        digest=lambda: "OFFLINE-recurring-profile",
        limits=SimpleNamespace(max_native_order_attempts=None, available_cash_only=True,
                               stake_sizing="quarter_kelly", max_order_stake_usd=None))
    runtime = SimpleNamespace(report=lambda *args: SimpleNamespace(safe_to_arm=True,
        live_trading_ready=True, sandbox_trading_ready=False),
        profile_for=lambda venue: profile,
        account_snapshots={"novig": SimpleNamespace(cash_available_verified=True,
            balance_usd=Decimal("25"), identity_verified=True, account_fingerprint=account,
            environment="production",
            geographic_attestation_expires_at=utcnow()+timedelta(minutes=1))},
        manager=SimpleNamespace(adapters={"novig": SimpleNamespace(client=object(), preflight=None)}),
        engine=SimpleNamespace(settings=SimpleNamespace(live_enabled=True, pricing=scoped),
                               markets={market.id: market}, risk=risk))
    order = SimpleNamespace(id="recurring", venue="novig", mode="production",
        account_fingerprint=account, market_id=market.id, strategy="native_positive_ev",
        reserved_cost=Decimal("6"), execution_origin="automatic")
    with pytest.raises(RiskRejected, match="Recurring trusted-provider source policy"):
        NativeRuntime.require_preflight(runtime, order)
    risk.auto_trade, risk.kill_switch = True, False
    NativeRuntime.require_preflight(runtime, order)
    assert runtime.manager.adapters["novig"].preflight.native_trading_enabled is True
    risk.kill_switch = True
    with pytest.raises(RiskRejected, match="Recurring trusted-provider source policy"):
        NativeRuntime.require_preflight(runtime, order)


@pytest.mark.asyncio
async def test_provisional_native_estimate_reaches_reference_dashboard_without_order(tmp_path):
    market, quotes, book = trusted_native_fixture(utcnow())
    engine = Engine(Settings(_env_file=None, mode="production", live_enabled=False,
        auto_trade=False, sample_feed_enabled=False, database_path=str(tmp_path / "native-research.db"),
        pricing=config()))
    engine.risk.kill_switch = True
    await engine.process_event(Envelope(schema_name="sports.market", source="test", payload=market))
    for quote in quotes:
        await engine.process_event(Envelope(schema_name="odds.quote", source="test", payload=quote))
    await engine.process_event(Envelope(schema_name="exchange.native_book", source="test", payload=book))
    state = engine.state()
    assert len(state["reference_estimates"]) == 1
    estimate = state["reference_estimates"][0]
    assert estimate["status"] == "PROVISIONAL POSITIVE EV"
    assert estimate["native_research_estimates"][0]["execution_eligible"] is False
    assert not state["native_opportunities"] and not state["native_orders"]
    assert not state["auto_trade"] and state["kill_switch"]


def test_source_prop_uses_exact_event_metadata_and_rejects_unknown_period():
    from evplatform.core.profile_import import STATISTICS
    assert STATISTICS["player_points"] == "points"
    profile = ReferenceProfile(selectors=(ProfileSelector(sport="basketball", league="NBA",
        market_type="player_prop", statistic="points"),),
        weights_percent={"pinnacle": Decimal(20), "fanduel": Decimal(50),
                         "draftkings": Decimal(20), "caesars": Decimal(10)},
        required_any_anchor=("fanduel",), min_distinct_books=3,
        status="research_prior", live_execution_allowed=False)
    policy = config().model_copy(update={"reference_profiles": {"props": profile},
                                 "enabled_reference_profiles": ["props"]})
    mapper = VerifiedProviderMapper()
    mapper.research_projector = TrustedSourceProjector(lambda: policy)
    mapper(frame())  # Same provider event ID supplies exact teams and start.
    rows = []
    for book in ("fanduel", "pinnacle", "draftkings"):
        rows.append({"event_id": "event-1", "bookmaker": book, "market_key": "player_points",
            "player": "Named Player", "period": "FULL", "line": 24.5,
            "over_price": -110, "under_price": -110, "last_update": int((NOW-timedelta(seconds=1)).timestamp()*1000)})
    prop = ProviderEnvelope("parlay_rest", "props_snapshot", rows, NOW, 1,
        published_at=NOW, metadata={"sport": "basketball_nba"})
    mapper(prop)
    assert len(mapper.research_updates) == 3
    market = mapper.research_updates[0][0]
    from evplatform.core.native_research import novig_prop_matches
    native_event_id = "22222222-2222-4222-8222-222222222222"
    native_market_id = "11111111-1111-4111-8111-111111111111"
    native = {"marketId": native_market_id, "eventId": native_event_id,
        "marketType": "POINTS", "status": "OPEN", "voids": "FMV", "strike": "24.5",
        "startsTs": int(market.start_time.timestamp() * 1000),
        "description": "Named Player 24.5 POINTS",
        "outcomes": [{"name": "Over 24.5", "outcomeId": "33333333-3333-4333-8333-333333333333", "status": "TBD"},
                     {"name": "Under 24.5", "outcomeId": "44444444-4444-4444-8444-444444444444", "status": "TBD"}]}
    native_event = {"eventId": native_event_id, "league": "NBA", "status": "OPEN_PREGAME",
        "startsTs": native["startsTs"], "description": "Away @ Home"}
    assert len(novig_prop_matches(market, [native], {native_event_id: native_event},
                                  observed_at=NOW)) == 2
    assert market.player == "Named Player" and market.line == Decimal("24.5")
    assert len({m.id for m, _ in mapper.research_updates}) == 1
    fair = fair_value(market, [q for _, q in mapper.research_updates], policy, NOW)
    assert len(fair.references) == 3 and not fair.execution_eligible
    with pytest.raises(NoSignal, match="Insufficient"):
        fair_value(market, [q.model_copy(update={"source_stored_at": NOW-timedelta(minutes=1)})
                            for _, q in mapper.research_updates], policy, NOW)
    mapper(ProviderEnvelope("parlay_rest", "props_snapshot", [{**rows[0], "period": "UNKNOWN"}],
        NOW, 2, published_at=NOW, metadata={"sport": "basketball_nba"}))
    assert mapper.research_updates == []


@pytest.mark.asyncio
async def test_research_reference_reaches_dashboard_without_native_order(tmp_path, monkeypatch):
    now = utcnow()
    engine = Engine(Settings(_env_file=None, mode="production", live_enabled=False,
        auto_trade=False, sample_feed_enabled=False, database_path=str(tmp_path / "research.db"),
        pricing=config()))
    engine.risk.kill_switch = True
    _, updates = projected(frame(received=now, start=now+timedelta(hours=1)))
    for market, quote in updates:
        await engine.process_event(Envelope(schema_name="sports.market", source="test", payload=market))
        await engine.process_event(Envelope(schema_name="odds.quote", source="test", payload=quote))
    state = engine.state()
    assert len(state["reference_estimates"]) == 1
    assert state["reference_estimates"][0]["quality_mode"] == "trusted_provider"
    assert state["reference_estimates"][0]["native_market_matched"] is False
    assert state["opportunities"] == [] and state["native_opportunities"] == []
    assert state["auto_trade"] is False and state["kill_switch"] is True
    assert state["orders"] == [] and state["native_orders"] == []
    monkeypatch.setattr("evplatform.engine.utcnow", lambda: now+timedelta(seconds=31))
    assert engine.public_reference_estimates() == []
