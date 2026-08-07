"""OpenSpeech V3 framing and profile-backed ASR contracts."""

from __future__ import annotations

import gzip
import json
import struct
from pathlib import Path
from unittest.mock import patch

from agent.openspeech_v3 import (
    AUDIO_ONLY_CLIENT,
    FULL_CLIENT_REQUEST,
    FULL_SERVER_RESPONSE,
    NEGATIVE_SEQUENCE,
    audio_only_request,
    full_client_request,
    parse_frame,
)
from agent.profile_transcription_provider import ProfileTranscriptionProvider
from providers import get_provider_profile


PROVIDER = "volcengine-agent-plan"


def _server_response(payload: dict, sequence: int = -1) -> bytes:
    body = gzip.compress(json.dumps(payload).encode())
    return bytes((0x11, 0x93, 0x11, 0)) + struct.pack(">iI", sequence, len(body)) + body


def test_full_client_request_is_gzip_json():
    frame = full_client_request({"request": {"model_name": "bigmodel"}})
    assert frame[:4] == bytes((0x11, 0x10, 0x11, 0))
    size = struct.unpack(">I", frame[4:8])[0]
    assert len(frame[8:]) == size
    assert json.loads(gzip.decompress(frame[8:])) == {
        "request": {"model_name": "bigmodel"}
    }


def test_last_audio_packet_uses_negative_sequence():
    frame = audio_only_request(b"audio", 3, final=True)
    assert frame[1] >> 4 == AUDIO_ONLY_CLIENT
    assert frame[1] & 0x0F == NEGATIVE_SEQUENCE
    assert struct.unpack(">i", frame[4:8])[0] == -3
    size = struct.unpack(">I", frame[8:12])[0]
    assert gzip.decompress(frame[12 : 12 + size]) == b"audio"


def test_parse_gzip_server_response():
    frame = parse_frame(_server_response({"result": {"text": "测试"}}))
    assert frame.message_type == FULL_SERVER_RESPONSE
    assert frame.sequence == -1
    assert json.loads(frame.payload) == {"result": {"text": "测试"}}


def test_transcription_uses_plan_headers_and_extracts_result(tmp_path, monkeypatch):
    monkeypatch.setenv("VOLCENGINE_AGENT_PLAN_API_KEY", "fake-plan-key")
    audio = tmp_path / "input.wav"
    audio.write_bytes(b"wave")
    websocket = _FakeWebSocket([_server_response({"result": {"text": "测试"}})])
    connect = _FakeConnect(websocket)
    provider = ProfileTranscriptionProvider(get_provider_profile(PROVIDER))

    with patch("websockets.sync.client.connect", connect):
        result = provider.transcribe(str(audio))

    assert result == {
        "success": True,
        "transcript": "测试",
        "provider": PROVIDER,
        "model": "doubao-seed-asr-2.0",
    }
    assert connect.url == (
        "wss://openspeech.bytedance.com/api/v3/plan/sauc/bigmodel_nostream"
    )
    assert connect.kwargs["additional_headers"]["X-Api-Key"] == "fake-plan-key"
    assert connect.kwargs["additional_headers"]["X-Api-Resource-Id"] == (
        "volc.seedasr.sauc.duration"
    )
    assert connect.kwargs["additional_headers"]["X-Api-Sequence"] == "-1"
    request = parse_frame(websocket.sent[0])
    assert request.message_type == FULL_CLIENT_REQUEST
    body = json.loads(request.payload)
    assert body["audio"] == {
        "format": "wav",
        "codec": "raw",
        "rate": 16000,
        "bits": 16,
        "channel": 1,
    }
    assert body["request"] == {
        "model_name": "bigmodel",
        "enable_itn": True,
        "enable_punc": True,
        "enable_ddc": True,
        "show_utterances": True,
        "enable_nonstream": True,
    }
    final_audio = parse_frame(websocket.sent[-1])
    assert final_audio.message_type == AUDIO_ONLY_CLIENT
    assert final_audio.sequence == -1


def test_transcription_failure_redacts_key(tmp_path, monkeypatch):
    monkeypatch.setenv("VOLCENGINE_AGENT_PLAN_API_KEY", "fake-plan-key")
    audio = tmp_path / "input.wav"
    audio.write_bytes(b"wave")
    provider = ProfileTranscriptionProvider(get_provider_profile(PROVIDER))

    with patch(
        "websockets.sync.client.connect",
        side_effect=RuntimeError("fake-plan-key"),
    ):
        result = provider.transcribe(str(audio))

    assert result["success"] is False
    assert "fake-plan-key" not in result["error"]


class _FakeWebSocket:
    def __init__(self, received: list[bytes]):
        self.received = iter(received)
        self.sent: list[bytes] = []

    def send(self, data: bytes):
        self.sent.append(data)

    def recv(self, timeout=None):
        return next(self.received)


class _FakeConnect:
    def __init__(self, websocket: _FakeWebSocket):
        self.websocket = websocket
        self.url = ""
        self.kwargs = {}

    def __call__(self, url: str, **kwargs):
        self.url = url
        self.kwargs = kwargs
        return self

    def __enter__(self):
        return self.websocket

    def __exit__(self, exc_type, exc, traceback):
        return False
