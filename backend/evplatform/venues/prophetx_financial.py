"""Read-only ProphetX reporting and wallet evidence, with no guessed money.

Official contracts (reviewed 2026-10-21):
https://docs.prophetx.co/reference/get_v4-mm-get-order-history
https://docs.prophetx.co/reference/get_v4-mm-get-order-matched-detail
https://docs.prophetx.co/reference/get_v4-mm-get-transactions
https://docs.prophetx.co/docs/wallets
https://docs.prophetx.co/docs/direct-link-order-lifecycle

REST reports lag the matching engine. A missing row/fee/clock never proves
absence or zero. Wallet `trade_id` must equal the owned order ID; no approximate
join, allocation across orders, or promotional/exposure-credit accounting.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import math

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from ..core.models import utcnow
from ..core.native_accounting import NativeOrderIntent
from .common import decimal_value
from .reconciliation import (
    NativeOrderReport, ReconciliationError, _identity, _number, _object,
    _snapshot, _text, _time, normalize_prophetx_order,
)


class ProphetXAccountReview(BaseModel):
    """Operator-reviewed account facts; never populated by a successful login.

    Complete-history reviews start before the first possible order. A separate
    current-empty-inventory review can instead use a recent reviewed baseline
    plus all subsequent order updates and fresh zero-exposure wallet balances.
    Neither route can infer absent LEC from `get_balance`; funding scope needs
    separate account evidence. This review does not authorize trading.
    """
    model_config = ConfigDict(extra="forbid", frozen=True)
    account_fingerprint: str = Field(min_length=64, max_length=64)
    history_created_from: AwareDatetime | None = None
    history_scope_evidence: str = ""
    current_inventory_reviewed_at: AwareDatetime | None = None
    current_inventory_scope_evidence: str = ""
    funded_cash_only: bool = False
    funding_scope_evidence: str = Field(min_length=16)
    valid_until: AwareDatetime

    @model_validator(mode="after")
    def one_inventory_basis(self):
        complete = self.history_created_from is not None
        current = self.current_inventory_reviewed_at is not None
        if complete == current:
            raise ValueError("Exactly one ProphetX inventory review basis is required")
        evidence = self.history_scope_evidence if complete else self.current_inventory_scope_evidence
        if len(evidence.strip()) < 16:
            raise ValueError("Account-specific inventory review evidence is required")
        return self


@dataclass(frozen=True)
class ProphetXHistory:
    rows: tuple[dict, ...]
    last_synced_at: datetime | None
    end_marker: str | None = None


def prophetx_fingerprint(environment: str, user_id: str) -> str:
    if environment not in {"sandbox", "production"}:
        raise ReconciliationError("Unknown ProphetX environment")
    return hashlib.sha256(f"prophetx:{environment}:user:{_text(user_id, 'signed user identity')}".encode()).hexdigest()


def authenticated_identity(client) -> tuple[str, str]:
    # Only the server-signed identity after successful private sign-in counts.
    if not client._signin_confirmed or not client.health_check().get("authenticated"):
        raise ReconciliationError("ProphetX confirmed authenticated private identity is required")
    user = _text(client._authenticated_user_id, "signed user identity")
    return user, prophetx_fingerprint(client.settings.environment, user)


def source_time(value, *, observed_at: datetime, name: str, max_age: timedelta | None = None,
                max_future_skew: timedelta = timedelta(0)) -> datetime:
    """Only documented ISO REST clocks; integer book-clock units are unknown."""
    if not isinstance(value, (str, datetime)):
        raise ReconciliationError(f"{name} requires an explicit ISO source timestamp; integer units are unverified")
    result, observed = _time(value), _time(observed_at)
    if max_future_skew < timedelta(0):
        raise ValueError("Future clock skew tolerance cannot be negative")
    if result > observed + max_future_skew or (max_age is not None and observed - result > max_age):
        raise ReconciliationError(f"{name} is stale or in the future")
    return result


def reporting_cursor(data: dict, *, endpoint: str, collection: str) -> tuple[str | None, str | None]:
    """Interpret the cursor, separately from account/history reconciliation.

    The wallet guide documents an absent final cursor. The authenticated order
    history service also returns an empty orders array with an explicit null
    cursor. Support that observed representation only for this endpoint/shape;
    never generalize it to nonempty pages or another venue's pagination.
    """
    if "next_cursor" not in data:
        return None, "absent"
    cursor = data["next_cursor"]
    if cursor == "":
        return None, "empty_string"
    if (cursor is None and endpoint == "/v4/mm/get_order_history" and collection == "orders"
            and data.get("orders") == []):
        return None, "observed_empty_order_null"
    if not isinstance(cursor, str) or not cursor.strip():
        raise ReconciliationError("Invalid ProphetX reporting cursor")
    return cursor, None


async def reporting_pages(client, endpoint: str, collection: str, *, params=None,
                          observed_at: datetime | None = None, require_clock=True,
                          max_age=timedelta(seconds=10), max_pages=20) -> ProphetXHistory:
    """Follow documented next_cursor, reject loops/partial or stale responses."""
    allowed = {("/v4/mm/get_order_history", "orders"),
               ("/v4/mm/get_order_matched_detail", "matching_details"),
               ("/v4/mm/get_transactions", "transactions"), ("/v4/mm/get_trades", "trades")}
    if (endpoint, collection) not in allowed or not 1 <= max_pages <= 1000:
        raise ReconciliationError("Unsupported ProphetX reporting request")
    params = dict(params or {})
    if "next_cursor" in params:
        raise ReconciliationError("Full reporting history must start on the first page")
    rows, cursors, clocks, cursor = [], set(), [], None
    # The published order and wallet-history APIs accept 1,000 rows per page.
    # Larger pages reduce backfill duration under the account-key request pace;
    # other report families retain the conservative size.
    page_size = 1000 if endpoint in {"/v4/mm/get_order_history", "/v4/mm/get_transactions"} else 100
    for _ in range(max_pages):
        raw = _object(await client.request("GET", endpoint,
            params={**params, "limit": page_size, **({"next_cursor": cursor} if cursor else {})}), "report")
        now = observed_at or utcnow()
        if require_clock or endpoint == "/v4/mm/get_order_history":
            clocks.append(source_time(raw.get("last_synced_at"), observed_at=now,
                                      name="Reporting watermark", max_age=max_age))
        data = _object(raw.get("data"), "report data")
        page = data.get(collection)
        if not isinstance(page, list):
            raise ReconciliationError("Incomplete ProphetX reporting collection")
        rows.extend(_object(row, "report row") for row in page)
        cursor, end_marker = reporting_cursor(data, endpoint=endpoint, collection=collection)
        if cursor is None:
            return ProphetXHistory(tuple(rows), min(clocks) if clocks else None, end_marker)
        if cursor in cursors:
            raise ReconciliationError("Invalid or repeated ProphetX reporting cursor")
        cursors.add(cursor)
    raise ReconciliationError("ProphetX reporting exceeds page bound; history is incomplete")


async def complete_order_history(client, review: ProphetXAccountReview, *, observed_at=None,
                                 max_windows=64, max_age=timedelta(seconds=10)) -> ProphetXHistory:
    now = observed_at or utcnow()
    _, fingerprint = authenticated_identity(client)
    if (review.account_fingerprint != fingerprint or review.valid_until <= now or
            review.history_created_from is None or review.history_created_from >= now or
            not review.funded_cash_only):
        raise ReconciliationError("Current account-specific funded-cash/history review is required")
    start, end = math.floor(review.history_created_from.timestamp()), math.ceil(now.timestamp())
    # The current Trading API permits a 60-day created-at window. Keep exact
    # contiguous boundaries so a boundary order is included and deduplicated.
    window_seconds = 60 * 86400
    if math.ceil((end-start)/window_seconds) > max_windows:
        raise ReconciliationError("Complete account history exceeds date-window bound")
    by_id, clocks = {}, []
    while start < end:
        stop = min(end, start+window_seconds)
        history = await reporting_pages(client, "/v4/mm/get_order_history", "orders",
            params={"from": start, "to": stop}, observed_at=observed_at, max_age=max_age)
        clocks.append(history.last_synced_at)
        for row in history.rows:
            native_id = _text(row.get("order_id"), "history order identity")
            created = source_time(row.get("created_at"), observed_at=now, name="Order creation")
            if not start <= created.timestamp() <= stop:
                raise ReconciliationError("Order history escaped the requested date window")
            if native_id in by_id and by_id[native_id] != row:
                raise ReconciliationError("Order changed during paginated history; resnapshot required")
            by_id[native_id] = row
        # Both boundaries are inclusive. Identical overlap is deduplicated;
        # different overlap is a concurrent change, not an accepted snapshot.
        start = stop
    return ProphetXHistory(tuple(by_id.values()), min(clocks))


def unique_rows(rows, identity_key: str) -> tuple[dict, ...]:
    indexed = {}
    for row in rows:
        row = _object(row, "history row")
        key = row.get(identity_key)
        if isinstance(key, bool) or not isinstance(key, (int, str)) or not str(key).strip():
            raise ReconciliationError("Stable ProphetX history identity is required")
        if key in indexed and indexed[key] != row:
            raise ReconciliationError("Conflicting ProphetX history identity")
        indexed[key] = row
    return tuple(indexed.values())


def validate_wallet_rows(rows, *, user_id, order_id, market_id, observed_at):
    """Require explicit completed cash movements with exact owned-order joins."""
    result = []
    for row in unique_rows(rows, "details"):
        _text(row.get("details"), "wallet transaction idempotency identity")
        if (row.get("user_id") != user_id or row.get("trade_id") != order_id or
                str(row.get("market_id")) != str(market_id)):
            raise ReconciliationError("Wallet transaction does not identify this owned order")
        if row.get("status") != "Completed":
            raise ReconciliationError("Pending/failed wallet transaction is not cash evidence")
        if row.get("transaction_sub_type") not in {"OTHER", "COMMISSION"}:
            raise ReconciliationError("Promotional, refunded or resettled cash requires review")
        if row.get("transaction_type") not in {"TRADE", "COMMISSION", "PAY"}:
            raise ReconciliationError("Unsupported refund, credit or cash correction")
        amount = _number(row.get("amount"), "wallet magnitude")
        change = decimal_value(row.get("change"))
        before = _number(row.get("balance_before"), "wallet balance before")
        after = _number(row.get("balance"), "wallet balance after")
        if after-before != change or abs(change) != amount:
            raise ReconciliationError("Wallet cash arithmetic does not reconcile")
        if (row["transaction_type"] in {"TRADE", "COMMISSION"} and change > 0) or (row["transaction_type"] == "PAY" and change < 0):
            raise ReconciliationError("Wallet cash direction contradicts transaction type")
        source_time(row.get("created_at"), observed_at=observed_at, name="Wallet transaction")
        result.append(row)
    return tuple(result)


def normalize_financial_order(intent: NativeOrderIntent, row: dict, matching_rows, wallet_rows, *,
                              user_id: str, expected_order_id: str, observed_at: datetime,
                              reporting_watermark: datetime) -> NativeOrderReport:
    """Cumulative accounting from actual matched profit plus actual commission.

    No allocation of aggregate commission to individual fills is manufactured.
    The result is an authoritative cumulative snapshot, not invented fill rows.
    """
    if intent.instrument.venue != "prophetx" or intent.instrument.quantity_unit != "usd_stake":
        raise ReconciliationError("ProphetX USD-stake intent required")
    reporting_watermark = source_time(reporting_watermark, observed_at=observed_at,
        name="Matched reporting watermark", max_age=timedelta(seconds=10))
    native_id = _identity(intent, row, "order_id", "external_id", expected_order_id)
    if row.get("user_id") != user_id or row.get("strike_id") != intent.instrument.instrument_id:
        raise ReconciliationError("ProphetX order account/instrument mismatch")
    _number(row.get("market_id"), "native market identity", positive=True, whole=True)
    updated = source_time(row.get("updated_at"), observed_at=observed_at, name="Order update")
    created = source_time(row.get("created_at"), observed_at=observed_at, name="Order creation")
    if created < intent.created_at-timedelta(seconds=1) or updated < created or reporting_watermark < updated:
        raise ReconciliationError("Reporting watermark does not cover the owned order")
    # The ordinary normalizer rejects closed settlement states intentionally.
    # Here only its execution quantities are reused; settlement is validated by
    # a separate wallet-backed adapter and never releases exposure here.
    execution_row = dict(row)
    if row.get("status") == "closed":
        if row.get("winning_status") not in {"won", "lost", "profit", "loss"}:
            raise ReconciliationError("Unsupported ProphetX settlement grade")
        execution_row["status"] = "open"
    status = normalize_prophetx_order(intent, {"data": execution_row},
        expected_order_id=native_id, observed_at=observed_at)
    if status.status == "acknowledged":
        raise ReconciliationError("Pre-match acknowledgement is not financial evidence")
    total, payout = Decimal(0), Decimal(0)
    for fill in unique_rows(matching_rows, "id"):
        if isinstance(fill["id"], bool) or not isinstance(fill["id"], int) or fill["id"] <= 0:
            raise ReconciliationError("Positive native matching ID required")
        if fill.get("order_id") != native_id:
            raise ReconciliationError("Matched detail belongs to a different order")
        qty = _number(fill.get("quantity"), "matched stake", positive=True)
        profit = _number(fill.get("profit"), "actual matched profit")
        price = decimal_value(fill.get("price"))
        intent.instrument.validate_price(price)
        intent.instrument.validate_quantity(qty)
        paid = qty+profit
        if not intent.instrument.payout_bound(qty, price) <= paid <= intent.instrument.payout_bound(qty, price, upper=True):
            raise ReconciliationError("Actual matched profit contradicts native price/rounding")
        if paid < intent.instrument.payout_bound(qty, intent.limit_price):
            raise ReconciliationError("Matched execution breached the durable limit")
        when = source_time(fill.get("matched_at"), observed_at=observed_at, name="Actual match")
        if when < created or when > updated or when > reporting_watermark:
            raise ReconciliationError("Matched detail lies outside order/reporting clocks")
        total += qty
        payout += paid
    if total != _number(row.get("filled_quantity"), "filled stake") or total > intent.quantity:
        raise ReconciliationError("Matched history does not cover cumulative filled stake")
    # History guide documents fee; v4 reference omits it. Its absence remains
    # unsupported even when the wallet has no COMMISSION row; absence != zero.
    fee = _number(row.get("fee"), "explicit actual order fee")
    wallet = validate_wallet_rows(wallet_rows, user_id=user_id, order_id=native_id,
                                  market_id=row.get("market_id"), observed_at=observed_at)
    if any(source_time(x["created_at"], observed_at=observed_at, name="Wallet movement") < created for x in wallet):
        raise ReconciliationError("Wallet movement precedes the owned order")
    cost = sum((_number(x["amount"], "trade debit") for x in wallet if x["transaction_type"] == "TRADE"), Decimal(0))
    commission = sum((_number(x["amount"], "actual commission") for x in wallet if x["transaction_type"] == "COMMISSION"), Decimal(0))
    if cost != total or commission != fee or fee > intent.max_fee_usd:
        raise ReconciliationError("Actual wallet stake/commission differs from fill evidence or reserved fee bound")
    snapshot = _snapshot(intent, native_id, status.status, total, cost, payout, fee,
                         updated, "prophetx.rest.matched_details_and_wallet")
    return status.model_copy(update={"financial_complete": True, "snapshot": snapshot, "reason": ""})


async def reconcile_prophetx_financial(client, intent, native_order_id, *, observed_at=None):
    """Read-only complete owned-order proof; never performs login or writes."""
    now = observed_at or utcnow()
    user, _ = authenticated_identity(client)
    if client.settings.environment != intent.mode:
        raise ReconciliationError("ProphetX intent environment mismatch")
    row = await owned_financial_order(client, intent, native_order_id, user_id=user, observed_at=observed_at)
    matched = await reporting_pages(client, "/v4/mm/get_order_matched_detail", "matching_details",
        params={"order_id": native_order_id}, observed_at=observed_at)
    wallet = await reporting_pages(client, "/v4/mm/get_transactions", "transactions",
        params={"trade_id": native_order_id}, observed_at=observed_at, require_clock=False)
    report = normalize_financial_order(intent, row, matched.rows, wallet.rows, user_id=user,
        expected_order_id=native_order_id, observed_at=observed_at or utcnow(), reporting_watermark=matched.last_synced_at)
    # Concurrent account identity changes cannot be accepted halfway through.
    if authenticated_identity(client)[0] != user:
        raise ReconciliationError("ProphetX authenticated account changed during reconciliation")
    return report


async def owned_financial_order(client, intent, native_order_id, *, user_id, observed_at=None, row=None):
    """Read the owned order, obtaining an omitted fee from exact order history.

    The order-history guide documents `fee`; the get-order schema does not.
    A matching history row must attest the same revision and all order facts.
    A newer/older row requires a later reconciliation, never an approximate join.
    """
    if row is None:
        raw = _object(await client.reconcile_order(native_order_id), "order response")
        row = _object(raw.get("data"), "owned order")
    row = _object(row, "owned order")
    _identity(intent, row, "order_id", "external_id", native_order_id)
    if row.get("user_id") != user_id or row.get("strike_id") != intent.instrument.instrument_id:
        raise ReconciliationError("ProphetX order account/instrument mismatch")
    if "fee" in row:
        return row
    now = observed_at or utcnow()
    created = source_time(row.get("created_at"), observed_at=now, name="Order creation")
    if created < intent.created_at-timedelta(seconds=1):
        raise ReconciliationError("Order creation precedes durable intent")
    history = await reporting_pages(client, "/v4/mm/get_order_history", "orders",
        params={"from": math.floor(created.timestamp()), "to": math.ceil(now.timestamp()),
                "market_id": str(_number(row.get("market_id"), "market identity", positive=True, whole=True))},
        observed_at=observed_at)
    return order_with_history_fee(row, history, native_order_id, observed_at=observed_at or utcnow())


def order_with_history_fee(row, history, native_order_id, *, observed_at):
    """Exact-revision fee enrichment shared by runtime and captured replay."""
    if "fee" in row:
        return row
    matches = [item for item in unique_rows(history.rows, "order_id") if item.get("order_id") == native_order_id]
    if len(matches) != 1:
        raise ReconciliationError("Owned order fee is missing from reporting history")
    reference = matches[0]
    # Every field served by get_order must agree. Additional history-only fields
    # do not weaken identity. JSON numeric types may differ, but Decimal/int
    # comparisons remain exact; conflicting strings conservatively fail.
    if any(key not in reference or reference[key] != value for key, value in row.items()):
        raise ReconciliationError("Order history revision differs from owned order")
    updated = source_time(row.get("updated_at"), observed_at=observed_at, name="Order update")
    if updated > history.last_synced_at:
        raise ReconciliationError("Order fee history watermark does not cover order revision")
    fee = _number(reference.get("fee"), "explicit order-history fee")
    return {**row, "fee": fee}
