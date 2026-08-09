"""Capability provider protocol — the plugin contract for media model kinds.

Chat models belong to **providers** (:class:`providers.base.ProviderProfile`);
image/video/voice/vector models belong to **capability plugins** implementing
this protocol. The model plane consumes media access exclusively through
:class:`CapabilityProvider` and never imports a plugin implementation.

Image/video generation plugins keep their execution ABCs
(``agent/image_gen_provider.ImageGenProvider``,
``agent/video_gen_provider.VideoGenProvider``) and register HERE through
:func:`register_media_generation_provider`; voice plugins keep their
execution ABCs (``agent/tts_provider.TTSProvider``,
``agent/transcription_provider.TranscriptionProvider``) and register HERE
through :func:`register_voice_provider`; vector capabilities register
:class:`ProfileEmbeddingCapability` directly. The registry is the single
source for both the catalog and runtime resolution — new media access must
register a capability provider HERE, never a parallel registry.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any, Optional, Protocol, runtime_checkable

from hermes_cli.model_plane.kinds import (
    BUILTIN_STT_PROVIDER_NAMES,
    BUILTIN_TTS_PROVIDER_NAMES,
    FALLBACK_CAPABILITY_PROVIDERS,
    GATEWAY_KINDS,
    MEDIA_KINDS,
    VECTOR,
    VOICE,
    VOICE_CAPABILITIES,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CapabilityModel:
    """One model offered by a capability provider."""

    id: str
    display: str = ""
    # Voice models carry the sub-capability they serve ("tts" or "asr");
    # other kinds leave this empty.
    capability: str = ""


@runtime_checkable
class CapabilityProvider(Protocol):
    """The narrow media-plugin contract consumed by the model plane."""

    kind: str  # one of MEDIA_KINDS
    name: str  # stable provider identifier used in registrations
    display_name: str
    capability: str  # "" except voice delegates ("tts" / "asr")

    def is_available(self) -> bool: ...

    def list_models(self) -> list[CapabilityModel]: ...

    def default_model(self) -> Optional[str]: ...

    def get_setup_schema(self) -> dict[str, Any]: ...

    def capabilities(self) -> dict[str, Any]: ...


# Registry keyed by (kind, name, capability). Voice providers register one
# delegate per sub-capability under the same name; reads merge them.
_RegistryKey = tuple[str, str, str]
_PROVIDERS: dict[_RegistryKey, CapabilityProvider] = {}
_LOCK = threading.Lock()


def register_capability_provider(provider: CapabilityProvider) -> None:
    """Register a capability provider. Re-registration overwrites."""
    kind = str(getattr(provider, "kind", "") or "")
    if kind not in MEDIA_KINDS:
        raise ValueError(f"capability kind must be one of {MEDIA_KINDS}, got {kind!r}")
    name = str(getattr(provider, "name", "") or "").strip()
    if not name:
        raise ValueError("capability provider name must be a non-empty string")
    capability = str(getattr(provider, "capability", "") or "")
    with _LOCK:
        _PROVIDERS[(kind, name, capability)] = provider
    logger.debug(
        "Registered capability provider '%s' kind=%s capability=%s (%s)",
        name,
        kind,
        capability or "-",
        type(provider).__name__,
    )


class _MergedCapabilityProvider:
    """Read-only view merging same-name voice delegates (tts + asr)."""

    def __init__(self, kind: str, name: str, delegates: list[CapabilityProvider]) -> None:
        self.kind = kind
        self.name = name
        self.capability = ""
        self._delegates = delegates

    @property
    def display_name(self) -> str:
        return self._delegates[0].display_name

    def is_available(self) -> bool:
        return any(_is_available_safe(provider) for provider in self._delegates)

    def list_models(self) -> list[CapabilityModel]:
        models: list[CapabilityModel] = []
        seen: set[str] = set()
        for provider in self._delegates:
            for model in provider.list_models():
                if model.id and model.id not in seen:
                    seen.add(model.id)
                    models.append(model)
        return models

    def default_model(self) -> Optional[str]:
        for provider in self._delegates:
            default = provider.default_model()
            if default:
                return default
        return None

    def get_setup_schema(self) -> dict[str, Any]:
        schemas = [provider.get_setup_schema() for provider in self._delegates]
        for schema in schemas:
            if isinstance(schema, dict) and schema.get("env_vars"):
                return schema
        return schemas[0] if schemas and isinstance(schemas[0], dict) else {}

    def capabilities(self) -> dict[str, Any]:
        return {}


def _delegates_for(kind: str, name: str) -> list[CapabilityProvider]:
    with _LOCK:
        return [
            provider
            for (registered_kind, registered_name, _capability), provider in _PROVIDERS.items()
            if registered_kind == kind and registered_name == name
        ]


def _merged(kind: str, name: str) -> Optional[CapabilityProvider]:
    delegates = _delegates_for(kind, name)
    if not delegates:
        return None
    if len(delegates) == 1:
        return delegates[0]
    return _MergedCapabilityProvider(kind, name, delegates)


def get_capability_provider(kind: str, name: str) -> Optional[CapabilityProvider]:
    """Return the (merged) provider for (kind, name), or None."""
    if kind not in MEDIA_KINDS:
        return None
    return _merged(kind, str(name or "").strip())


def list_capability_providers(kind: str) -> list[CapabilityProvider]:
    """Return all providers for a media kind, sorted by name."""
    if kind not in MEDIA_KINDS:
        return []
    with _LOCK:
        names = sorted({name for (k, name, _c) in _PROVIDERS if k == kind})
    merged = [_merged(kind, name) for name in names]
    return [provider for provider in merged if provider is not None]


def _reset_for_tests() -> None:
    """Clear the registry. **Test-only.**"""
    with _LOCK:
        _PROVIDERS.clear()


def _is_available_safe(provider: CapabilityProvider) -> bool:
    try:
        return bool(provider.is_available())
    except Exception as exc:  # noqa: BLE001
        logger.debug("capability provider %s.is_available() raised %s", provider.name, exc)
        return False


# ---------------------------------------------------------------------------
# Media generation adapters — the standard registration wrapper for the
# image/video execution ABCs. ``MediaGenerationAdapter`` normalizes the
# catalog surface and delegates execution attributes (``generate`` and
# provider-specific extras) to the wrapped plugin.
# ---------------------------------------------------------------------------


class MediaGenerationAdapter:
    """Adapt an image/video generation provider to the capability protocol."""

    capability = ""

    def __init__(self, kind: str, provider: Any) -> None:
        if kind not in GATEWAY_KINDS:
            raise ValueError(f"media generation kind must be one of {GATEWAY_KINDS}")
        self.kind = kind
        self._provider = provider

    def __getattr__(self, attribute: str) -> Any:
        # Execution surface (``generate``) and provider-specific extras are
        # owned by the wrapped plugin; catalog attributes are defined above.
        return getattr(self._provider, attribute)

    @property
    def name(self) -> str:
        return self._provider.name

    @property
    def display_name(self) -> str:
        return self._provider.display_name

    def is_available(self) -> bool:
        return self._provider.is_available()

    def list_models(self) -> list[CapabilityModel]:
        models: list[CapabilityModel] = []
        for item in self._provider.list_models() or []:
            if isinstance(item, dict) and item.get("id"):
                models.append(
                    CapabilityModel(
                        id=str(item["id"]),
                        display=str(item.get("display") or item["id"]),
                    )
                )
        return models

    def default_model(self) -> Optional[str]:
        return self._provider.default_model()

    def model_entries(self) -> list[dict[str, Any]]:
        """Return the wrapped provider's rich per-model metadata dicts.

        The protocol ``list_models`` surface is normalized to id/display for
        the catalog; tool schemas need the provider's native metadata
        (modalities, constraints, durations).
        """
        entries = self._provider.list_models() or []
        return [entry for entry in entries if isinstance(entry, dict)]

    def get_setup_schema(self) -> dict[str, Any]:
        schema = self._provider.get_setup_schema()
        return schema if isinstance(schema, dict) else {}

    def capabilities(self) -> dict[str, Any]:
        caps = self._provider.capabilities()
        return caps if isinstance(caps, dict) else {}


def register_media_generation_provider(kind: str, provider: Any) -> None:
    """Register an image/video generation plugin with the capability registry."""
    register_capability_provider(MediaGenerationAdapter(kind, provider))


@dataclass(frozen=True)
class CapabilityResolution:
    """One media-selection result shared by tool checks and dispatch."""

    provider: Optional[CapabilityProvider]
    configured_name: Optional[str]
    explicit: bool
    available: bool
    error_type: Optional[str] = None


def _configured_provider_name(kind: str) -> Optional[str]:
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
        section = cfg.get(f"{kind}_gen") if isinstance(cfg, dict) else None
        if isinstance(section, dict):
            raw = section.get("provider")
            if isinstance(raw, str) and raw.strip():
                return raw.strip()
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not read %s_gen.provider from config: %s", kind, exc)
    return None


def resolve_capability_provider(
    kind: str,
    configured_name: Optional[str] = None,
    *,
    read_config: bool = True,
) -> CapabilityResolution:
    """Resolve selection, availability, and explicit-config errors once.

    ``read_config=False`` lets callers that already loaded the setting pass an
    authoritative value without a second config read. Explicit configuration
    never falls back to another backend. Without configuration the resolution
    prefers the only available provider, then the kind's fallback provider
    (:data:`FALLBACK_CAPABILITY_PROVIDERS`).
    """
    configured = _configured_provider_name(kind) if read_config else configured_name
    if isinstance(configured, str):
        configured = configured.strip() or None

    providers = {provider.name: provider for provider in list_capability_providers(kind)}

    if configured:
        provider = providers.get(configured)
        if provider is None:
            return CapabilityResolution(
                provider=None,
                configured_name=configured,
                explicit=True,
                available=False,
                error_type="provider_not_registered",
            )
        available = _is_available_safe(provider)
        return CapabilityResolution(
            provider=provider,
            configured_name=configured,
            explicit=True,
            available=available,
            error_type=None if available else "provider_unavailable",
        )

    available = [provider for provider in providers.values() if _is_available_safe(provider)]
    if len(available) == 1:
        return CapabilityResolution(available[0], None, False, True)

    fallback = providers.get(FALLBACK_CAPABILITY_PROVIDERS.get(kind, ""))
    if fallback is not None and fallback in available:
        return CapabilityResolution(fallback, None, False, True)

    return CapabilityResolution(
        provider=None,
        configured_name=None,
        explicit=False,
        available=False,
        error_type="provider_ambiguous" if available else "provider_unavailable",
    )


class VoiceCapabilityAdapter:
    """Adapt a TTS or transcription provider to a voice capability.

    Voice plugins keep their execution ABCs
    (``agent/tts_provider.TTSProvider``,
    ``agent/transcription_provider.TranscriptionProvider``); this adapter
    normalizes the catalog surface (tagging models with the sub-capability)
    and delegates execution attributes (``synthesize``/``transcribe``,
    ``voice_compatible``, ``list_voices``) to the wrapped plugin.
    """

    kind = VOICE

    def __init__(self, capability: str, provider: Any) -> None:
        if capability not in VOICE_CAPABILITIES:
            raise ValueError(
                f"voice capability must be one of {VOICE_CAPABILITIES}, "
                f"got {capability!r}"
            )
        self.capability = capability
        self._provider = provider

    def __getattr__(self, attribute: str) -> Any:
        # Execution surface (``synthesize``/``transcribe``) and
        # provider-specific extras are owned by the wrapped plugin; catalog
        # attributes are defined below.
        return getattr(self._provider, attribute)

    @property
    def name(self) -> str:
        return self._provider.name

    @property
    def display_name(self) -> str:
        return self._provider.display_name

    def is_available(self) -> bool:
        return self._provider.is_available()

    def list_models(self) -> list[CapabilityModel]:
        models: list[CapabilityModel] = []
        for item in self._provider.list_models() or []:
            if isinstance(item, dict) and item.get("id"):
                models.append(
                    CapabilityModel(
                        id=str(item["id"]),
                        display=str(item.get("display") or item["id"]),
                        capability=self.capability,
                    )
                )
        return models

    def default_model(self) -> Optional[str]:
        return self._provider.default_model()

    def get_setup_schema(self) -> dict[str, Any]:
        schema = self._provider.get_setup_schema()
        return schema if isinstance(schema, dict) else {}

    def capabilities(self) -> dict[str, Any]:
        caps = getattr(self._provider, "capabilities", None)
        if not callable(caps):
            return {}
        result = caps()
        return result if isinstance(result, dict) else {}


def register_voice_provider(capability: str, provider: Any) -> None:
    """Register a TTS or transcription plugin with the capability registry.

    Rejects:

    - Providers of the wrong ABC for *capability* (raises :class:`TypeError`).
    - Empty/whitespace ``.name`` (raises :class:`ValueError`).
    - Names colliding with a built-in voice backend (logs a warning and
      ignores the registration — built-ins always win at dispatch time, so
      a colliding plugin could never be reached).
    """
    if capability not in VOICE_CAPABILITIES:
        raise ValueError(
            f"voice capability must be one of {VOICE_CAPABILITIES}, got {capability!r}"
        )
    if capability == "tts":
        from agent.tts_provider import TTSProvider as expected
        builtin_names = BUILTIN_TTS_PROVIDER_NAMES
    else:
        from agent.transcription_provider import TranscriptionProvider as expected
        builtin_names = BUILTIN_STT_PROVIDER_NAMES
    if not isinstance(provider, expected):
        raise TypeError(
            f"register_voice_provider({capability!r}) expects a "
            f"{expected.__name__} instance, got {type(provider).__name__}"
        )
    name = getattr(provider, "name", None)
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"{expected.__name__} .name must be a non-empty string")
    if name.strip().lower() in builtin_names:
        logger.warning(
            "Voice provider '%s' shadows a built-in name; registration ignored. "
            "Built-in %s providers (%s) always win — pick a different name.",
            name.strip().lower(),
            capability.upper(),
            ", ".join(sorted(builtin_names)),
        )
        return
    register_capability_provider(VoiceCapabilityAdapter(capability, provider))


def get_voice_delegate(name: str, capability: str) -> Optional[VoiceCapabilityAdapter]:
    """Return the voice delegate for (*name*, *capability*), or None.

    Unlike :func:`get_capability_provider` — which merges same-name tts/asr
    delegates into one catalog view — this returns the single registered
    adapter so dispatch code can call the execution surface. Name matching
    is case-insensitive and whitespace-tolerant, mirroring how the voice
    tools normalize the configured provider value.
    """
    if capability not in VOICE_CAPABILITIES or not isinstance(name, str):
        return None
    key = name.strip().lower()
    if not key:
        return None
    with _LOCK:
        for (kind, registered_name, registered_capability), provider in _PROVIDERS.items():
            if (
                kind == VOICE
                and registered_capability == capability
                and registered_name.strip().lower() == key
            ):
                return provider
    return None


class ProfileEmbeddingError(RuntimeError):
    """A profile-backed embedding request failed without exposing credentials."""


class ProfileEmbeddingCapability:
    """Adapt a ProviderProfile embedding declaration to a vector capability.

    Owns the execution surface (``embed``) — the profile posts one
    OpenAI-style multimodal embedding request and returns the dense vector.
    """

    kind = VECTOR
    capability = ""

    def __init__(self, profile: Any) -> None:
        self._profile = profile

    @property
    def name(self) -> str:
        return self._profile.name

    @property
    def display_name(self) -> str:
        return self._profile.display_name or self._profile.name

    def is_available(self) -> bool:
        try:
            from agent.profile_provider_credentials import resolve_profile_api_key

            return bool(resolve_profile_api_key(self._profile))
        except Exception as exc:  # noqa: BLE001
            logger.debug("profile embedding %s availability raised %s", self.name, exc)
            return False

    def list_models(self) -> list[CapabilityModel]:
        model = self._profile.embedding_model
        return [CapabilityModel(id=model, display=model)] if model else []

    def default_model(self) -> Optional[str]:
        return self._profile.embedding_model or None

    def get_setup_schema(self) -> dict[str, Any]:
        return {
            "name": self.display_name,
            "env_vars": [{"key": key} for key in self._profile.env_vars],
        }

    def capabilities(self) -> dict[str, Any]:
        dimensions = tuple(self._profile.embedding_dimensions or ())
        return {"dimensions": list(dimensions)} if dimensions else {}

    def embed(
        self,
        *,
        text: Optional[str] = None,
        image_url: Optional[str] = None,
        dimensions: Optional[int] = None,
        instructions: Optional[str] = None,
        timeout: float = 60.0,
    ) -> dict[str, Any]:
        """Return one dense embedding for a text, image, or combined sample."""
        import requests

        from agent.profile_provider_credentials import resolve_profile_api_key

        profile = self._profile
        if not profile.embedding_model or not profile.embedding_path:
            raise ValueError(f"Provider {profile.name!r} has no embedding capability")
        api_key = resolve_profile_api_key(profile)
        if not api_key:
            env_hint = profile.env_vars[0] if profile.env_vars else "provider API key"
            raise ProfileEmbeddingError(f"{env_hint} is not configured")

        inputs: list[dict[str, Any]] = []
        if isinstance(text, str) and text.strip():
            inputs.append({"type": "text", "text": text.strip()})
        if isinstance(image_url, str) and image_url.strip():
            inputs.append({
                "type": "image_url",
                "image_url": {"url": image_url.strip()},
            })
        if not inputs:
            raise ValueError("At least one of text or image_url is required")

        supported_dimensions = tuple(profile.embedding_dimensions or ())
        if dimensions is None:
            dimensions = supported_dimensions[0] if supported_dimensions else None
        elif supported_dimensions and dimensions not in supported_dimensions:
            supported = ", ".join(str(item) for item in supported_dimensions)
            raise ValueError(f"dimensions must be one of: {supported}")

        payload: dict[str, Any] = {
            "model": profile.embedding_model,
            "input": inputs,
            "encoding_format": "float",
        }
        if dimensions is not None:
            payload["dimensions"] = dimensions
        if isinstance(instructions, str) and instructions.strip():
            payload["instructions"] = instructions.strip()

        endpoint = f"{profile.base_url.rstrip('/')}/{profile.embedding_path.lstrip('/')}"
        try:
            response = requests.post(
                endpoint,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=timeout,
            )
            response.raise_for_status()
            body = response.json()
        except Exception as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            suffix = f" (HTTP {status})" if status else ""
            raise ProfileEmbeddingError(f"Embedding request failed{suffix}") from exc

        data = body.get("data") if isinstance(body, dict) else None
        if isinstance(data, list):
            data = data[0] if data else None
        embedding = data.get("embedding") if isinstance(data, dict) else None
        if not isinstance(embedding, list):
            raise ProfileEmbeddingError("Embedding endpoint returned no dense vector")

        usage = body.get("usage") if isinstance(body, dict) else None
        return {
            "provider": profile.name,
            "model": body.get("model") or profile.embedding_model,
            "embedding": embedding,
            "dimensions": len(embedding),
            "usage": usage if isinstance(usage, dict) else {},
        }


def resolve_embedding_capability(
    provider: Optional[str] = None,
) -> ProfileEmbeddingCapability:
    """Return the embedding capability for *provider* or the vector selection.

    Without an explicit name the unified ``vector_gen`` selection is read;
    when it names no provider, a single available vector capability wins.
    Raises :class:`ValueError` when no embedding capability can be resolved.
    """
    if isinstance(provider, str) and provider.strip():
        resolved = get_capability_provider(VECTOR, provider.strip())
        if isinstance(resolved, ProfileEmbeddingCapability):
            return resolved
        raise ValueError(f"Provider {provider.strip()!r} has no embedding capability")
    resolution = resolve_capability_provider(VECTOR)
    if isinstance(resolution.provider, ProfileEmbeddingCapability):
        return resolution.provider
    detail = f" ({resolution.error_type})" if resolution.error_type else ""
    raise ValueError(f"No embedding capability is available{detail}")


def ensure_capability_providers() -> None:
    """Ensure plugin discovery (and with it capability registration) has run.

    Image/video/voice plugins and profile media bridges register natively
    during plugin discovery — image/video through
    :func:`register_media_generation_provider`, voice through
    :func:`register_voice_provider`, vector through
    :class:`ProfileEmbeddingCapability`. Idempotent: re-registration
    overwrites, so repeated calls track plugin reloads.
    """
    from hermes_cli.plugins import _ensure_plugins_discovered

    _ensure_plugins_discovered()
