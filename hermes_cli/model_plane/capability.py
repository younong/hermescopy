"""Capability provider protocol — the plugin contract for media model kinds.

Chat models belong to **providers** (:class:`providers.base.ProviderProfile`);
image/video/voice/vector models belong to **capability plugins** implementing
this protocol. The model plane consumes media access exclusively through
:class:`CapabilityProvider` and never imports a plugin implementation.

Image/video generation plugins keep their execution ABCs
(``agent/image_gen_provider.ImageGenProvider``,
``agent/video_gen_provider.VideoGenProvider``) and register HERE through
:func:`register_media_generation_provider`; the registry is the single source
for both the catalog and runtime resolution. Voice/vector sources still
bridge through :func:`ensure_capability_providers` until their migration
lands — new media access must register a capability provider HERE, never a
parallel registry.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any, Optional, Protocol, runtime_checkable

from hermes_cli.model_plane.kinds import (
    FALLBACK_CAPABILITY_PROVIDERS,
    GATEWAY_KINDS,
    MEDIA_KINDS,
    VECTOR,
    VOICE,
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


class _LegacyVoiceAdapter:
    """Adapt a TTS or transcription provider to a voice capability.

    Voice keeps its legacy registries until the PR3 migration; this bridge
    mirrors the catalog surface without delegating execution attributes.
    """

    kind = VOICE

    def __init__(self, capability: str, provider: Any) -> None:
        self.capability = capability
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
    reloads. Image/video plugins register natively through
    :func:`register_media_generation_provider` during plugin discovery. This
    bridge remains only for the voice/vector legacy sources until their PR3
    migration lands.
    """
    from hermes_cli.plugins import _ensure_plugins_discovered

    _ensure_plugins_discovered()

    from agent import transcription_registry, tts_registry

    for provider in tts_registry.list_providers():
        register_capability_provider(_LegacyVoiceAdapter("tts", provider))
    for provider in transcription_registry.list_providers():
        register_capability_provider(_LegacyVoiceAdapter("asr", provider))

    from providers import list_providers as list_profiles

    for profile in list_profiles():
        if getattr(profile, "embedding_model", "") and getattr(profile, "embedding_path", ""):
            register_capability_provider(ProfileEmbeddingCapability(profile))
