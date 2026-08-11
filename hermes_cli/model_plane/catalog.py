"""Unified (kind, provider, model) catalog for the model plane.

Every kind exposes one catalog shape to registrations, activation, and the
dashboard:

- chat ← ``hermes_cli.inventory`` (providers, custom providers, deployment
  routes are merged upstream);
- capability kinds ← the capability registry
  (:func:`hermes_cli.model_plane.capability.ensure_capability_providers`).

Rows are credential-safe: they carry availability booleans and setup metadata,
never secret values.
"""

from __future__ import annotations

from typing import Any, Optional

from hermes_cli.model_plane.capability import (
    ensure_capability_providers,
    get_capability_provider,
    list_capability_providers,
)
from hermes_cli.model_plane.kinds import CAPABILITY_KINDS, CODE


def _inventory_catalog() -> list[dict[str, Any]]:
    from hermes_cli.inventory import build_models_payload, load_picker_context

    payload = build_models_payload(
        load_picker_context(),
        include_unconfigured=True,
        picker_hints=True,
        canonical_order=True,
        allow_network=False,
    )
    return [item for item in payload.get("providers") or [] if isinstance(item, dict)]


def chat_catalog() -> list[dict[str, Any]]:
    """Return provider-owned Chat models not explicitly owned by Code."""
    from hermes_cli.model_plane.capability import get_code_provider_for_model
    from providers import get_provider_profile

    result: list[dict[str, Any]] = []
    for item in _inventory_catalog():
        slug = str(item.get("slug") or "")
        try:
            profile = get_provider_profile(slug)
        except Exception:
            profile = None
        if profile is not None and not getattr(profile, "chat_enabled", True):
            continue
        models = [
            str(model)
            for model in item.get("models") or []
            if get_code_provider_for_model(slug, str(model)) is None
        ]
        if not models:
            continue
        result.append({
            "slug": slug,
            "name": item.get("name", slug),
            "models": models,
            "authenticated": bool(item.get("authenticated", False)),
            "credential_configured": bool(item.get("authenticated", False)),
            "auth_type": item.get("auth_type", ""),
            "warning": item.get("warning", ""),
        })
    return result


def _safe_setup(setup: Any) -> dict[str, Any]:
    setup = setup if isinstance(setup, dict) else {}
    safe = {
        key: setup.get(key)
        for key in ("name", "badge", "tag")
        if setup.get(key) is not None
    }
    env_fields = []
    for item in setup.get("env_vars") or []:
        if isinstance(item, dict):
            env_fields.append({
                key: item.get(key)
                for key in ("key", "prompt", "url")
                if item.get(key) is not None
            })
    safe["env_vars"] = env_fields
    return safe


def capability_catalog(kind: str) -> list[dict[str, Any]]:
    """Return credential-safe rows for one capability-owned kind."""
    if kind not in CAPABILITY_KINDS:
        raise ValueError(f"kind must be one of {CAPABILITY_KINDS}, got {kind!r}")
    ensure_capability_providers()
    result: list[dict[str, Any]] = []
    for provider in list_capability_providers(kind):
        try:
            models = [
                {
                    "id": model.id,
                    "display": model.display or model.id,
                    **({"capability": model.capability} if model.capability else {}),
                }
                for model in provider.list_models()
            ]
            setup = provider.get_setup_schema()
            capabilities = provider.capabilities()
            available = bool(provider.is_available())
            default_model = provider.default_model()
        except Exception:
            continue
        result.append({
            "provider": provider.name,
            "name": provider.display_name,
            "available": available,
            "credential_configured": available,
            "models": models,
            "default_model": default_model,
            "capabilities": capabilities if isinstance(capabilities, dict) else {},
            "setup": _safe_setup(setup),
        })
    return result


def capability_model_catalog(kind: str, provider_name: str) -> tuple[dict[str, dict], Optional[str]]:
    """Return ({model_id: {}}, default_model) for tools_config selection."""
    if kind not in CAPABILITY_KINDS:
        raise ValueError(f"kind must be one of {CAPABILITY_KINDS}, got {kind!r}")
    ensure_capability_providers()
    provider = get_capability_provider(kind, provider_name)
    if provider is None:
        raise KeyError(f"Unknown {kind} capability provider: {provider_name!r}")
    models = {model.id: {} for model in provider.list_models() if model.id}
    return models, provider.default_model()
