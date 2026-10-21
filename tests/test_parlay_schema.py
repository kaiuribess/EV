"""Offline extraction of the row variants in Parlay's public documentation.

These fixtures validate normalization, not account access or live connectivity.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

import pytest

from evplatform.core.models import Market, Settlement
from evplatform.integrations.base import ProviderEnvelope
from evplatform.integrations.normalization import VerifiedOfferMapping, VerifiedProviderMapper, extract_offers


RECEIVED = datetime(2026, 5, 13, 23, 24, 25, tzinfo=UTC)
SOURCE = datetime(2026, 5, 13, 23, 24, 18, tzinfo=UTC)


def documented_prop(**updates):
    # https://parlay-api.com/docs/websocket/player-props#row-shape-props-specifically
    return {
        "event_id": "2026-05-13_Boston_Celtics_New_York_Knicks",
        "bookmaker": "draftkings", "market_key": "player_points",
        "outcome": "Jayson Tatum Over", "player": "Jayson Tatum", "line": 28.5,
        "price_american": -118, "price_decimal": 1.847,
        "last_update": "2026-05-13T23:24:18Z", **updates,
    }


def envelope(*rows, provider="parlay_ws", kind="odds_update"):
    return ProviderEnvelope(
        provider, kind, list(rows) if provider == "parlay_rest" else {"data": list(rows)},
        RECEIVED, 1_000_000, published_at=RECEIVED - timedelta(seconds=1),
    )


@pytest.mark.parametrize("provider,kind", [("parlay_ws", "odds_update"), ("parlay_rest", "props_snapshot")])
def test_documented_prop_preserves_source_outcome_and_identity(provider, kind):
    raw = envelope(documented_prop(), provider=provider, kind=kind)
    offer, = extract_offers(raw)
    assert offer.bookmaker == "draftkings"
    assert offer.event_id == "2026-05-13_Boston_Celtics_New_York_Knicks"
    assert offer.market_key == "player_points"
    assert offer.player_key == "Jayson Tatum"
    assert offer.line == "28.5"
    assert offer.outcome_points == {"Jayson Tatum Over": "28.5"}
    # Use the exact American conversion when the companion decimal is rounded.
    assert offer.prices == {"Jayson Tatum Over": 1 + Decimal(100) / 118}
    assert offer.observed_at == SOURCE
    assert offer.provider_time == raw.published_at


@pytest.mark.parametrize("prices,expected", [
    ({"price_american": -200}, "1.5"),
    ({"price_american": "+150"}, "2.5"),
    ({"price_decimal": "1.91"}, "1.91"),
    ({"price_american": None, "price_decimal": "2.25"}, "2.25"),
    ({"price_american": 100, "price_decimal": 2}, "2"),
    ({"price": -200, "price_american": -200, "price_decimal": 1.5}, "1.5"),
])
def test_explicit_price_fields_keep_their_documented_format(prices, expected):
    row = documented_prop()
    row.pop("price_american")
    row.pop("price_decimal")
    row.update(prices)
    assert extract_offers(envelope(row))[0].prices == {"Jayson Tatum Over": Decimal(expected)}


@pytest.mark.parametrize("prices", [{}, {"price": None}, {"price_american": None, "price_decimal": None}])
def test_missing_or_null_prices_do_not_fabricate_an_offer(prices):
    row = documented_prop()
    row.pop("price_american")
    row.pop("price_decimal")
    row.update(prices)
    assert extract_offers(envelope(row)) == []


@pytest.mark.parametrize("prices", [
    {"price_american": 0}, {"price_american": 99}, {"price_american": -99},
    {"price_american": -118.5}, {"price_american": "NaN"}, {"price_american": True},
    {"price_decimal": 0}, {"price_decimal": 1}, {"price_decimal": -118},
    {"price_decimal": "Infinity"}, {"price_decimal": "void"}, {"price_decimal": False},
    {"price_american": -118, "price_decimal": "void"},
])
def test_invalid_prices_fail_closed(prices):
    row = documented_prop()
    row.pop("price_american")
    row.pop("price_decimal")
    row.update(prices)
    with pytest.raises((ValueError, InvalidOperation)):
        extract_offers(envelope(row))
    mapper = VerifiedProviderMapper()
    assert mapper(envelope(row)) == []
    assert mapper.quarantine["invalid:parlay_ws"]["reason"] == "Invalid provider payload or price"


@pytest.mark.parametrize("prices", [
    {"price": -110, "price_american": -118},
    {"price_american": -118, "price_decimal": 1.848},
    {"price_american": -118, "price_decimal": 2.18},
])
def test_conflicting_price_representations_are_rejected(prices):
    row = documented_prop()
    row.pop("price_american")
    row.pop("price_decimal")
    row.update(prices)
    with pytest.raises(ValueError, match="Conflicting Parlay"):
        extract_offers(envelope(row))


@pytest.mark.parametrize("extra", [{"over_price": -118}, {"home_ml": -118}])
def test_conflicting_row_shapes_are_not_silently_prioritized(extra):
    with pytest.raises(ValueError, match="Conflicting Parlay row shapes"):
        extract_offers(envelope(documented_prop(**extra)))


def observed_moneyline_alias(**updates):
    # Shape observed on the authenticated stream October 21, 2026. Event and
    # participant below are fixture identities, not a live tradable selection.
    return {"event_id": "offline-alias", "bookmaker": "draftkings", "kind": "game",
        "market_key": "h2h", "market": "Moneyline", "team": "Fixture Team",
        "price": -180, "over_price": -180, "under_price": None, "line": None, **updates}


@pytest.mark.parametrize("price", [-180, 150, -130, 110, 124])
def test_observed_game_moneyline_duplicate_price_is_one_named_outcome(price):
    row = observed_moneyline_alias(price=price, over_price=price)
    offer, = extract_offers(envelope(row))
    assert offer.prices == {"Fixture Team": 1 + (Decimal(price)/100 if price > 0 else Decimal(100)/abs(price))}
    assert offer.outcome_points == {"Fixture Team": None}
    assert not offer.observation_verified and not offer.market_status_verified
    assert VerifiedProviderMapper()(envelope(row)) == []


@pytest.mark.parametrize("change", [
    {"over_price": -181}, {"under_price": 150}, {"kind": "prop"},
    {"market_key": "player_points"}, {"line": 1.5}, {"total": 100},
    {"player": "Fixture Player"}, {"outcome": "Different Team"},
    {"home_ml": -180}, {"price_decimal": "3"}, {"team": ""},
])
def test_duplicate_alias_exception_never_hides_a_conflicting_market_or_price(change):
    with pytest.raises(ValueError, match="Conflicting Parlay"):
        extract_offers(envelope(observed_moneyline_alias(**change)))


@pytest.mark.parametrize("line", ["-4.5", "4.5", "0", "-2", "1.25"])
def test_observed_spread_price_alias_keeps_the_named_team_and_signed_handicap(line):
    # Authenticated October 21 NBA stream shape; identities are offline substitutes.
    row = observed_moneyline_alias(market_key="spreads", market="Spread", line=line)
    offer, = extract_offers(envelope(row))
    assert offer.prices == {"Fixture Team": 1+Decimal(100)/180}
    assert offer.line == str(Decimal(line).normalize())
    assert offer.outcome_points == {"Fixture Team": str(Decimal(line).normalize())}
    assert not offer.market_status_verified and not offer.observation_complete
    assert VerifiedProviderMapper()(envelope(row)) == []


@pytest.mark.parametrize("changes", [{"line": None}, {"line": "NaN"}, {"line": True}, {"line": "bad"},
    {"over_price": -181}, {"under_price": 150}, {"market_key": "totals"}, {"kind": "prop"},
    {"player": "Player"}, {"total": 220.5}, {"outcome": "Different Team"}])
def test_spread_alias_never_hides_conflicting_prices_or_invalid_handicaps(changes):
    row = observed_moneyline_alias(market_key="spreads", line="-4.5")
    with pytest.raises(ValueError, match="Conflicting Parlay"):
        extract_offers(envelope({**row, **changes}))


@pytest.mark.parametrize("source_time,expected", [
    ("2026-05-13T23:24:18Z", SOURCE),
    ("2026-05-13T16:24:18-07:00", SOURCE),
    (int(SOURCE.timestamp() * 1000), SOURCE),
    (None, None), ("invalid", None), ("2026-05-13T23:24:18", None),
])
def test_source_timestamp_accepts_iso_or_milliseconds_without_receipt_fallback(source_time, expected):
    offer, = extract_offers(envelope(documented_prop(last_update=source_time)))
    assert offer.observed_at == expected
    assert offer.observed_at != RECEIVED
    assert offer.provider_time == RECEIVED - timedelta(seconds=1)


@pytest.mark.parametrize("row,prices,points", [
    ({"over_price": -200, "under_price": 150, "line": 28.5},
     {"over": Decimal("1.5"), "under": Decimal("2.5")}, {"over": "28.5", "under": "28.5"}),
    ({"home_ml": -200, "away_ml": 150, "draw_ml": 300},
     {"home": Decimal("1.5"), "away": Decimal("2.5"), "draw": Decimal("4")},
     {"home": None, "away": None, "draw": None}),
    ({"draw_ml": 300}, {"draw": Decimal("4")}, {"draw": None}),
    ({"team": "Boston Celtics", "price": -200},
     {"Boston Celtics": Decimal("1.5")}, {"Boston Celtics": None}),
])
def test_asyncapi_legacy_rows_remain_supported(row, prices, points):
    row = {"event_id": "native-event", "bookmaker": "example", "market_key": "example", **row}
    offer, = extract_offers(envelope(row))
    assert offer.prices == prices
    assert offer.outcome_points == points


@pytest.mark.parametrize("label", [None, "", "  ", 1])
def test_explicit_outcome_must_be_a_source_label(label):
    with pytest.raises(ValueError, match="Missing Parlay outcome label"):
        extract_offers(envelope(documented_prop(outcome=label)))


def test_source_outcome_wins_over_generic_side_without_name_inference():
    row = documented_prop(outcome="Jayson Tatum Under", side="under")
    offer, = extract_offers(envelope(row))
    assert set(offer.prices) == {"Jayson Tatum Under"}
    assert offer.context["side"] == "under"


def test_documented_prop_rows_still_require_verified_mapping():
    raw = envelope(documented_prop(), documented_prop(outcome="Jayson Tatum Under"))
    mapper = VerifiedProviderMapper()
    assert mapper(raw) == []
    assert len(mapper.quarantine) == 1
    assert all(record["reason"] == "Verified event/player/line/settlement mapping required"
               for record in mapper.quarantine.values())
    assert {tuple(record["identity"]["outcome_points"]) for record in mapper.quarantine.values()} == {
        ("Jayson Tatum Over", "Jayson Tatum Under"),
    }


def paired_mapper(*, canonical_line="28.5", provider="parlay_ws"):
    over = documented_prop()
    under = documented_prop(outcome="Jayson Tatum Under", last_update=(SOURCE + timedelta(seconds=2)).isoformat())
    # Derive the source signature from an untrusted observation, then explicitly
    # approve that exact source structure for this offline test only.
    mapper = VerifiedProviderMapper()
    kind = "props_snapshot" if provider == "parlay_rest" else "odds_update"
    raw = envelope(over, under, provider=provider, kind=kind)
    assert mapper(raw) == []
    assert len(mapper.quarantine) == 1
    signature = next(iter(mapper.quarantine))
    market = Market(
        id="verified-points", event_id="verified-fixture", event_name="Boston vs New York",
        sport="basketball", league="NBA", participants=("Boston Celtics", "New York Knicks"),
        start_time=RECEIVED + timedelta(hours=1), market_type="player_prop",
        player="Jayson Tatum", player_id="verified-player", statistic="points",
        line=Decimal(canonical_line), outcomes=("over", "under"),
        settlement=Settlement(scope="player", participation="must_play"),
    )
    mapper.register(VerifiedOfferMapping(
        signature=signature, market=market,
        outcome_map={"Jayson Tatum Over": "over", "Jayson Tatum Under": "under"},
        bookmaker="draftkings", verified=True,
        evidence="Offline fixture player, threshold, period, and settlement explicitly verified",
    ))
    mapper(replace(raw, kind="feed_invalidated", payload={}))
    return mapper, over, under


def test_exact_prop_sides_can_fill_a_complete_verified_mapping_across_frames():
    mapper, over, under = paired_mapper()
    assert mapper(envelope(over)) == []
    result = mapper(envelope(under))
    assert len(result) == 1
    _, quote = result[0]
    assert set(quote.decimal_odds) == {"over", "under"}
    assert quote.source_time == SOURCE  # oldest side, rather than latest update
    assert quote.provider_time == RECEIVED - timedelta(seconds=1)
    assert quote.received_at == RECEIVED
    assert quote.status == "active"


def test_rest_prop_snapshot_pairs_exact_sides_into_one_verified_quote():
    mapper, over, under = paired_mapper(provider="parlay_rest")
    raw = envelope(over, under, provider="parlay_rest", kind="props_snapshot")
    (market, quote), = mapper(raw)
    assert market.id == "verified-points"
    assert quote.provider == "parlay_rest"
    assert set(quote.decimal_odds) == {"over", "under"}
    assert quote.source_time == SOURCE  # Both sides' clocks remain required.
    assert quote.provider_time == raw.published_at
    assert quote.received_at == raw.received_at
    assert quote.status == "active"


def test_rest_prop_pairing_never_retains_sides_between_polling_snapshots():
    mapper, over, under = paired_mapper(provider="parlay_rest")
    assert mapper(envelope(over, provider="parlay_rest", kind="props_snapshot")) == []
    assert mapper(envelope(under, provider="parlay_rest", kind="props_snapshot")) == []
    assert mapper.mapped_count == 0
    # A later incomplete poll also invalidates the previously complete quote.
    mapper(envelope(over, under, provider="parlay_rest", kind="props_snapshot"))
    invalidated = mapper(envelope(over, provider="parlay_rest", kind="props_snapshot"))
    assert invalidated
    assert all(quote.status == "suspended" for _, quote in invalidated)


@pytest.mark.parametrize("changed", [
    {"line": 29.5}, {"player": "Jayson Tatum Jr."}, {"bookmaker": "other-book"},
    {"event_id": "other-game"}, {"market_key": "player_rebounds"}, {"period": "first_half"},
    {"settlement": "different"},
])
def test_rest_prop_pairing_requires_exact_source_identity_within_snapshot(changed):
    mapper, over, under = paired_mapper(provider="parlay_rest")
    raw = envelope(over, {**under, **changed}, provider="parlay_rest", kind="props_snapshot")
    assert mapper(raw) == []
    assert mapper.mapped_count == 0


@pytest.mark.parametrize("changed", [
    {"line": 29.5}, {"player": "Jayson Tatum Jr."}, {"bookmaker": "other-book"},
    {"event_id": "other-game"}, {"market_key": "player_rebounds"}, {"period": "first_half"},
    {"settlement": "different"},
])
def test_prop_pairing_never_crosses_source_identity(changed):
    mapper, over, under = paired_mapper()
    assert mapper(envelope(over)) == []
    assert mapper(envelope({**under, **changed})) == []
    assert mapper.mapped_count == 0


def test_paired_prop_mapping_still_rejects_incorrect_canonical_threshold():
    mapper, over, under = paired_mapper(canonical_line="29.5")
    assert mapper(envelope(over, under)) == []
    assert any(record["reason"] == "Source and canonical thresholds differ"
               for record in mapper.quarantine.values())


def test_unknown_clock_on_either_prop_side_stays_unknown():
    mapper, over, under = paired_mapper()
    _, quote = mapper(envelope({**over, "last_update": None}, under))[0]
    assert quote.source_time is None
    assert quote.fresh(30_000, RECEIVED) is False


def test_invalidation_clears_partial_prop_sides_before_reconnect():
    mapper, over, under = paired_mapper()
    assert mapper(envelope(over)) == []
    mapper(replace(envelope(), kind="feed_invalidated", payload={}))
    assert mapper(envelope(under)) == []
    assert mapper.mapped_count == 0


@pytest.mark.parametrize("updates", [
    {"line": None}, {"line": "invalid"}, {"market_key": "player_first_basket"},
    {"market_key": "player_anytime_td"}, {"outcome": "Over"},
])
def test_non_threshold_and_unidentified_source_labels_are_not_paired(updates):
    over = documented_prop(**updates)
    under = documented_prop(**{**updates, "outcome": "Jayson Tatum Under"})
    mapper = VerifiedProviderMapper()
    assert mapper(envelope(over, under)) == []
    assert len(mapper.quarantine) == 2


@pytest.mark.parametrize("market_time", [None, "invalid", SOURCE.isoformat()])
@pytest.mark.parametrize("topped_up", [False, True])
def test_rest_book_poll_clock_never_refreshes_missing_or_old_market_clock(market_time, topped_up):
    # Official docs and cookbook disagree on block last_update_ms semantics.
    # A fresh block/verification/receipt clock cannot date the actual offer.
    source_market = {"key": "h2h", "last_update": market_time,
                     "outcomes": [{"name": "A", "price": "1.8"}, {"name": "B", "price": "2.1"}]}
    row = {"id": "fixture-one", "sport_key": "basketball_nba", "home_team": "A", "away_team": "B",
           "bookmakers": [{"key": "pinnacle", "last_update_ms": int(RECEIVED.timestamp() * 1000),
                           "last_update": RECEIVED.isoformat(), "verified_at": RECEIVED.isoformat(),
                           "is_current": True, "topped_up": topped_up, "markets": [source_market]}]}
    raw = envelope(row, provider="parlay_rest", kind="snapshot")
    offer, = extract_offers(raw)
    expected_time = SOURCE if market_time == SOURCE.isoformat() else None
    assert offer.observed_at == expected_time
    market = Market(id="fixture-one-moneyline", event_id="fixture-one", event_name="A vs B",
                    sport="basketball", league="nba", participants=("A", "B"), start_time=RECEIVED,
                    market_type="moneyline", outcomes=("home", "away"), settlement=Settlement())
    mapper = VerifiedProviderMapper([VerifiedOfferMapping(signature=offer.signature, market=market,
        outcome_map={"A": "home", "B": "away"}, bookmaker="pinnacle", verified=True,
        evidence="Offline fixture mapping for source-clock regression only")])
    (_, quote), = mapper(raw)
    assert quote.source_time == expected_time
    assert quote.received_at == RECEIVED
    assert not quote.fresh(5000, RECEIVED)
