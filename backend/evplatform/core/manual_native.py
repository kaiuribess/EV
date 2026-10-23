"""One-use, short-lived operator review for a single qualified native order.

The review is deliberately volatile. A restart, worse economics or an expired
quote requires a new review; no API call here enables automatic trading.
"""
from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from .models import utcnow
from .native_runtime import NativeRuntime
from .native_pricing import native_opportunity
from .pricing import NoSignal, fair_value
from .readiness import digest_json
from .risk import RiskRejected


@dataclass(frozen=True)
class ManualReview:
    market_id: str
    venue: str
    outcome: str
    max_cost_usd: Decimal
    terms_digest: str
    proposal_digest: str
    profile_digest: str
    scope_digest: str
    quantity_ceiling: Decimal
    price_ceiling: Decimal
    fee_ceiling_usd: Decimal
    debit_ceiling_usd: Decimal
    minimum_ev: Decimal
    minimum_roi: Decimal
    minimum_sensitivity_ev: Decimal | None
    minimum_sensitivity_roi: Decimal | None
    created_at: datetime
    expires_at: datetime


class ManualNativeService:
    def __init__(self, engine):
        self.engine = engine
        self.reviews: dict[str, ManualReview] = {}
        self.lock = asyncio.Lock()

    def _gate(self, *, for_submit=False):
        engine = self.engine
        if engine.settings.mode not in {"sandbox", "production"}:
            raise RiskRejected("Manual native orders require a sandbox or production workspace")
        if engine.risk.auto_trade:
            raise RiskRejected("Pause automation before reviewing a manual order")
        if for_submit and engine.risk.kill_switch:
            raise RiskRejected("Emergency kill switch blocks manual orders")
        report = engine.native.report()
        if not engine.settings.live_enabled or not report.safe_to_arm or not report.profile_digest:
            raise RiskRejected("Native launch checks must pass before a manual order")
        if not engine.native.owns_execution_lease():
            raise RiskRejected("Native execution lease is unavailable")
        return report.profile_digest

    @staticmethod
    def _terms(candidate):
        intent = candidate.intent
        return digest_json({
            "market_id": candidate.market_id, "venue": candidate.venue, "outcome": candidate.outcome,
            "instrument": intent.instrument.model_dump(mode="json"),
            "quantity": str(intent.quantity), "limit_price": str(intent.limit_price),
            "max_fee_usd": str(intent.max_fee_usd), "reserved_cost_usd": str(intent.reserved_cost_usd),
            "fair_probability": str(candidate.fair_probability), "roi": str(candidate.roi),
            "expected_profit_usd": str(candidate.expected_profit_usd),
            "model": candidate.model, "sensitivity_model": candidate.sensitivity_model,
            "source_quality_mode": candidate.source_quality_mode,
            "uncertainty_margin_probability": str(candidate.uncertainty_margin_probability),
            "sensitivity_roi": str(candidate.sensitivity_roi),
            "reference_profile_id": candidate.reference_profile_id,
            "reference_policy_digest": candidate.reference_policy_digest,
            # Bind the quote clocks, weights and underlying probabilities, not
            # just a list of book names that could stay the same as prices move.
            "references": sorted(candidate.references, key=lambda ref: (str(ref.get("bookmaker")),
                                                                        str(ref.get("provider")))),
            "book_revision": candidate.book_revision,
            "settlement_probability_source": candidate.settlement_probability_source,
        })

    def _proposal_digest(self, *, candidate, order, profile_digest: str,
                         max_cost_usd: Decimal, created_at: datetime, expires_at: datetime) -> str:
        book = self.engine.native.books[(candidate.market_id, candidate.venue, candidate.outcome)]
        return digest_json({
            "scope_digest": self._scope_digest(candidate, order, profile_digest),
            "terms_digest": self._terms(candidate),
            "book_asks": [level.model_dump(mode="json") for level in book.asks],
            "book_source_time": book.source_time.isoformat() if book.source_time else None,
            "fee_bound": book.fee_bound.model_dump(mode="json"),
            "max_cost_usd": str(max_cost_usd),
            "created_at": created_at.isoformat(), "expires_at": expires_at.isoformat(),
        })

    def _scope_digest(self, candidate, order, profile_digest: str) -> str:
        """Static authorized identity and policy, independent of quote clocks/depth."""
        market = self.engine.markets[candidate.market_id]
        registry = getattr(self.engine, "registry", None)
        source_mappings = {}
        for name, record in getattr(registry, "records", {}).items():
            mapper = getattr(record.plugin, "mapper", None)
            if mapper is not None:
                source_mappings[name] = [item.model_dump(mode="json") for item in sorted(
                    (item for item in mapper.mappings.values() if item.market.id == market.id),
                    key=lambda item: item.signature)]
        return digest_json({
            "account_fingerprint": order.account_fingerprint,
            "profile_digest": profile_digest, "market_identity": market.identity(),
            "market_id": candidate.market_id, "venue": candidate.venue, "outcome": candidate.outcome,
            "event_id": market.event_id, "native_outcome": candidate.intent.instrument.native_outcome,
            "native_ticker": candidate.intent.instrument.instrument_id,
            "instrument": candidate.intent.instrument.model_dump(mode="json"),
            "model": candidate.model, "sensitivity_model": candidate.sensitivity_model,
            "reference_profile_id": candidate.reference_profile_id,
            "reference_policy_digest": candidate.reference_policy_digest,
            "settlement_probability_source": candidate.settlement_probability_source,
            "fee_source": candidate.intent.fee_source,
            "source_mapping_digest": digest_json(source_mappings),
            "build_id": getattr(self.engine.native, "build_id", None),
            "pricing_digest": getattr(getattr(self.engine, "settings", None), "pricing", None).model_dump(mode="json")
                if getattr(getattr(self.engine, "settings", None), "pricing", None) is not None else None,
        })

    async def _fresh_candidate(self, market_id: str, venue: str, outcome: str, max_cost_usd: Decimal):
        native: NativeRuntime = self.engine.native
        record = self.engine.registry.records.get("native_positive_ev")
        if not record or not record.enabled:
            raise RiskRejected("Native positive-EV strategy is disabled")
        if market_id not in self.engine.markets or (market_id, venue, outcome) not in native.books:
            raise RiskRejected("No reviewed native market and executable book for this selection")
        await native.recalculate(market_id)
        candidate = native.candidates.get((market_id, venue, outcome))
        if candidate is None or not candidate.qualified or not candidate.execution_eligible:
            raise RiskRejected("No currently qualified native EV order for this selection")
        if candidate.intent.reserved_cost_usd > max_cost_usd:
            try:
                market = self.engine.markets[market_id]
                fair = fair_value(market, list(self.engine.quotes.get(market_id, {}).values()),
                                  self.engine.settings.pricing)
                cash = native.sizing_cash(venue) if self.engine.risk.config.stake_sizing == "quarter_kelly" else None
                candidate = native_opportunity(market, fair, native.books[(market_id, venue, outcome)],
                    self.engine.settings.pricing, max_stake_usd=max_cost_usd,
                    mode=self.engine.settings.mode, stake_sizing=self.engine.risk.config.stake_sizing,
                    available_cash_usd=cash, unit_size_usd=self.engine.risk.config.unit_size_usd)
            except (NoSignal, ValueError) as exc:
                raise RiskRejected(str(exc)) from exc
            if not candidate.qualified or not candidate.execution_eligible:
                raise RiskRejected("The selected maximum cost does not leave a qualifying native EV order")
        return candidate

    async def preview(self, market_id: str, venue: str, outcome: str, max_cost_usd: Decimal):
        async with self.lock:
            if not max_cost_usd.is_finite() or max_cost_usd <= 0:
                raise RiskRejected("Manual maximum cost must be a positive dollar amount")
            profile_digest = self._gate()
            candidate = await self._fresh_candidate(market_id, venue, outcome, max_cost_usd)
            order = self.engine.native.build_order(candidate, execution_origin="manual")
            # Preserve the kill switch while checking all non-arming limits.
            # The submit path repeats the full armed check under the OMS lock.
            async with self.engine.oms.lock:
                self.engine.native.preview_check(order)
            created_at = utcnow()
            expires_at = created_at + timedelta(seconds=90)
            token = secrets.token_urlsafe(32)
            proposal_digest = self._proposal_digest(candidate=candidate, order=order,
                profile_digest=profile_digest, max_cost_usd=max_cost_usd,
                created_at=created_at, expires_at=expires_at)
            self.reviews = {key: value for key, value in self.reviews.items() if value.expires_at > utcnow()}
            if len(self.reviews) >= 32:
                self.reviews.pop(next(iter(self.reviews)))
            self.reviews[token] = ManualReview(market_id, venue, outcome, max_cost_usd,
                self._terms(candidate), proposal_digest, profile_digest,
                self._scope_digest(candidate, order, profile_digest),
                candidate.intent.quantity, candidate.intent.limit_price, candidate.intent.max_fee_usd,
                candidate.intent.reserved_cost_usd, candidate.ev_per_usd_payout, candidate.roi,
                candidate.sensitivity_ev_per_usd_payout, candidate.sensitivity_roi,
                created_at, expires_at)
            market = self.engine.markets[market_id]
            book = self.engine.native.books[(market_id, venue, outcome)]
            return {"review_token": token, "terms_digest": self._terms(candidate),
                    "proposal_digest": proposal_digest,
                    "authorization_scope_digest": self.reviews[token].scope_digest,
                    "created_at": created_at.isoformat(), "expires_at": expires_at.isoformat(),
                    "environment": self.engine.settings.mode,
                    "market": {"id": market.id, "event_id": market.event_id,
                               "identity": market.identity(), "event_name": market.event_name,
                               "market_type": market.market_type, "player": market.player,
                               "outcome": outcome, "settlement": market.settlement.model_dump(mode="json"),
                               "original_ids": market.original_ids},
                    "account_fingerprint_suffix": order.account_fingerprint[-8:],
                    "build_id": getattr(self.engine.native, "build_id", None),
                    "profile_digest": profile_digest,
                    "source_policy": {"quality_mode": candidate.source_quality_mode,
                        "trusted_pilot": (self.engine.settings.pricing.trusted_pilot_policy.model_dump(mode="json")
                            if candidate.source_quality_mode == "trusted_provider"
                            and getattr(self.engine.settings, "pricing", None) is not None else None),
                        "unadjusted_fair_probability": str(candidate.unadjusted_fair_probability)
                            if candidate.unadjusted_fair_probability is not None else None,
                        "uncertainty_margin_probability": str(candidate.uncertainty_margin_probability)
                            if candidate.uncertainty_margin_probability is not None else None,
                        "quote_observation_age_known": candidate.quote_age_ms is not None},
                    "max_cost_usd": str(max_cost_usd),
                    "executable_depth": [level.model_dump(mode="json") for level in book.asks],
                    "book_source_time": book.source_time.isoformat() if book.source_time else None,
                    "risk_decisions": {"non_arming_checks_passed": True,
                                       "automatic_orders_off": True,
                                       "kill_switch_on": self.engine.risk.kill_switch,
                                       "submit_requires_kill_switch_off": True},
                    "unsigned": True,
                    "candidate": candidate.model_dump(mode="json"),
                    "note": "Review only. No order sent. Fresh final evidence must meet these same approved limits and scope."}

    async def submit(self, token: str, terms_digest: str, approved_max_cost_usd: Decimal,
                     proposal_digest: str | None = None):
        async with self.lock:
            review = self.reviews.pop(token, None)  # Consume before any await; never replay a click.
            if review is None or utcnow() >= review.expires_at:
                raise RiskRejected("Manual review expired or was already used; review the trade again")
            try:
                if (terms_digest != review.terms_digest or approved_max_cost_usd != review.max_cost_usd
                        or proposal_digest != review.proposal_digest):
                    raise RiskRejected("Explicit confirmation does not match the reviewed trade")
                profile_digest = self._gate(for_submit=True)
                if profile_digest != review.profile_digest:
                    raise RiskRejected("Native launch scope changed; review the trade again")
                candidate = await self._fresh_candidate(review.market_id, review.venue, review.outcome, review.max_cost_usd)
                order = self.engine.native.build_order(candidate, execution_origin="manual")
                if self._scope_digest(candidate, order, profile_digest) != review.scope_digest:
                    raise RiskRejected("Account, market, mapping, model or configuration changed")
                instrument = candidate.intent.instrument
                if (candidate.intent.quantity > review.quantity_ceiling
                        or not instrument.price_satisfies_limit(candidate.intent.limit_price, review.price_ceiling)
                        or candidate.intent.max_fee_usd > review.fee_ceiling_usd
                        or candidate.intent.reserved_cost_usd > min(review.debit_ceiling_usd, review.max_cost_usd)
                        or candidate.ev_per_usd_payout < review.minimum_ev
                        or candidate.roi < review.minimum_roi
                        or (review.minimum_sensitivity_ev is not None and (
                            candidate.sensitivity_ev_per_usd_payout is None
                            or candidate.sensitivity_ev_per_usd_payout < review.minimum_sensitivity_ev))
                        or (review.minimum_sensitivity_roi is not None and (
                            candidate.sensitivity_roi is None
                            or candidate.sensitivity_roi < review.minimum_sensitivity_roi))):
                    raise RiskRejected("Fresh price, size, debit or edge exceeds the approved bounds")
                final_digest = self._proposal_digest(candidate=candidate, order=order,
                    profile_digest=profile_digest, max_cost_usd=review.max_cost_usd,
                    created_at=review.created_at, expires_at=review.expires_at)
                storage = getattr(self.engine, "storage", None)
                if storage is not None:
                    from .models import Envelope
                    evidence = Envelope(schema_name="manual.final_validation", source="manual_native",
                        payload={"original_proposal_digest": review.proposal_digest,
                                 "authorization_scope_digest": review.scope_digest,
                                 "final_validation_digest": final_digest,
                                 "candidate": candidate.model_dump(mode="json"),
                                 "book": self.engine.native.books[
                                     (candidate.market_id, candidate.venue, candidate.outcome)].model_dump(mode="json")})
                    await storage.event(evidence, durable=True)
                with self.engine.risk.manual_order(candidate.intent.id):
                    order = await self.engine.native.execute(candidate, execution_origin="manual")
                return {"order": order.model_dump(mode="json"), "automation_enabled": self.engine.risk.auto_trade,
                        "final_validation_digest": final_digest,
                        "note": "Order acknowledgement is not a fill. Check the native ledger and reconciliation."}
            finally:
                # Consuming the owner confirmation always returns this local
                # workspace to a disarmed state, including unknown outcomes.
                self.engine.risk.halt("Single-use manual review consumed; reconcile before another attempt")
