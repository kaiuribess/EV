"""Application event-time observations; never an exchange or launch certificate.

Records join the existing durable order transactions. No extra pre-order fsync
is added, and monotonic clocks are compared only within one process instance.
"""
from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime
from uuid import uuid4

from .models import utcnow


PROCESS_INSTANCE = str(uuid4())


def intent_digest(order):
    return hashlib.sha256(order.intent.model_dump_json().encode()).hexdigest()


class NativeTrace:
    def __init__(self, build_id):
        self.build_id = build_id
        self.run_id = str(uuid4())

    def event(self, order, phase, **facts):
        return {"schema_version": 1, "phase": phase, "run_id": self.run_id,
            "process_instance": PROCESS_INSTANCE, "build_id": self.build_id,
            "observed_at": utcnow().isoformat(), "monotonic_ns": time.monotonic_ns(),
            "order_id": order.id, "client_order_id": order.client_order_id,
            "intent_digest": intent_digest(order), "venue": order.venue,
            "environment": order.mode, "account_fingerprint": order.account_fingerprint,
            "market_fingerprint": order.market_fingerprint, "status": order.status.value,
            "native_order_id": order.venue_order_id, "facts": facts}

    @staticmethod
    def statement(event):
        return ("INSERT INTO native_lifecycle_trace VALUES(NULL,?,?)",
                (event["order_id"], json.dumps(event, sort_keys=True, allow_nan=False)))


def review_application_trace(rows, order):
    """Inspect local ordering evidence without promoting it to native proof."""
    result = {"available": rows is not None, "valid": False,
        "intent_commit_before_dispatch_recorded": False,
        "recovery_in_distinct_process_recorded": False,
        "settlement_duplicate_observed": False, "event_count": 0,
        "certifies_native_lifecycle": False}
    if rows is None:
        return result
    events, sequences = [], []
    try:
        for row in rows:
            if row.get("order_id") != order.id:
                continue
            event = json.loads(row["data"])
            if (type(row.get("seq")) is not int or type(event.get("monotonic_ns")) is not int
                    or event["monotonic_ns"] < 0 or event.get("schema_version") != 1
                    or not all(isinstance(event.get(key), str) and event[key] for key in
                               ("run_id", "process_instance", "build_id", "phase"))
                    or not isinstance(event.get("facts"), dict)):
                raise ValueError("Malformed trace")
            clock = datetime.fromisoformat(event["observed_at"])
            if clock.tzinfo is None or clock > utcnow():
                raise ValueError("Invalid application observation clock")
            if event["phase"] not in {"state_saved", "intent_commit_returned", "dispatch_entered",
                    "order_recovered", "financial_reconciliation", "settlement_recorded", "settlement_duplicate"}:
                raise ValueError("Unknown trace phase")
            for field, expected in {"order_id": order.id, "client_order_id": order.client_order_id,
                    "intent_digest": intent_digest(order), "venue": order.venue, "environment": order.mode,
                    "account_fingerprint": order.account_fingerprint,
                    "market_fingerprint": order.market_fingerprint}.items():
                if event.get(field) != expected:
                    raise ValueError("Trace scope conflict")
            if event.get("native_order_id") not in {None, order.venue_order_id}:
                raise ValueError("Trace native order conflict")
            sequences.append(row["seq"])
            events.append(event)
        if len(set(sequences)) != len(sequences):
            raise ValueError("Duplicate trace sequence")
        events = [e for _, e in sorted(zip(sequences, events))]
        last = {}
        for e in events:
            process = e["process_instance"]
            if e["monotonic_ns"] < last.get(process, 0):
                raise ValueError("Regressing process clock")
            last[process] = e["monotonic_ns"]
        dispatches = [e for e in events if e["phase"] == "dispatch_entered"]
        commits = [e for e in events if e["phase"] == "intent_commit_returned"]
        result["intent_commit_before_dispatch_recorded"] = len(dispatches) == 1 and any(
            (c["process_instance"], c["run_id"], c["build_id"]) ==
            (dispatches[0]["process_instance"], dispatches[0]["run_id"], dispatches[0]["build_id"])
            and c["monotonic_ns"] <= dispatches[0]["monotonic_ns"] for c in commits)
        result["recovery_in_distinct_process_recorded"] = any(
            e["phase"] == "order_recovered" and e["facts"].get("was_unresolved") is True
            and e["process_instance"] != d["process_instance"] and e["build_id"] == d["build_id"]
            for i, e in enumerate(events) for d in events[:i] if d["phase"] == "dispatch_entered")
        result["settlement_duplicate_observed"] = any(e["phase"] == "settlement_duplicate" for e in events)
        result.update(valid=True, event_count=len(events))
    except (KeyError, TypeError, ValueError):
        result["error"] = "invalid_or_conflicting_application_trace"
    return result
