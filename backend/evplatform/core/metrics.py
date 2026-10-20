from collections import Counter, defaultdict, deque
from datetime import datetime
from math import ceil


class Metrics:
    def __init__(self):
        self.samples = defaultdict(lambda: deque(maxlen=10000))
        self.counters = Counter()
        self.active_opportunities = {}

    def observe(self, name, value):
        if value >= 0:
            self.samples[name].append(float(value))

    def update_opportunities(self, keys: set[str], now: datetime):
        for key in keys - self.active_opportunities.keys():
            self.active_opportunities[key] = now
            self.counters["opportunities_detected"] += 1
        for key in list(self.active_opportunities):
            if key not in keys:
                self.observe("opportunity_duration_ms", (now - self.active_opportunities.pop(key)).total_seconds() * 1000)

    def snapshot(self):
        summary = {}
        for key, values in self.samples.items():
            ordered = sorted(values)
            n = len(ordered)
            if not n:
                continue
            summary[key] = {
                "count": n, "median": ordered[(n - 1) // 2],
                "p95": ordered[ceil(n * .95) - 1] if n >= 20 else None,
                "p99": ordered[ceil(n * .99) - 1] if n >= 100 else None,
            }
        return {"latency": summary, "counters": dict(self.counters),
                "sample_limit": 10000, "latency_unit": "ms",
                "note": "Local measured processing. Synthetic feed delays are not live sportsbook latency."}

