"""Offline capability admission; these fixtures prove no real settlement."""
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from evplatform.core.config import RiskConfig
from evplatform.core.models import OrderStatus, utcnow
from evplatform.core.readiness import LaunchProfile
from evplatform.core.risk import RiskEngine
from evplatform.venues.bridge import NativeExecutionVenue, VerifiedVenueMapping
from evplatform.venues.novig import NovigClient, NovigSettings
from evplatform.venues.settlement import supports_settlement_scope

from test_launch_readiness import ready_fixture
from test_native_book_normalization import mapping
from test_native_oms import order


@pytest.fixture
async def scope(market):
    def no_network(request):
        raise AssertionError("Capability inspection must perform no network I/O")
    async with httpx.AsyncClient(transport=httpx.MockTransport(no_network)) as http:
        client = NovigClient(NovigSettings(environment="sandbox"), http)
        item = mapping(market, "novig")
        instrument = item.instrument
        execution = VerifiedVenueMapping(canonical_market_id=market.id, canonical_outcome=instrument.outcome,
            native_instrument_id=instrument.instrument_id, native_outcome=instrument.native_outcome,
            settlement_fingerprint=market.identity(), mapping_evidence="OFFLINE reviewed fixture",
            verified=True, fee_schedule_verified=True, native_payout_usd=instrument.payout_per_unit_usd,
            native_quantity_unit=instrument.quantity_unit)
        adapter = NativeExecutionVenue(client, RiskEngine(RiskConfig()).authority, mappings=[execution])
        profile, _, _ = ready_fixture("sandbox")
        profile = profile.model_copy(update={"venue": "novig", "market_ids": (market.id,),
            "settlement_policy": "novig_ioc_win_loss_halt_on_exception", "evidence": ()})
        yield profile, adapter, (item,), order(market)


async def test_scope_requires_explicit_opt_in_and_does_not_claim_real_evidence(scope):
    profile, adapter, mappings, managed = scope
    assert supports_settlement_scope(profile, adapter, mappings, [])
    assert supports_settlement_scope(profile, adapter, mappings, [managed])
    assert not profile.evidence  # Separate readiness gates still require genuine evidence.
    default = LaunchProfile.model_validate(profile.model_dump(exclude={"settlement_policy"}))
    assert default.settlement_policy == "full"
    assert not supports_settlement_scope(default, adapter, mappings, [])
    assert default.digest() != profile.digest()
    with pytest.raises(ValidationError):
        LaunchProfile.model_validate(profile.model_dump() | {"settlement_policy": "ignore_exceptions"})


@pytest.mark.parametrize("change", ["venue", "environment", "adapter", "client", "missing_market"])
async def test_scope_rejects_wrong_adapter_environment_or_missing_requested_market(scope, change):
    profile, adapter, mappings, managed = scope
    if change == "venue":
        profile = profile.model_copy(update={"venue": "kalshi"})
    elif change == "environment":
        profile = profile.model_copy(update={"environment": "production"})
    elif change == "adapter":
        adapter = SimpleNamespace(client=adapter.client, mappings=adapter.mappings)
    elif change == "client":
        adapter.client = SimpleNamespace(settings=adapter.client.settings)
    else:
        profile = profile.model_copy(update={"market_ids": (*profile.market_ids, "unmapped-market")})
    assert not supports_settlement_scope(profile, adapter, mappings, [])


@pytest.mark.parametrize("change", ["identity", "instrument_unverified", "fee_unverified", "fee_expired",
                                    "payout", "execution_unverified", "execution_fee", "execution_identity",
                                    "execution_units", "execution_missing"])
async def test_scope_requires_full_reviewed_mapping_units_and_fees(scope, change):
    profile, adapter, mappings, managed = scope
    item = mappings[0]
    if change == "identity":
        item = item.model_copy(update={"instrument": item.instrument.model_copy(update={"settlement_rules_fingerprint": "wrong"})})
    elif change == "instrument_unverified":
        item = item.model_copy(update={"instrument": item.instrument.model_copy(update={"verified": False})})
    elif change == "fee_unverified":
        item = item.model_copy(update={"fee_bound": item.fee_bound.model_copy(update={"verified": False})})
    elif change == "fee_expired":
        item = item.model_copy(update={"fee_bound": item.fee_bound.model_copy(update={"valid_until": utcnow()-timedelta(seconds=1)})})
    elif change == "payout":
        item = item.model_copy(update={"instrument": item.instrument.model_copy(update={"payout_per_unit_usd": Decimal(1)})})
    else:
        key = (item.market.id, item.instrument.outcome)
        changes = {"execution_unverified": {"verified": False}, "execution_fee": {"fee_schedule_verified": False},
                   "execution_identity": {"settlement_fingerprint": "wrong"}, "execution_units": {"native_quantity_unit": "contracts"}}
        if change == "execution_missing":
            adapter.mappings.clear()
        else:
            adapter.mappings[key] = adapter.mappings[key].model_copy(update=changes[change])
    assert not supports_settlement_scope(profile, adapter, (item,), [])


@pytest.mark.parametrize("prior_status", [OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED, OrderStatus.UNKNOWN])
async def test_scope_counts_entire_order_history_including_terminal_orders(scope, prior_status):
    profile, adapter, mappings, managed = scope
    prior = managed.model_copy(update={"status": prior_status})
    next_order = managed.model_copy(update={"intent": managed.intent.model_copy(update={"id": "new-local-order", "client_order_id": "new-client-id"})})
    assert not supports_settlement_scope(profile, adapter, mappings, [prior, next_order])


async def test_scope_counts_opposing_outcomes_together_but_keeps_accounts_and_environments_separate(scope):
    profile, adapter, mappings, managed = scope
    other = managed.model_copy(update={"intent": managed.intent.model_copy(update={"id": "other",
        "instrument": managed.intent.instrument.model_copy(update={"outcome": "away", "native_outcome": "native-away"})})})
    assert not supports_settlement_scope(profile, adapter, mappings, [managed, other])
    assert supports_settlement_scope(profile, adapter, mappings, [managed, other.model_copy(update={"account_fingerprint": "b"*64})])
    other_environment = other.model_copy(update={"intent": other.intent.model_copy(update={"mode": "production"})})
    assert supports_settlement_scope(profile, adapter, mappings, [managed, other_environment])
