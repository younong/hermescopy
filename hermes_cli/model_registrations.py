"""Profile-scoped registered model configuration."""
from __future__ import annotations

import hashlib
import re
import threading
import uuid
from typing import Any

from hermes_cli.config import (
    _custom_provider_entry_to_provider_config,
    _normalize_custom_provider_entry,
    load_config,
    load_env,
    read_raw_config,
    remove_env_value,
    save_config,
    save_env_value,
)

from hermes_cli.model_plane.kinds import (
    ACTIVATABLE_KINDS,
    CHAT,
    CODE,
    GATEWAY_KINDS,
    KINDS,
    selection_section,
)

_KINDS = frozenset(KINDS)
_ACTIVATABLE_KINDS = frozenset(ACTIVATABLE_KINDS)
_GATEWAY_KINDS = frozenset(GATEWAY_KINDS)
# ``manual`` remains accepted for voice/vector so legacy records created
# before the catalog-covered kinds stay editable; new registrations default
# to the catalog for every kind.
_LEGACY_MANUAL_KINDS = frozenset({"voice", "vector"})
_SERVER_MANAGED_FIELDS = frozenset({
    "mutable", "owner", "owner_home", "owner_id", "owner_key", "scope",
})
_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")
_LOCK = threading.RLock()


class ModelRegistrationError(ValueError):
    """A model registration request failed validation."""


class ModelRegistrationNotFound(ModelRegistrationError):
    """The requested registration does not exist."""


class ModelRegistrationConflict(ModelRegistrationError):
    """The request conflicts with another registration or active selection."""


class ModelRegistrationImmutable(ModelRegistrationError):
    """An administrator registration cannot be changed by its consumers."""


def _text(value: Any, field: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ModelRegistrationError(f"{field} is required")
    return result


def _registrations(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    value = config.get("model_registrations")
    if value is None:
        value = {}
        config["model_registrations"] = value
    if not isinstance(value, dict):
        raise ModelRegistrationError("model_registrations must be a mapping")
    return value


def _admin_registration_id(kind: str, provider: str, model: str) -> str:
    identity = "\0".join((kind, provider.casefold(), model.casefold())).encode()
    return f"admin-{kind}-{hashlib.sha256(identity).hexdigest()[:24]}"


def _admin_media_descriptor():
    """Return the deployment media descriptor for admin registrations.

    Workers decode the supervisor-injected, secret-free descriptor payload;
    the Control Plane derives the descriptor from its own route policy —
    which applies the default policy id and also covers the legacy APIYI
    auto-route when no routes are declared.
    """
    from hermes_cli.deployment_media import (
        deployment_media_descriptor_from_environment,
        policy_from_control_plane_environment,
    )
    from hermes_cli.owner_runtime import is_owner_worker_env

    if is_owner_worker_env():
        return deployment_media_descriptor_from_environment()
    policy = policy_from_control_plane_environment()
    return policy.descriptor() if policy is not None else None


def _deployment_route_kind(route: Any) -> str:
    """Return the model-plane kind owned by a deployment inference route.

    Deployment routes use the Chat inference transport, but transport is not
    model ownership. A route for a provider registered as a Code capability
    must remain a Code registration and must not leak into the Chat surface.
    """
    from hermes_cli.model_plane.capability import get_code_provider_for_model

    if get_code_provider_for_model(
        str(route.provider or "").strip(),
        str(route.model or "").strip(),
    ) is not None:
        return CODE
    return "chat"


def _admin_chat_route_descriptors():
    """Return deployment Chat descriptors in Control Plane and Owner Workers."""
    from hermes_cli.deployment_inference import (
        DeploymentInferenceRouteDescriptor,
        deployment_descriptor_from_environment,
        policy_from_control_plane_environment,
        route_descriptors_from_control_plane,
    )
    from hermes_cli.owner_runtime import is_owner_worker_env

    relayed = route_descriptors_from_control_plane()
    if relayed:
        return relayed
    if is_owner_worker_env():
        descriptor = deployment_descriptor_from_environment()
        if descriptor is None:
            return ()
        # The compact startup descriptor preserves only the default route's
        # provider and API mode. Additional allowed models can belong to exact
        # routes with different providers, so never manufacture identities for
        # them when the private route projection is unavailable.
        return (
            DeploymentInferenceRouteDescriptor(
                provider=descriptor.provider,
                model=descriptor.model,
                api_mode=descriptor.api_mode,
            ),
        )
    try:
        return policy_from_control_plane_environment().route_descriptors()
    except Exception:
        return ()


def _admin_registrations() -> dict[str, dict[str, Any]]:
    registrations: dict[str, dict[str, Any]] = {}
    for route in _admin_chat_route_descriptors():
        kind = _deployment_route_kind(route)
        registration_id = _admin_registration_id(kind, route.provider, route.model)
        registrations[registration_id] = {
            "name": route.name or route.model,
            "kind": kind,
            "provider": route.provider,
            "model": route.model,
            "source": "catalog",
            "scope": "admin",
        }

    descriptor = _admin_media_descriptor()
    if descriptor is not None:
        for route in descriptor.routes:
            for model in route.models:
                registration_id = _admin_registration_id(route.kind, route.provider, model)
                registrations[registration_id] = {
                    "name": f"{route.provider.upper()} · {model}",
                    "kind": route.kind,
                    "provider": route.provider,
                    "model": model,
                    "source": "catalog",
                    "scope": "admin",
                    "use_gateway": False,
                }
    return registrations


def _migrate_legacy_code_registration(item: dict[str, Any]) -> dict[str, Any]:
    """Normalize the old Chat/category=code record at the model-plane boundary."""
    if item.get("kind") != "chat" or str(item.get("category") or "").strip().lower() != "code":
        return item
    migrated = dict(item)
    migrated["kind"] = CODE
    migrated.pop("category", None)
    if str(migrated.get("source") or "catalog").strip().lower() == "catalog":
        try:
            provider = str(migrated.get("provider") or "").strip()
            model = str(migrated.get("model") or "").strip()
            row = next(
                (
                    candidate
                    for candidate in _capability_catalog()
                    if str(candidate.get("provider") or "") == provider
                ),
                None,
            )
            if row is None:
                raise ModelRegistrationError(
                    f"Code provider '{provider}' is no longer available"
                )
            _validate_catalog_model(row, model)
        except Exception as exc:  # noqa: BLE001
            migrated["migration_error"] = str(exc)
    return migrated


def _migrate_persisted_registrations(config: dict[str, Any]) -> bool:
    registrations = _registrations(config)
    changed = False
    for registration_id, item in list(registrations.items()):
        if not isinstance(item, dict):
            continue
        migrated = _migrate_legacy_code_registration(item)
        if migrated != item:
            registrations[registration_id] = migrated
            changed = True
    return changed


def _effective_registrations(
    user_registrations: dict[str, dict[str, Any]],
    admin_registrations: dict[str, dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    effective = {
        registration_id: {**_migrate_legacy_code_registration(item), "scope": "user"}
        for registration_id, item in user_registrations.items()
        if isinstance(item, dict)
    }
    effective.update(
        admin_registrations
        if admin_registrations is not None
        else _admin_registrations()
    )
    return effective


def _mutable_config() -> dict[str, Any]:
    raw = read_raw_config()
    raw["model_registrations"] = dict(_registrations(raw))
    raw["providers"] = dict(raw.get("providers") or {})
    return raw


def _reject_server_managed_fields(data: dict[str, Any]) -> None:
    if _SERVER_MANAGED_FIELDS.intersection(data):
        raise ModelRegistrationError("Registration authority is server-managed")


def _reject_admin_registration(registration_id: str) -> None:
    if registration_id.startswith("admin-"):
        raise ModelRegistrationImmutable("Administrator registrations are read-only")


def _chat_catalog() -> list[dict[str, Any]]:
    from hermes_cli.model_plane.catalog import chat_catalog

    return chat_catalog()


def _capability_catalog() -> list[dict[str, Any]]:
    from hermes_cli.model_plane.catalog import capability_catalog

    return capability_catalog(CODE)


def _media_catalog(kind: str) -> list[dict[str, Any]]:
    from hermes_cli.model_plane.catalog import capability_catalog

    return capability_catalog(kind)


def _find_provider(catalog: list[dict[str, Any]], provider: str, *, media: bool) -> dict[str, Any]:
    key = "provider" if media else "slug"
    match = next((item for item in catalog if str(item.get(key) or "") == provider), None)
    if match is None:
        raise ModelRegistrationError(f"Provider '{provider}' is not available")
    return match


def _validate_catalog_model(provider_row: dict[str, Any], model: str) -> None:
    model_ids = {
        str(item.get("id") or "") if isinstance(item, dict) else str(item)
        for item in provider_row.get("models") or []
    }
    if model not in model_ids:
        raise ModelRegistrationError(f"Model '{model}' is not available from this provider")


def _custom_provider_key(registration_id: str) -> str:
    return f"registered-{registration_id.lower()}"


def _custom_env_key(registration_id: str) -> str:
    suffix = re.sub(r"[^A-Za-z0-9]", "_", registration_id).upper()
    return f"HERMES_REGISTERED_MODEL_{suffix}_API_KEY"


def _normalize_request(
    data: dict[str, Any],
    *,
    registration_id: str,
    existing: dict[str, Any] | None,
    chat_catalog: list[dict[str, Any]] | None,
    capability_catalog: list[dict[str, Any]] | None,
    media_catalog: list[dict[str, Any]] | None,
) -> tuple[dict[str, Any], dict[str, Any] | None, str]:
    name = _text(data.get("name"), "name")
    kind = _text(data.get("kind", existing.get("kind") if existing else None), "kind").lower()
    if kind not in _KINDS:
        raise ModelRegistrationError("kind must be chat, code, image, video, voice, or vector")
    if existing is not None and kind != existing.get("kind"):
        raise ModelRegistrationError("kind cannot be changed")

    model = _text(data.get("model"), "model")
    default_source = "catalog"
    source = str(data.get("source") or (existing or {}).get("source") or default_source).strip().lower()
    api_key = str(data.get("api_key") or "").strip()
    provider_config: dict[str, Any] | None = None

    if source == "manual":
        if kind not in _LEGACY_MANUAL_KINDS:
            raise ModelRegistrationError(
                "Registration source is not supported for this model type"
            )
        provider = _text(data.get("provider"), "provider")
        registration = {
            "name": name,
            "kind": kind,
            "provider": provider,
            "model": model,
            "source": "manual",
        }
    elif kind == "chat" and source == "custom":
        provider = str((existing or {}).get("provider") or _custom_provider_key(registration_id))
        base_url = _text(data.get("base_url"), "base_url")
        api_mode = _text(data.get("api_mode") or "openai", "api_mode")
        context_length = data.get("context_length")
        candidate: dict[str, Any] = {
            "name": name,
            "base_url": base_url,
            "api_mode": api_mode,
            "model": model,
            "models": {model: {}},
            "key_env": _custom_env_key(registration_id),
        }
        if context_length is not None:
            try:
                candidate["context_length"] = int(context_length)
            except (TypeError, ValueError) as exc:
                raise ModelRegistrationError("context_length must be a positive integer") from exc
        normalized = _normalize_custom_provider_entry(candidate, provider_key=provider)
        if normalized is None:
            raise ModelRegistrationError("Custom provider configuration is invalid")
        provider_config = _custom_provider_entry_to_provider_config(
            candidate,
            provider_key=provider,
        )
        if provider_config is None:
            raise ModelRegistrationError("Custom provider configuration is invalid")
        registration = {
            "name": name,
            "kind": kind,
            "provider": provider,
            "model": model,
            "source": "custom",
            "key_env": normalized["key_env"],
        }
    else:
        if source != "catalog":
            raise ModelRegistrationError("Registration source is not supported for this model type")
        provider = _text(data.get("provider"), "provider")
        if kind == "chat":
            catalog = chat_catalog
        elif kind == CODE:
            catalog = capability_catalog
        else:
            catalog = media_catalog
        if catalog is None:
            raise ModelRegistrationError("Provider catalog is unavailable")
        row = _find_provider(catalog, provider, media=kind != "chat")
        _validate_catalog_model(row, model)
        registration = {
            "name": name,
            "kind": kind,
            "provider": provider,
            "model": model,
            "source": "catalog",
        }
        if kind in _GATEWAY_KINDS:
            registration["use_gateway"] = bool(data.get("use_gateway", (existing or {}).get("use_gateway", False)))

    return registration, provider_config, api_key


def _assert_unique(
    registrations: dict[str, dict[str, Any]],
    registration_id: str,
    candidate: dict[str, Any],
) -> None:
    name = candidate["name"].casefold()
    target = (candidate["kind"], candidate["provider"].casefold(), candidate["model"].casefold())
    for other_id, other in registrations.items():
        if other_id == registration_id or not isinstance(other, dict):
            continue
        if str(other.get("name") or "").casefold() == name:
            raise ModelRegistrationConflict("Registration name already exists")
        other_target = (
            other.get("kind"),
            str(other.get("provider") or "").casefold(),
            str(other.get("model") or "").casefold(),
        )
        if other_target == target:
            raise ModelRegistrationConflict("This provider model is already registered")

def _public_registration(registration_id: str, item: dict[str, Any], env: dict[str, str]) -> dict[str, Any]:
    scope = "admin" if item.get("scope") == "admin" else "user"
    result = {
        "id": registration_id,
        "name": item.get("name", ""),
        "kind": item.get("kind", ""),
        "provider": item.get("provider", ""),
        "model": item.get("model", ""),
        "source": item.get("source", "catalog"),
        "scope": scope,
        "mutable": scope == "user",
        "use_gateway": bool(item.get("use_gateway", False)),
    }
    if item.get("migration_error"):
        result["migration_error"] = str(item["migration_error"])
    if item.get("source") == "custom":
        key_env = str(item.get("key_env") or "")
        result["credential_configured"] = bool(key_env and env.get(key_env))
    else:
        result["credential_configured"] = None
    return result


def _selection_section(config: dict[str, Any], kind: str) -> dict[str, Any]:
    if kind == CHAT:
        section = config.get("model")
        if isinstance(section, str):
            return {"default": section}
    else:
        section = config.get(selection_section(kind))
    return section if isinstance(section, dict) else {}


def _active(config: dict[str, Any], registrations: dict[str, dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for kind in _KINDS:
        section = _selection_section(config, kind)
        configured_id = str(section.get("registration_id") or "").strip()
        item = registrations.get(configured_id) if configured_id else None
        if not isinstance(item, dict) or item.get("kind") != kind:
            item = None
        provider = str(section.get("provider") or "").strip()
        model_key = "default" if kind == CHAT else "model"
        model = str(section.get(model_key) or section.get("name") or "").strip()
        if item is None and not configured_id:
            item = next((
                candidate for candidate in registrations.values()
                if isinstance(candidate, dict)
                and candidate.get("kind") == kind
                and str(candidate.get("provider") or "").strip().casefold() == provider.casefold()
                and str(candidate.get("model") or "").strip().casefold() == model.casefold()
            ), None)
        registration_id = next((rid for rid, candidate in registrations.items() if candidate is item), None)
        if item is not None:
            provider = str(item.get("provider") or "").strip()
            model = str(item.get("model") or "").strip()
        result[kind] = {
            "registration_id": registration_id,
            "provider": provider,
            "model": model,
        }
    return result


def _resolve_registered_model(registration_id: str, *, kind: str) -> dict[str, str]:
    """Resolve a registration for one explicit model-plane kind."""
    registration_id = str(registration_id or "").strip()
    if not _ID_RE.fullmatch(registration_id):
        raise ModelRegistrationNotFound("Model registration not found")
    with _LOCK:
        config = load_config()
        item = _effective_registrations(dict(_registrations(read_raw_config()))).get(
            registration_id
        )
        if not isinstance(item, dict) or item.get("kind") != kind:
            raise ModelRegistrationNotFound("Model registration not found")
        if item.get("migration_error"):
            raise ModelRegistrationError(str(item["migration_error"]))
        provider = _text(item.get("provider"), "provider")
        model = _text(item.get("model"), "model")
        source = str(item.get("source") or "catalog").strip().lower()
        result = {
            "registration_id": registration_id,
            "provider": provider,
            "model": model,
            "source": source,
        }
        if item.get("scope") == "admin":
            result["selection_source"] = "deployment"
        elif source == "custom":
            providers = config.get("providers")
            provider_config = providers.get(provider) if isinstance(providers, dict) else None
            if not isinstance(provider_config, dict):
                raise ModelRegistrationError("Custom provider configuration is invalid")
            normalized = _normalize_custom_provider_entry(provider_config, provider_key=provider)
            if normalized is None:
                raise ModelRegistrationError("Custom provider configuration is invalid")
            result["base_url"] = str(normalized.get("base_url") or "")
            result["api_mode"] = str(normalized.get("api_mode") or "")
        return result


def resolve_chat_model_registration(registration_id: str) -> dict[str, str]:
    """Resolve one stable Chat registration to non-secret runtime identity."""
    return _resolve_registered_model(registration_id, kind="chat")


def resolve_admin_chat_model_registration(registration_id: str) -> dict[str, str]:
    """Resolve one exact deployment-owned Chat registration."""
    registration_id = str(registration_id or "").strip()
    if not _ID_RE.fullmatch(registration_id):
        raise ModelRegistrationNotFound("Administrator Chat registration not found")
    item = _admin_registrations().get(registration_id)
    if not isinstance(item, dict) or item.get("kind") != "chat":
        raise ModelRegistrationNotFound("Administrator Chat registration not found")
    return {
        "registration_id": registration_id,
        "provider": _text(item.get("provider"), "provider"),
        "model": _text(item.get("model"), "model"),
        "source": str(item.get("source") or "catalog").strip().lower(),
        "selection_source": "deployment",
    }


def admin_chat_registrations_payload() -> list[dict[str, Any]]:
    """Return the selectable deployment-owned Chat registrations."""
    env = load_env()
    return [
        _public_registration(registration_id, item, env)
        for registration_id, item in _admin_registrations().items()
        if item.get("kind") == "chat"
    ]


def resolve_code_model_registration(registration_id: str) -> dict[str, str]:
    """Resolve one stable Code registration to non-secret runtime identity."""
    result = _resolve_registered_model(registration_id, kind=CODE)
    result["profile"] = "coding"
    result["toolset"] = "coding"
    return result


def get_model_registrations_payload() -> dict[str, Any]:
    """Return public registrations and active selections without loading catalogs."""
    with _LOCK:
        config = load_config()
        raw = read_raw_config()
        if _migrate_persisted_registrations(raw):
            save_config(raw, preserve_keys={("model_registrations",)})
            config = load_config()
        registrations = _effective_registrations(dict(_registrations(raw)))
        env = load_env()
    return {
        "registrations": [
            _public_registration(rid, item, env)
            for rid, item in registrations.items()
            if isinstance(item, dict)
        ],
        "active": _active(config, registrations),
    }


def get_model_registration_catalog(kind: str) -> dict[str, Any]:
    """Return the selectable catalog for one registration kind."""
    normalized = str(kind or "").strip().lower()
    if normalized not in _KINDS:
        raise ModelRegistrationError("kind must be chat, code, image, video, voice, or vector")
    if normalized == "chat":
        providers = _chat_catalog()
    elif normalized == CODE:
        providers = _capability_catalog()
    else:
        providers = _media_catalog(normalized)
    return {"kind": normalized, "providers": providers}


def create_model_registration(data: dict[str, Any]) -> dict[str, Any]:
    _reject_server_managed_fields(data)
    registration_id = uuid.uuid4().hex
    kind = str(data.get("kind") or "").strip().lower()
    source = str(data.get("source") or "catalog").strip().lower()
    chat = _chat_catalog() if kind == "chat" and source != "custom" else None
    code = _capability_catalog() if kind == CODE and source != "manual" else None
    media = _media_catalog(kind) if kind in _ACTIVATABLE_KINDS and source != "manual" else None
    candidate, provider_config, api_key = _normalize_request(
        data,
        registration_id=registration_id,
        existing=None,
        chat_catalog=chat,
        capability_catalog=code,
        media_catalog=media,
    )
    admin_registrations = _admin_registrations()
    with _LOCK:
        config = _mutable_config()
        registrations = _registrations(config)
        _assert_unique(
            _effective_registrations(registrations, admin_registrations),
            registration_id,
            candidate,
        )
        if provider_config is not None:
            providers = config.setdefault("providers", {})
            if not isinstance(providers, dict):
                raise ModelRegistrationError("providers must be a mapping")
            providers[candidate["provider"]] = provider_config
            if api_key:
                save_env_value(candidate["key_env"], api_key)
        registrations[registration_id] = candidate
        save_config(config, preserve_keys={("model_registrations",), ("providers",)})
        env = load_env() if candidate.get("source") == "custom" else {}
        return _public_registration(registration_id, candidate, env)


def update_model_registration(registration_id: str, data: dict[str, Any]) -> dict[str, Any]:
    _reject_server_managed_fields(data)
    if not _ID_RE.fullmatch(str(registration_id or "")):
        raise ModelRegistrationNotFound("Registration not found")
    _reject_admin_registration(registration_id)
    with _LOCK:
        initial = _mutable_config()
        existing = _registrations(initial).get(registration_id)
        if not isinstance(existing, dict):
            raise ModelRegistrationNotFound("Registration not found")
        existing = dict(existing)
    kind = existing.get("kind")
    source = str(data.get("source") or existing.get("source") or "catalog").strip().lower()
    chat = _chat_catalog() if kind == "chat" and source != "custom" else None
    code = _capability_catalog() if kind == CODE and source != "manual" else None
    media = _media_catalog(str(kind)) if kind in _ACTIVATABLE_KINDS and source != "manual" else None
    merged = dict(existing)
    merged.update(data)
    candidate, provider_config, api_key = _normalize_request(
        merged,
        registration_id=registration_id,
        existing=existing,
        chat_catalog=chat,
        capability_catalog=code,
        media_catalog=media,
    )
    admin_registrations = _admin_registrations()
    with _LOCK:
        config = _mutable_config()
        registrations = _registrations(config)
        current = registrations.get(registration_id)
        if not isinstance(current, dict):
            raise ModelRegistrationNotFound("Registration not found")
        if current.get("kind") != existing.get("kind"):
            raise ModelRegistrationConflict("Registration changed concurrently")
        _assert_unique(
            _effective_registrations(registrations, admin_registrations),
            registration_id,
            candidate,
        )
        if provider_config is not None:
            providers = config.setdefault("providers", {})
            if not isinstance(providers, dict):
                raise ModelRegistrationError("providers must be a mapping")
            providers[candidate["provider"]] = provider_config
            if api_key:
                save_env_value(candidate["key_env"], api_key)
        registrations[registration_id] = candidate
        save_config(config, preserve_keys={("model_registrations",), ("providers",)})
        env = load_env() if candidate.get("source") == "custom" else {}
        return _public_registration(registration_id, candidate, env)


def delete_model_registration(registration_id: str) -> dict[str, Any]:
    _reject_admin_registration(str(registration_id or ""))
    with _LOCK:
        config = _mutable_config()
        registrations = _registrations(config)
        item = registrations.get(registration_id)
        if not isinstance(item, dict):
            raise ModelRegistrationNotFound("Registration not found")
        active = _active(config, registrations).get(str(item.get("kind")), {})
        if active.get("registration_id") == registration_id:
            raise ModelRegistrationConflict("Active registration must be switched before deletion")
        preserve_keys = {("model_registrations",)}
        if item.get("kind") in {"chat", CODE} and item.get("source") == "custom":
            providers = config.get("providers")
            if isinstance(providers, dict):
                providers.pop(str(item.get("provider") or ""), None)
                preserve_keys.add(("providers",))
            key_env = str(item.get("key_env") or "")
            if key_env:
                remove_env_value(key_env)
        del registrations[registration_id]
        save_config(config, preserve_keys=preserve_keys)
    return {"ok": True, "id": registration_id}


def activate_model_registration(registration_id: str) -> dict[str, Any]:
    with _LOCK:
        config = _mutable_config()
        registrations = _effective_registrations(_registrations(config))
        item = registrations.get(registration_id)
        if not isinstance(item, dict):
            raise ModelRegistrationNotFound("Registration not found")
        kind = str(item.get("kind") or "")
        provider = str(item.get("provider") or "").strip()
        model = str(item.get("model") or "").strip()
        if kind not in _KINDS:
            raise ModelRegistrationError("Unknown model registration kind")
        if kind == CHAT:
            section = config.get("model")
            if not isinstance(section, dict):
                section = {}
                config["model"] = section
            if item.get("scope") == "admin":
                section["registration_id"] = registration_id
                for key in (
                    "provider",
                    "default",
                    "model",
                    "base_url",
                    "api_mode",
                    "api_key",
                ):
                    section.pop(key, None)
            else:
                section.update({
                    "registration_id": registration_id,
                    "provider": provider,
                    "default": model,
                })
            save_config(config, preserve_keys={("model",)})
        elif kind == CODE:
            section = config.setdefault(selection_section(CODE), {})
            if not isinstance(section, dict):
                section = {}
                config[selection_section(CODE)] = section
            section.update({"registration_id": registration_id, "provider": provider, "model": model})
            save_config(config, preserve_keys={(selection_section(CODE),)})
        else:
            from hermes_cli.tools_config import select_media_model

            catalog = None
            if item.get("scope") == "admin":
                catalog = {
                    candidate["model"]: {}
                    for candidate in registrations.values()
                    if candidate.get("scope") == "admin"
                    and candidate.get("kind") == kind
                    and candidate.get("provider") == provider
                }
            select_media_model(
                config,
                kind=kind,
                provider_name=provider,
                model=model,
                use_gateway=bool(item.get("use_gateway", False)),
                catalog=catalog,
            )
            section = config.get(selection_section(kind))
            if isinstance(section, dict):
                section["registration_id"] = registration_id
            save_config(config, preserve_keys={(selection_section(kind),)})
        return {
            "ok": True,
            "registration_id": registration_id,
            "kind": kind,
            "provider": provider,
            "model": model,
        }

