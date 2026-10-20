import asyncio
import json

from ..core.models import Envelope


async def replay_jsonl(path, bus, *, pace_seconds=0):
    """Replay recorded source times verbatim; never re-label old observations as live."""
    # Replay is offline ingestion, outside the live processing path.
    content = await asyncio.to_thread(path.read_text, encoding="utf-8")
    count = 0
    for line in content.splitlines():
        if line.strip():
            await bus.publish(Envelope.model_validate(json.loads(line)))
            count += 1
            if pace_seconds:
                await asyncio.sleep(pace_seconds)
    return count

