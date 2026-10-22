"""One Novig Paper order through the durable native OMS, never production.

This is an exchange-connector exercise, not an EV signal. It uses a native-only
market identity so this observation cannot be mistaken for a sportsbook match.
The local database permits one attempt across restarts; reruns only reconcile.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

try:  # Windows is the primary target; retain an advisory lock in CI on Unix.
    import msvcrt
except ImportError:  # pragma: no cover - exercised only off Windows
    msvcrt = None
    import fcntl

from dotenv import load_dotenv

from evplatform.core.bus import EventBus, SchemaRegistry
from evplatform.core.config import RiskConfig
from evplatform.core.conflicts import reviewed_correlations, risk_family
from evplatform.core.metrics import Metrics
from evplatform.core.models import Market, Settlement, utcnow
from evplatform.core.native_accounting import NativeInstrument, NativeOrderIntent, NativePriceBand
from evplatform.core.native_oms import NativeOrder, NativeOrderManager, NativeSettlement
from evplatform.core.risk import RiskEngine
from evplatform.core.storage import Storage
from evplatform.venues.account_probe import probe_native_account
from evplatform.venues.bridge import NativeExecutionVenue, NativePreflight, VerifiedVenueMapping
from evplatform.venues.novig import NovigClient, NovigSettings
from evplatform.venues.reconciliation import NativeOrderReport


ROOT = Path(__file__).resolve().parents[1]
DATABASE = ROOT / "data" / "novig_paper_order_probe.db"
LOCK = ROOT / "data" / "novig_paper_order_probe.lock"
QUANTITY = Decimal(100)  # 100 native cent contracts = $1 gross winning payout.
MAX_STAKE = Decimal(1)
MAX_FEE = Decimal("0.01500")  # 100 * $0.01 * 0.06 * max(P*(1-P)).
PRICE_BANDS = (NativePriceBand(minimum="0.001", maximum="0.050", step="0.001"),
               NativePriceBand(minimum="0.055", maximum="0.945", step="0.005"),
               NativePriceBand(minimum="0.950", maximum="0.999", step="0.001"))


@contextmanager
def one_process():
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with LOCK.open("a+b") as file:
        file.seek(0, os.SEEK_END)
        if file.tell() == 0:
            file.write(b"0")
            file.flush()
        file.seek(0)
        try:
            if msvcrt is not None:
                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("Another Novig Paper probe owns the local lock") from exc
        try:
            yield
        finally:
            file.seek(0)
            if msvcrt is not None:
                msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(file.fileno(), fcntl.LOCK_UN)


def candidate_from_catalog(market: dict, event: dict, book: dict) -> tuple[Market, NativeInstrument, Decimal]:
    """Review a native-only sandbox purchase; no sportsbook fair price exists."""
    market_id = UUID(market["marketId"])
    event_id = UUID(market["eventId"])
    fee = market.get("fee")
    if (book.get("marketId") != str(market_id) or event.get("eventId") != str(event_id)
            or market.get("status") != "OPEN" or event.get("status") != "OPEN_PREGAME"
            or market.get("marketType") != "MONEY" or market.get("voids") != "FMV"
            or not isinstance(fee, dict) or fee.get("coefficient") != "0.06"
            or fee.get("charged") != "WHEN_LIVE"
            or not 0 <= Decimal(str(fee.get("makerCredit"))) <= 1
            or event.get("sport") != "BASKETBALL" or event.get("league") != "NBA"
            or not isinstance(book.get("seq"), int) or book["seq"] < 0):
        raise ValueError("Novig Paper market, event, fee or book is outside the reviewed probe scope")
    starts = datetime.fromtimestamp(market["startsTs"] / 1000, timezone.utc)
    if (event.get("startsTs") != market["startsTs"] or starts <= utcnow()
            or len(market.get("outcomes", ())) != 2 or len(book.get("orders", {})) != 2):
        raise ValueError("Novig Paper event is not a future two-sided game")
    outcomes = market["outcomes"]
    if any(not isinstance(o, dict) or o.get("status") != "TBD" or not isinstance(o.get("name"), str)
           or not o["name"] or not isinstance(o.get("outcomeId"), str) for o in outcomes):
        raise ValueError("Novig Paper outcomes are incomplete")
    UUID(outcomes[0]["outcomeId"])
    UUID(outcomes[1]["outcomeId"])
    if outcomes[0]["outcomeId"] == outcomes[1]["outcomeId"]:
        raise ValueError("Duplicate Novig Paper outcome")
    if set(book["orders"]) != {o["outcomeId"] for o in outcomes}:
        raise ValueError("Novig Paper book outcome IDs differ from the market")
    # Buy the lower-cost side at the opposing bid. One price-limited IOC can
    # take at most 100 contracts; the immediate book is rechecked at dispatch.
    choices = []
    for target, opposite in ((outcomes[0], outcomes[1]), (outcomes[1], outcomes[0])):
        levels = book["orders"][opposite["outcomeId"]]
        if not isinstance(levels, list):
            continue
        eligible = [(Decimal(str(row["price"])), row["qty"]) for row in levels
                    if isinstance(row, dict) and isinstance(row.get("qty"), int)
                    and not isinstance(row["qty"], bool) and row["qty"] >= QUANTITY]
        if eligible:
            bid, depth = max(eligible)
            price = Decimal(1) - bid
            if price <= Decimal("0.5") and any(b.contains(price) for b in PRICE_BANDS):
                choices.append((price, target))
    if not choices:
        raise ValueError("No reviewed Novig Paper executable offer within the $1 probe cap")
    price, target = min(choices, key=lambda pair: pair[0])
    canonical = Market(id=f"novig-paper:{market_id}", event_id=f"novig-paper:{event_id}",
        event_name=event.get("description", "Novig Paper native market"), sport="basketball",
        league="NBA", participants=tuple(o["name"] for o in outcomes), start_time=starts,
        market_type="native_sandbox_moneyline_probe", outcomes=tuple(o["name"] for o in outcomes),
        settlement=Settlement(period="full_game", scope="native_sandbox_only",
            overtime="novig_native_unspecified", void_rule="novig_catalog_FMV_unmapped",
            push_rule="native_contract_unspecified", payout=Decimal("0.01"),
            contract_type="novig_cent_contract"),
        original_ids={"novig": str(market_id)},
        extensions={"sandbox_test_only": True, "book_seq_at_review": book["seq"]})
    instrument = NativeInstrument(venue="novig", instrument_id=str(market_id),
        canonical_market_id=canonical.id, outcome=target["name"],
        native_outcome=target["outcomeId"], quantity_unit="whole_cent_contracts",
        price_format="probability", payout_per_unit_usd="0.01", quantity_step="1",
        minimum_quantity="1", cash_quantum_usd="0.00001", price_bands=PRICE_BANDS,
        settlement_rules_fingerprint=canonical.identity(), verified=True,
        source="Novig Paper catalog/book plus official v3 execution and money contracts; native-only test")
    if instrument.cost_for(QUANTITY, price) + MAX_FEE > MAX_STAKE:
        raise ValueError("Novig Paper probe exceeds the $1 cash reservation")
    return canonical, instrument, price


def mapping_for(order: NativeOrder) -> VerifiedVenueMapping:
    i = order.intent.instrument
    return VerifiedVenueMapping(canonical_market_id=order.market_id, canonical_outcome=order.outcome,
        native_instrument_id=i.instrument_id, native_outcome=i.native_outcome,
        native_quantity_unit=i.quantity_unit, native_payout_usd=i.payout_per_unit_usd,
        settlement_fingerprint=order.market_fingerprint,
        mapping_evidence="Native-only Paper catalog/order identity; no sportsbook settlement equivalence",
        verified=True, fee_schedule_verified=True)


def result(order: NativeOrder, risk: RiskEngine) -> dict:
    return {"paper_only": True, "ev_signal": False, "order_id": order.id,
            "native_order_id": order.venue_order_id, "state": order.status.value,
            "filled_contracts": str(order.filled_quantity), "cost_usd": str(order.filled_cost_usd),
            "fees_usd": str(order.fees), "financial_reconciled": order.reconciliation_complete,
            "risk_halted": risk.kill_switch}


async def repair_clock_skew_conflict(storage: Storage, client: NovigClient) -> bool:
    """Audit and clear only this Paper probe's known 143 ms false positive.

    This does not change any order/fill money field or release a risk halt. The
    ordinary OMS recovery and fresh native reconciliation do that afterward.
    All original reports and conflict history remain in SQLite.
    """
    rows = await (await storage.connection.execute("SELECT data FROM native_orders")).fetchall()
    if len(rows) != 1:
        raise ValueError("Exactly one durable Paper order is required for clock-skew repair")
    order = NativeOrder.model_validate_json(rows[0][0])
    if (order.mode != "sandbox" or order.venue != "novig" or order.status.value != "unknown"
            or order.filled_quantity or order.filled_cost_usd or not order.venue_order_id):
        raise ValueError("Paper order is outside the narrow clock-skew repair state")
    conflicts = await (await storage.connection.execute(
        "SELECT reason FROM native_fill_conflicts WHERE order_id=?", (order.id,))).fetchall()
    evidence = await (await storage.connection.execute(
        "SELECT fill_id FROM native_fill_evidence WHERE order_id=?", (order.id,))).fetchall()
    settlements = await (await storage.connection.execute(
        "SELECT order_id FROM native_settlements WHERE order_id=?", (order.id,))).fetchall()
    if conflicts != [("Conflicting durable native fill evidence",)] or evidence or settlements:
        raise ValueError("Paper conflict has other financial evidence and cannot be repaired here")
    rows = await (await storage.connection.execute(
        "SELECT data FROM native_reconciliations WHERE order_id=? ORDER BY seq", (order.id,))).fetchall()
    reports = tuple(NativeOrderReport.model_validate_json(row[0]) for row in rows)
    if not reports or any(r.native_order_id != order.venue_order_id or
                          r.client_order_id != order.client_order_id for r in reports):
        raise ValueError("Paper reconciliation history does not match the durable intent")
    # Replay every originally captured fill against the corrected bounded
    # source-clock rule. A different ID, quantity, price or fee remains fatal.
    stager = type("FillStager", (), {"_fill_evidence": {order.id: {}}})()
    skew_seen = False
    for report in reports:
        for fill in report.fills:
            if fill.filled_at and fill.filled_at > fill.observed_at:
                skew_seen = True
        staged, _ = NativeOrderManager._stage_fills(stager, order, report.fills)
        stager._fill_evidence[order.id] = staged
    if not skew_seen or not any(r.financial_complete and r.status == "filled" for r in reports):
        raise ValueError("Paper history does not establish a bounded source-clock false positive")
    adapter = NativeExecutionVenue(client, RiskEngine(RiskConfig()).authority,
                                   mappings=[mapping_for(order)])
    fresh = await adapter.reconcile_native(order.venue_order_id, order.intent)
    if (fresh.status != "filled" or not fresh.terminal or not fresh.financial_complete
            or fresh.snapshot is None or fresh.snapshot.cumulative_quantity != order.quantity
            or {f.fill_id for f in fresh.fills} != set(stager._fill_evidence[order.id])):
        raise ValueError("Fresh Paper order and fill history do not prove the same full fill")
    staged, _ = NativeOrderManager._stage_fills(stager, order, fresh.fills)
    if (sum((f.quantity for f in staged.values()), Decimal(0)) != fresh.snapshot.cumulative_quantity
            or sum((f.cost_usd for f in staged.values()), Decimal(0)) != fresh.snapshot.cumulative_cost_usd
            or sum((f.fee_usd for f in staged.values()), Decimal(0)) != fresh.snapshot.cumulative_fee_usd):
        raise ValueError("Fresh Paper financial totals differ from every retained execution")
    digest = hashlib.sha256("".join(row[0] for row in rows).encode()+fresh.model_dump_json().encode()).hexdigest()
    await storage.write([
        ("CREATE TABLE IF NOT EXISTS paper_probe_repairs (order_id TEXT PRIMARY KEY, reason TEXT NOT NULL, evidence_sha256 TEXT NOT NULL, repaired_at TEXT NOT NULL)", ()),
        ("INSERT INTO paper_probe_repairs VALUES(?,?,?,?)", (order.id,
            "source_match_clock_ahead_of_local_receipt_by_at_most_one_second", digest, utcnow().isoformat())),
        ("DELETE FROM native_fill_conflicts WHERE order_id=?", (order.id,)),
    ], (), True)
    return True


async def run(*, place: bool, repair: bool = False) -> dict:
    load_dotenv(ROOT / ".env", override=False)
    key = os.getenv("NOVIG_PAPER_TRADING_API_KEY", "")
    private_path = os.getenv("NOVIG_PAPER_TRADING_PRIVATE_KEY_PATH", "")
    management_key = os.getenv("NOVIG_MANAGEMENT_API_KEY", "")
    management_path = os.getenv("NOVIG_MANAGEMENT_PRIVATE_KEY_PATH", "")
    if not all((key, private_path, management_key, management_path)):
        raise ValueError("Existing Novig Paper trading and management keypairs are required")
    UUID(key)
    UUID(management_key)
    client = NovigClient(NovigSettings(environment="sandbox", api_key=key,
                                      private_key_path=private_path))
    management_client = NovigClient(NovigSettings(environment="sandbox", api_key=management_key,
                                                  private_key_path=management_path))
    storage = Storage(str(DATABASE))
    risk = RiskEngine(RiskConfig(max_order_stake=MAX_STAKE, live_max_order_stake=MAX_STAKE,
        max_total_exposure=MAX_STAKE, max_venue_exposure=MAX_STAKE,
        max_event_exposure=MAX_STAKE, max_correlated_exposure=MAX_STAKE,
        max_native_order_attempts=1, max_open_orders=1, max_orders_per_minute=1,
        block_overlapping_events=True, daily_loss_limit=MAX_STAKE))
    schemas = SchemaRegistry()
    schemas.register("native.order", 1, NativeOrder)
    schemas.register("native.settlement", 1, NativeSettlement)
    bus = EventBus(schemas, risk.halt)
    account = None
    reviewed_at = None
    order_to_send = None

    def preflight(order: NativeOrder):
        now = utcnow()
        if (client.settings.environment != "sandbox" or account is None
                or account.account_fingerprint != order.account_fingerprint
                or not account.identity_verified or not account.reconciled
                or not account.cash_available_verified or account.balance_usd is None
                or account.balance_usd < order.reserved_cost
                or not 0 <= (now-account.observed_at).total_seconds() < 20):
            raise ValueError("Current Novig Paper wallet identity, cash and inventory required")

    def revalidate(order: NativeOrder):
        if (order_to_send is not order and order_to_send is not None and order_to_send.id != order.id):
            raise ValueError("Novig Paper order identity changed")
        if reviewed_at is None or not 0 <= (utcnow()-reviewed_at).total_seconds() < 10:
            raise ValueError("Novig Paper native book review expired before dispatch")

    manager = NativeOrderManager(risk, storage, bus, Metrics(), lock=asyncio.Lock(),
                                 preflight=preflight, revalidate=revalidate)
    try:
        await client.connect()
        await storage.open()
        if repair:
            await repair_clock_skew_conflict(storage, client)
        await manager.start()
        existing = tuple(risk.native_orders.values())
        if existing:
            if len(existing) != 1 or existing[0].mode != "sandbox" or existing[0].venue != "novig":
                raise ValueError("Paper probe database contains an unexpected native order")
            saved = existing[0]
            adapter = NativeExecutionVenue(client, risk.authority, mappings=[mapping_for(saved)])
            adapter.bound_account_fingerprint = saved.account_fingerprint
            manager.register("novig", adapter)
            try:
                updated = await manager.reconcile(saved.id)
            except (ValueError, TimeoutError):
                updated = risk.native_orders[saved.id]
            return {"ok": True, "new_order": False, **result(updated, risk)}
        account = await probe_native_account(client, management_client=management_client)
        if (not account.reconciled or not account.cash_available_verified
                or account.open_order_count != 0 or account.position_count != 0):
            raise ValueError("Novig Paper wallet has unresolved inventory")
        if not place:
            return {"ok": True, "paper_only": True, "ready_for_one_test": account.balance_usd >= MAX_STAKE,
                    "balance_usd": str(account.balance_usd), "new_order": False}
        rows = await client.discover_markets(league="NBA", marketType="MONEY", limit=30)
        selection = None
        for summary in rows.get("items", []):
            try:
                m = await client.request("GET", f"/v3/catalog/markets/{UUID(summary['marketId'])}")
                e = await client.request("GET", f"/v3/catalog/events/{UUID(m['eventId'])}")
                b = await client.request("GET", f"/v3/catalog/markets/{UUID(m['marketId'])}/book")
                selection = candidate_from_catalog(m, e, b)
                reviewed_at = utcnow()
                break
            except (KeyError, TypeError, ValueError):
                continue
        if selection is None:
            raise ValueError("No current, bounded Novig Paper market was found")
        market, instrument, price = selection
        intent_id = str(uuid4())
        intent = NativeOrderIntent(id=intent_id, client_order_id=intent_id,
            instrument=instrument, quantity=QUANTITY, limit_price=price,
            max_fee_usd=MAX_FEE, fee_source="Novig catalog c=.06; max P(1-P)=.25; 100 cent contracts",
            fees_verified=True, mode="sandbox")
        order_to_send = NativeOrder(intent=intent, candidate_id="novig-paper-connector-probe",
            strategy="sandbox_connector_probe", execution_origin="manual",
            account_fingerprint=account.account_fingerprint, market_snapshot=market,
            market_fingerprint=market.identity(), risk_family_id=risk_family(market),
            reviewed_correlation_groups=reviewed_correlations(market))
        adapter = NativeExecutionVenue(client, risk.authority, mappings=[mapping_for(order_to_send)],
            preflight=NativePreflight(environment="sandbox", account_authorized=True,
                valid_until=time.time()+20, evidence="Current Paper wallet probe and native-only market review",
                native_trading_enabled=True))
        adapter.bound_account_fingerprint = account.account_fingerprint
        adapter.dispatch_guard = lambda _: (preflight(order_to_send), revalidate(order_to_send))
        manager.register("novig", adapter)
        with risk.manual_order(order_to_send.id):
            submitted = await manager.execute(order_to_send)
        if submitted.venue_order_id:
            try:
                submitted = await manager.reconcile(submitted.id)
            except (ValueError, TimeoutError):
                submitted = risk.native_orders[submitted.id]
        return {"ok": True, "new_order": True, **result(submitted, risk)}
    finally:
        if storage.connection is not None:
            await storage.close()
        await client.disconnect()
        await management_client.disconnect()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--place", action="store_true", help="send the single play-money IOC")
    parser.add_argument("--repair-clock-skew", action="store_true",
                        help="audit and repair only the original Paper probe's bounded match-clock conflict")
    options = parser.parse_args()
    try:
        if options.place and options.repair_clock_skew:
            raise ValueError("Place and repair cannot run together")
        with one_process():
            print(json.dumps(asyncio.run(run(place=options.place, repair=options.repair_clock_skew)), sort_keys=True))
    except Exception as exc:
        print(json.dumps({"ok": False, "paper_only": True,
                          "error_type": type(exc).__name__}, sort_keys=True))
        raise SystemExit(1)
