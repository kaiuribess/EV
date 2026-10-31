"""A declared supported settlement scope is never live account certification."""
from datetime import timedelta

import httpx
import pytest

from evplatform.core.config import RiskConfig
from evplatform.core.models import utcnow
from evplatform.core.risk import RiskEngine
from evplatform.venues.bridge import NativeExecutionVenue, VerifiedVenueMapping
from evplatform.venues.prophetx import ProphetXClient, ProphetXSettings
from evplatform.venues.prophetx_financial import ProphetXAccountReview
from evplatform.venues.prophetx_settlement import supports_prophetx_settlement_scope
from test_launch_readiness import ready_fixture
from test_native_book_normalization import mapping


async def test_scope_requires_reviewed_cash_history_exact_mapping_and_current_fee(market):
    item = mapping(market, "prophetx")
    i = item.instrument
    execution = VerifiedVenueMapping(canonical_market_id=market.id, canonical_outcome=i.outcome,
        native_instrument_id=i.instrument_id, native_outcome=i.native_outcome,
        settlement_fingerprint=market.identity(), mapping_evidence="OFFLINE explicit mapping",
        verified=True, fee_schedule_verified=True, native_quantity_unit=i.quantity_unit,
        native_payout_usd=None)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: pytest.fail("No network"))) as http:
        client = ProphetXClient(ProphetXSettings(), http)
        adapter = NativeExecutionVenue(client, RiskEngine(RiskConfig()).authority, mappings=[execution])
        profile, _, _ = ready_fixture("sandbox")
        profile = profile.model_copy(update={"venue": "prophetx", "market_ids": (market.id,),
            "settlement_policy": "prophetx_funded_cash_win_loss_halt_on_exception", "evidence": ()})
        client.account_review = ProphetXAccountReview(account_fingerprint=profile.account_fingerprint,
            history_created_from=utcnow()-timedelta(days=10), history_scope_evidence="OFFLINE account inception evidence",
            funded_cash_only=True, funding_scope_evidence="OFFLINE no exposure credit evidence",
            valid_until=utcnow()+timedelta(hours=1))
        assert supports_prophetx_settlement_scope(profile, adapter, (item,), ())
        assert not profile.evidence
        assert not supports_prophetx_settlement_scope(profile.model_copy(update={"settlement_policy": "full"}), adapter, (item,), ())
        assert not supports_prophetx_settlement_scope(profile, adapter, (), ())
        bad = item.model_copy(update={"fee_bound": item.fee_bound.model_copy(update={"valid_until": utcnow()-timedelta(seconds=1)})})
        assert not supports_prophetx_settlement_scope(profile, adapter, (bad,), ())
        client.account_review = client.account_review.model_copy(update={"funded_cash_only": False})
        assert not supports_prophetx_settlement_scope(profile, adapter, (item,), ())
        client.account_review = ProphetXAccountReview(account_fingerprint=profile.account_fingerprint,
            current_inventory_reviewed_at=utcnow()-timedelta(minutes=1),
            current_inventory_scope_evidence="OFFLINE reviewed empty wallet fixture",
            funded_cash_only=True, funding_scope_evidence="OFFLINE no LEC or GEC fixture",
            valid_until=utcnow()+timedelta(hours=1))
        assert supports_prophetx_settlement_scope(profile, adapter, (item,), ())
