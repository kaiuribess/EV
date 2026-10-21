"""Versioned, lossless storage for audit JSON; old TEXT rows remain readable.

No record is sampled, merged, or removed. SQLite's events.data affinity stays
TEXT, while compressed records use BLOB storage with an unambiguous marker.
"""
from __future__ import annotations

import hashlib
import struct
import zlib

MAGIC = b"EVPAUDIT\x01Z"
HEADER = struct.Struct(">Q32s")
COMPRESSION_THRESHOLD = 4096
MAX_COMPRESSED_SOURCE_BYTES = 16 * 1024 * 1024


class AuditDataError(ValueError):
    """An unsupported/corrupt record must not be silently replayed or skipped."""


def encode_audit_data(data: str) -> str | bytes:
    if not isinstance(data, str):
        raise TypeError("Audit source must be serialized JSON text")
    raw = data.encode("utf-8")
    # Oversized legacy-compatible TEXT is preserved, never truncated. Keeping a
    # compression bound also bounds decoder allocation for every new BLOB.
    if len(raw) < COMPRESSION_THRESHOLD or len(raw) > MAX_COMPRESSED_SOURCE_BYTES:
        return data
    compressed = zlib.compress(raw, level=1)
    packed = MAGIC + HEADER.pack(len(raw), hashlib.sha256(raw).digest()) + compressed
    return packed if len(packed) < len(raw) else data


def decode_audit_data(data: str | bytes) -> str:
    if isinstance(data, str):
        return data
    if not isinstance(data, bytes) or not data.startswith(MAGIC):
        raise AuditDataError("Unsupported audit encoding")
    header_end = len(MAGIC) + HEADER.size
    if len(data) <= header_end:
        raise AuditDataError("Truncated audit record")
    size, digest = HEADER.unpack(data[len(MAGIC):header_end])
    if not 0 < size <= MAX_COMPRESSED_SOURCE_BYTES or len(data) > MAX_COMPRESSED_SOURCE_BYTES:
        raise AuditDataError("Audit record exceeds decompression bound")
    try:
        decoder = zlib.decompressobj()
        raw = decoder.decompress(data[header_end:], size + 1)
        # Do not flush an unbounded decompressor. eof proves the complete stream
        # was consumed; trailing streams/bytes and understated sizes are invalid.
        if len(raw) != size or not decoder.eof or decoder.unconsumed_tail or decoder.unused_data:
            raise AuditDataError("Invalid audit record length or stream")
        if hashlib.sha256(raw).digest() != digest:
            raise AuditDataError("Audit record checksum mismatch")
        return raw.decode("utf-8")
    except (zlib.error, UnicodeDecodeError):
        raise AuditDataError("Corrupt audit record") from None
