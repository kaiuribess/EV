"""Offline negative-status precedence; never certifies upstream active status."""
import pytest

from test_parlay_row_isolation import batch, setup, sample_row


@pytest.mark.parametrize("provider", ["parlay_ws", "parlay_rest"])
def test_suspended_status_without_prices_revokes_cached_quote(provider):
    original = sample_row()
    mapper = setup([original], provider)
    notice = {key: original[key] for key in ("event_id", "bookmaker", "market_key")}
    notice["status"] = "suspended"
    result = mapper(batch(notice, provider=provider))
    assert len(result) == 1 and result[0][1].status == "suspended"
    assert all(quote.status != "active" for _, _, quote, _ in mapper._last.values())


@pytest.mark.parametrize("provider", ["parlay_ws", "parlay_rest"])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("adverse", [{"status": "suspended"}, {"suspended": True},
    {"available": False}, {"action": "remove"}, {"over_price": None, "under_price": None}])
def test_adverse_row_dominates_same_delivery_regardless_of_order(provider, reverse, adverse):
    original = sample_row()
    mapper = setup([original], provider)
    rows = [original, {**original, **adverse}]
    result = mapper(batch(*reversed(rows) if reverse else rows, provider=provider))
    assert result and all(quote.status == "suspended" for _, quote in result)
    assert not any(quote.status == "active" for _, _, quote, _ in mapper._last.values())


def test_suspension_isolated_to_its_explicit_event_book_market():
    original, unrelated = sample_row(), sample_row(event_id="other-event")
    mapper = setup([original, unrelated])
    result = mapper(batch(original, unrelated, {**original, "status": "suspended"}))
    quotes = {market.id: quote for market, quote in result}
    assert quotes["offline-0"].status == "suspended"
    assert quotes["offline-1"].status == "active"
    assert not quotes["offline-1"].market_status_verified


def test_nullable_moneyline_columns_do_not_withdraw_a_complete_prop():
    original = sample_row(home_ml=None, away_ml=None)
    mapper = setup([original])
    result = mapper(batch(original))
    assert len(result) == 1 and result[0][1].status == "active"
    assert not result[0][1].market_status_verified
