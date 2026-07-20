"""
Image Generation Provider Registry
==================================

Central map of registered providers. Populated by plugins at import-time via
``PluginContext.register_image_gen_provider()``; consumed by the
``image_generate`` tool to dispatch each call to the active backend.

Active selection
----------------
The active provider is chosen by ``image_gen.provider`` in ``config.yaml``.
If unset, :func:`get_active_provider` applies fallback logic:

1. If exactly one provider is registered, use it.
2. Otherwise if a provider named ``fal`` is registered, use it (legacy
   default — matches pre-plugin behavior).
3. Otherwise return ``None`` (the tool surfaces a helpful error pointing
   the user at ``hermes tools``).
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Dict, List, Optional

from agent.image_gen_provider import ImageGenProvider

logger = logging.getLogger(__name__)


_providers: Dict[str, ImageGenProvider] = {}
_lock = threading.Lock()


def register_provider(provider: ImageGenProvider) -> None:
    """Register an image generation provider.

    Re-registration (same ``name``) overwrites the previous entry and logs
    a debug message — this makes hot-reload scenarios (tests, dev loops)
    behave predictably.
    """
    if not isinstance(provider, ImageGenProvider):
        raise TypeError(
            f"register_provider() expects an ImageGenProvider instance, "
            f"got {type(provider).__name__}"
        )
    name = provider.name
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Image gen provider .name must be a non-empty string")
    with _lock:
        existing = _providers.get(name)
        _providers[name] = provider
    if existing is not None:
        logger.debug("Image gen provider '%s' re-registered (was %r)", name, type(existing).__name__)
    else:
        logger.debug("Registered image gen provider '%s' (%s)", name, type(provider).__name__)


def list_providers() -> List[ImageGenProvider]:
    """Return all registered providers, sorted by name."""
    with _lock:
        items = list(_providers.values())
    return sorted(items, key=lambda p: p.name)


def get_provider(name: str) -> Optional[ImageGenProvider]:
    """Return the provider registered under *name*, or None."""
    if not isinstance(name, str):
        return None
    with _lock:
        return _providers.get(name.strip())


@dataclass(frozen=True)
class ImageGenResolution:
    """One provider-selection result shared by tool checks and dispatch."""

    provider: Optional[ImageGenProvider]
    configured_name: Optional[str]
    explicit: bool
    available: bool
    error_type: Optional[str] = None


def _configured_provider_name() -> Optional[str]:
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
        section = cfg.get("image_gen") if isinstance(cfg, dict) else None
        if isinstance(section, dict):
            raw = section.get("provider")
            if isinstance(raw, str) and raw.strip():
                return raw.strip()
    except Exception as exc:
        logger.debug("Could not read image_gen.provider from config: %s", exc)
    return None


def _is_available_safe(provider: ImageGenProvider) -> bool:
    try:
        return bool(provider.is_available())
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "image_gen provider %s.is_available() raised %s",
            provider.name,
            exc,
        )
        return False


def resolve_active_provider(
    configured_name: Optional[str] = None,
    *,
    read_config: bool = True,
) -> ImageGenResolution:
    """Resolve selection, availability, and explicit-config errors once.

    ``read_config=False`` lets callers that already loaded the setting pass an
    authoritative value without a second config read. Explicit configuration
    never falls back to another backend.
    """
    configured = _configured_provider_name() if read_config else configured_name
    if isinstance(configured, str):
        configured = configured.strip() or None

    with _lock:
        snapshot = dict(_providers)

    if configured:
        provider = snapshot.get(configured)
        if provider is None:
            return ImageGenResolution(
                provider=None,
                configured_name=configured,
                explicit=True,
                available=False,
                error_type="provider_not_registered",
            )
        available = _is_available_safe(provider)
        return ImageGenResolution(
            provider=provider,
            configured_name=configured,
            explicit=True,
            available=available,
            error_type=None if available else "provider_unavailable",
        )

    available = [p for p in snapshot.values() if _is_available_safe(p)]
    if len(available) == 1:
        return ImageGenResolution(available[0], None, False, True)

    fal = snapshot.get("fal")
    if fal is not None and fal in available:
        return ImageGenResolution(fal, None, False, True)

    return ImageGenResolution(
        provider=None,
        configured_name=None,
        explicit=False,
        available=False,
        error_type="provider_ambiguous" if available else "provider_unavailable",
    )


def get_active_provider() -> Optional[ImageGenProvider]:
    """Return the active provider, preserving the historical convenience API."""
    return resolve_active_provider().provider


def _reset_for_tests() -> None:
    """Clear the registry. **Test-only.**"""
    with _lock:
        _providers.clear()
