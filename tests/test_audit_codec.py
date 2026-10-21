import asyncio
import hashlib
import json
import struct
import threading
import zlib

import pytest

from evplatform.core.audit_codec import (
    AuditDataError, HEADER, MAGIC, MAX_COMPRESSED_SOURCE_BYTES,
    decode_audit_data, encode_audit_data,
)
from evplatform.core.models import Envelope
from evplatform.core.storage import Storage


def large_json():
    return json.dumps({"decimal_price": "0.1234567890123456789012345", "label": "Málaga 🏀",
                       "quotes": [{"sequence": i, "odds": "1.9900", "source_time": None} for i in range(500)]}, ensure_ascii=False)


def test_compressed_audit_roundtrip_preserves_exact_bytes_decimals_unicode_and_order():
    raw = large_json()
    encoded = encode_audit_data(raw)
    assert isinstance(encoded, bytes) and len(encoded) < len(raw.encode()) / 5
    assert decode_audit_data(encoded) == raw
    assert decode_audit_data('{"old": "unchanged"}') == '{"old": "unchanged"}'
    assert encode_audit_data('{"small": 1}') == '{"small": 1}'


@pytest.mark.parametrize("damage", ["checksum", "truncated", "unknown_version", "trailing", "size_understated", "oversize", "bad_zlib"])
def test_corrupt_or_unbounded_data_is_never_silently_replayed(damage):
    encoded = encode_audit_data(large_json())
    if damage == "checksum":
        encoded = encoded[:len(MAGIC)+8] + b"x"*32 + encoded[len(MAGIC)+HEADER.size:]
    elif damage == "truncated":
        encoded = encoded[:-2]
    elif damage == "unknown_version":
        encoded = encoded.replace(b"\x01Z", b"\x02Z", 1)
    elif damage == "trailing":
        encoded += zlib.compress(b"extra")
    elif damage == "size_understated":
        encoded = MAGIC + struct.pack(">Q", 10) + encoded[len(MAGIC)+8:]
    elif damage == "oversize":
        encoded = MAGIC + struct.pack(">Q", MAX_COMPRESSED_SOURCE_BYTES+1) + encoded[len(MAGIC)+8:]
    else:
        encoded = encoded[:len(MAGIC)+HEADER.size] + b"invalid"
    with pytest.raises(AuditDataError):
        decode_audit_data(encoded)


def test_binary_plain_json_is_not_confused_with_supported_legacy_text():
    with pytest.raises(AuditDataError):
        decode_audit_data(b'{"not_a_compressed_record":true}')
    with pytest.raises(AuditDataError):
        decode_audit_data(MAGIC)
    invalid_utf8 = b"\xff"
    with pytest.raises(AuditDataError):
        decode_audit_data(MAGIC + HEADER.pack(1, hashlib.sha256(invalid_utf8).digest()) + zlib.compress(invalid_utf8))


def test_oversized_or_incompressible_records_are_preserved_as_text(monkeypatch):
    import evplatform.core.audit_codec as codec
    monkeypatch.setattr(codec, "MAX_COMPRESSED_SOURCE_BYTES", 5000)
    raw = "x" * 5001
    assert encode_audit_data(raw) is raw
    monkeypatch.setattr(codec.zlib, "compress", lambda value, level: value)
    raw = "x" * 4096
    assert encode_audit_data(raw) is raw
    with pytest.raises(TypeError):
        encode_audit_data(b"not text")


async def test_storage_preserves_mixed_legacy_and_compressed_events_across_restart(tmp_path, monkeypatch):
    import evplatform.core.storage as module
    path = str(tmp_path / "audit.db")
    event = Envelope(schema_name="signal.observation", source="offline", payload=json.loads(large_json()))
    small = Envelope(schema_name="system.notice", source="offline", payload={"message": "small"})
    main_thread, calls = threading.get_ident(), []
    original = module.encode_audit_data
    def capture(value):
        calls.append(threading.get_ident())
        return original(value)
    monkeypatch.setattr(module, "encode_audit_data", capture)
    store = Storage(path)
    await store.open()
    await store.event(event)
    await store.event(small)
    await store.event(event)  # Original ID idempotency is preserved.
    await store.save_config("offline", {"unchanged": True})
    await store.queue.join()
    rows = await (await store.connection.execute("SELECT id,typeof(data),data FROM events ORDER BY seq")).fetchall()
    assert [(row[0], row[1]) for row in rows] == [(event.id, "blob"), (small.id, "text")]
    assert all(thread != main_thread for thread in calls)
    assert await store.decode_event_data(rows[0][2]) == event.model_dump_json()
    assert await store.load_config() == {"offline": {"unchanged": True}}
    await store.close()
    restored = Storage(path)
    await restored.open()
    rows = await (await restored.connection.execute("SELECT data FROM events ORDER BY seq")).fetchall()
    actual = [Envelope.model_validate_json(await restored.decode_event_data(row[0])) for row in rows]
    assert [item.model_dump_json() for item in actual] == [event.model_dump_json(), small.model_dump_json()]
    await restored.close()


async def test_decode_and_encode_failures_propagate_and_trip_existing_storage_halt(tmp_path, monkeypatch):
    import evplatform.core.storage as module
    store = Storage(str(tmp_path / "failure.db"))
    await store.open()
    failures = []
    store.on_failure = failures.append
    def fail(value):
        raise RuntimeError("OFFLINE codec failure")
    monkeypatch.setattr(module, "encode_audit_data", fail)
    await store.event(Envelope(schema_name="system.notice", source="offline", payload={}))
    await store.queue.join()
    assert store.failure == "RuntimeError" and failures == ["Audit storage failure"]
    with pytest.raises(RuntimeError):
        await store.save_config("must_not_write", {})
    with pytest.raises(AuditDataError):
        await store.decode_event_data(b"corrupt")
    await store.close()
