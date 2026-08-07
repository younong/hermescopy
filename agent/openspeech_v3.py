"""Binary framing helpers for OpenSpeech V3 request/response streams."""

from __future__ import annotations

import gzip
import json
import struct
from dataclasses import dataclass
from typing import Any


FULL_CLIENT_REQUEST = 0x1
AUDIO_ONLY_CLIENT = 0x2
FULL_SERVER_RESPONSE = 0x9
AUDIO_ONLY_SERVER = 0xB
SERVER_ERROR = 0xF

NO_SEQUENCE = 0x0
POSITIVE_SEQUENCE = 0x1
LAST_NO_SEQUENCE = 0x2
NEGATIVE_SEQUENCE = 0x3

RAW = 0x0
JSON = 0x1
NO_COMPRESSION = 0x0
GZIP = 0x1


@dataclass(frozen=True)
class OpenSpeechFrame:
    message_type: int
    flags: int
    payload: bytes
    sequence: int | None = None
    error_code: int | None = None


def _header(message_type: int, flags: int, serialization: int, compression: int) -> bytes:
    return bytes((0x11, (message_type << 4) | flags, (serialization << 4) | compression, 0))


def full_client_request(payload: dict[str, Any]) -> bytes:
    compressed = gzip.compress(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    return (
        _header(FULL_CLIENT_REQUEST, NO_SEQUENCE, JSON, GZIP)
        + struct.pack(">I", len(compressed))
        + compressed
    )


def audio_only_request(payload: bytes, sequence: int, *, final: bool = False) -> bytes:
    compressed = gzip.compress(payload)
    flags = NEGATIVE_SEQUENCE if final else POSITIVE_SEQUENCE
    wire_sequence = -abs(sequence) if final else abs(sequence)
    return (
        _header(AUDIO_ONLY_CLIENT, flags, RAW, GZIP)
        + struct.pack(">iI", wire_sequence, len(compressed))
        + compressed
    )


def parse_frame(data: bytes) -> OpenSpeechFrame:
    if len(data) < 8:
        raise ValueError("OpenSpeech frame is shorter than its header")
    header_length = (data[0] & 0x0F) * 4
    if header_length < 4 or len(data) < header_length + 4:
        raise ValueError("OpenSpeech frame has an invalid header length")

    message_type = data[1] >> 4
    flags = data[1] & 0x0F
    compression = data[2] & 0x0F
    offset = header_length
    sequence = None
    error_code = None

    if message_type == SERVER_ERROR:
        if len(data) < offset + 8:
            raise ValueError("OpenSpeech error frame is truncated")
        error_code = struct.unpack_from(">I", data, offset)[0]
        offset += 4
    elif flags in (POSITIVE_SEQUENCE, NEGATIVE_SEQUENCE):
        if len(data) < offset + 8:
            raise ValueError("OpenSpeech sequence frame is truncated")
        sequence = struct.unpack_from(">i", data, offset)[0]
        offset += 4

    size = struct.unpack_from(">I", data, offset)[0]
    offset += 4
    payload = data[offset : offset + size]
    if len(payload) != size:
        raise ValueError("OpenSpeech frame payload is truncated")
    if compression == GZIP and payload:
        payload = gzip.decompress(payload)

    return OpenSpeechFrame(
        message_type=message_type,
        flags=flags,
        payload=payload,
        sequence=sequence,
        error_code=error_code,
    )
