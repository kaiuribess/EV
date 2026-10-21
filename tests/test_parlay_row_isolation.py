"""Malformed-row isolation without preserving unsafe cached companions."""
from dataclasses import replace
from datetime import timedelta

import pytest

from evplatform.integrations.base import ProviderEnvelope
from evplatform.integrations.normalization import VerifiedOfferMapping, VerifiedProviderMapper, extract_offers
from evplatform.integrations import normalization
from test_providers import NOW, sample_row, market


def batch(*rows, provider="parlay_ws", **body):
    return ProviderEnvelope(provider, "props_snapshot" if provider == "parlay_rest" else "odds_update",
        list(rows) if provider == "parlay_rest" else {"data": list(rows), **body}, NOW, 1)


def setup(rows, provider="parlay_ws"):
    mapper = VerifiedProviderMapper()
    for index, offer in enumerate(extract_offers(batch(*rows, provider=provider))):
        mapper.register(VerifiedOfferMapping(signature=offer.signature,
            market=market().model_copy(update={"id": f"offline-{index}", "event_id": offer.event_id}),
            outcome_map={"over": "over", "under": "under"}, bookmaker=offer.bookmaker,
            verified=True, evidence="Offline regression fixture only; no live mapping"))
    mapper(batch(*rows, provider=provider))
    return mapper


@pytest.mark.parametrize("provider", ["parlay_ws", "parlay_rest"])
@pytest.mark.parametrize("different", [{"bookmaker": "fanduel"}, {"event_id": "other-event"},
                                     {"market_key": "player_rebounds"}])
def test_bad_quote_invalidates_only_identified_scope_and_delivers_unrelated_updates(provider, different):
    original, other = sample_row(), sample_row(**different)
    mapper = setup([original, other], provider)
    result = mapper(batch({**original, "over_price": "broken"}, {**other, "over_price": -120}, provider=provider))
    by_id = {m.id: q for m, q in result}
    assert by_id["offline-0"].status == "suspended"
    assert by_id["offline-0"].source_time == NOW-timedelta(seconds=4)
    assert not by_id["offline-0"].observation_complete
    assert by_id["offline-1"].status == "active"
    assert by_id["offline-1"].decimal_odds["over"] != by_id["offline-1"].decimal_odds["under"]
    assert not by_id["offline-1"].market_status_verified
    assert mapper.quarantine[f"invalid:{provider}"]["affected_source_scope"]["event_id"] == original["event_id"]


@pytest.mark.parametrize("reverse", [False, True])
def test_valid_row_in_same_batch_cannot_resurrect_corrupted_scope(reverse):
    original = sample_row()
    mapper = setup([original])
    rows = [original, {**original, "over_price": "NaN"}]
    result = mapper(batch(*reversed(rows) if reverse else rows))
    assert len(result) == 1 and result[0][1].status == "suspended"


@pytest.mark.parametrize("bad", [None, "invalid", {"over_price": "broken"},
    {"bookmaker": None, "event_id": [], "market_key": 3, "over_price": "broken"}])
def test_unidentifiable_failure_invalidates_all_provider_scope(bad):
    rows = [sample_row(), sample_row(bookmaker="fanduel", event_id="other")]
    mapper = setup(rows)
    result = mapper(batch(*rows, bad))
    assert len(result) == 2 and all(q.status == "suspended" for _, q in result)


def test_partial_identity_broadens_scope_without_guessing_player_or_line():
    rows = [sample_row(), sample_row(event_id="other"), sample_row(bookmaker="fanduel")]
    mapper = setup(rows)
    result = mapper(batch({"bookmaker": "pinnacle", "over_price": "bad"}, *rows))
    by_id = {m.id: q for m, q in result}
    assert by_id["offline-0"].status == by_id["offline-1"].status == "suspended"
    assert by_id["offline-2"].status == "active"


def test_corruption_discards_cached_partial_outcomes_before_next_update():
    def side(label, **changes):
        return {"bookmaker": "pinnacle", "event_id": "e", "market_key": "h2h",
                "team": label, "price": -110, **changes}
    mapper = VerifiedProviderMapper()
    mapper(batch(side("A")))
    assert mapper._partial
    mapper(batch(side("A", price="bad")))
    assert not mapper._partial
    mapper(batch(side("B")))
    assert all(set(parts) == {"B"} for parts in mapper._partial.values())


def test_invalidated_verified_quote_needs_newer_evidence_to_recover(monkeypatch):
    real_extract = normalization.extract_offers
    # Offline active-status assertion to test the recovery gate, not a provider guarantee.
    monkeypatch.setattr(normalization, "extract_offers", lambda raw: [
        replace(o, market_status_verified=True) for o in real_extract(raw)])
    row = sample_row(last_observed=(NOW-timedelta(seconds=1)).isoformat(),
                     last_update_type="collector_observation")
    mapper = setup([row])
    mapper(batch({**row, "over_price": "bad"}))
    (_, replay), = mapper(batch(row))
    assert not replay.observation_complete
    (_, recovered), = mapper(batch({**row, "last_observed": NOW.isoformat()}))
    assert recovered.observation_complete and recovered.market_status_verified


def test_complete_replacement_still_invalidates_omitted_quotes():
    rows = [sample_row(), sample_row(bookmaker="fanduel")]
    mapper = setup(rows)
    raw = replace(batch(rows[0]), kind="initial_state", snapshot_complete=True)
    by_id = {m.id: q for m, q in mapper(raw)}
    assert by_id["offline-0"].status == "active" and by_id["offline-1"].status == "suspended"


def test_stream_discovery_preserves_stated_start_and_sport_without_inventing_them():
    raw = batch(sample_row(commence_time="2026-10-21T23:00:00Z"))
    raw = replace(raw, metadata={"sport": "basketball_nba"})
    offer, = extract_offers(raw)
    assert offer.context["scheduled_start"] == "2026-10-21T23:00:00Z"
    assert offer.context["event_sport"] == "basketball_nba"
    missing, = extract_offers(batch(sample_row()))
    assert "scheduled_start" not in missing.context and "event_sport" not in missing.context
    changed, = extract_offers(replace(raw, payload={"data": [sample_row(commence_time="2026-10-22T23:00:00Z")]}))
    assert changed.signature != offer.signature  # A changed schedule requires renewed mapping review.
