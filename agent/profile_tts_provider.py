"""Text-to-speech adapter backed by a model-provider profile."""

from __future__ import annotations

import base64
import json
import uuid
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

import requests

from agent.profile_provider_credentials import resolve_profile_api_key
from agent.redact import redact_sensitive_text
from agent.tts_provider import DEFAULT_OUTPUT_FORMAT, TTSProvider
from providers.base import ProviderProfile


_FORMATS = {
    "mp3": "mp3",
    "ogg": "ogg_opus",
    "opus": "ogg_opus",
}


def _safe_error(exc: BaseException, api_key: str = "") -> str:
    message = str(exc)
    if api_key:
        message = message.replace(api_key, "«redacted-secret»")
    return redact_sensitive_text(message, force=True)


class ProfileTTSProvider(TTSProvider):
    """Synthesize speech through an HTTP chunked profile endpoint."""

    def __init__(self, profile: ProviderProfile):
        if not profile.tts_model or not profile.tts_url or not profile.tts_resource_id:
            raise ValueError(f"Provider profile {profile.name!r} has no TTS capability")
        self.profile = profile

    @property
    def name(self) -> str:
        return self.profile.name

    @property
    def display_name(self) -> str:
        return self.profile.display_name or self.profile.name

    @property
    def voice_compatible(self) -> bool:
        return True

    def is_available(self) -> bool:
        return bool(resolve_profile_api_key(self.profile))

    def list_models(self) -> List[Dict[str, Any]]:
        return [{"id": self.profile.tts_model, "display": self.profile.tts_model}]

    def list_voices(self) -> List[Dict[str, Any]]:
        if not self.profile.tts_default_voice:
            return []
        return [{
            "id": self.profile.tts_default_voice,
            "display": self.profile.tts_default_voice,
            "language": "zh-CN",
        }]

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": self.display_name,
            "badge": "plan",
            "tag": f"{self.profile.tts_model} via the provider subscription",
            "env_vars": [
                {
                    "key": key,
                    "prompt": f"{self.display_name} API key",
                    "url": self.profile.signup_url,
                }
                for key in self.profile.env_vars
            ],
        }

    def _audio_chunks(
        self,
        text: str,
        *,
        voice: str,
        format: str,
        speed: Optional[float],
    ) -> Iterator[bytes]:
        api_key = resolve_profile_api_key(self.profile)
        if not api_key:
            env_hint = self.profile.env_vars[0] if self.profile.env_vars else "provider API key"
            raise RuntimeError(f"{env_hint} is not configured")

        if format not in _FORMATS:
            raise ValueError(f"Unsupported TTS output format: {format}")
        audio_params: dict[str, Any] = {
            "format": _FORMATS[format],
            "sample_rate": 24000,
        }
        if speed is not None:
            if speed <= 0:
                raise ValueError("TTS speed must be greater than zero")
            audio_params["speech_rate"] = max(-50, min(100, round((speed - 1) * 100)))
        response = requests.post(
            self.profile.tts_url,
            headers={
                "X-Api-Key": api_key,
                "X-Api-Resource-Id": self.profile.tts_resource_id,
                "X-Api-Request-Id": str(uuid.uuid4()),
                "X-Control-Require-Usage-Tokens-Return": "*",
                "Content-Type": "application/json",
                "Connection": "keep-alive",
            },
            json={
                "req_params": {
                    "text": text,
                    "speaker": voice,
                    "audio_params": audio_params,
                }
            },
            stream=True,
            timeout=300,
        )
        response.raise_for_status()
        for raw_line in response.iter_lines():
            if not raw_line:
                continue
            message = json.loads(raw_line)
            code = message.get("code")
            if code == 20000000:
                break
            if code != 0:
                raise RuntimeError(f"TTS endpoint returned error code {code}")
            data = message.get("data")
            if isinstance(data, str) and data:
                yield base64.b64decode(data)

    def synthesize(
        self,
        text: str,
        output_path: str,
        *,
        voice: Optional[str] = None,
        model: Optional[str] = None,
        speed: Optional[float] = None,
        format: str = DEFAULT_OUTPUT_FORMAT,
        **extra: Any,
    ) -> str:
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text is required")
        if model and model != self.profile.tts_model:
            raise ValueError(f"Unsupported TTS model: {model}")
        output = Path(output_path).expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        selected_voice = voice or self.profile.tts_default_voice
        if not selected_voice:
            raise ValueError("voice is required")
        api_key = resolve_profile_api_key(self.profile)
        try:
            with output.open("wb") as destination:
                for chunk in self._audio_chunks(
                    text.strip(),
                    voice=selected_voice,
                    format=str(format or "mp3").lower(),
                    speed=speed,
                ):
                    destination.write(chunk)
        except Exception as exc:
            output.unlink(missing_ok=True)
            raise RuntimeError(f"TTS request failed: {_safe_error(exc, api_key)}") from None
        return str(output)
