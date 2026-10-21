from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest
from pydantic import ValidationError

from evplatform.core.models import OrderStatus
from evplatform.core.native_accounting import (
    NativeAccounting, NativeCumulativeSnapshot, NativeFill, NativeInstrument,
    NativeOrderIntent, NativePriceBand, NativeSettlement, ReconciliationRequired,
)

NOW = datetime(2026, 10, 21, tzinfo=timezone.utc)


def instrument(venue="novig", **changes):
    common = dict(venue=venue, instrument_id="native-market", canonical_market_id="canonical-market",
                  outcome="home", native_outcome="native-home", quantity_unit="whole_cent_contracts",
                  price_format="probability", payout_per_unit_usd="0.01", quantity_step="1",
                  minimum_quantity="1", cash_quantum_usd="0.00001", verified=True,
                  settlement_rules_fingerprint="reviewed-rules-sha256", source="official catalog + reviewed rules",
                  price_bands=(NativePriceBand(minimum="0.001", maximum="0.050", step="0.001"),
                               NativePriceBand(minimum="0.055", maximum="0.945", step="0.005"),
                               NativePriceBand(minimum="0.950", maximum="0.999", step="0.001")))
    if venue == "kalshi":
        common.update(quantity_unit="contracts", payout_per_unit_usd="1", quantity_step="0.01",
                      minimum_quantity="0.01", cash_quantum_usd="0.000001",
                      price_bands=(NativePriceBand(minimum="0.001", maximum="0.999", step="0.001"),))
    elif venue == "prophetx":
        common.update(quantity_unit="usd_stake", price_format="american", payout_per_unit_usd=None,
                      quantity_step="0.01", minimum_quantity="1", cash_quantum_usd="0.01", price_bands=())
    return NativeInstrument(**(common | changes))


def intent(venue="novig", **changes):
    values = dict(id="internal-id", client_order_id="client-id", instrument=instrument(venue), quantity="110",
                  limit_price="0.665", max_fee_usd="1", fee_source="verified account schedule",
                  fees_verified=True, created_at=NOW, mode="sandbox")
    if venue == "prophetx":
        values.update(quantity="20", limit_price="-150")
    return NativeOrderIntent(**(values | changes))


def fill(**changes):
    return NativeFill(**(dict(fill_id="fill-1", order_id="native-order", client_order_id="client-id",
                             quantity="10", price="0.665", cost_usd="0.0665", gross_payout_usd="0.10",
                             fee_usd="0.00001", filled_at=NOW, observed_at=NOW, source="native fills") | changes))


def snapshot(**changes):
    return NativeCumulativeSnapshot(**(dict(order_id="native-order", client_order_id="client-id",
        cumulative_quantity="10", cumulative_cost_usd="0.0665", cumulative_gross_payout_usd="0.10",
        cumulative_fee_usd="0.00001", observed_at=NOW, status=OrderStatus.PARTIAL,
        authoritative=True, source="native orders", revision=1) | changes))


def test_novig_documented_cent_contract_example_is_not_dollar_contracts():
    order = intent()
    assert order.maximum_cost_usd == D("0.73150")
    assert order.minimum_gross_payout_usd == D("1.10")
    assert order.reserved_cost_usd == D("1.73150")
    assert NativeOrderIntent.model_validate_json(order.model_dump_json()) == order


@pytest.mark.parametrize("quantity", ["1.5", "0.01", "0", "-1"])
def test_novig_rejects_fractional_and_nonpositive_quantities(quantity):
    with pytest.raises(ValidationError):
        intent(quantity=quantity)


@pytest.mark.parametrize("price", ["0.667", "0.051", "0", "1", "NaN", "Infinity"])
def test_novig_enforces_banded_native_price_grid(price):
    with pytest.raises(ValidationError):
        intent(limit_price=price)


def test_kalshi_fractional_contracts_keep_subcent_money_and_fee():
    order = intent("kalshi", quantity="2.51", limit_price="0.123", max_fee_usd="0.000137")
    assert order.maximum_cost_usd == D("0.30873")
    assert order.minimum_gross_payout_usd == D("2.51")
    ledger = NativeAccounting(order, "native-order")
    ledger.apply_fill(fill(quantity="0.01", price="0.123", cost_usd="0.00123",
                           gross_payout_usd="0.01", fee_usd="0.000037"))
    assert ledger.totals.total_cost_usd == D("0.001267")


@pytest.mark.parametrize("field,value", [("quantity", 1.0), ("limit_price", 0.665), ("max_fee_usd", False)])
def test_no_native_float_or_boolean_coercion(field, value):
    with pytest.raises(ValidationError):
        intent(**{field: value})


def test_unverified_mapping_fee_and_wrong_currency_fail_closed():
    with pytest.raises(ValidationError):
        intent(instrument=instrument(verified=False))
    with pytest.raises(ValidationError):
        intent(fees_verified=False)
    with pytest.raises(ValidationError):
        instrument(currency="EUR")
    with pytest.raises(ValidationError):
        instrument(quantity_unit="contracts")


def test_prophetx_exact_american_limits_and_conservative_payout_bounds():
    order = intent("prophetx")
    assert order.maximum_cost_usd == D("20")
    assert order.minimum_gross_payout_usd == D("33.33")
    assert order.instrument.payout_bound(D("20"), D("-150"), upper=True) == D("33.34")
    assert order.instrument.price_satisfies_limit(D("-140"), D("-150"))
    assert order.instrument.price_satisfies_limit(D("100"), D("-150"))
    assert not order.instrument.price_satisfies_limit(D("-160"), D("-150"))
    assert order.instrument.payout_bound(D("20"), D("150")) == D("50")


def test_prophetx_partial_usd_stake_and_actual_fill_price_not_order_limit():
    ledger = NativeAccounting(intent("prophetx"), "native-order")
    ledger.apply_fill(fill(quantity="10", price="-125", cost_usd="10", gross_payout_usd="18", fee_usd="0.15"))
    assert ledger.totals.quantity == D("10")
    assert ledger.totals.gross_payout_usd == D("18")
    assert ledger.totals.total_cost_usd == D("10.15")
    with pytest.raises(ReconciliationRequired):
        ledger.apply_fill(fill(fill_id="wrong", quantity="10", price="-160", cost_usd="10",
                               gross_payout_usd="16.25", fee_usd="0.1"))
    assert ledger.totals.quantity == D("10")


def test_fill_idempotency_conflicting_duplicate_and_foreign_order():
    ledger = NativeAccounting(intent(), "native-order")
    assert ledger.apply_fill(fill()).quantity == D("10")
    assert ledger.apply_fill(fill()).duplicate
    with pytest.raises(ReconciliationRequired):
        ledger.apply_fill(fill(fee_usd="0.00002"))
    with pytest.raises(ReconciliationRequired):
        ledger.apply_fill(fill(fill_id="other", order_id="other-order"))
    assert ledger.totals.quantity == D("10")


def test_identical_fill_reread_has_new_receipt_clock_without_double_counting():
    ledger = NativeAccounting(intent(), "native-order")
    first = fill()
    ledger.apply_fill(first)
    totals = ledger.totals
    reread = first.model_copy(update={"observed_at": first.observed_at + timedelta(seconds=5)})
    assert ledger.apply_fill(reread).duplicate
    assert ledger.totals == totals
    with pytest.raises(ReconciliationRequired, match="Conflicting duplicate"):
        ledger.apply_fill(reread.model_copy(update={"filled_at": first.filled_at + timedelta(seconds=1)}))
    with pytest.raises(ReconciliationRequired, match="Conflicting duplicate"):
        ledger.apply_fill(reread.model_copy(update={"fee_usd": D(".00002")}))
    assert ledger.totals == totals


@pytest.mark.parametrize("changes", [{"cost_usd": "0.06"}, {"gross_payout_usd": "10"},
                                     {"fee_usd": "2"}, {"quantity": "111", "cost_usd": "0.73815", "gross_payout_usd": "1.11"}])
def test_inconsistent_fill_never_partially_mutates_accounting(changes):
    ledger = NativeAccounting(intent(), "native-order")
    with pytest.raises(ReconciliationRequired):
        ledger.apply_fill(fill(**changes))
    assert ledger.totals.quantity == 0


def test_aggregate_after_fills_not_added_twice_and_unknown_overlap_rejected():
    ledger = NativeAccounting(intent(), "native-order")
    ledger.apply_fill(fill())
    assert ledger.apply_cumulative(snapshot()).duplicate
    with pytest.raises(ReconciliationRequired, match="overlap"):
        ledger.apply_fill(fill(fill_id="fill-2"))
    ledger.apply_fill(fill(fill_id="fill-2", cumulative_quantity_after="20"))
    assert ledger.totals.quantity == 20
    assert ledger.totals.cost_usd == D("0.133")


def test_snapshot_followed_by_covered_fill_does_not_double_count():
    ledger = NativeAccounting(intent(), "native-order")
    ledger.apply_cumulative(snapshot())
    assert ledger.apply_fill(fill(cumulative_quantity_after="10")).duplicate
    assert ledger.totals.quantity == 10


def test_covered_fill_requires_consistent_aggregate_cash_and_unique_intervals():
    ledger = NativeAccounting(intent(), "native-order")
    ledger.apply_cumulative(snapshot())
    with pytest.raises(ReconciliationRequired):
        ledger.apply_fill(fill(price="0.660", cost_usd="0.066", cumulative_quantity_after="10"))
    ledger.apply_fill(fill(cumulative_quantity_after="10"))
    with pytest.raises(ReconciliationRequired):
        ledger.apply_fill(fill(fill_id="different-id", cumulative_quantity_after="10"))


def test_unknown_individual_overlap_not_mistaken_for_a_covered_snapshot():
    ledger = NativeAccounting(intent(), "native-order")
    ledger.apply_fill(fill(cumulative_quantity_after="10"))
    with pytest.raises(ReconciliationRequired):
        ledger.apply_fill(fill(fill_id="different-id", cumulative_quantity_after="10"))


def test_cumulative_gaps_stale_data_conflicts_and_unconfirmed_not_used():
    ledger = NativeAccounting(intent(), "native-order")
    ledger.apply_cumulative(snapshot(revision=3))
    with pytest.raises(ReconciliationRequired, match="gap"):
        ledger.apply_fill(fill(cumulative_quantity_after="30"))
    assert ledger.apply_cumulative(snapshot(revision=2, cumulative_quantity="0", cumulative_cost_usd="0",
                                           cumulative_gross_payout_usd="0", cumulative_fee_usd="0")).duplicate
    with pytest.raises(ReconciliationRequired, match="revision"):
        ledger.apply_cumulative(snapshot(revision=3, cumulative_fee_usd="0.00002"))
    with pytest.raises(ReconciliationRequired, match="Non-authoritative"):
        ledger.apply_cumulative(snapshot(authoritative=False))
    with pytest.raises(ReconciliationRequired, match="regressed"):
        ledger.apply_cumulative(snapshot(revision=4, cumulative_quantity="0", cumulative_cost_usd="0",
                                           cumulative_gross_payout_usd="0", cumulative_fee_usd="0"))


@pytest.mark.parametrize("status", [OrderStatus.FILLED, OrderStatus.REJECTED])
def test_terminal_status_cannot_invent_or_erase_fill_quantity(status):
    with pytest.raises(ReconciliationRequired):
        NativeAccounting(intent(), "native-order").apply_cumulative(snapshot(status=status))


def test_cancellation_preserves_partial_execution_totals():
    ledger = NativeAccounting(intent(), "native-order")
    ledger.apply_cumulative(snapshot(status=OrderStatus.CANCELLED))
    assert ledger.totals.quantity == 10
    assert ledger.totals.total_cost_usd == D("0.06651")


def test_venue_publication_clock_is_not_confirmation_time():
    publication = fill(filled_at=None, timestamp_kind="publication")
    assert publication.filled_at is None
    with pytest.raises(ValidationError):
        fill(timestamp_kind="publication")
    with pytest.raises(ValidationError):
        fill(filled_at=NOW.replace(tzinfo=None))


@pytest.mark.parametrize("kind,gross,fee_return,profit", [
    ("win", "0.10", "0", "0.03349"), ("loss", "0", "0", "-0.06651"),
    ("void", "0.0665", "0.00001", "0"), ("push", "0.0665", "0", "-0.00001"),
    ("partial", "0.05", "0", "-0.01651"),
])
def test_explicit_settlement_and_fee_refunds(kind, gross, fee_return, profit):
    ledger = NativeAccounting(intent(), "native-order")
    ledger.apply_fill(fill())
    result = NativeSettlement(order_id="native-order", client_order_id="client-id", settlement_id="s1",
                              kind=kind, gross_return_usd=gross, returned_fees_usd=fee_return,
                              settled_at=NOW + timedelta(hours=2), source="native settlement", verified=True)
    assert result.realized_profit(ledger) == D(profit)


def test_settlement_unknown_and_excessive_fee_refund_rejected():
    ledger = NativeAccounting(intent(), "native-order")
    ledger.apply_fill(fill())
    values = dict(order_id="native-order", client_order_id="client-id", settlement_id="s1", kind="win",
                  gross_return_usd="0.10", returned_fees_usd="0", settled_at=NOW, source="native settlement")
    with pytest.raises(ReconciliationRequired):
        NativeSettlement(**values).realized_profit(ledger)
    with pytest.raises(ReconciliationRequired):
        NativeSettlement(**(values | {"verified": True, "returned_fees_usd": "0.01"})).realized_profit(ledger)
