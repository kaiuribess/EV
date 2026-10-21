"""Durable signal episodes and explicitly verified, hypothetical one-contract grades.

This is an independent event-bus observer, never an execution authority. It keeps
bounded active state and enqueues SQLite writes. Cache misses, API reads and result
imports may consult SQLite from this observer; pricing never awaits this worker.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
from collections import OrderedDict
from datetime import datetime
from decimal import Decimal, ROUND_CEILING
from typing import Literal
from uuid import uuid4

from pydantic import Field, field_validator, model_validator

from .models import Candidate, Contract, FeeSchedule, Market, Quote, SettlementResult, utcnow
from .storage import Storage

D = Decimal


class SignalObservation(Contract):
    market: Market
    candidates: tuple[Candidate, ...] = ()
    status: Literal["POSITIVE EV", "NEGATIVE EV", "NO SIGNAL", "RESEARCH ONLY"]
    reason: str
    evaluated_at: datetime = Field(default_factory=utcnow)
    qualified_candidate_ids: tuple[str, ...] = ()
    model: str = "unspecified"
    pricing_thresholds: dict[str, Decimal] = Field(default_factory=dict)
    fee_schedules: dict[str, FeeSchedule] = Field(default_factory=dict)
    strategies: tuple[str, ...] = ("positive_ev",)
    synthetic: bool = True
    # Retain the evaluated reference universe, including rejected/unused books,
    # in the independent audit stream for later grouped model comparison.
    reference_quotes: tuple[Quote, ...] = ()
    pricing_policy: dict = Field(default_factory=dict)

    @field_validator("evaluated_at")
    @classmethod
    def aware_time(cls, value):
        if value.tzinfo is None:
            raise ValueError("Signal timestamps must include a timezone")
        if value > utcnow():
            raise ValueError("Signal observation cannot have a future local clock")
        return value

    @model_validator(mode="after")
    def exact_market(self):
        ids = {c.id for c in self.candidates}
        if len(ids) != len(self.candidates) or set(self.qualified_candidate_ids) - ids:
            raise ValueError("Qualified candidates must identify distinct observed candidates")
        if any(c.market_id != self.market.id or c.outcome not in self.market.outcomes
               for c in self.candidates):
            raise ValueError("Signal candidates must match the exact canonical market")
        if self.status == "NO SIGNAL" and self.candidates:
            raise ValueError("NO SIGNAL cannot contain executable candidates")
        if any(q.market_id != self.market.id for q in self.reference_quotes):
            raise ValueError("Signal reference observations must match the canonical market")
        if any(not c.execution_eligible for c in self.candidates if c.id in self.qualified_candidate_ids):
            raise ValueError("Research-only candidates cannot be qualified")
        if set(self.fee_schedules) - ids:
            raise ValueError("Fee schedules must identify observed candidates")
        for candidate in self.candidates:
            if candidate.calculated_at.tzinfo is None or candidate.calculated_at > self.evaluated_at:
                raise ValueError("Calculation timestamp must precede the aware observation timestamp")
            if candidate.quote_age_ms < 0 or not math.isfinite(candidate.quote_age_ms):
                raise ValueError("Signal age must be finite and nonnegative")
            if any(not value.is_finite() for value in (candidate.ev_per_contract, candidate.roi)):
                raise ValueError("Signal edge and ROI must be finite")
        return self


class ResultProvenance(Contract):
    """An explicit local operator assertion, not automatic outcome verification."""
    source: str
    reference: str
    verified_by: str
    verified_at: datetime
    verification: Literal["canonical_settlement_verified"]
    synthetic: bool

    @field_validator("source", "reference", "verified_by")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Result provenance must not be blank")
        return value

    @field_validator("verified_at")
    @classmethod
    def aware_time(cls, value):
        if value.tzinfo is None:
            raise ValueError("Verification timestamp must include a timezone")
        return value


class SignalResultImport(Contract):
    market: Market
    result: SettlementResult
    provenance: ResultProvenance
    refund_fees_returned: bool | None = None


class SignalSnapshot(Contract):
    candidate: Candidate
    evaluated_at: datetime
    qualified: bool
    reference_model: str
    pricing_thresholds: dict[str, Decimal]
    fee_schedule: FeeSchedule | None


class _ActiveEpisode:
    def __init__(self, episode_id, market, snapshot):
        self.id = episode_id
        self.market = market
        self.first = self.last = self.best = snapshot
        self.updates = 1


class SignalLedger:
    """Episode keys include canonical rules, strategy, venue, outcome and origin.

    A qualification change or an unavailable signal ends an episode. Every tick is
    retained as an observation, but statistics use only the first snapshot of each
    qualifying episode. Repeated ticks cannot manufacture independent wins.
    """
    def __init__(self, storage: Storage, *, max_open_episodes=10000):
        self.storage = storage
        self.max_open_episodes = max_open_episodes
        self.active: dict[tuple, _ActiveEpisode] = {}
        self._clocks = OrderedDict()
        self._results = OrderedDict()
        self.lock = asyncio.Lock()
        self.started = False

    async def start(self):
        async with self.lock:
            await self.storage.write([
                ("""CREATE TABLE IF NOT EXISTS signal_episodes (
                    id TEXT PRIMARY KEY, identity TEXT NOT NULL, market_id TEXT NOT NULL,
                    strategy TEXT NOT NULL, venue TEXT NOT NULL, outcome TEXT NOT NULL,
                    synthetic INTEGER NOT NULL, qualified INTEGER NOT NULL,
                    sport TEXT NOT NULL, market_type TEXT NOT NULL, reference_model TEXT NOT NULL,
                    first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, ended_at TEXT,
                    close_reason TEXT, update_count INTEGER NOT NULL,
                    market_json TEXT NOT NULL, first_json TEXT NOT NULL,
                    last_json TEXT NOT NULL, best_json TEXT NOT NULL,
                    grade TEXT NOT NULL DEFAULT 'pending', payout TEXT, fee TEXT,
                    hypothetical_pnl TEXT, hypothetical_cost TEXT, brier TEXT,
                    result_identity TEXT, grade_reason TEXT)""", ()),
                ("CREATE INDEX IF NOT EXISTS signal_episode_lookup ON signal_episodes(identity,synthetic,first_seen)", ()),
                ("CREATE INDEX IF NOT EXISTS signal_episode_page ON signal_episodes(synthetic,first_seen DESC,id)", ()),
                ("CREATE INDEX IF NOT EXISTS signal_episode_report ON signal_episodes(synthetic,qualified,grade)", ()),
                ("""CREATE TABLE IF NOT EXISTS signal_observations (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, episode_id TEXT NOT NULL,
                    observed_at TEXT NOT NULL, candidate_id TEXT NOT NULL, snapshot_json TEXT NOT NULL)""", ()),
                ("CREATE INDEX IF NOT EXISTS signal_observation_episode ON signal_observations(episode_id,seq)", ()),
                ("""CREATE TABLE IF NOT EXISTS signal_transitions (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, identity TEXT NOT NULL,
                    market_id TEXT NOT NULL, strategy TEXT NOT NULL, synthetic INTEGER NOT NULL,
                    observed_at TEXT NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL)""", ()),
                ("CREATE INDEX IF NOT EXISTS signal_transition_lookup ON signal_transitions(identity,strategy,synthetic,seq DESC)", ()),
                ("CREATE INDEX IF NOT EXISTS signal_transition_report ON signal_transitions(synthetic,status)", ()),
                ("""CREATE TABLE IF NOT EXISTS signal_cursors (
                    identity TEXT NOT NULL, strategy TEXT NOT NULL, observed_at TEXT NOT NULL,
                    PRIMARY KEY(identity,strategy))""", ()),
                ("""INSERT OR IGNORE INTO signal_cursors(identity,strategy,observed_at)
                    SELECT identity,strategy,MAX(observed_at) FROM (
                        SELECT identity,strategy,observed_at FROM signal_transitions UNION ALL
                        SELECT identity,strategy,last_seen AS observed_at FROM signal_episodes)
                    GROUP BY identity,strategy""", ()),
                ("""CREATE TABLE IF NOT EXISTS signal_results (
                    identity TEXT NOT NULL, synthetic INTEGER NOT NULL, market_id TEXT NOT NULL,
                    imported_at TEXT NOT NULL, result_json TEXT NOT NULL,
                    provenance_json TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    PRIMARY KEY(identity,synthetic))""", ()),
                ("UPDATE signal_episodes SET ended_at=?,close_reason='interrupted' WHERE ended_at IS NULL",
                 (utcnow().isoformat(),)),
            ], (), durable=True)
            self.active.clear()
            self._clocks.clear()
            self._results.clear()
            self.started = True

    async def drain(self):
        await self.storage.queue.join()
        if self.storage.failure:
            raise RuntimeError("Signal persistence unavailable")

    def _close(self, key, reason, at, statements):
        episode = self.active.pop(key)
        statements.append(("UPDATE signal_episodes SET ended_at=?,close_reason=? WHERE id=? AND ended_at IS NULL",
                           (at.isoformat(), reason, episode.id)))

    def _cache(self, cache, key, value):
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > self.max_open_episodes:
            cache.popitem(last=False)

    async def _load_guards(self, identity, strategies, origins):
        """Lazy bounded caches; only the independent observer can await disk here."""
        missing_clocks = [(identity, strategy) for strategy in strategies if (identity, strategy) not in self._clocks]
        missing_results = [(identity, origin) for origin in origins if (identity, origin) not in self._results]
        if missing_clocks or missing_results:
            await self.drain()  # queued cursor writes must precede an evicted-key lookup
        clocks, settled = {}, {}
        for key in [(identity, strategy) for strategy in strategies]:
            if key in self._clocks:
                clocks[key] = self._clocks[key]
                self._clocks.move_to_end(key)
            else:
                row = await (await self.storage.connection.execute(
                    "SELECT observed_at FROM signal_cursors WHERE identity=? AND strategy=?", key)).fetchone()
                clocks[key] = datetime.fromisoformat(row[0]) if row else None
                self._cache(self._clocks, key, clocks[key])
        for key in [(identity, origin) for origin in origins]:
            if key in self._results:
                settled[key] = self._results[key]
                self._results.move_to_end(key)
            else:
                row = await (await self.storage.connection.execute(
                    "SELECT 1 FROM signal_results WHERE identity=? AND synthetic=?", (key[0], int(key[1])))).fetchone()
                settled[key] = row is not None
                self._cache(self._results, key, settled[key])
        return clocks, settled

    async def process(self, observation: SignalObservation):
        if not self.started:
            raise RuntimeError("Signal ledger has not started")
        async with self.lock:
            identity, at = observation.market.identity(), observation.evaluated_at
            strategies = set(observation.strategies) | {c.strategy for c in observation.candidates}
            previous_origins = {strategy: {key[4] for key in self.active if key[1] == identity and key[0] == strategy}
                                for strategy in strategies}
            origins = ({c.synthetic for c in observation.candidates} |
                       {origin for values in previous_origins.values() for origin in values} |
                       {observation.synthetic})
            clocks, settled = await self._load_guards(identity, strategies, origins)
            if any(clock is not None and at < clock for clock in clocks.values()):
                raise ValueError("Signal observations must be processed in timestamp order")
            candidates = [c for c in observation.candidates if not settled[identity, c.synthetic]]
            already_settled = bool(observation.candidates) and not candidates
            reason = "verified result already recorded" if already_settled else observation.reason
            keys = {(c.strategy, identity, c.venue, c.outcome, c.synthetic) for c in candidates}
            statements = []
            # Explicit NO SIGNAL closes both candidate origins for a strategy/market:
            # stale data must not retain an old usable positive episode.
            for key in list(self.active):
                if key[1] == identity and key[0] in strategies and key not in keys:
                    self._close(key, reason or "candidate unavailable", at, statements)
            qualified = set(observation.qualified_candidate_ids)
            for candidate in candidates:
                key = (candidate.strategy, identity, candidate.venue, candidate.outcome, candidate.synthetic)
                snapshot = SignalSnapshot(candidate=candidate, evaluated_at=at,
                    qualified=candidate.id in qualified, reference_model=observation.model,
                    pricing_thresholds=observation.pricing_thresholds,
                    fee_schedule=observation.fee_schedules.get(candidate.id))
                current = self.active.get(key)
                if current and (current.last.qualified != snapshot.qualified or
                                current.last.reference_model != snapshot.reference_model or
                                current.last.candidate.reference_inputs_eligible != candidate.reference_inputs_eligible or
                                current.last.candidate.reference_policy_digest != candidate.reference_policy_digest or
                                current.last.pricing_thresholds != snapshot.pricing_thresholds):
                    self._close(key, "qualification or pricing model changed", at, statements)
                    current = None
                if current is None:
                    if len(self.active) >= self.max_open_episodes:
                        raise RuntimeError("Signal active-episode limit exceeded; tracking cannot drop signals")
                    current = _ActiveEpisode(str(uuid4()), observation.market, snapshot)
                    self.active[key] = current
                    text = snapshot.model_dump_json()
                    statements.append(("""INSERT INTO signal_episodes
                        (id,identity,market_id,strategy,venue,outcome,synthetic,qualified,sport,market_type,
                         reference_model,first_seen,last_seen,update_count,market_json,first_json,last_json,best_json)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                        current.id, identity, observation.market.id, candidate.strategy, candidate.venue,
                        candidate.outcome, int(candidate.synthetic), int(snapshot.qualified), observation.market.sport,
                        observation.market.market_type, observation.model, at.isoformat(), at.isoformat(), 1,
                        observation.market.model_dump_json(), text, text, text)))
                else:
                    if at < current.last.evaluated_at:
                        raise ValueError("Signal observations must be processed in timestamp order")
                    current.updates += 1
                    current.last = snapshot
                    if candidate.ev_per_contract > current.best.candidate.ev_per_contract:
                        current.best = snapshot
                    statements.append(("""UPDATE signal_episodes SET last_seen=?,update_count=?,last_json=?,best_json=?
                        WHERE id=?""", (at.isoformat(), current.updates, snapshot.model_dump_json(),
                                          current.best.model_dump_json(), current.id)))
                statements.append(("""INSERT INTO signal_observations
                    (episode_id,observed_at,candidate_id,snapshot_json) VALUES(?,?,?,?)""",
                    (current.id, at.isoformat(), candidate.id, snapshot.model_dump_json())))
            # SQL dedup keeps NO SIGNAL transitions durable without unbounded in-memory history.
            for strategy in sorted(strategies):
                original = [c for c in observation.candidates if c.strategy == strategy]
                strategy_origins = {c.synthetic for c in original} | previous_origins[strategy]
                for origin in sorted(strategy_origins or {observation.synthetic}):
                    strategy_candidates = [c for c in candidates if c.strategy == strategy and c.synthetic == origin]
                    strategy_settled = settled.get((identity, origin), False)
                    status = ("SETTLED" if strategy_settled else
                              "POSITIVE EV" if any(c.id in qualified for c in strategy_candidates)
                              else "RESEARCH ONLY" if strategy_candidates and all(not c.reference_inputs_eligible for c in strategy_candidates)
                              else "NEGATIVE EV" if strategy_candidates else "NO SIGNAL")
                    transition_reason = "verified result already recorded" if strategy_settled else observation.reason
                    statements.append(("""INSERT INTO signal_transitions
                        (identity,market_id,strategy,synthetic,observed_at,status,reason)
                        SELECT ?,?,?,?,?,?,? WHERE NOT EXISTS (
                            SELECT 1 FROM (SELECT status,reason FROM signal_transitions
                              WHERE identity=? AND strategy=? AND synthetic=? ORDER BY seq DESC LIMIT 1)
                            WHERE status=? AND reason=?)""",
                        (identity, observation.market.id, strategy, int(origin), at.isoformat(), status,
                         transition_reason, identity, strategy, int(origin), status, transition_reason)))
                statements.append(("""INSERT INTO signal_cursors(identity,strategy,observed_at) VALUES(?,?,?)
                    ON CONFLICT(identity,strategy) DO UPDATE SET observed_at=excluded.observed_at""",
                    (identity, strategy, at.isoformat())))
            if statements:
                await self.storage.write(statements, ())
            for key in clocks:
                self._cache(self._clocks, key, at)

    async def close_all(self, reason="shutdown"):
        async with self.lock:
            at, statements = utcnow(), []
            for key in list(self.active):
                self._close(key, reason, at, statements)
            if statements:
                await self.storage.write(statements, ())
        await self.drain()

    @staticmethod
    def _grade(snapshot, result, refund_fees_returned):
        candidate, schedule = snapshot.candidate, snapshot.fee_schedule
        if schedule is None or not schedule.verified:
            return {"grade": "unavailable", "grade_reason": "First-entry fee schedule was not verified"}
        price = candidate.price
        payout = price if result.refund else result.outcome_payouts[candidate.outcome]
        if schedule.model == "none":
            fee = D(0)
        elif schedule.model == "fixed":
            fee = schedule.per_contract
        elif schedule.model == "binary_quadratic":
            fee = schedule.rate * price * (1 - price)
        elif schedule.model == "profit_commission":
            fee = max(D(0), payout - price) * schedule.rate
        else:
            return {"grade": "unavailable", "grade_reason": "Unsupported first-entry fee model"}
        fee = (fee / schedule.rounding).to_integral_value(rounding=ROUND_CEILING) * schedule.rounding
        if result.refund and refund_fees_returned:
            fee = D(0)
        pnl = payout - price - fee
        kind = ("refund" if result.refund else "partial" if payout not in {D(0), D(1)}
                else "win" if payout == 1 else "loss")
        brier = None if kind in {"refund", "partial"} else (candidate.fair_probability - payout) ** 2
        return {"grade": kind, "payout": str(payout), "fee": str(fee),
                "hypothetical_pnl": str(pnl), "hypothetical_cost": str(price),
                "brier": str(brier) if brier is not None else None, "grade_reason": None}

    async def apply_result(self, market: Market, result: SettlementResult,
                           provenance: ResultProvenance, refund_fees_returned: bool | None = None):
        """Grade a hypothetical one-contract entry; never changes orders or balances.

        Partial payouts and refunds are reported separately from binary win rates
        and calibration. A refund requires an explicit policy for returned fees.
        """
        if not self.started:
            raise RuntimeError("Signal ledger has not started")
        if (market.id != result.market_id or len(market.outcomes) != 2 or
                set(result.outcome_payouts) != set(market.outcomes)):
            raise ValueError("Result must cover the exact canonical market and opposing outcomes")
        settlement = market.settlement
        if (settlement.contract_type != "binary" or settlement.currency != "USD" or
                settlement.payout != 1 or settlement.push_rule != "not_applicable"):
            raise ValueError("Signal grading requires a USD 1 binary market without unmodelled pushes")
        if (not result.evidence.strip() or result.settled_at.tzinfo is None or
                provenance.synthetic != result.synthetic or provenance.verified_at < result.settled_at):
            raise ValueError("Result needs matching origin, aware clocks and explicit verified evidence")
        if result.settled_at > utcnow() or provenance.verified_at > utcnow():
            raise ValueError("Future result/verification clocks are not permitted")
        if result.refund and refund_fees_returned is None:
            raise ValueError("Refund grading requires an explicit fee-return policy")
        if not result.refund and sum(result.outcome_payouts.values()) != 1:
            raise ValueError("Complete opposing binary payout fractions must sum to one")
        identity = market.identity()
        payload = {"market_identity": identity, "result": result.model_dump(mode="json"),
                   "refund_fees_returned": refund_fees_returned}
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
        async with self.lock:
            await self.drain()
            conflicting = await (await self.storage.connection.execute(
                "SELECT DISTINCT identity FROM signal_episodes WHERE market_id=? AND synthetic=?",
                (market.id, int(result.synthetic)))).fetchall()
            if any(row[0] != identity for row in conflicting):
                raise ValueError("Canonical rules or identity conflict with recorded signal")
            existing = await (await self.storage.connection.execute(
                "SELECT fingerprint FROM signal_results WHERE identity=? AND synthetic=?",
                (identity, int(result.synthetic)))).fetchone()
            if existing and existing[0] != fingerprint:
                raise ValueError("Conflicting result already imported; no silent result replacement")
            rows = await (await self.storage.connection.execute(
                "SELECT id,first_json,first_seen FROM signal_episodes WHERE identity=? AND synthetic=? AND grade='pending'",
                (identity, int(result.synthetic)))).fetchall()
            statements = [("""INSERT OR IGNORE INTO signal_results
                (identity,synthetic,market_id,imported_at,result_json,provenance_json,fingerprint)
                VALUES(?,?,?,?,?,?,?)""", (identity, int(result.synthetic), market.id, utcnow().isoformat(),
                    encoded, provenance.model_dump_json(), fingerprint))]
            graded = 0
            for episode_id, snapshot_json, first_seen in rows:
                snapshot = SignalSnapshot.model_validate_json(snapshot_json)
                if datetime.fromisoformat(first_seen) >= result.settled_at:
                    grade = {"grade": "unavailable", "grade_reason": "Entry was observed at or after settlement"}
                else:
                    grade = self._grade(snapshot, result, refund_fees_returned)
                graded += grade["grade"] != "unavailable"
                statements.append(("""UPDATE signal_episodes SET grade=?,payout=?,fee=?,hypothetical_pnl=?,
                    hypothetical_cost=?,brier=?,result_identity=?,grade_reason=?,ended_at=COALESCE(ended_at,?),
                    close_reason=COALESCE(close_reason,'verified result imported') WHERE id=?""", (
                    grade["grade"], grade.get("payout"), grade.get("fee"), grade.get("hypothetical_pnl"),
                    grade.get("hypothetical_cost"), grade.get("brier"), identity, grade.get("grade_reason"),
                    result.settled_at.isoformat(), episode_id)))
            await self.storage.write(statements, (), durable=True)
            self._cache(self._results, (identity, result.synthetic), True)
            for key in list(self.active):
                if key[1] == identity and key[4] == result.synthetic:
                    self.active.pop(key)
            return {"canonical_identity": identity, "synthetic": result.synthetic,
                    "graded": graded, "idempotent": existing is not None,
                    "hypothetical_only": True, "actual_fills_affected": False}

    @staticmethod
    def _public(row, columns):
        item = dict(zip(columns, row))
        market = Market.model_validate_json(item.pop("market_json"))
        first = SignalSnapshot.model_validate_json(item.pop("first_json"))
        last = SignalSnapshot.model_validate_json(item.pop("last_json"))
        best = SignalSnapshot.model_validate_json(item.pop("best_json"))
        item.update({"synthetic": bool(item["synthetic"]), "qualified": bool(item["qualified"]),
            "canonical_identity": item.pop("identity"), "event_name": market.event_name,
            "player": market.player, "line": str(market.line) if market.line is not None else None,
            "status": "closed" if item["ended_at"] else "open", "market": market.model_dump(mode="json"),
            "first_price": float(first.candidate.price), "first_fair_probability": float(first.candidate.fair_probability),
            "first_ev_per_contract": float(first.candidate.ev_per_contract),
            "first": first.model_dump(mode="json"), "last": last.model_dump(mode="json"),
            "best": best.model_dump(mode="json"), "hypothetical_only": True,
            "clv": None, "clv_status": "unavailable: verified compatible closing observation required"})
        item["result"] = json.loads(item.pop("result_json")) if item.get("result_json") else None
        item["provenance"] = json.loads(item.pop("provenance_json")) if item.get("provenance_json") else None
        item.pop("result_json", None)
        item.pop("provenance_json", None)
        # Display numbers are convenient for clients; snapshots/SQLite retain exact decimal text.
        for name in ("payout", "fee", "hypothetical_pnl", "hypothetical_cost", "brier"):
            item[name] = float(item[name]) if item[name] is not None else None
        return item

    async def query(self, limit=50, offset=0, synthetic: bool | None = None):
        if not 1 <= limit <= 200 or offset < 0:
            raise ValueError("Signal page limit must be 1..200 with a nonnegative offset")
        async with self.lock:
            await self.drain()
            where, params = (" WHERE e.synthetic=?", (int(synthetic),)) if synthetic is not None else ("", ())
            total = (await (await self.storage.connection.execute(
                "SELECT COUNT(*) FROM signal_episodes e" + where, params)).fetchone())[0]
            cursor = await self.storage.connection.execute(
                "SELECT e.*,r.result_json,r.provenance_json FROM signal_episodes e LEFT JOIN signal_results r "
                "ON r.identity=e.result_identity AND r.synthetic=e.synthetic" + where +
                " ORDER BY e.first_seen DESC,e.id DESC LIMIT ? OFFSET ?",
                (*params, limit, offset))
            columns = [col[0] for col in cursor.description]
            return {"items": [self._public(row, columns) for row in await cursor.fetchall()],
                    "total": total, "limit": limit, "offset": offset, "hypothetical_only": True}

    async def transitions(self, limit=50, offset=0, synthetic: bool | None = None):
        if not 1 <= limit <= 200 or offset < 0:
            raise ValueError("Transition page limit must be 1..200 with a nonnegative offset")
        async with self.lock:
            await self.drain()
            where, params = (" WHERE synthetic=?", (int(synthetic),)) if synthetic is not None else ("", ())
            cursor = await self.storage.connection.execute(
                "SELECT * FROM signal_transitions" + where + " ORDER BY seq DESC LIMIT ? OFFSET ?",
                (*params, limit, offset))
            columns = [col[0] for col in cursor.description]
            return {"items": [dict(zip(columns, row)) for row in await cursor.fetchall()],
                    "limit": limit, "offset": offset}

    async def summary(self, synthetic: bool | None = None):
        async with self.lock:
            await self.drain()
            where, params = (" WHERE synthetic=?", (int(synthetic),)) if synthetic is not None else ("", ())
            columns = """COUNT(*) episodes,COALESCE(SUM(qualified),0) eligible_episodes,
                COALESCE(SUM(ended_at IS NULL),0) open,
                COALESCE(SUM(qualified=1 AND grade='pending'),0) pending,
                COALESCE(SUM(qualified=1 AND grade IN ('win','loss','partial','refund')),0) graded,
                COALESCE(SUM(qualified=1 AND grade='win'),0) wins,
                COALESCE(SUM(qualified=1 AND grade='loss'),0) losses,
                COALESCE(SUM(qualified=1 AND grade='refund'),0) refunds,
                COALESCE(SUM(qualified=1 AND grade='partial'),0) partials,
                COALESCE(SUM(qualified=1 AND grade='unavailable'),0) unavailable,
                COALESCE(SUM(grade IN ('win','loss','partial','refund')),0) all_graded,
                COALESCE(SUM(grade='pending'),0) all_pending,
                COALESCE(SUM(CASE WHEN qualified=1 THEN CAST(hypothetical_pnl AS REAL) ELSE 0 END),0) hypothetical_pnl,
                COALESCE(SUM(CASE WHEN qualified=1 THEN CAST(hypothetical_cost AS REAL) ELSE 0 END),0) hypothetical_cost,
                AVG(CASE WHEN qualified=1 THEN CAST(brier AS REAL) END) brier_score,
                COUNT(DISTINCT CASE WHEN qualified=1 THEN identity END) distinct_eligible_markets,
                COUNT(DISTINCT CASE WHEN qualified=1 THEN json_extract(market_json,'$.event_id') END) distinct_eligible_events"""
            cursor = await self.storage.connection.execute("SELECT " + columns + " FROM signal_episodes" + where, params)
            result = dict(zip([c[0] for c in cursor.description], await cursor.fetchone()))
            cost = result["hypothetical_cost"]
            result["hypothetical_roi"] = result["hypothetical_pnl"] / cost if cost else None
            result["win_rate"] = result["wins"] / (result["wins"] + result["losses"]) if result["wins"] + result["losses"] else None
            transition_counts = await (await self.storage.connection.execute(
                "SELECT COUNT(*),COALESCE(SUM(status='NO SIGNAL'),0) FROM signal_transitions" + where, params)).fetchone()
            result["transition_count"], result["no_signal_count"] = transition_counts
            obs_where = " WHERE e.synthetic=?" if synthetic is not None else ""
            result["decision_count"] = (await (await self.storage.connection.execute(
                "SELECT COUNT(*) FROM signal_observations o JOIN signal_episodes e ON e.id=o.episode_id" + obs_where, params)).fetchone())[0]
            groups = []
            # All dimensions are internal allowlisted identifiers, never request SQL.
            for dimension in ("synthetic", "strategy", "venue", "sport", "market_type", "reference_model", "qualified", "grade"):
                cursor = await self.storage.connection.execute(
                    f"SELECT {dimension} value," + columns + " FROM signal_episodes" + where + f" GROUP BY {dimension}", params)
                names = [c[0] for c in cursor.description]
                for row in await cursor.fetchall():
                    group = dict(zip(names, row))
                    group["dimension"] = dimension
                    group["count"] = group["episodes"]
                    group["hypothetical_roi"] = group["hypothetical_pnl"] / group["hypothetical_cost"] if group["hypothetical_cost"] else None
                    groups.append(group)
            result.update({"groups": groups, "synthetic": synthetic, "hypothetical_only": True,
                "clv": None, "clv_status": "unavailable: verified compatible closing observation required",
                "note": "One hypothetical contract at each qualifying episode's first observed executable price; assumes a fill and is not actual trading P&L. Negative episodes are retained but excluded from performance. Refunds and partial payouts are excluded from binary win rate and calibration. No verified result means pending. Episodes can be correlated; profit is not guaranteed."})
            result["win_rate_interval"] = None
            result["uncertainty_status"] = "Unavailable until independent event/contract clustering is validated; venue, threshold-change and restart episodes can be correlated"
            return result
