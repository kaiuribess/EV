"""Offline fault injection for native release facts; no genuine account evidence.

Fake workers, feeds, accounts and executable books intentionally exercise a
healthy baseline so individual regressions cannot hide behind unrelated gates.
No fixture is saved as a native lifecycle certification.
"""
import asyncio
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from evplatform.core.config import RiskConfig, Settings
from evplatform.core.models import OrderStatus, utcnow
from evplatform.core.native_runtime import NativeRuntime, pilot_prior_attempts
from evplatform.engine import Engine
from evplatform.venues.account_probe import NativeAccountSnapshot

from test_launch_readiness import ready_fixture
from test_native_pricing import setup_native


class Worker:
    def __init__(self, name):
        self.name = name
        self.stopped = False

    def get_name(self): return self.name
    def done(self): return self.stopped
    def cancelled(self): return False


def test_first_unsent_pilot_intent_does_not_consume_its_own_preflight():
    order = SimpleNamespace(status=OrderStatus.SUBMITTING, venue_order_id=None,
                            filled_quantity=Decimal(0))
    assert pilot_prior_attempts([order]) == 0
    for status in (OrderStatus.UNKNOWN, OrderStatus.REJECTED, OrderStatus.FILLED):
        order.status = status
        assert pilot_prior_attempts([order]) == 1
    order.status = OrderStatus.SUBMITTING
    order.venue_order_id = "native-id"
    assert pilot_prior_attempts([order]) == 1


@pytest.fixture
def runtime(market, quotes):
    market, quotes, book, _, _, _ = setup_native(market, quotes)
    profile, _, _ = ready_fixture("sandbox")
    profile = profile.model_copy(update={"venue": "novig", "market_ids": (market.id,),
                                         "strategy_ids": ("native_positive_ev",), "provider_ids": ("parlay_rest",)})
    client = SimpleNamespace(venue_id="novig", settings=SimpleNamespace(environment="sandbox"),
                             book=SimpleNamespace(sequences=SimpleNamespace(invalid=set(), values={"PRIVATE:orders": 3, "PRIVATE:positions": 4})))
    health = {"authenticated": True, "streaming": True, "state": "connected", "status": "connected",
              "quota": {"cooldown_seconds": 0, "provider_remaining": 100, "minute_remaining": 20,
                        "local_session_budget": 30, "reserved_cost": 1}}
    provider = SimpleNamespace(enabled=True, health={**health, "quota": dict(health["quota"])},
        credential_status="OFFLINE fixture", plugin=SimpleNamespace(task=Worker("OFFLINE-provider"),
            manifest=SimpleNamespace(transports=["rest"])))
    venue = SimpleNamespace(enabled=True, health=dict(health), credential_status="OFFLINE fixture",
        plugin=SimpleNamespace(client=client, task=Worker("OFFLINE-native"), manifest=SimpleNamespace(transports=["websocket"])))
    strategy = SimpleNamespace(enabled=True)
    records = {"parlay_rest": provider, "novig": venue, "native_positive_ev": strategy}
    risk = SimpleNamespace(config=RiskConfig(live_max_order_stake=Decimal("1")), native_orders={},
                           auto_trade=False, kill_switch=True, settled=set(),
                           external_exposure_bounded=lambda venue: True)
    engine = SimpleNamespace(registry=SimpleNamespace(records=records), risk=risk,
        settings=Settings(mode="sandbox"), markets={market.id: market},
        quotes={market.id: {str(index): q.model_copy(update={"provider": "parlay_rest", "source_time": utcnow()})
                           for index, q in enumerate(quotes)}},
        tasks=[Worker(name) for name in ("normalization-strategy", "risk-routing", "audit-enqueue", "signal-ledger", "native-reconciliation")],
        risk_queue=asyncio.Queue(2), signal_queue=asyncio.Queue(2), audit_queue=asyncio.Queue(2),
        storage=SimpleNamespace(connection=object(), failure=None, task=Worker("writer"), queue=asyncio.Queue(2)),
        signal_ledger=SimpleNamespace(started=True))
    native = NativeRuntime.__new__(NativeRuntime)
    native.engine, native.profile, native.profile_error, native.build_id = engine, profile, "", "OFFLINE fixture"
    native.invalidated_family_markets = set()
    native.execution_lease = SimpleNamespace(held=True, scope=(profile.venue, profile.environment))
    native.books = {(market.id, "novig", "home"): book}
    native.mapper = SimpleNamespace(mapping_digest=profile.mapping_digest, mappings=())
    native.account_snapshots = {"novig": NativeAccountSnapshot(venue="novig", environment="sandbox",
        account_fingerprint=profile.account_fingerprint, identity_verified=True, observed_at=utcnow(),
        reconciled=True, order_sequence=3, position_sequence=4, reason="OFFLINE fixture")}
    adapter = SimpleNamespace(client=client, bound_account_fingerprint=profile.account_fingerprint,
                               submit_intent=lambda *a: None, reconcile_native=lambda *a: None, cancel_native=lambda *a: None)
    native.manager = SimpleNamespace(adapters={"novig": adapter}, recovered=True, invalidated_settlements=set())
    native._account_snapshot_fences = {"novig": native._ledger_fence()}
    return native


def test_runtime_baseline_is_explicit_and_remains_blocked_for_missing_settlement(runtime):
    facts = runtime.runtime_facts()
    assert facts.central_risk_enforced and facts.persistence_healthy and facts.signal_ledger_healthy
    assert facts.account_identity_verified and facts.private_stream_synchronized and facts.reconciliation_healthy
    assert facts.native_routing_supported and facts.feeds[0].quota_available
    assert facts.markets[0].identity_verified and facts.markets[0].provider_mapping_verified
    assert facts.markets[0].fee_schedule_verified
    assert not facts.settlement_reconciliation_supported
    assert not runtime.report().safe_to_arm


@pytest.mark.parametrize("ages", [(None, None, None), (None, 10, 20)])
def test_research_reference_unknown_age_stays_unknown_without_breaking_setup(runtime, monkeypatch, ages):
    from evplatform.core.pricing import fair_value
    market = next(iter(runtime.engine.markets.values()))
    fair = fair_value(market, list(runtime.engine.quotes[market.id].values()), runtime.engine.settings.pricing)
    fair = fair.model_copy(update={"execution_eligible": False,
        "references": tuple({**ref, "age_ms": age} for ref, age in zip(fair.references, ages))})
    monkeypatch.setattr("evplatform.core.native_runtime.fair_value", lambda *args, **kwargs: fair)
    facts = runtime.runtime_facts()
    assert facts.markets[0].oldest_reference_age_ms is None
    assert not facts.markets[0].required_books_present
    assert not runtime.report().safe_to_arm


@pytest.mark.parametrize("update", [
    {"provider_remaining": 0}, {"minute_remaining": 0}, {"cooldown_seconds": 0.1},
    {"reserved_cost": 30}, {"local_session_budget": 0}, {"reserved_cost": -1},
    {"provider_remaining": "100"}, {"provider_remaining": True}, {"minute_remaining": float("nan")},
])
def test_exhausted_or_malformed_quota_cannot_pass(runtime, update):
    runtime.engine.registry.records["parlay_rest"].health["quota"].update(update)
    assert not runtime.runtime_facts().feeds[0].quota_available


@pytest.mark.parametrize("quota", [None, {}, "not a quota", {"cooldown_seconds": 0}])
def test_missing_quota_evidence_is_unknown(runtime, quota):
    runtime.engine.registry.records["parlay_rest"].health["quota"] = quota
    assert not runtime.runtime_facts().feeds[0].quota_available


def test_stream_handshake_does_not_prove_initial_snapshot(runtime):
    provider = runtime.engine.registry.records["parlay_rest"]
    provider.plugin.manifest.transports = ["websocket"]
    provider.health["awaiting_snapshot_sports"] = ["nba"]
    assert not runtime.runtime_facts().feeds[0].synchronization_ready
    provider.health["awaiting_snapshot_sports"] = []
    provider.health["streaming"] = False
    assert not runtime.runtime_facts().feeds[0].synchronization_ready


@pytest.mark.parametrize("change", [
    {"valid_until": utcnow()-timedelta(seconds=1)},
    {"observed_at": utcnow()+timedelta(hours=1), "valid_until": utcnow()+timedelta(hours=2)},
    {"instrument_fingerprint": "wrong-instrument"}, {"verified": False},
])
def test_stale_future_or_misbound_fee_blocks(runtime, change):
    key, book = next(iter(runtime.books.items()))
    runtime.books[key] = book.model_copy(update={"fee_bound": book.fee_bound.model_copy(update=change)})
    assert not runtime.runtime_facts().markets[0].fee_schedule_verified


def test_unapproved_provider_and_changed_native_contract_block(runtime):
    market_id = runtime.profile.market_ids[0]
    runtime.engine.quotes[market_id] = {name: q.model_copy(update={"provider": "unapproved-provider"})
                                     for name, q in runtime.engine.quotes[market_id].items()}
    assert not runtime.runtime_facts().markets[0].provider_mapping_verified
    key, book = next(iter(runtime.books.items()))
    runtime.books[key] = book.model_copy(update={"instrument": book.instrument.model_copy(update={"settlement_rules_fingerprint": "different"})})
    assert not runtime.runtime_facts().markets[0].identity_verified


@pytest.mark.parametrize("worker,field", [("audit-enqueue", "persistence_healthy"), ("signal-ledger", "signal_ledger_healthy"),
                                         ("risk-routing", "central_risk_enforced"), ("normalization-strategy", "central_risk_enforced"),
                                         ("native-reconciliation", "reconciliation_healthy")])
def test_crashed_critical_worker_blocks_even_when_connection_exists(runtime, worker, field):
    next(task for task in runtime.engine.tasks if task.get_name() == worker).stopped = True
    assert not getattr(runtime.runtime_facts(), field)


def test_dead_writer_and_full_critical_queue_block(runtime):
    runtime.engine.storage.task.stopped = True
    facts = runtime.runtime_facts()
    assert not facts.persistence_healthy and not facts.durable_intents and not facts.signal_ledger_healthy
    runtime.engine.storage.task.stopped = False
    runtime.engine.risk_queue.put_nowait("one")
    runtime.engine.risk_queue.put_nowait("two")
    assert not runtime.runtime_facts().central_risk_enforced


@pytest.mark.parametrize("change", [
    {"environment": "production"}, {"venue": "other"}, {"account_fingerprint": "e"*64},
    {"observed_at": lambda: utcnow()-timedelta(seconds=31)},
    {"observed_at": lambda: utcnow()+timedelta(seconds=20)},
    {"observed_at": lambda: utcnow().replace(tzinfo=None)}, {"identity_verified": False},
])
def test_foreign_or_stale_inventory_does_not_authorize(runtime, change):
    # Derive clock faults when the case runs, not during collection: a long
    # full-suite run must not turn the future timestamp into a valid past one.
    change = {key: value() if callable(value) else value for key, value in change.items()}
    runtime.account_snapshots["novig"] = runtime.account_snapshots["novig"].model_copy(update=change)
    facts = runtime.runtime_facts()
    assert not facts.account_identity_verified and not facts.native_routing_supported


def test_adapter_account_and_connection_binding_are_required(runtime):
    adapter = runtime.manager.adapters["novig"]
    adapter.bound_account_fingerprint = "wrong"
    assert not runtime.runtime_facts().native_routing_supported
    adapter.bound_account_fingerprint = runtime.profile.account_fingerprint
    adapter.client = object()
    assert not runtime.runtime_facts().native_routing_supported


def test_missing_or_wrong_execution_lease_blocks_native_routing(runtime):
    runtime.execution_lease.held = False
    assert not runtime.runtime_facts().native_routing_supported
    runtime.execution_lease.held = True
    runtime.execution_lease.scope = ("novig", "production")
    assert not runtime.runtime_facts().native_routing_supported


def test_prophetx_order_location_capability_is_separate_from_stale_cash(runtime):
    profile = runtime.profile.model_copy(update={"venue": "prophetx"})
    runtime.profile = profile
    client = SimpleNamespace(venue_id="prophetx",
        settings=SimpleNamespace(environment="sandbox"),
        check_current_location=lambda order_id: True,
        location_valid_for=lambda order_id: False)
    venue = runtime.engine.registry.records["novig"]
    runtime.engine.registry.records["prophetx"] = SimpleNamespace(
        enabled=True, health=venue.health, credential_status=venue.credential_status,
        plugin=SimpleNamespace(client=client, task=Worker("OFFLINE-prophetx")))
    runtime.manager.adapters["prophetx"] = SimpleNamespace(client=client)
    facts = runtime.runtime_facts("prophetx")
    assert facts.order_bound_location_check_supported
    assert not facts.account_identity_verified
    assert not facts.native_routing_supported
    client.check_current_location = None
    assert not runtime.runtime_facts("prophetx").order_bound_location_check_supported


def test_available_cash_policy_requires_verified_cash_in_readiness(runtime):
    runtime.engine.risk.config.available_cash_only = True
    assert not runtime.runtime_facts().account_identity_verified
    runtime.account_snapshots["novig"] = runtime.account_snapshots["novig"].model_copy(update={"cash_available_verified": True})
    assert runtime.runtime_facts().account_identity_verified


def test_missing_ledger_fence_or_probe_in_progress_never_looks_ready(runtime):
    runtime._account_snapshot_fences.clear()
    assert not runtime.runtime_facts().account_identity_verified
    runtime._account_snapshot_fences["novig"] = runtime._ledger_fence()
    runtime._account_probes = {"novig"}
    assert not runtime.runtime_facts().account_identity_verified
    runtime._account_probes.clear()
    assert runtime.runtime_facts().account_identity_verified


def test_private_missing_sequences_must_not_compare_equal(runtime):
    record = runtime.engine.registry.records["novig"]
    tracker = record.plugin.client.book.sequences
    tracker.values.clear()
    snapshot = runtime.account_snapshots["novig"].model_copy(update={"order_sequence": None, "position_sequence": None})
    assert not runtime.private_synchronized(record, snapshot)
    tracker.values.update({"PRIVATE:orders": True, "PRIVATE:positions": 4})
    snapshot = snapshot.model_copy(update={"order_sequence": True, "position_sequence": 4})
    assert not runtime.private_synchronized(record, snapshot)


def test_private_sequence_gap_and_disabled_monitor_block(runtime):
    record = runtime.engine.registry.records["novig"]
    record.plugin.client.book.sequences.values["PRIVATE:orders"] = 5
    assert not runtime.runtime_facts().private_stream_synchronized
    record.plugin.client.book.sequences.values["PRIVATE:orders"] = 3
    record.plugin.client.book.sequences.invalid.add("PRIVATE:positions")
    assert not runtime.runtime_facts().private_stream_synchronized
    record.plugin.client.book.sequences.invalid.clear()
    record.enabled = False
    assert not runtime.runtime_facts().private_stream_authenticated
    assert not runtime.runtime_facts().private_stream_synchronized


async def test_malformed_launch_artifact_fails_closed_without_secret_echo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path/"data").mkdir()
    secret_marker = "misplaced-secret-must-not-be-displayed"
    (tmp_path/"data"/"launch_profile.json").write_text('{"venue":"'+secret_marker+'"}', encoding="utf-8")
    engine = Engine(Settings(mode="sandbox", database_path=str(tmp_path/"test.db"),
                             data_dir=str(tmp_path / "data")))
    await engine.start()
    try:
        assert engine.native.profile is None
        assert engine.native.profile_error
        report = engine.native.report()
        assert not report.safe_to_arm and not report.live_trading_ready
        assert "profile.missing" in {item.code for item in report.blockers}
        assert secret_marker not in report.model_dump_json()
    finally:
        await engine.stop()
