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
    list_capability_providers,
)
from hermes_cli.model_plane.kinds import CAPABILITY_KINDS, RELAY_KINDS


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


def _deployment_media_descriptor() -> Any:
    """Return the active deployment descriptor without exposing credentials."""
    try:
        from hermes_cli.deployment_media import (
            deployment_media_descriptor_from_environment,
            policy_from_control_plane_environment,
        )
        from hermes_cli.owner_runtime import is_owner_worker_env

        if is_owner_worker_env():
            return deployment_media_descriptor_from_environment()
        policy = policy_from_control_plane_environment()
        return policy.descriptor() if policy is not None else None
    except Exception:
        # Catalog discovery must not make local providers disappear when a
        # deployment descriptor is absent or malformed.
        return None


def _deployment_catalog_rows(kind: str) -> list[dict[str, Any]]:
    if kind not in RELAY_KINDS:
        return []
    descriptor = _deployment_media_descriptor()
    if descriptor is None:
        return []

    rows: list[dict[str, Any]] = []
    for route in descriptor.routes:
        if route.kind != kind:
            continue
        models = [
            {
                "id": model,
                "display": model,
                "deployment_owned": True,
                "execution_mode": "deployment_relay",
            }
            for model in route.models
        ]
        rows.append({
            "provider": route.provider,
            "name": f"{route.provider.upper()} (Deployment)",
            "available": True,
            "credential_configured": True,
            "models": models,
            "default_model": route.default_model,
            "capabilities": route.capabilities_for(route.default_model),
            "setup": {"env_vars": []},
            "deployment_owned": True,
        })
    return rows


def _merge_deployment_rows(
    local_rows: list[dict[str, Any]],
    deployment_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge route models into local rows while preserving provider identity."""
    result = [dict(row) for row in local_rows]
    by_provider = {
        str(row.get("provider") or "").casefold(): row
        for row in result
        if isinstance(row, dict)
    }
    for deployment in deployment_rows:
        provider_key = str(deployment.get("provider") or "").casefold()
        existing = by_provider.get(provider_key)
        if existing is None:
            existing = dict(deployment)
            existing["models"] = [dict(model) for model in deployment.get("models") or []]
            result.append(existing)
            by_provider[provider_key] = existing
            continue

        existing_models = {
            str(model.get("id") or ""): model
            for model in existing.get("models") or []
            if isinstance(model, dict) and model.get("id")
        }
        for model in deployment.get("models") or []:
            model_id = str(model.get("id") or "")
            if model_id:
                # Deployment execution wins when a local plugin advertises the
                # same identity, since the relay owns the active route.
                existing_models[model_id] = dict(model)
        existing["models"] = list(existing_models.values())
        existing["available"] = bool(existing.get("available")) or bool(
            deployment.get("available")
        )
        existing["credential_configured"] = bool(
            existing.get("credential_configured")
        ) or bool(deployment.get("credential_configured"))
        existing.setdefault("deployment_owned", False)
        existing["deployment_owned"] = True
    return result


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
    return _merge_deployment_rows(result, _deployment_catalog_rows(kind))


def capability_model_catalog(kind: str, provider_name: str) -> tuple[dict[str, dict], Optional[str]]:
    """Return ({model_id: {}}, default_model) for tools_config selection."""
    if kind not in CAPABILITY_KINDS:
        raise ValueError(f"kind must be one of {CAPABILITY_KINDS}, got {kind!r}")
    rows = capability_catalog(kind)
    provider_key = str(provider_name or "").strip().casefold()
    row = next(
        (
            candidate
            for candidate in rows
            if str(candidate.get("provider") or "").strip().casefold() == provider_key
        ),
        None,
    )
    if row is None:
        raise KeyError(f"Unknown {kind} capability provider: {provider_name!r}")
    models = {
        str(model["id"]): {}
        for model in row.get("models") or []
        if isinstance(model, dict) and model.get("id")
    }
    return models, row.get("default_model")
