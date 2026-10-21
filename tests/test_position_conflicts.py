from datetime import timedelta
from decimal import Decimal as D

import pytest

from evplatform.core.config import RiskConfig
from evplatform.core.conflicts import metadata_consistent, risk_family
from evplatform.core.models import Order, OrderStatus, utcnow
from evplatform.core.pricing import fair_value, opportunity
from evplatform.core.risk import RiskEngine, RiskRejected
from evplatform.core.storage import Storage


@pytest.fixture
def risk():
    engine = RiskEngine(RiskConfig())
    engine.auto_trade = True
    return engine


@pytest.fixture
def candidate(market, quotes, book, pricing):
    return opportunity(market, fair_value(market, quotes, pricing), book, pricing).model_copy(
        update={"max_quantity": 1})


def fresh(candidate, book, market, *, outcome=None, venue=None, strategy="second_strategy"):
    outcome = outcome or candidate.outcome
    venue = venue or candidate.venue
    return (
        candidate.model_copy(update={"market_id": market.id, "outcome": outcome, "venue": venue,
                                     "strategy": strategy, "calculated_at": utcnow()}),
        book.model_copy(update={"market_id": market.id, "outcome": outcome, "venue": venue,
                                "source_time": utcnow()}),
    )


def test_opposing_contract_blocked_across_all_venues_strategies_and_canonical_aliases(
        risk, candidate, market, book):
    order = risk.reserve(candidate, market, book, "paper")
    assert metadata_consistent(order)
    # A separately discovered canonical ID with the exact same canonical terms
    # must not reopen the opposite side through a different strategy or venue.
    other_market = market.model_copy(update={"id": "separate-canonical-id"})
    other, other_book = fresh(candidate, book, other_market, outcome="away", venue="another_exchange")
    with pytest.raises(RiskRejected, match="Opposing position"):
        risk.reserve(other, other_market, other_book, "paper")
    assert len(risk.orders) == 1


def test_same_side_duplicate_cooldown_is_global_and_survives_rehydration(risk, candidate, market, book):
    order = risk.reserve(candidate, market, book, "paper")
    restored = RiskEngine(risk.config)
    restored.auto_trade = True
    restored.orders[order.id] = Order.model_validate_json(order.model_dump_json())
    other, other_book = fresh(candidate, book, market, venue="another_exchange")
    with pytest.raises(RiskRejected, match="Duplicate"):
        restored.reserve(other, market, other_book, "paper")


@pytest.mark.parametrize("status,filled", [
    (OrderStatus.INTENT, 0), (OrderStatus.SUBMITTING, 0), (OrderStatus.OPEN, 0),
    (OrderStatus.PARTIAL, 1), (OrderStatus.UNKNOWN, 0), (OrderStatus.CANCEL_PENDING, 0),
    (OrderStatus.CANCELLED, 1), (OrderStatus.FILLED, 1), (OrderStatus.REJECTED, 1),
])
def test_unsettled_or_uncertain_positions_never_release_conflict(risk, candidate, market, book, status, filled):
    order = risk.reserve(candidate, market, book, "paper")
    risk.orders[order.id] = order.model_copy(update={"status": status, "filled_quantity": filled})
    other, other_book = fresh(candidate, book, market, outcome="away", venue="another_exchange")
    with pytest.raises(RiskRejected, match="Opposing"):
        risk.reserve(other, market, other_book, "paper")


@pytest.mark.parametrize("status", [OrderStatus.CANCELLED, OrderStatus.REJECTED])
def test_terminal_unfilled_releases_conflict_but_not_burst_budget(risk, candidate, market, book, status):
    order = risk.reserve(candidate, market, book, "paper")
    risk.orders[order.id] = order.model_copy(update={"status": status})
    other, other_book = fresh(candidate, book, market, outcome="away")
    assert risk.reserve(other, market, other_book, "paper")


def test_settled_position_releases_family_reservation(risk, candidate, market, book):
    order = risk.reserve(candidate, market, book, "paper")
    risk.orders[order.id] = order.model_copy(update={"status": OrderStatus.FILLED, "filled_quantity": 1})
    risk.settled.add(order.id)
    other, other_book = fresh(candidate, book, market, outcome="away")
    assert risk.reserve(other, market, other_book, "paper")


def test_alternate_thresholds_block_same_and_opposite_side(risk, candidate, market, book):
    prop = market.model_copy(update={"market_type": "player_prop", "player_id": "verified-player",
                                     "player": "Player", "statistic": "points", "line": D("24.5"),
                                     "outcomes": ("over", "under")})
    first, first_book = fresh(candidate, book, prop, outcome="over")
    risk.reserve(first, prop, first_book, "paper")
    alternate = prop.model_copy(update={"id": "alternate", "line": D("25.5")})
    for side in alternate.outcomes:
        other, other_book = fresh(candidate, book, alternate, outcome=side, venue="another_exchange")
        with pytest.raises(RiskRejected, match="family"):
            risk.reserve(other, alternate, other_book, "paper")


def test_different_settlement_rules_never_become_equivalent_contracts(risk, candidate, market, book):
    risk.reserve(candidate, market, book, "paper")
    other_market = market.model_copy(update={
        "id": "different-rules", "settlement": market.settlement.model_copy(update={"overtime": "excluded"})})
    assert market.identity() != other_market.identity()
    assert risk_family(market) == risk_family(other_market)
    other, other_book = fresh(candidate, book, other_market, outcome="away")
    # A conservative family restriction blocks stacking; no hedge is assumed.
    with pytest.raises(RiskRejected, match="family"):
        risk.reserve(other, other_market, other_book, "paper")


def test_verified_player_ids_and_periods_are_not_guessed_from_display_names(risk, candidate, market, book):
    prop = market.model_copy(update={"market_type": "player_prop", "player_id": "player-one",
                                     "player": "Same Name", "statistic": "points", "line": D("24.5")})
    first, first_book = fresh(candidate, book, prop)
    risk.reserve(first, prop, first_book, "paper")
    another_player = prop.model_copy(update={"id": "another-player", "player_id": "player-two"})
    other, other_book = fresh(candidate, book, another_player)
    assert risk.reserve(other, another_player, other_book, "paper")
    another_period = prop.model_copy(update={"id": "another-period", "settlement":
                                             prop.settlement.model_copy(update={"period": "first_half"})})
    other, other_book = fresh(candidate, book, another_period)
    assert risk.reserve(other, another_period, other_book, "paper")


@pytest.mark.parametrize("status,filled", [(OrderStatus.UNKNOWN, 0), (OrderStatus.FILLED, 1),
                                         (OrderStatus.CANCELLED, 1)])
def test_missing_legacy_identity_fails_closed_for_event(risk, candidate, market, book, status, filled):
    order = risk.reserve(candidate, market, book, "paper")
    risk.orders[order.id] = order.model_copy(update={"market_snapshot": None, "market_fingerprint": None,
                                                    "risk_family_id": None, "status": status,
                                                    "filled_quantity": filled})
    different_type = market.model_copy(update={"id": "new-type", "market_type": "total"})
    other, other_book = fresh(candidate, book, different_type)
    with pytest.raises(RiskRejected, match="legacy"):
        risk.reserve(other, different_type, other_book, "paper")


def test_unknown_or_tampered_snapshot_cannot_bypass_conflicts(risk, candidate, market, book):
    order = risk.reserve(candidate, market, book, "paper")
    risk.orders[order.id] = order.model_copy(update={"risk_family_id": "incorrect-family"})
    other, other_book = fresh(candidate, book, market, outcome="away")
    with pytest.raises(RiskRejected, match="legacy"):
        risk.reserve(other, market, other_book, "paper")


def test_cross_type_correlation_requires_explicit_reviewed_metadata(risk, candidate, market, book):
    extension = {"risk_correlation_v1": {"verified": True, "groups": ["reviewed-game-outcome"],
                                          "evidence": "fixture correlation review"}}
    first_market = market.model_copy(update={"extensions": extension})
    risk.reserve(candidate, first_market, book, "paper")
    second_market = market.model_copy(update={"id": "other-type", "market_type": "spread",
                                               "line": D("1.5"), "extensions": extension})
    other, other_book = fresh(candidate, book, second_market, venue="another_exchange")
    with pytest.raises(RiskRejected, match="Reviewed correlated"):
        risk.reserve(other, second_market, other_book, "paper")
    malformed = second_market.model_copy(update={"extensions": {"risk_correlation_v1": {
        "verified": False, "groups": ["reviewed-game-outcome"], "evidence": "unreviewed"}}})
    with pytest.raises(RiskRejected, match="requires reviewed"):
        risk.reserve(other, malformed, other_book, "paper")


def test_durable_barrier_recheck_excludes_only_own_reservation(risk, candidate, market, book):
    order = risk.reserve(candidate, market, book, "paper")
    risk.check_position_conflicts(candidate, market, exclude_order_id=order.id)
    risk.check_order_limits(exclude_order_id=order.id)
    with pytest.raises(RiskRejected, match="identity changed"):
        risk.check_position_conflicts(candidate.model_copy(update={"outcome": "away"}), market,
                                      exclude_order_id=order.id)
    opposite = order.model_copy(update={"id": "concurrent-order", "outcome": "away", "venue": "other"})
    risk.orders[opposite.id] = opposite
    with pytest.raises(RiskRejected, match="Opposing"):
        risk.check_position_conflicts(candidate, market, exclude_order_id=order.id)
    changed = market.model_copy(update={"line": D("1.5")})
    with pytest.raises(RiskRejected, match="identity changed"):
        risk.check_position_conflicts(candidate, changed, exclude_order_id=order.id)


def test_open_order_cap_is_global_and_counts_unknowns(risk, candidate, market, book):
    risk.config.max_open_orders = 1
    order = risk.reserve(candidate, market, book, "paper")
    risk.orders[order.id] = order.model_copy(update={"status": OrderStatus.UNKNOWN})
    risk.check_order_limits(exclude_order_id=order.id)
    other_market = market.model_copy(update={"id": "other", "event_id": "another-event"})
    other, other_book = fresh(candidate, book, other_market, venue="other")
    with pytest.raises(RiskRejected, match="open order count"):
        risk.reserve(other, other_market, other_book, "paper")
    risk.orders[order.id] = order.model_copy(update={"status": OrderStatus.CANCELLED})
    assert risk.reserve(other, other_market, other_book, "paper")


def test_rolling_burst_cap_counts_cancelled_and_recovered_orders(risk, candidate, market, book):
    risk.config.max_orders_per_minute = 1
    order = risk.reserve(candidate, market, book, "paper")
    risk.orders[order.id] = Order.model_validate_json(order.model_copy(update={
        "status": OrderStatus.CANCELLED}).model_dump_json())
    other_market = market.model_copy(update={"id": "other", "event_id": "another-event"})
    other, other_book = fresh(candidate, book, other_market)
    with pytest.raises(RiskRejected, match="per minute"):
        risk.reserve(other, other_market, other_book, "paper")
    risk.orders[order.id] = order.model_copy(update={"status": OrderStatus.CANCELLED,
                                                   "created_at": utcnow() - timedelta(seconds=61)})
    assert risk.reserve(other, other_market, other_book, "paper")


async def test_sqlite_roundtrip_preserves_full_conflict_identity(tmp_path, risk, candidate, market, book):
    storage = Storage(str(tmp_path / "identity.db"))
    await storage.open()
    try:
        order = risk.reserve(candidate, market, book, "paper")
        await storage.save_order(order)
        recovered = (await storage.load_orders())[0]
        assert recovered.market_snapshot == market
        assert metadata_consistent(recovered)
        restarted = RiskEngine(risk.config)
        restarted.auto_trade = True
        restarted.orders[recovered.id] = recovered.model_copy(update={"status": OrderStatus.FILLED,
                                                                    "filled_quantity": 1})
        other, other_book = fresh(candidate, book, market, outcome="away", venue="other")
        with pytest.raises(RiskRejected, match="Opposing"):
            restarted.reserve(other, market, other_book, "paper")
    finally:
        await storage.close()


@pytest.mark.parametrize("field", ["max_open_orders", "max_orders_per_minute"])
def test_submission_caps_must_be_positive(field):
    with pytest.raises(ValueError):
        RiskConfig(**{field: 0})


@pytest.mark.parametrize("limit", ["max_order_stake", "max_event_exposure", "max_player_exposure",
                                  "max_venue_exposure", "max_correlated_exposure", "max_total_exposure"])
def test_reserved_order_rechecks_lowered_limits(risk, candidate, market, book, limit):
    player_market = market.model_copy(update={"player_id": "verified-player"})
    order = risk.reserve(candidate, player_market, book, "paper")
    risk.check_reserved_limits(order, book)
    setattr(risk.config, limit, D(".10"))
    with pytest.raises(RiskRejected, match="Current.*limit"):
        risk.check_reserved_limits(order, book)
    assert risk.orders[order.id] == order
    assert risk.exposures() > 0


def test_fee_increase_cannot_silently_expand_reservation(risk, candidate, market, book):
    order = risk.reserve(candidate, market, book, "paper")
    higher_fees = book.model_copy(update={"fee": book.fee.model_copy(update={"per_contract": D(".02")})})
    with pytest.raises(RiskRejected, match="exceed.*reservation"):
        risk.check_reserved_limits(order, higher_fees)
    assert risk.orders[order.id].reserved_cost == order.reserved_cost
    assert risk.orders[order.id].status == OrderStatus.INTENT


def test_partial_fill_remains_accounted_when_residual_budget_is_invalid(risk, candidate, market, book):
    order = risk.reserve(candidate.model_copy(update={"max_quantity": 4}), market, book, "paper")
    partial = order.model_copy(update={"status": OrderStatus.PARTIAL, "filled_quantity": 1,
                                        "average_fill_price": order.price, "fees": D(".01")})
    risk.orders[order.id] = partial
    risk.check_reserved_limits(partial, book)
    higher_fees = book.model_copy(update={"fee": book.fee.model_copy(update={"per_contract": D(".02")})})
    with pytest.raises(RiskRejected, match="exceed.*reservation"):
        risk.check_reserved_limits(partial, higher_fees)
    assert risk.orders[order.id].filled_quantity == 1
    assert risk.orders[order.id].fees == D(".01")
    assert risk.exposures() == partial.reserved_cost


def test_reserved_limit_recheck_rejects_missing_mismatched_and_unverified_books(risk, candidate, market, book):
    order = risk.reserve(candidate, market, book, "paper")
    for invalid in (None, book.model_copy(update={"venue": "another-exchange"})):
        with pytest.raises(RiskRejected, match="fee book"):
            risk.check_reserved_limits(order, invalid)
    unverified = book.model_copy(update={"fee": book.fee.model_copy(update={"verified": False})})
    with pytest.raises(RiskRejected, match="not verified"):
        risk.check_reserved_limits(order, unverified)
