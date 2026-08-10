"""Speech-to-text adapter backed by a model-provider profile."""

from __future__ import annotations

import json
import ssl
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.openspeech_v3 import (
    FULL_SERVER_RESPONSE,
    SERVER_ERROR,
    audio_only_request,
    full_client_request,
    parse_frame,
)
from agent.profile_provider_credentials import resolve_profile_api_key
from agent.redact import redact_sensitive_text
from agent.transcription_provider import TranscriptionProvider
from providers.base import ProviderProfile


_FORMATS = {
    ".wav": "wav",
    ".pcm": "pcm",
    ".mp3": "mp3",
    ".ogg": "ogg_opus",
    ".opus": "ogg_opus",
}


def _safe_error(exc: BaseException, api_key: str = "") -> str:
    message = str(exc)
    if api_key:
        message = message.replace(api_key, "«redacted-secret»")
    return redact_sensitive_text(message, force=True)


def _websocket_ssl_context() -> ssl.SSLContext:
    """Return the WSS verification context, preferring the certifi bundle.

    ``websockets`` verifies against OpenSSL's default paths while the TTS
    and embedding endpoints go through ``requests``/certifi. Hosts whose
    system CA store lags certifi (minimal server images) otherwise fail
    with ``CERTIFICATE_VERIFY_FAILED`` for the very same endpoint.
    """
    try:
        import certifi
    except ImportError:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


def _transcript(payload: dict[str, Any]) -> str:
    result = payload.get("result")
    if not isinstance(result, dict):
        return ""
    text = result.get("text")
    if isinstance(text, str) and text.strip():
        return text.strip()
    utterances = result.get("utterances")
    if not isinstance(utterances, list):
        return ""
    return "".join(
        item.get("text", "")
        for item in utterances
        if isinstance(item, dict) and isinstance(item.get("text"), str)
    ).strip()


class ProfileTranscriptionProvider(TranscriptionProvider):
    """Transcribe audio through an OpenSpeech V3 profile endpoint."""

    def __init__(self, profile: ProviderProfile):
        if (
            not profile.transcription_model
            or not profile.transcription_url
            or not profile.transcription_resource_id
        ):
            raise ValueError(
                f"Provider profile {profile.name!r} has no transcription capability"
            )
        self.profile = profile

    @property
    def name(self) -> str:
        return self.profile.name

    @property
    def display_name(self) -> str:
        return self.profile.display_name or self.profile.name

    def is_available(self) -> bool:
        return bool(resolve_profile_api_key(self.profile))

    def list_models(self) -> List[Dict[str, Any]]:
        return [{"id": self.profile.transcription_model, "display": self.profile.transcription_model}]

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": self.display_name,
            "badge": "plan",
            "tag": f"{self.profile.transcription_model} via the provider subscription",
            "env_vars": [
                {
                    "key": key,
                    "prompt": f"{self.display_name} API key",
                    "url": self.profile.signup_url,
                }
                for key in self.profile.env_vars
            ],
        }

    def _transcribe(self, path: Path, api_key: str) -> str:
        from websockets.sync.client import connect

        audio_format = _FORMATS.get(path.suffix.lower(), "wav")        request = {
            "user": {"uid": "hermes"},
            "audio": {
                "format": audio_format,
                "codec": "raw",
                "rate": 16000,
                "bits": 16,
                "channel": 1,
            },
            "request": {
                "model_name": "bigmodel",
                "enable_itn": True,
                "enable_punc": True,
                "enable_ddc": True,
                "show_utterances": True,
                "enable_nonstream": True,
            },
        }
        headers = {
            "X-Api-Key": api_key,
            "X-Api-Resource-Id": self.profile.transcription_resource_id,
            "X-Api-Request-Id": str(uuid.uuid4()),
            "X-Api-Connect-Id": str(uuid.uuid4()),
            "X-Api-Sequence": "-1",
        }
        final_text = ""
        with connect(
            self.profile.transcription_url,
            ssl=_websocket_ssl_context(),
            additional_headers=headers,
            max_size=20 * 1024 * 1024,
            open_timeout=30,
        ) as websocket:
            websocket.send(full_client_request(request))
            with path.open("rb") as source:
                chunk = source.read(16 * 1024)
                sequence = 1
                if not chunk:
                    websocket.send(audio_only_request(b"", sequence, final=True))
                while chunk:
                    next_chunk = source.read(16 * 1024)
                    websocket.send(
                        audio_only_request(
                            chunk,
                            sequence,
                            final=not next_chunk,
                        )
                    )
                    chunk = next_chunk
                    sequence += 1

            while True:
                raw = websocket.recv(timeout=60)
                if not isinstance(raw, bytes):
                    raise RuntimeError("ASR endpoint returned a text WebSocket message")
                frame = parse_frame(raw)
                if frame.message_type == SERVER_ERROR:
                    raise RuntimeError(
                        f"ASR endpoint returned error code {frame.error_code}"
                    )
                if frame.message_type != FULL_SERVER_RESPONSE:
                    continue
                payload = json.loads(frame.payload.decode("utf-8"))
                text = _transcript(payload)
                if text:
                    final_text = text
                if frame.sequence is not None and frame.sequence < 0:
                    break
        return final_text

    def transcribe(
        self,
        file_path: str,
        *,
        model: Optional[str] = None,
        language: Optional[str] = None,
        **extra: Any,
    ) -> Dict[str, Any]:
        if model and model != self.profile.transcription_model:
            return {
                "success": False,
                "transcript": "",
                "error": f"Unsupported transcription model: {model}",
                "provider": self.name,
            }
        api_key = resolve_profile_api_key(self.profile)
        if not api_key:
            env_hint = self.profile.env_vars[0] if self.profile.env_vars else "provider API key"
            return {
                "success": False,
                "transcript": "",
                "error": f"{env_hint} is not configured",
                "provider": self.name,
            }
        try:
            text = self._transcribe(Path(file_path), api_key)
            return {
                "success": True,
                "transcript": text,
                "provider": self.name,
                "model": self.profile.transcription_model,
            }
        except Exception as exc:
            return {
                "success": False,
                "transcript": "",
                "error": f"Transcription failed: {_safe_error(exc, api_key)}",
                "provider": self.name,
            }
