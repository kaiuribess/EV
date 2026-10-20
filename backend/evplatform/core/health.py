import random
import time
from dataclasses import dataclass


class QuotaExceeded(RuntimeError):
    pass


class TokenBucket:
    def __init__(self, capacity: int, refill_per_second: float, clock=time.monotonic):
        self.capacity, self.refill = capacity, refill_per_second
        self.tokens, self.clock, self.updated = float(capacity), clock, clock()

    def consume(self, units=1):
        now = self.clock()
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.refill)
        self.updated = now
        if self.tokens < units:
            raise QuotaExceeded("Local quota budget exhausted")
        self.tokens -= units


@dataclass
class SequenceGuard:
    sequence: int | None = None
    synchronized: bool = False

    def snapshot(self, sequence: int):
        self.sequence, self.synchronized = sequence, True

    def delta(self, sequence: int) -> bool:
        if not self.synchronized or self.sequence is None:
            return False
        if sequence <= self.sequence:
            return False
        if sequence != self.sequence + 1:
            self.synchronized = False
            return False
        self.sequence = sequence
        return True

    def disconnect(self):
        self.synchronized = False


class CircuitBreaker:
    def __init__(self, failures=3, reset_seconds=30, clock=time.monotonic):
        self.threshold, self.reset_seconds, self.clock = failures, reset_seconds, clock
        self.failures, self.opened_at = 0, None

    @property
    def available(self):
        return self.opened_at is None or self.clock() - self.opened_at >= self.reset_seconds

    def success(self):
        self.failures, self.opened_at = 0, None

    def failure(self):
        self.failures += 1
        if self.failures >= self.threshold:
            self.opened_at = self.clock()


def reconnect_delay(attempt: int, rng=None) -> float:
    return min(30, .5 * 2 ** min(attempt, 10)) * (rng or random).uniform(.5, 1)

