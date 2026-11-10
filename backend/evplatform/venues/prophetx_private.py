"""Local invalidation fences for the documented ProphetX private channel.

Registration binding types identify orders and private_system_signal. The
5-second heartbeat proves reachability only. Sequence numbers order per-order
updates; neither adjacency nor timestamp units is documented. A complete REST
history watermark and exact latest private order facts provide the separate
inventory baseline. No market/public trade event can establish private state.

Sources: docs.prophetx.co/docs/integrate-with-websockets and
docs.prophetx.co/docs/websocket-events.
"""
from __future__ import annotations

import time
from decimal import Decimal

from ..core.models import utcnow
from .prophetx_financial import prophetx_fingerprint, source_time
from .reconciliation import ReconciliationError, _number, _text


class ProphetXPrivateState:
    def __init__(self, *, max_orders=4096, heartbeat_seconds=10, monotonic=time.monotonic):
        if not 1 <= max_orders <= 100000 or not 0 < heartbeat_seconds <= 10:
            raise ValueError("Bounded private history and heartbeat limits are required")
        self.max_orders, self.heartbeat_seconds, self._monotonic = max_orders, heartbeat_seconds, monotonic
        self.generation, self.revision = 0, 0
        self.reset()

    def reset(self):
        self.generation += 1
        self.revision = 0
        self.user = self.environment = ""
        self.bindings = {}
        self.orders = {}
        self.last_heartbeat = None
        self.baseline = None
        self.invalid = False
        self.reason = "Private registration and reporting baseline required"

    def _change(self):
        self.revision += 1
        self.baseline = None

    def _reject(self, reason):
        self._change()
        self.invalid, self.reason = True, reason

    @staticmethod
    def _bindings(channels):
        result, seen = {}, set()
        if not isinstance(channels, (list, tuple)):
            raise ValueError("Expected registered channels")
        for channel in channels:
            if not isinstance(channel, dict):
                raise ValueError("Invalid registered channel")
            name = _text(channel.get("channel_name"), "registered channel")
            if name in seen:
                raise ValueError("Duplicate registered channel")
            seen.add(name)
            events = channel.get("binding_events")
            if not isinstance(events, list) or any(not isinstance(item, dict) for item in events):
                raise ValueError("Missing registered binding events")
            types = {item.get("type") for item in events if isinstance(item, dict)}
            if not types.intersection({"orders", "private_system_signal"}):
                continue
            if types != {"orders", "private_system_signal"} or channel.get("scope") not in (None, {}):
                raise ValueError("Incomplete or ambiguous private binding types")
            names = {}
            for item in events:
                event = _text(item.get("name"), "registered binding name")
                if event in names:
                    raise ValueError("Duplicate private binding name")
                names[event] = item["type"]
            result[name] = names
        if len(result) != 1:
            raise ValueError("One account-bound private channel is required")
        return result

    def register(self, channels, *, user, environment):
        # Every registration changes the local binding even if names repeat.
        self.reset()
        try:
            self.user = _text(user, "signed participant")
            prophetx_fingerprint(environment, self.user)
            self.environment = environment
            self.bindings = self._bindings(channels)
        except (ValueError, TypeError, KeyError):
            self._reject("Confirmed private orders/heartbeat registration is unavailable")

    def _bound(self, client):
        try:
            return bool(not self.invalid and self.bindings and client._ws is not None
                and client._signin_confirmed and client._authenticated_user_id == self.user
                and client.settings.environment == self.environment
                and client.health_check().get("authenticated") is True
                and set(self.bindings).issubset(client._confirmed_channels)
                and self._bindings(list(client.channels.values())) == self.bindings)
        except (AttributeError, TypeError, ValueError, KeyError):
            return False

    def _heartbeat_current(self):
        if self.last_heartbeat is None:
            return False
        age = self._monotonic() - self.last_heartbeat
        if age < 0 or age > self.heartbeat_seconds:
            # Returning heartbeats cannot revive a baseline across a quiet gap.
            self._change()
            self.last_heartbeat = None
            self.reason = "Private heartbeat gap requires fresh reporting reconciliation"
            return False
        return True

    def fence(self, *, client):
        if self._bound(client) and self._heartbeat_current():
            return self.generation, self.revision
        return None

    def begin_probe(self, *, client):
        self.baseline = None
        return self.fence(client=client)

    @staticmethod
    def _order_facts(info, user):
        if not isinstance(info, dict) or info.get("user_id") != user:
            raise ValueError("Private order belongs to an unverified participant")
        facts = {name: _text(info.get(name), "private order field") for name in
                 ("id", "user_id", "external_id", "strike_id", "status", "matching_status", "winning_status")}
        for name in ("market_id", "sport_event_id", "outcome_id"):
            value = info.get(name)
            if type(value) is not int or value < 0:
                raise ValueError("Private order needs exact numeric market identity")
            facts[name] = value
        for name in ("quantity", "open_quantity", "total_filled_quantity", "profit"):
            if isinstance(info.get(name), (float, bool)):
                raise ValueError("Private quantity must preserve decimal precision")
            facts[name] = _number(info.get(name), "private order quantity")
        price = info.get("price")
        if isinstance(price, (bool, float)) or not isinstance(price, (str, Decimal, int)):
            raise ValueError("Private order price must be exact")
        price = Decimal(price)
        if not price.is_finite() or price != price.to_integral_value() or abs(price) < 100:
            raise ValueError("Invalid private American price")
        facts["price"] = price
        if facts["open_quantity"] + facts["total_filled_quantity"] > facts["quantity"]:
            raise ValueError("Private quantities exceed the current order")
        sequence = info.get("sequence_number")
        if type(sequence) is not int or sequence < 0:
            raise ValueError("Private order needs an integer ordering token")
        return facts, sequence

    def observe(self, frame, *, client):
        channel = frame.get("channel")
        if channel not in self.bindings:
            return False
        if not self._bound(client):
            self._reject("Private account binding changed")
            return False
        kind = self.bindings[channel].get(frame.get("event"))
        frame["private_event_type"] = kind
        envelope = frame.get("envelope")
        if kind is None or (envelope is not None and
                (not isinstance(envelope, dict) or envelope.get("change_type") != kind)):
            self._reject("Unknown or conflicting private event type")
            return False
        if kind == "private_system_signal":
            if frame.get("data") != {}:
                self._reject("Malformed private heartbeat")
                return False
            self._heartbeat_current()  # Expire any prior baseline before renewal.
            self.last_heartbeat = self._monotonic()
            return True
        self._change()  # Invalidate before parsing, including malformed/replayed updates.
        try:
            facts, sequence = self._order_facts(frame.get("data", {}).get("info"), self.user)
            oid = facts["id"]
            previous = self.orders.get(oid)
            if previous and sequence <= previous[1]:
                if sequence == previous[1] and facts != previous[0]:
                    raise ValueError("Conflicting private order token")
                return False
            if oid not in self.orders and len(self.orders) >= self.max_orders:
                raise ValueError("Private order history exceeds its bound")
            self.orders[oid] = facts, sequence
            return True
        except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError):
            self.invalid, self.reason = True, "Invalid or unbounded private order state requires reconnect/reconciliation"
            return False

    def reconcile_history(self, history, *, started_at, captured_fence, client, observed_at=None):
        self.baseline = None
        now = observed_at or utcnow()
        if captured_fence is None or self.fence(client=client) != captured_fence:
            raise ReconciliationError("ProphetX private account changed or heartbeat is unavailable")
        start = source_time(started_at, observed_at=now, name="Private reconciliation start")
        watermark = source_time(history.last_synced_at, observed_at=now, name="Private reporting watermark")
        if watermark < start:
            raise ReconciliationError("ProphetX complete history watermark does not cover private probe start")
        rows = {}
        for row in history.rows:
            oid = _text(row.get("order_id"), "reporting order identity")
            if oid in rows:
                raise ReconciliationError("Ambiguous private reporting identity")
            rows[oid] = row
        for oid, (facts, _) in self.orders.items():
            row = rows.get(oid)
            if row is None:
                raise ReconciliationError("Private order is missing from complete reporting history")
            for key, expected in facts.items():
                target = {"id": "order_id", "total_filled_quantity": "filled_quantity"}.get(key, key)
                actual = row.get(target)
                if isinstance(expected, Decimal):
                    if isinstance(actual, (float, bool)) or not isinstance(actual, (str, int, Decimal)):
                        raise ReconciliationError("Missing exact private/reporting money field")
                    actual = Decimal(actual)
                elif type(expected) is int and type(actual) is not int:
                    raise ReconciliationError("Private/reporting market identifier types differ")
                if actual != expected:
                    raise ReconciliationError("Latest ProphetX private order differs from reporting history")
            update = source_time(row.get("updated_at"), observed_at=now, name="Private order reporting update")
            if update > watermark:
                raise ReconciliationError("Private order exceeds reporting watermark")
        if self.fence(client=client) != captured_fence:
            raise ReconciliationError("ProphetX private account changed during history validation")
        self.baseline = captured_fence, watermark
        self.reason = "Private orders agree with complete reporting history"
        return watermark

    def reconcile_current_empty(self, *, started_at, reporting_watermark,
                                captured_fence, client, observed_at=None):
        """Bind a current zero-inventory REST read to the live private stream.

        Terminal private events may remain cached. Any active or unrecognized
        private state prevents an empty account from being certified.
        """
        self.baseline = None
        now = observed_at or utcnow()
        if captured_fence is None or self.fence(client=client) != captured_fence:
            raise ReconciliationError("ProphetX private account changed or heartbeat is unavailable")
        start = source_time(started_at, observed_at=now, name="Private reconciliation start")
        watermark = source_time(reporting_watermark, observed_at=now,
                                name="Private reporting watermark")
        if watermark < start:
            raise ReconciliationError("Current ProphetX reporting has not caught up to the wallet probe")
        terminal = {"canceled", "cancelled", "invalid", "settled", "closed"}
        if any(facts.get("status") not in terminal for facts, _ in self.orders.values()):
            raise ReconciliationError("ProphetX private stream still has active or unknown orders")
        if self.fence(client=client) != captured_fence:
            raise ReconciliationError("ProphetX private account changed during current inventory validation")
        self.baseline = captured_fence, watermark
        self.reason = "Private stream agrees with current empty account inventory"
        return watermark

    def matches(self, snapshot, *, client):
        fence = self.fence(client=client)
        return bool(fence and self.baseline and self.baseline[0] == fence and snapshot is not None
            and snapshot.venue == "prophetx" and snapshot.environment == self.environment
            and snapshot.identity_verified and snapshot.reconciled
            and snapshot.account_fingerprint == prophetx_fingerprint(self.environment, self.user)
            and (snapshot.private_connection_generation, snapshot.private_revision) == fence
            and snapshot.private_as_of == self.baseline[1])
