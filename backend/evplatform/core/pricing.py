"""Pure pricing functions. No IO, provider transports, execution, or mutable configuration."""
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING
from math import isfinite

from .config import PricingConfig
from .models import Candidate, FairValue, FeeSchedule, Market, OrderBook, Quote, utcnow
from .reference_profiles import freshness_rule, policy_digest, profile_for_market, quote_eligibility

D = Decimal


def american_to_decimal(odds: Decimal) -> Decimal:
    if not odds.is_finite() or abs(odds) < 100:
        raise ValueError("American odds must be <= -100 or >= +100")
    return 1 + (odds / 100 if odds > 0 else 100 / abs(odds))


def devig(odds: dict[str, Decimal], method="proportional") -> dict[str, Decimal]:
    if len(odds) < 2 or any(not v.is_finite() or v <= 1 for v in odds.values()):
        raise ValueError("Complete, finite odds > 1 required")
    implied = {k: 1 / v for k, v in odds.items()}
    if method == "proportional":
        total = sum(implied.values())
        return {k: v / total for k, v in implied.items()}
    if method != "power":
        raise ValueError("Unsupported devig method")
    # Solve sum(q_i ** k) = 1; monotonic for 0 < q_i < 1.
    lo, hi = 0.0, 1024.0
    for _ in range(100):
        mid = (lo + hi) / 2
        if sum(float(v) ** mid for v in implied.values()) > 1:
            lo = mid
        else:
            hi = mid
    values = {k: D(str(float(v) ** ((lo + hi) / 2))) for k, v in implied.items()}
    total = sum(values.values())
    if not isfinite(float(total)) or total <= 0:
        raise ValueError("Power devig did not converge")
    return {k: v / total for k, v in values.items()}


def expected_fee(schedule: FeeSchedule, price: Decimal, quantity: int, probability: Decimal) -> Decimal:
    if quantity <= 0:
        return D(0)
    if not schedule.verified:
        raise ValueError("Unverified fee schedule")
    if schedule.model == "none":
        return D(0)
    if schedule.model == "fixed":
        raw = schedule.per_contract * quantity
    elif schedule.model == "binary_quadratic":
        raw = schedule.rate * quantity * price * (1 - price)
    elif schedule.model == "profit_commission":
        raw = probability * (1 - price) * quantity * schedule.rate
    else:
        raise ValueError("Unknown fee model")
    return (raw / schedule.rounding).to_integral_value(rounding=ROUND_CEILING) * schedule.rounding


def worst_fee(schedule: FeeSchedule, price: Decimal, quantity: int) -> Decimal:
    return expected_fee(schedule, price, quantity, D(1))


class NoSignal(ValueError):
    pass


def market_config(config: PricingConfig, market: Market):
    override = config.market_overrides.get(f"{market.sport}:{market.market_type}")
    if override is None:
        return config
    # Overrides were validated independently against the global configuration.
    # A resolved market policy must not rebase unrelated sports' overrides.
    return PricingConfig.model_validate({**config.model_dump(), **override, "market_overrides": {}})


def resolved_reference_policy(config: PricingConfig, market: Market, now: datetime):
    config = market_config(config, market)
    try:
        selected = profile_for_market(config, market, now)
    except ValueError as exc:
        raise NoSignal(str(exc)) from exc
    if selected is None:
        return config, None, None, None
    name, profile = selected
    digest = policy_digest(config, market)
    resolved = config.model_copy(update={"devig_method": profile.primary_devig,
        "weights": profile.weights_percent, "eligible_books": list(profile.weights_percent),
        "min_references": profile.min_distinct_books})
    return resolved, name, profile, digest


def fair_value(market: Market, quotes: list[Quote], config: PricingConfig,
               now: datetime | None = None) -> FairValue:
    now = now or utcnow()
    config, profile_id, profile, digest = resolved_reference_policy(config, market, now)
    if market.status in {"suspended", "closed"}:
        raise NoSignal("Market suspended or closed")
    # Select once per bookmaker; multiple API vendors are not independent evidence.
    by_book: dict[str, Quote] = {}
    diagnostics: dict[str, tuple[bool, tuple[str, ...], float | None]] = {}
    conflicts = set()
    eligible = None if config.eligible_books is None else set(config.eligible_books)
    for quote in quotes:
        if quote.market_id != market.id or quote.status != "active":
            continue
        if not quote.bookmaker or quote.bookmaker != quote.bookmaker.strip():
            continue
        if eligible is not None and quote.bookmaker not in eligible:
            continue
        if set(quote.decimal_odds) != set(market.outcomes):
            continue
        if profile is None and not quote.fresh(config.max_quote_age_ms, now):
            continue
        if quote.received_at > now or quote.provider_time is not None and quote.provider_time > now:
            continue
        if profile:
            eligibility = quote_eligibility(config, market, quote, now)
            clock = quote.source_observed_at if quote.observation_verified else quote.source_time
            if clock is not None and clock > now:
                continue
        else:
            eligibility = (True, (), quote.age_ms(now))
        current = by_book.get(quote.bookmaker)
        def ranking(q):
            if profile is None:
                return q.source_time, q.quality, config.provider_priority.get(q.provider, 0)
            valid = quote_eligibility(config, market, q, now)[0]
            clock = q.source_observed_at if q.observation_verified else q.source_time
            return valid, clock or datetime.min.replace(tzinfo=timezone.utc)
        if (profile and current is not None and ranking(quote) == ranking(current)
                and quote.decimal_odds != current.decimal_odds):
            conflicts.add(quote.bookmaker)
        if current is None or ranking(quote) > ranking(current):
            by_book[quote.bookmaker] = quote
            diagnostics[quote.bookmaker] = eligibility
            conflicts.discard(quote.bookmaker)
    if profile:
        usable = {book: quote for book, quote in by_book.items() if diagnostics[book][0] and book not in conflicts}
        if not conflicts and len(usable) >= config.min_references and set(profile.required_any_anchor) & set(usable) and not (set(config.required_books) - set(usable)):
            by_book = usable
    if len(by_book) < config.min_references:
        raise NoSignal("Insufficient fresh independent references")
    missing = set(config.required_books) - set(by_book)
    if missing:
        raise NoSignal("Required reference unavailable: " + ", ".join(sorted(missing)))
    if profile and not set(profile.required_any_anchor) & set(by_book):
        raise NoSignal("No required reference-profile anchor is available")
    probabilities = {book: devig(q.decimal_odds, config.devig_method) for book, q in by_book.items()}
    disagreement = max(max(p[outcome] for p in probabilities.values()) -
                       min(p[outcome] for p in probabilities.values()) for outcome in market.outcomes)
    if profile is None and disagreement > config.max_disagreement:
        raise NoSignal("Reference disagreement exceeds configured limit")
    weights = {book: config.weights.get(book, D(1)) for book in by_book}
    total = sum(weights.values())
    result = {outcome: sum(probabilities[b][outcome] * weights[b] for b in by_book) / total
              for outcome in market.outcomes}
    sensitivity = {book: devig(q.decimal_odds, profile.sensitivity_devig) for book, q in by_book.items()} if profile else None
    sensitivity_result = ({outcome: sum(sensitivity[b][outcome] * weights[b] for b in by_book) / total
                           for outcome in market.outcomes} if sensitivity else None)
    reasons = []
    if profile:
        for book in by_book:
            reasons.extend(f"{book}: {reason}" for reason in diagnostics[book][1])
            if book in conflicts:
                reasons.append(f"{book}: Conflicting equally recent source observations")
        if disagreement > config.max_disagreement:
            reasons.append("Reference disagreement exceeds configured limit")
        sensitivity_disagreement = max(max(p[o] for p in sensitivity.values()) - min(p[o] for p in sensitivity.values()) for o in market.outcomes)
        if sensitivity_disagreement > config.max_disagreement:
            reasons.append("Sensitivity-model reference disagreement exceeds configured limit")
    references = []
    for book, quote in sorted(by_book.items()):
        observed = quote.source_observed_at if profile and quote.observation_verified else quote.source_time
        evidence = {"bookmaker": book, "provider": quote.provider,
            "age_ms": (now - observed).total_seconds() * 1000 if observed else None,
            "source_time": observed.isoformat() if observed else None, "weight": str(weights[book]),
            "probabilities": {k: str(v) for k, v in probabilities[book].items()}}
        if profile:
            rule = freshness_rule(config, market, quote.provider, quote.bookmaker)
            eligible = diagnostics[book][0] and book not in conflicts
            evidence.update({"source_observed_at": quote.source_observed_at.isoformat() if quote.source_observed_at else None,
                "source_price_changed_at": quote.source_price_changed_at.isoformat() if quote.source_price_changed_at else None,
                "observation_verified": quote.observation_verified, "observation_complete": quote.observation_complete,
                "market_status_verified": quote.market_status_verified, "execution_eligible": eligible,
                "freshness_factor": "1" if eligible else "0", "ttl_seconds": str(rule.ttl_seconds) if rule else None,
                "sensitivity_probabilities": {k: str(v) for k, v in sensitivity[book].items()}})
        references.append(evidence)
    return FairValue(market_id=market.id, probabilities=result, references=references,
                     model=config.devig_method, calculated_at=now, disagreement=disagreement,
                     synthetic=any(q.synthetic for q in by_book.values()),
                     reference_profile_id=profile_id, reference_policy_digest=digest,
                     reference_profile_status=profile.status if profile else None,
                     live_execution_allowed=profile.live_execution_allowed if profile else True,
                     execution_eligible=not reasons, ineligibility_reasons=tuple(dict.fromkeys(reasons)),
                     sensitivity_model=profile.sensitivity_devig if profile else None,
                     sensitivity_probabilities=sensitivity_result)


def validate_fair_references(market: Market, fair: FairValue, config: PricingConfig,
                             now: datetime) -> None:
    """Recheck cached fair evidence at use time, including current reference policy.

    Calculation time cannot refresh a sportsbook's source clock. A changed
    devig/weight/eligibility policy requires a newly calculated fair value.
    """
    config, profile_id, profile, digest = resolved_reference_policy(config, market, now)
    if fair.reference_profile_id != profile_id or fair.reference_policy_digest != digest:
        raise NoSignal("Reference profile policy changed; recalculation required")
    if profile and (fair.live_execution_allowed != profile.live_execution_allowed
                    or fair.reference_profile_status != profile.status):
        raise NoSignal("Reference profile permission or status changed")
    if fair.market_id != market.id or set(fair.probabilities) != set(market.outcomes):
        raise NoSignal("Fair value does not match the normalized market")
    probabilities = list(fair.probabilities.values())
    if (any(not p.is_finite() or not 0 <= p <= 1 for p in probabilities)
            or abs(sum(probabilities) - 1) > D("0.00000001")):
        raise NoSignal("Invalid fair probability distribution")
    if fair.calculated_at.tzinfo is None:
        raise NoSignal("Fair-value calculation clock is unavailable")
    fair_age = (now - fair.calculated_at).total_seconds() * 1000
    if not 0 <= fair_age <= config.max_quote_age_ms:
        raise NoSignal("Fair-value calculation is stale or future-dated")
    if fair.model != config.devig_method:
        raise NoSignal("Fair-value method changed; recalculation required")
    if profile:
        sensitivity = fair.sensitivity_probabilities
        if (fair.sensitivity_model != profile.sensitivity_devig or sensitivity is None
                or set(sensitivity) != set(market.outcomes)
                or any(not p.is_finite() or not 0 <= p <= 1 for p in sensitivity.values())
                or abs(sum(sensitivity.values()) - 1) > D("0.00000001")):
            raise NoSignal("Complete sensitivity-model probabilities are required")
    books = set()
    eligible = None if config.eligible_books is None else set(config.eligible_books)
    for reference in fair.references:
        try:
            raw_time = reference["source_time"]
            source_time = datetime.fromisoformat(raw_time) if raw_time is not None else None
            if source_time is not None and source_time.tzinfo is None:
                raise ValueError("Naive reference clock")
            reference_age = (now - source_time).total_seconds() * 1000 if source_time else None
            bookmaker = reference["bookmaker"]
            if not isinstance(bookmaker, str) or not bookmaker or bookmaker != bookmaker.strip():
                raise ValueError("Missing bookmaker")
            weight = D(reference["weight"])
            if not weight.is_finite() or weight <= 0:
                raise ValueError("Invalid weight")
        except (TypeError, ValueError, KeyError, ArithmeticError) as exc:
            raise NoSignal("Reference source clock/book identity/weight is unavailable") from exc
        if profile:
            rule = freshness_rule(config, market, reference.get("provider"), bookmaker)
            if fair.execution_eligible and (rule is None or source_time is None
                    or reference.get("source_observed_at") != raw_time
                    or any(reference.get(flag) is not True for flag in (
                        "execution_eligible", "observation_verified", "observation_complete", "market_status_verified"))
                    or reference_age < 0 or D(str(reference_age)) > rule.ttl_seconds * 1000):
                raise NoSignal("Reference profile observation/TTL eligibility expired or is unverified")
        elif reference_age is None or not 0 <= reference_age <= config.max_quote_age_ms:
            raise NoSignal("Fair-value reference expired or is future-dated")
        if bookmaker in books:
            raise NoSignal("Reference evidence double-counts a bookmaker")
        if eligible is not None and bookmaker not in eligible:
            raise NoSignal("Fair-value reference is no longer eligible")
        if weight != config.weights.get(bookmaker, D(1)):
            raise NoSignal("Fair-value reference weight changed; recalculation required")
        books.add(bookmaker)
    if len(books) < config.min_references or set(config.required_books) - books:
        raise NoSignal("Independent reference requirements are no longer satisfied")
    if profile and not set(profile.required_any_anchor) & books:
        raise NoSignal("Required reference-profile anchor is no longer available")
    if not fair.disagreement.is_finite() or fair.disagreement < 0 or (
            (profile is None or fair.execution_eligible) and fair.disagreement > config.max_disagreement):
        raise NoSignal("Reference disagreement exceeds configured limit")


def opportunity(market: Market, fair: FairValue, book: OrderBook, config: PricingConfig,
                now: datetime | None = None) -> Candidate:
    now = now or utcnow()
    config = market_config(config, market)
    if market.settlement.contract_type != "binary" or market.settlement.payout != 1:
        raise NoSignal("Strategy requires a verified $1 binary payout adapter")
    if market.settlement.push_rule != "not_applicable":
        raise NoSignal("Push probability model is required for this contract")
    if book.market_id != market.id or fair.market_id != market.id or book.outcome not in fair.probabilities:
        raise NoSignal("Book does not match the normalized market")
    if market.status in {"suspended", "closed"}:
        raise NoSignal("Market suspended or closed")
    validate_fair_references(market, fair, config, now)
    if not book.synchronized or book.status != "active" or not book.asks:
        raise NoSignal("Order book unavailable or not synchronized")
    if not book.fresh(config.max_book_age_ms, now):
        raise NoSignal("Stale order book")
    if book.received_at > now or book.provider_time is not None and book.provider_time > now:
        raise NoSignal("Order-book receipt/publication clock is future-dated")
    if not book.fee.verified:
        raise NoSignal("Fee schedule is not verified")
    p = fair.probabilities[book.outcome]
    sensitivity_p = fair.sensitivity_probabilities[book.outcome] if fair.sensitivity_probabilities else None
    price = book.asks[0].price
    curve, qty, cost = [], 0, D(0)
    for level in book.asks:
        qty += level.quantity
        cost += level.price * level.quantity
        avg = cost / qty
        fees = expected_fee(book.fee, avg, qty, p)
        profit = p * qty - cost - fees
        curve.append({"quantity": qty, "limit_price": str(level.price),
                      "cost": str(cost), "expected_fees": str(fees),
                      "expected_profit": str(profit), "roi": str(profit / cost),
                      "average_price": str(avg), "slippage": str(avg - price)})
        if sensitivity_p is not None:
            sensitivity_fees = expected_fee(book.fee, avg, qty, sensitivity_p)
            sensitivity_profit = sensitivity_p * qty - cost - sensitivity_fees
            curve[-1].update({"sensitivity_expected_fees": str(sensitivity_fees),
                "sensitivity_expected_profit": str(sensitivity_profit), "sensitivity_roi": str(sensitivity_profit / cost),
                "execution_eligible": fair.execution_eligible and profit / qty >= config.min_ev
                    and profit / cost >= config.min_roi and sensitivity_profit / qty >= config.min_ev
                    and sensitivity_profit / cost >= config.min_roi})
    # Candidate uses best-price depth only. Expanding into deeper levels requires repricing.
    available = sum(level.quantity for level in book.asks if level.price == price)
    fees = expected_fee(book.fee, price, available, p)
    ev = p - price - fees / available
    sensitivity_ev = (sensitivity_p - price - expected_fee(book.fee, price, available, sensitivity_p) / available
                      if sensitivity_p is not None else None)
    reasons = list(fair.ineligibility_reasons)
    if sensitivity_ev is not None and (min(ev, sensitivity_ev) < config.min_ev or min(ev / price, sensitivity_ev / price) < config.min_roi):
        reasons.append("Both primary and sensitivity models must meet EV and ROI thresholds")
    self_reference = any(reference["bookmaker"] == book.venue for reference in fair.references)
    if self_reference:
        reasons.append("Target venue cannot be its own independent reference")
    return Candidate(
        strategy="positive_ev", market_id=market.id, venue=book.venue, outcome=book.outcome,
        price=price, fair_probability=p, ev_per_contract=ev, roi=ev / price,
        max_quantity=available, book_sequence=book.sequence, references=fair.references,
        calculated_at=now, quote_age_ms=max([book.age_ms(now)] +
                                          [r["age_ms"] for r in fair.references if r["age_ms"] is not None]),
        synthetic=book.synthetic or fair.synthetic, size_curve=tuple(curve),
        reference_profile_id=fair.reference_profile_id, reference_policy_digest=fair.reference_policy_digest,
        reference_profile_status=fair.reference_profile_status, live_execution_allowed=fair.live_execution_allowed,
        execution_eligible=fair.execution_eligible and not reasons, ineligibility_reasons=tuple(dict.fromkeys(reasons)),
        reference_inputs_eligible=fair.execution_eligible and not self_reference,
        primary_model=fair.model, sensitivity_model=fair.sensitivity_model,
        sensitivity_fair_probability=sensitivity_p, sensitivity_ev_per_contract=sensitivity_ev,
        sensitivity_roi=sensitivity_ev / price if sensitivity_ev is not None else None,
    )


def validate_candidate_size(candidate: Candidate, fee: FeeSchedule, price: Decimal,
                            quantity: int, config: PricingConfig) -> tuple[Decimal, Decimal]:
    """Require both model edges at the actual standing limit and sized fee cap.

    Returns the conservative EV and ROI for routing checks; the candidate keeps
    each model's separate estimate for audit and research display.
    """
    if not candidate.execution_eligible or quantity <= 0:
        raise NoSignal("Candidate is research-only or has no executable quantity")
    probabilities = [candidate.fair_probability]
    if candidate.reference_profile_id is not None:
        if candidate.sensitivity_fair_probability is None:
            raise NoSignal("Reference profile requires sensitivity probabilities")
        probabilities.append(candidate.sensitivity_fair_probability)
    fees = worst_fee(fee, price, quantity)
    ev = min(p - price - fees / quantity for p in probabilities)
    roi = ev / price
    if ev < config.min_ev or roi < config.min_roi:
        raise NoSignal("Both model edges must qualify at the actual size, limit and fee cap")
    return ev, roi

