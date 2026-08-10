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


def _admin_registrations() -> dict[str, dict[str, Any]]:
    from hermes_cli.deployment_inference import route_descriptors_from_control_plane

    registrations: dict[str, dict[str, Any]] = {}
    for route in route_descriptors_from_control_plane():
        registration_id = _admin_registration_id("chat", route.provider, route.model)
        registrations[registration_id] = {
            "name": route.name or route.model,
            "kind": "chat",
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


def _effective_registrations(
    user_registrations: dict[str, dict[str, Any]],
    admin_registrations: dict[str, dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    effective = {
        registration_id: {**item, "scope": "user"}
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
    media_catalog: list[dict[str, Any]] | None,
) -> tuple[dict[str, Any], dict[str, Any] | None, str]:
    name = _text(data.get("name"), "name")
    kind = _text(data.get("kind", existing.get("kind") if existing else None), "kind").lower()
    if kind not in _KINDS:
        raise ModelRegistrationError("kind must be chat, image, video, voice, or vector")
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
        catalog = chat_catalog if kind == "chat" else media_catalog
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
    if item.get("source") == "custom":
        key_env = str(item.get("key_env") or "")
        result["credential_configured"] = bool(key_env and env.get(key_env))
    else:
        result["credential_configured"] = None
    return result


def _active(config: dict[str, Any], registrations: dict[str, dict[str, Any]]) -> dict[str, Any]:
    model_cfg = config.get("model")
    if isinstance(model_cfg, dict):
        selections = {
            "chat": (model_cfg.get("provider"), model_cfg.get("default", model_cfg.get("name"))),
        }
    else:
        selections = {"chat": (None, model_cfg)}
    for kind in _ACTIVATABLE_KINDS:
        section = config.get(selection_section(kind))
        selections[kind] = (
            section.get("provider") if isinstance(section, dict) else None,
            section.get("model") if isinstance(section, dict) else None,
        )
    result: dict[str, Any] = {}
    for kind, (provider, model) in selections.items():
        registration_id = next((
            rid for rid, item in registrations.items()
            if isinstance(item, dict)
            and item.get("kind") == kind
            and item.get("provider") == provider
            and item.get("model") == model
        ), None)
        result[kind] = {
            "registration_id": registration_id,
            "provider": provider or "",
            "model": model or "",
        }
    for kind in _KINDS - selections.keys():
        result[kind] = {
            "registration_id": None,
            "provider": "",
            "model": "",
        }
    return result


def resolve_chat_model_registration(registration_id: str) -> dict[str, str]:
    """Resolve one stable chat registration to non-secret runtime identity."""
    registration_id = str(registration_id or "").strip()
    if not _ID_RE.fullmatch(registration_id):
        raise ModelRegistrationNotFound("Model registration not found")
    with _LOCK:
        config = load_config()
        item = _effective_registrations(dict(_registrations(read_raw_config()))).get(
            registration_id
        )
        if not isinstance(item, dict) or item.get("kind") != "chat":
            raise ModelRegistrationNotFound("Model registration not found")
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


def get_model_registrations_payload() -> dict[str, Any]:
    """Return public registrations and active selections without loading catalogs."""
    with _LOCK:
        config = load_config()
        registrations = _effective_registrations(
            dict(_registrations(read_raw_config()))
        )
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
        raise ModelRegistrationError("kind must be chat, image, video, voice, or vector")
    providers = _chat_catalog() if normalized == "chat" else _media_catalog(normalized)
    return {"kind": normalized, "providers": providers}


def create_model_registration(data: dict[str, Any]) -> dict[str, Any]:
    _reject_server_managed_fields(data)
    registration_id = uuid.uuid4().hex
    kind = str(data.get("kind") or "").strip().lower()
    source = str(data.get("source") or "catalog").strip().lower()
    chat = _chat_catalog() if kind == "chat" and source != "custom" else None
    media = _media_catalog(kind) if kind in _ACTIVATABLE_KINDS and source != "manual" else None
    candidate, provider_config, api_key = _normalize_request(
        data,
        registration_id=registration_id,
        existing=None,
        chat_catalog=chat,
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
    media = _media_catalog(str(kind)) if kind in _ACTIVATABLE_KINDS and source != "manual" else None
    merged = dict(existing)
    merged.update(data)
    candidate, provider_config, api_key = _normalize_request(
        merged,
        registration_id=registration_id,
        existing=existing,
        chat_catalog=chat,
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
        if item.get("kind") == "chat" and item.get("source") == "custom":
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
        kind = item.get("kind")
        if kind not in _ACTIVATABLE_KINDS:
            raise ModelRegistrationError(
                "Only image, video, voice, and vector registrations can be activated"
            )
        from hermes_cli.tools_config import select_media_model

        catalog = None
        if item.get("scope") == "admin":
            catalog = {
                candidate["model"]: {}
                for candidate in registrations.values()
                if candidate.get("scope") == "admin"
                and candidate.get("kind") == kind
                and candidate.get("provider") == item.get("provider")
            }

        select_media_model(
            config,
            kind=kind,
            provider_name=str(item.get("provider") or ""),
            model=str(item.get("model") or ""),
            use_gateway=bool(item.get("use_gateway", False)),
            catalog=catalog,
        )
        save_config(config, preserve_keys={(selection_section(kind),)})
        return {
            "ok": True,
            "registration_id": registration_id,
            "kind": kind,
            "provider": item["provider"],
            "model": item["model"],
        }

