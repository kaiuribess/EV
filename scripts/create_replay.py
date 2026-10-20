"""Generate portable deterministic source events with original clocks preserved."""
import json
from pathlib import Path
from evplatform.core.models import Envelope
from evplatform.plugins.demo import demo_frame

target = Path("fixtures/demo_replay.jsonl")
target.parent.mkdir(exist_ok=True)
with target.open("w", encoding="utf-8") as output:
    for tick in (0, 1, 23, 42, 56, 61):
        for index, (schema, payload) in enumerate(demo_frame(tick)):
            event = Envelope(id=f"fixture:{tick}:{index}", schema_name=schema, source="synthetic_fixture",
                             payload=payload, emitted_at=payload.source_time if getattr(payload, "source_time", None) else payload.start_time)
            output.write(event.model_dump_json() + "\n")
print(f"Wrote {target}")

