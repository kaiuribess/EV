"""Pure pricing functions. No IO, provider transports, execution, or mutable configuration."""
from datetime import datetime
from decimal import Decimal, ROUND_CEILING
from math import isfinite

from .config import PricingConfig
from .models import Candidate, FairValue, FeeSchedule, Market, OrderBook, Quote, utcnow

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
    override = config.market_overrides.get(f"{market.sport}:{market.market_type}", {})
    return PricingConfig.model_validate({**config.model_dump(), **override})


def fair_value(market: Market, quotes: list[Quote], config: PricingConfig,
               now: datetime | None = None) -> FairValue:
    now = now or utcnow()
    config = market_config(config, market)
    if market.status in {"suspended", "closed"}:
        raise NoSignal("Market suspended or closed")
    # Select once per bookmaker; multiple API vendors are not independent evidence.
    by_book: dict[str, Quote] = {}
    for quote in quotes:
        if quote.market_id != market.id or quote.status != "active":
            continue
        if set(quote.decimal_odds) != set(market.outcomes):
            continue
        if not quote.fresh(config.max_quote_age_ms, now):
            continue
        current = by_book.get(quote.bookmaker)
        ranking = lambda q: (q.source_time, q.quality, config.provider_priority.get(q.provider, 0))
        if current is None or ranking(quote) > ranking(current):
            by_book[quote.bookmaker] = quote
    if len(by_book) < config.min_references:
        raise NoSignal("Insufficient fresh independent references")
    missing = set(config.required_books) - set(by_book)
    if missing:
        raise NoSignal("Required reference unavailable: " + ", ".join(sorted(missing)))
    probabilities = {book: devig(q.decimal_odds, config.devig_method) for book, q in by_book.items()}
    disagreement = max(max(p[outcome] for p in probabilities.values()) -
                       min(p[outcome] for p in probabilities.values()) for outcome in market.outcomes)
    if disagreement > config.max_disagreement:
        raise NoSignal("Reference disagreement exceeds configured limit")
    weights = {book: config.weights.get(book, D(1)) for book in by_book}
    total = sum(weights.values())
    result = {outcome: sum(probabilities[b][outcome] * weights[b] for b in by_book) / total
              for outcome in market.outcomes}
    references = tuple({
        "bookmaker": book, "provider": quote.provider, "age_ms": quote.age_ms(now),
        "source_time": quote.source_time.isoformat(), "weight": str(weights[book]),
        "probabilities": {k: str(v) for k, v in probabilities[book].items()},
    } for book, quote in sorted(by_book.items()))
    return FairValue(market_id=market.id, probabilities=result, references=references,
                     model=config.devig_method, calculated_at=now, disagreement=disagreement,
                     synthetic=any(q.synthetic for q in by_book.values()))


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
    if not book.synchronized or book.status != "active" or not book.asks:
        raise NoSignal("Order book unavailable or not synchronized")
    if not book.fresh(config.max_book_age_ms, now):
        raise NoSignal("Stale order book")
    if not book.fee.verified:
        raise NoSignal("Fee schedule is not verified")
    p = fair.probabilities[book.outcome]
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
    # Candidate uses best-price depth only. Expanding into deeper levels requires repricing.
    available = sum(level.quantity for level in book.asks if level.price == price)
    fees = expected_fee(book.fee, price, available, p)
    ev = p - price - fees / available
    return Candidate(
        strategy="positive_ev", market_id=market.id, venue=book.venue, outcome=book.outcome,
        price=price, fair_probability=p, ev_per_contract=ev, roi=ev / price,
        max_quantity=available, book_sequence=book.sequence, references=fair.references,
        calculated_at=now, quote_age_ms=max([book.age_ms(now)] +
                                          [r["age_ms"] for r in fair.references]),
        synthetic=book.synthetic or fair.synthetic, size_curve=tuple(curve),
    )

