"""Typed fan-out. Overloaded critical consumers trip safety; observers never delay pricing."""
import asyncio
from collections import Counter
from dataclasses import dataclass
from typing import Callable

from pydantic import BaseModel

from .models import Envelope


class SafetyHalt(RuntimeError):
    pass


class SchemaRegistry:
    def __init__(self):
        self.models: dict[tuple[str, int], type[BaseModel]] = {}
        self.migrations: dict[tuple[str, int], Callable] = {}

    def register(self, name: str, version: int, model: type[BaseModel]):
        key = (name, version)
        if key in self.models and self.models[key] is not model:
            raise ValueError(f"Schema already registered: {key}")
        self.models[key] = model

    def add_migration(self, name: str, from_version: int, migrate: Callable):
        self.migrations[name, from_version] = migrate

    def validate(self, name: str, version: int, data):
        if (name, version) not in self.models:
            raise ValueError(f"Unknown event schema: {name}@{version}")
        return self.models[name, version].model_validate(data)

    def migrate(self, name: str, version: int, target: int, data: dict):
        if target < version:
            raise ValueError("Downgrade migrations are not implicit")
        while version < target:
            data = self.migrations[name, version](data)
            version += 1
        return self.validate(name, target, data)


@dataclass
class Subscription:
    name: str
    schemas: set[str]
    queue: asyncio.Queue
    critical: bool


class EventBus:
    def __init__(self, schemas: SchemaRegistry, on_halt: Callable[[str], None]):
        self.schemas = schemas
        self.on_halt = on_halt
        self.subscribers: list[Subscription] = []
        self.counts = Counter()
        self.dropped = Counter()

    def subscribe(self, name: str, schemas=("*",), *, critical=False, capacity=4096):
        subscription = Subscription(name, set(schemas), asyncio.Queue(capacity), critical)
        self.subscribers.append(subscription)
        return subscription.queue

    def unsubscribe(self, queue: asyncio.Queue):
        """Detach a stopped consumer before another producer can fill its queue."""
        self.subscribers = [sub for sub in self.subscribers if sub.queue is not queue]

    async def publish(self, event: Envelope):
        validated = self.schemas.validate(event.schema_name, event.schema_version, event.payload)
        event = event.model_copy(update={"payload": validated})
        targets = [s for s in self.subscribers if "*" in s.schemas or event.schema_name in s.schemas]
        # Atomic enqueue preflight: no subset of critical consumers sees an event on overflow.
        full = next((s for s in targets if s.critical and s.queue.full()), None)
        if full:
            self.on_halt(f"Critical event queue overflow: {full.name}")
            raise SafetyHalt(full.name)
        self.counts[event.schema_name] += 1
        for sub in targets:
            if sub.queue.full():
                self.dropped[sub.name] += 1
                sub.queue.get_nowait()
                sub.queue.task_done()
            sub.queue.put_nowait(event)

