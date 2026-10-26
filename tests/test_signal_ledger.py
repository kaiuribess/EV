import asyncio
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from evplatform.core.models import Candidate, FeeSchedule, SettlementResult, utcnow
from evplatform.core.signals import ResultProvenance, SignalLedger, SignalObservation, SignalResultImport
from evplatform.core.storage import Storage
from evplatform.engine import Engine

D = Decimal


async def test_signal_worker_keeps_research_running_after_stale_observation():
    engine = Engine.__new__(Engine)
    engine.signal_queue = asyncio.Queue()
    engine.signal_ledger = SimpleNamespace(process=AsyncMock(side_effect=[
        ValueError("Signal observations must be processed in timestamp order"), None]))
    engine.risk = SimpleNamespace(halt=Mock())
    engine.metrics = SimpleNamespace(counters=defaultdict(int))
    await engine.signal_queue.put(SimpleNamespace(schema_name="signal.observation", payload="old"))
    await engine.signal_queue.put(SimpleNamespace(schema_name="signal.observation", payload="new"))
    worker = asyncio.create_task(engine._signal_loop())
    await asyncio.wait_for(engine.signal_queue.join(), timeout=2)
    assert not worker.done()
    assert engine.signal_ledger.process.await_count == 2
    assert engine.metrics.counters["out_of_order_signal_observations"] == 1
    engine.risk.halt.assert_called_once()
    worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await worker


@pytest.fixture
async def ledger(tmp_path):
    storage = Storage(str(tmp_path / "signals.db"))
    await storage.open()
    value = SignalLedger(storage)
    await value.start()
    yield value
    await value.close_all()
    await storage.close()


def candidate(market, *, ev=".15", price=".45", synthetic=True, venue="test", outcome=None):
    return Candidate(strategy="positive_ev", market_id=market.id, venue=venue,
        outcome=outcome or market.outcomes[0], price=D(price), fair_probability=D(price) + D(ev),
        ev_per_contract=D(ev), roi=D(ev) / D(price), max_quantity=50, book_sequence=1,
        references=({"bookmaker": "pinnacle", "source_time": "2026-01-01T00:00:00+00:00",
                     "provider": "fixture", "age_ms": 100},),
        calculated_at=utcnow() - timedelta(seconds=1), quote_age_ms=100, synthetic=synthetic)


def observation(market, candidates=(), *, qualified=True, reason="executable offers", model="proportional", fees=None):
    candidates = tuple(candidates)
    return SignalObservation(market=market, candidates=candidates,
        status="POSITIVE EV" if candidates and qualified else "NEGATIVE EV" if candidates else "NO SIGNAL",
        reason=reason, evaluated_at=utcnow() - timedelta(milliseconds=200),
        qualified_candidate_ids=tuple(c.id for c in candidates) if qualified else (),
        model=model, pricing_thresholds={"min_ev": D(".02"), "min_roi": D(".01")},
        fee_schedules={c.id: fees or FeeSchedule(model="none", verified=True, source="fixture") for c in candidates},
        synthetic=True)


def result(market, *, winning=True, synthetic=True, refund=False, partial=False):
    payout = D(".5") if partial else D(1) if winning else D(0)
    return SettlementResult(market_id=market.id,
        outcome_payouts={market.outcomes[0]: payout, market.outcomes[1]: 1 - payout},
        refund=refund, settled_at=utcnow(), synthetic=synthetic,
        evidence="fixture exact canonical event, outcome and settlement rules")


def provenance(settlement):
    return ResultProvenance(source="fixture" if settlement.synthetic else "operator_import",
        reference="fixture:exact-settlement" if settlement.synthetic else "https://venue.example/official/settlement/1",
        verified_by="local_test_operator", verified_at=utcnow(),
        verification="canonical_settlement_verified", synthetic=settlement.synthetic)


async def test_all_signals_persist_without_trading_authorization(ledger, market):
    # Ledger does not even accept an auto-trade permission or kill-switch bypass.
    positive, negative = candidate(market), candidate(market, ev="-.1", venue="other")
    observed = observation(market, [positive, negative])
    observed = observed.model_copy(update={"qualified_candidate_ids": (positive.id,)})
    await ledger.process(observed)
    page = await ledger.query()
    assert page["total"] == 2
    assert {row["qualified"] for row in page["items"]} == {True, False}
    assert all(row["grade"] == "pending" for row in page["items"])
    summary = await ledger.summary()
    assert summary["eligible_episodes"] == 1
    assert summary["decision_count"] == 2
    assert summary["graded"] == 0
    assert summary["hypothetical_pnl"] == 0
    assert summary["clv"] is None


async def test_episode_snapshots_use_lossless_compact_storage(ledger, market):
    await ledger.process(observation(market, [candidate(market)]))
    await ledger.drain()
    types = await (await ledger.storage.connection.execute(
        "SELECT typeof(market_json),typeof(first_json),typeof(last_json),typeof(best_json) "
        "FROM signal_episodes")).fetchone()
    assert types == ("text", "blob", "blob", "blob")
    assert (await ledger.query())["items"][0]["first"]["candidate"]["ev_per_contract"] == "0.15"
    settlement = result(market)
    await ledger.apply_result(market, settlement, provenance(settlement))
    assert (await ledger.summary())["wins"] == 1


async def test_ticks_retain_first_last_best_without_fake_wins(ledger, market):
    first = candidate(market, ev=".10")
    await ledger.process(observation(market, [first]))
    for edge in (".15", ".12", ".11"):
        await ledger.process(observation(market, [candidate(market, ev=edge)]))
    row = (await ledger.query())["items"][0]
    assert row["update_count"] == 4
    assert D(str(row["first_ev_per_contract"])) == D(".10")
    assert D(row["last"]["candidate"]["ev_per_contract"]) == D(".11")
    assert D(row["best"]["candidate"]["ev_per_contract"]) == D(".15")
    assert row["first"]["candidate"]["references"][0]["source_time"] == "2026-01-01T00:00:00+00:00"
    settlement = result(market)
    await ledger.apply_result(market, settlement, provenance(settlement))
    summary = await ledger.summary()
    assert summary["decision_count"] == 4
    assert summary["episodes"] == summary["wins"] == 1
    assert summary["brier_score"] == pytest.approx((.55 - 1) ** 2)


async def test_negative_and_no_signal_transitions_are_durable(ledger, market):
    await ledger.process(observation(market, [candidate(market)]))
    await ledger.process(observation(market, [candidate(market, ev="-.05")], qualified=False))
    await ledger.process(observation(market, reason="required Circa is stale"))
    await ledger.process(observation(market, reason="required Circa is stale"))
    rows = (await ledger.query())["items"]
    assert len(rows) == 2
    assert all(row["status"] == "closed" for row in rows)
    transitions = (await ledger.transitions())["items"]
    assert [row["status"] for row in reversed(transitions)] == ["POSITIVE EV", "NEGATIVE EV", "NO SIGNAL"]
    assert (await ledger.summary())["no_signal_count"] == 1


async def test_restart_closes_open_episode_in_sql_without_loading_history(tmp_path, market):
    path = str(tmp_path / "restart.db")
    first_store = Storage(path)
    await first_store.open()
    first = SignalLedger(first_store)
    await first.start()
    await first.process(observation(market, [candidate(market)]))
    await first_store.close()  # unexpected application stop, no ledger shutdown
    store = Storage(path)
    await store.open()
    second = SignalLedger(store)
    await second.start()
    try:
        assert not second.active
        old = (await second.query())["items"][0]
        assert old["close_reason"] == "interrupted"
        assert old["grade"] == "pending"
        await second.process(observation(market, [candidate(market)]))
        assert (await second.query())["total"] == 2
    finally:
        await second.close_all()
        await store.close()


async def test_idempotent_result_rejects_conflicting_replacement(ledger, market):
    await ledger.process(observation(market, [candidate(market)]))
    settlement = result(market)
    imported = await ledger.apply_result(market, settlement, provenance(settlement))
    assert imported["graded"] == 1
    repeated = await ledger.apply_result(market, settlement, provenance(settlement))
    assert repeated["idempotent"]
    assert repeated["graded"] == 0
    conflict = result(market, winning=False)
    with pytest.raises(ValueError, match="Conflicting result"):
        await ledger.apply_result(market, conflict, provenance(conflict))
    assert (await ledger.summary())["wins"] == 1


async def test_synthetic_and_real_performance_stay_separate(ledger, market):
    await ledger.process(observation(market, [candidate(market, synthetic=True),
                                            candidate(market, synthetic=False, venue="real")]))
    fake = result(market, synthetic=True)
    await ledger.apply_result(market, fake, provenance(fake))
    assert (await ledger.summary(synthetic=True))["wins"] == 1
    real = await ledger.summary(synthetic=False)
    assert real["graded"] == 0 and real["pending"] == 1
    real_result = result(market, synthetic=False, winning=False)
    await ledger.apply_result(market, real_result, provenance(real_result))
    assert (await ledger.summary(synthetic=False))["losses"] == 1
    page = await ledger.query(synthetic=False)
    assert page["total"] == 1
    assert page["items"][0]["synthetic"] is False
    assert page["items"][0]["provenance"]["source"] == "operator_import"


async def test_negative_episodes_are_graded_but_excluded_from_profit(ledger, market):
    await ledger.process(observation(market, [candidate(market, ev="-.1")], qualified=False))
    settlement = result(market)
    await ledger.apply_result(market, settlement, provenance(settlement))
    summary = await ledger.summary()
    assert summary["all_graded"] == 1
    assert summary["graded"] == summary["wins"] == 0
    assert summary["hypothetical_cost"] == summary["hypothetical_pnl"] == 0
    assert summary["brier_score"] is None
    assert (await ledger.query())["items"][0]["grade"] == "win"


@pytest.mark.parametrize("changed", ["threshold", "rules", "player"])
async def test_result_rejects_inexact_canonical_identity(ledger, market, changed):
    await ledger.process(observation(market, [candidate(market)]))
    if changed == "threshold":
        bad = market.model_copy(update={"line": D("17.5")})
    elif changed == "rules":
        bad = market.model_copy(update={"settlement": market.settlement.model_copy(update={"overtime": "excluded"})})
    else:
        bad = market.model_copy(update={"player_id": "different_player"})
    settlement = result(bad)
    with pytest.raises(ValueError, match="Canonical rules or identity conflict"):
        await ledger.apply_result(bad, settlement, provenance(settlement))
    assert (await ledger.summary())["pending"] == 1


async def test_result_requires_complete_payouts_and_verified_evidence(ledger, market):
    await ledger.process(observation(market, [candidate(market)]))
    settlement = result(market)
    incomplete = settlement.model_copy(update={"outcome_payouts": {market.outcomes[0]: D(1)}})
    with pytest.raises(ValueError, match="exact canonical"):
        await ledger.apply_result(market, incomplete, provenance(settlement))
    blank = settlement.model_copy(update={"evidence": " "})
    with pytest.raises(ValueError, match="evidence"):
        await ledger.apply_result(market, blank, provenance(settlement))
    wrong_sum = settlement.model_copy(update={"outcome_payouts": dict.fromkeys(market.outcomes, D(".7"))})
    with pytest.raises(ValueError, match="sum to one"):
        await ledger.apply_result(market, wrong_sum, provenance(settlement))
    with pytest.raises(ValueError):
        ResultProvenance(source="manual", reference="https://result", verified_by="local",
                         verified_at=utcnow(), verification="unverified", synthetic=True)


@pytest.mark.parametrize("refund,partial,returned,pnl,kind", [
    (False, False, None, ".54", "win"),
    (False, True, None, ".04", "partial"),
    (True, False, True, "0", "refund"),
    (True, False, False, "-.01", "refund"),
])
async def test_one_contract_fee_rounding_partial_and_refund(ledger, market, refund, partial, returned, pnl, kind):
    schedule = FeeSchedule(model="fixed", per_contract=D(".004"), rounding=D(".01"), verified=True, source="fixture")
    await ledger.process(observation(market, [candidate(market)], fees=schedule))
    settlement = result(market, refund=refund, partial=partial)
    await ledger.apply_result(market, settlement, provenance(settlement), returned)
    row = (await ledger.query())["items"][0]
    assert row["grade"] == kind
    assert D(str(row["hypothetical_pnl"])) == D(pnl)
    summary = await ledger.summary()
    assert summary["hypothetical_cost"] == .45
    assert summary["hypothetical_roi"] == pytest.approx(float(D(pnl) / D(".45")))
    if refund or partial:
        assert summary["win_rate"] is None
        assert summary["brier_score"] is None


async def test_profit_commission_uses_realized_partial_payout_profit(ledger, market):
    schedule = FeeSchedule(model="profit_commission", rate=D(".1"), verified=True, source="fixture")
    await ledger.process(observation(market, [candidate(market)], fees=schedule))
    settlement = result(market, partial=True)
    await ledger.apply_result(market, settlement, provenance(settlement))
    row = (await ledger.query())["items"][0]
    assert D(str(row["fee"])) == D(".01")
    assert D(str(row["hypothetical_pnl"])) == D(".04")


async def test_missing_fee_schedule_cannot_claim_profit(ledger, market):
    observed = observation(market, [candidate(market)]).model_copy(update={"fee_schedules": {}})
    await ledger.process(observed)
    settlement = result(market)
    await ledger.apply_result(market, settlement, provenance(settlement))
    row = (await ledger.query())["items"][0]
    assert row["grade"] == "unavailable"
    assert row["hypothetical_pnl"] is None
    assert (await ledger.summary())["graded"] == 0


async def test_pagination_bounded_active_state_and_no_silent_drop(ledger, market):
    ledger.max_open_episodes = 2
    for venue in ("a", "b"):
        # One evaluation contains all current venue candidates, as the engine does.
        candidates = [candidate(market, venue=v) for v in ("a", "b")[:1 if venue == "a" else 2]]
        await ledger.process(observation(market, candidates))
    assert (await ledger.query(limit=1, offset=1))["total"] == 2
    with pytest.raises(ValueError):
        await ledger.query(limit=201)
    with pytest.raises(RuntimeError, match="limit exceeded"):
        await ledger.process(observation(market, [candidate(market, venue=v) for v in ("a", "b", "c")]))


async def test_pricing_model_change_starts_new_episode(ledger, market):
    await ledger.process(observation(market, [candidate(market)], model="proportional"))
    await ledger.process(observation(market, [candidate(market)], model="power"))
    summary = await ledger.summary()
    assert summary["episodes"] == 2
    assert {group["value"] for group in summary["groups"] if group["dimension"] == "reference_model"} == {"proportional", "power"}


@pytest.mark.parametrize("field,initial,changed", [
    ("reference_policy_digest", "old-policy", "new-policy"),
    ("reference_inputs_eligible", False, True),
])
async def test_profile_and_input_quality_changes_split_unqualified_episodes(ledger, market, field, initial, changed):
    first = candidate(market, ev="-.1").model_copy(update={field: initial, "execution_eligible": False})
    await ledger.process(observation(market, [first], qualified=False))
    second = candidate(market, ev="-.1").model_copy(update={field: changed, "execution_eligible": False})
    await ledger.process(observation(market, [second], qualified=False))
    assert (await ledger.summary())["episodes"] == 2


async def test_import_contract_has_no_execution_fields(market):
    settlement = result(market)
    imported = SignalResultImport(market=market, result=settlement, provenance=provenance(settlement))
    assert set(imported.model_dump()) == {"market", "result", "provenance", "refund_fees_returned"}


def test_observation_rejects_future_clock_and_nonfinite_edge(market):
    valid = observation(market, [candidate(market)])
    with pytest.raises(ValueError, match="future local clock"):
        SignalObservation.model_validate({**valid.model_dump(), "evaluated_at": utcnow() + timedelta(days=1)})
    bad_candidate = candidate(market).model_copy(update={"ev_per_contract": D("NaN")})
    with pytest.raises(ValueError, match="must be finite"):
        observation(market, [bad_candidate])


async def test_out_of_order_observation_rejected_without_freshening_source(ledger, market):
    original = observation(market, [candidate(market)])
    await ledger.process(original)
    old = original.model_copy(update={"evaluated_at": original.evaluated_at - timedelta(seconds=2)})
    with pytest.raises(ValueError, match="timestamp order"):
        await ledger.process(old)
    row = (await ledger.query())["items"][0]
    assert row["update_count"] == 1
    assert row["first"]["candidate"]["quote_age_ms"] == 100


async def test_no_result_never_becomes_win_after_shutdown(ledger, market):
    await ledger.process(observation(market, [candidate(market)]))
    await ledger.close_all()
    summary = await ledger.summary()
    assert summary["open"] == 0
    assert summary["pending"] == 1
    assert summary["graded"] == 0


async def test_result_cannot_grade_entry_after_known_settlement(ledger, market):
    settlement = result(market)
    await asyncio.sleep(.001)
    await ledger.process(observation(market, [candidate(market)]).model_copy(update={"evaluated_at": utcnow()}))
    await ledger.apply_result(market, settlement, provenance(settlement))
    row = (await ledger.query())["items"][0]
    assert row["grade"] == "unavailable"
    assert "after settlement" in row["grade_reason"]


@pytest.mark.parametrize("kind", ["no_signal", "model_change", "threshold_change"])
async def test_old_observation_cannot_close_or_replace_newest_episode(ledger, market, kind):
    current = observation(market, [candidate(market)])
    await ledger.process(current)
    if kind == "no_signal":
        old = observation(market, reason="old unavailable input")
    elif kind == "model_change":
        old = observation(market, [candidate(market)], model="power")
    else:
        old = observation(market, [candidate(market)]).model_copy(update={"pricing_thresholds": {"min_ev": D(".1")}})
    old = old.model_copy(update={"evaluated_at": current.evaluated_at - timedelta(seconds=1)})
    with pytest.raises(ValueError, match="timestamp order"):
        await ledger.process(old)
    page = await ledger.query()
    assert page["total"] == 1
    assert page["items"][0]["status"] == "open"
    assert page["items"][0]["update_count"] == 1
    assert (await ledger.summary())["no_signal_count"] == 0


async def test_known_settlement_suppresses_new_pending_episodes_and_restart(tmp_path, market):
    path = str(tmp_path / "settled-restart.db")
    storage = Storage(path)
    await storage.open()
    first = SignalLedger(storage)
    await first.start()
    await first.process(observation(market, [candidate(market)]))
    settlement = result(market)
    await first.apply_result(market, settlement, provenance(settlement))
    await first.process(observation(market, [candidate(market)]))
    assert (await first.query())["total"] == 1
    assert (await first.transitions())["items"][0]["status"] == "SETTLED"
    await storage.close()
    storage = Storage(path)
    await storage.open()
    restarted = SignalLedger(storage)
    await restarted.start()
    try:
        await restarted.process(observation(market, [candidate(market)]))
        assert (await restarted.query())["total"] == 1
        summary = await restarted.summary()
        assert summary["pending"] == 0 and summary["wins"] == 1
        assert summary["win_rate_interval"] is None
        assert summary["distinct_eligible_events"] == summary["distinct_eligible_markets"] == 1
    finally:
        await restarted.close_all()
        await storage.close()


async def test_clock_guard_survives_closed_episode_cache_eviction(ledger, market):
    ledger.max_open_episodes = 1
    current = observation(market, [candidate(market)])
    await ledger.process(current)
    await ledger.process(observation(market, reason="current suspended"))
    other = market.model_copy(update={"id": "fixture:other", "event_id": "fixture:other-event"})
    await ledger.process(observation(other, reason="other unavailable"))
    assert len(ledger._clocks) <= 1 and len(ledger._results) <= 1
    with pytest.raises(ValueError, match="timestamp order"):
        await ledger.process(current.model_copy(update={"evaluated_at": current.evaluated_at - timedelta(seconds=1)}))
    assert (await ledger.query())["total"] == 1


async def test_mixed_origins_have_separate_transitions_and_no_signal_closes_both(ledger, market):
    synthetic = candidate(market, synthetic=True, venue="synthetic")
    real = candidate(market, synthetic=False, venue="real", ev="-.1")
    observed = observation(market, [synthetic, real]).model_copy(update={"qualified_candidate_ids": (synthetic.id,)})
    await ledger.process(observed)
    synthetic_transitions = (await ledger.transitions(synthetic=True))["items"]
    real_transitions = (await ledger.transitions(synthetic=False))["items"]
    assert len(synthetic_transitions) == len(real_transitions) == 1
    assert synthetic_transitions[0]["status"] == "POSITIVE EV"
    assert real_transitions[0]["status"] == "NEGATIVE EV"
    # A market-wide invalidation closes and records both previously active origins.
    await ledger.process(observation(market, reason="all books suspended"))
    for origin in (True, False):
        transitions = (await ledger.transitions(synthetic=origin))["items"]
        assert transitions[0]["status"] == "NO SIGNAL"
        assert (await ledger.query(synthetic=origin))["items"][0]["status"] == "closed"
        assert (await ledger.summary(synthetic=origin))["no_signal_count"] == 1

