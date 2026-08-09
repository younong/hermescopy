"""Capability provider protocol — the plugin contract for media model kinds.

Chat models belong to **providers** (:class:`providers.base.ProviderProfile`);
image/video/voice/vector models belong to **capability plugins** implementing
this protocol. The model plane consumes media access exclusively through
:class:`CapabilityProvider` and never imports a plugin implementation.

Until the legacy media registries are migrated (``agent/image_gen_registry``,
``agent/video_gen_registry``, ``agent/tts_registry``,
``agent/transcription_registry``) and profile embedding declarations move into
native capability plugins, :func:`ensure_capability_providers` bridges those
sources through the adapters below. Deleting the bridges together with the
legacy registries is part of that migration — new media access must register
a capability provider HERE, never a parallel registry.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any, Optional, Protocol, runtime_checkable

from hermes_cli.model_plane.kinds import IMAGE, MEDIA_KINDS, VECTOR, VIDEO, VOICE

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
# Legacy-source adapters (deleted with the legacy registries in the media
# migration; new media access registers a CapabilityProvider directly).
# ---------------------------------------------------------------------------


class _LegacyMediaAdapter:
    """Adapt an image/video generation provider to the capability protocol."""

    capability = ""

    def __init__(self, kind: str, provider: Any) -> None:
        self.kind = kind
        self._provider = provider

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

    def get_setup_schema(self) -> dict[str, Any]:
        schema = self._provider.get_setup_schema()
        return schema if isinstance(schema, dict) else {}

    def capabilities(self) -> dict[str, Any]:
        caps = self._provider.capabilities()
        return caps if isinstance(caps, dict) else {}


class _LegacyVoiceAdapter(_LegacyMediaAdapter):
    """Adapt a TTS or transcription provider to a voice capability."""

    def __init__(self, capability: str, provider: Any) -> None:
        super().__init__(VOICE, provider)
        self.capability = capability

    def list_models(self) -> list[CapabilityModel]:
        return [
            CapabilityModel(id=model.id, display=model.display, capability=self.capability)
            for model in super().list_models()
        ]

    def capabilities(self) -> dict[str, Any]:
        caps = getattr(self._provider, "capabilities", None)
        if not callable(caps):
            return {}
        result = caps()
        return result if isinstance(result, dict) else {}


class ProfileEmbeddingCapability:
    """Adapt a ProviderProfile embedding declaration to a vector capability."""

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


def ensure_capability_providers() -> None:
    """Populate the capability registry from the current media sources.

    Idempotent: re-registration overwrites, so repeated calls track plugin
    reloads. This is the PR1 bridge — it adapts the four legacy media
    registries and profile embedding declarations. The migration replaces
    these bridges with native capability plugins and deletes the registries.
    """
    from hermes_cli.plugins import _ensure_plugins_discovered

    _ensure_plugins_discovered()

    from agent import (
        image_gen_registry,
        transcription_registry,
        tts_registry,
        video_gen_registry,
    )

    for provider in image_gen_registry.list_providers():
        register_capability_provider(_LegacyMediaAdapter(IMAGE, provider))
    for provider in video_gen_registry.list_providers():
        register_capability_provider(_LegacyMediaAdapter(VIDEO, provider))
    for provider in tts_registry.list_providers():
        register_capability_provider(_LegacyVoiceAdapter("tts", provider))
    for provider in transcription_registry.list_providers():
        register_capability_provider(_LegacyVoiceAdapter("asr", provider))

    from providers import list_providers as list_profiles

    for profile in list_profiles():
        if getattr(profile, "embedding_model", "") and getattr(profile, "embedding_path", ""):
            register_capability_provider(ProfileEmbeddingCapability(profile))
