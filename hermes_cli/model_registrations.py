"""Profile-scoped registered chat and media model configuration."""
from __future__ import annotations

import re
import threading
import uuid
from typing import Any

from hermes_cli.config import (
    _custom_provider_entry_to_provider_config,
    _normalize_custom_provider_entry,
    load_config,
    load_env,
    remove_env_value,
    save_config,
    save_env_value,
)

_KINDS = frozenset({"chat", "image", "video"})
_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")
_LOCK = threading.RLock()


class ModelRegistrationError(ValueError):
    """A model registration request failed validation."""


class ModelRegistrationNotFound(ModelRegistrationError):
    """The requested registration does not exist."""


class ModelRegistrationConflict(ModelRegistrationError):
    """The request conflicts with another registration or active selection."""


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


def _chat_catalog() -> list[dict[str, Any]]:
    from hermes_cli.inventory import build_models_payload, load_picker_context

    payload = build_models_payload(
        load_picker_context(),
        include_unconfigured=True,
        picker_hints=True,
        canonical_order=True,
        allow_network=False,
    )
    result: list[dict[str, Any]] = []
    for item in payload.get("providers") or []:
        if not isinstance(item, dict):
            continue
        result.append({
            "slug": item.get("slug", ""),
            "name": item.get("name", item.get("slug", "")),
            "models": [str(model) for model in item.get("models") or []],
            "authenticated": bool(item.get("authenticated", False)),
            "credential_configured": bool(item.get("authenticated", False)),
            "auth_type": item.get("auth_type", ""),
            "warning": item.get("warning", ""),
        })
    return result


def _media_catalog(kind: str) -> list[dict[str, Any]]:
    from hermes_cli.plugins import _ensure_plugins_discovered

    _ensure_plugins_discovered()
    if kind == "image":
        from agent.image_gen_registry import list_providers
    else:
        from agent.video_gen_registry import list_providers

    result: list[dict[str, Any]] = []
    for provider in list_providers():
        try:
            raw_models = provider.list_models() or []
            models = [dict(item) for item in raw_models if isinstance(item, dict) and item.get("id")]
            setup = provider.get_setup_schema()
            capabilities = provider.capabilities()
            available = bool(provider.is_available())
            default_model = provider.default_model()
        except Exception:
            continue
        setup = setup if isinstance(setup, dict) else {}
        safe_setup = {
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
        safe_setup["env_vars"] = env_fields
        result.append({
            "provider": provider.name,
            "name": provider.display_name,
            "available": available,
            "credential_configured": available,
            "models": models,
            "default_model": default_model,
            "capabilities": capabilities if isinstance(capabilities, dict) else {},
            "setup": safe_setup,
        })
    return result


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
        raise ModelRegistrationError("kind must be chat, image, or video")
    if existing is not None and kind != existing.get("kind"):
        raise ModelRegistrationError("kind cannot be changed")

    model = _text(data.get("model"), "model")
    source = str(data.get("source") or (existing or {}).get("source") or "catalog").strip().lower()
    api_key = str(data.get("api_key") or "").strip()
    provider_config: dict[str, Any] | None = None

    if kind == "chat" and source == "custom":
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
            raise ModelRegistrationError("Only chat registrations may use a custom source")
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
        if kind in {"image", "video"}:
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
    result = {
        "id": registration_id,
        "name": item.get("name", ""),
        "kind": item.get("kind", ""),
        "provider": item.get("provider", ""),
        "model": item.get("model", ""),
        "source": item.get("source", "catalog"),
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
    for kind in ("image", "video"):
        section = config.get(f"{kind}_gen")
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
    return result


def resolve_chat_model_registration(registration_id: str) -> dict[str, str]:
    """Resolve one stable chat registration to non-secret runtime identity."""
    registration_id = str(registration_id or "").strip()
    if not _ID_RE.fullmatch(registration_id):
        raise ModelRegistrationNotFound("Model registration not found")
    with _LOCK:
        config = load_config()
        item = _registrations(config).get(registration_id)
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
        if source == "custom":
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
        registrations = dict(_registrations(config))
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
        raise ModelRegistrationError("kind must be chat, image, or video")
    providers = _chat_catalog() if normalized == "chat" else _media_catalog(normalized)
    return {"kind": normalized, "providers": providers}


def create_model_registration(data: dict[str, Any]) -> dict[str, Any]:
    registration_id = uuid.uuid4().hex
    kind = str(data.get("kind") or "").strip().lower()
    source = str(data.get("source") or "catalog").strip().lower()
    chat = _chat_catalog() if kind == "chat" and source != "custom" else None
    media = _media_catalog(kind) if kind in {"image", "video"} else None
    candidate, provider_config, api_key = _normalize_request(
        data,
        registration_id=registration_id,
        existing=None,
        chat_catalog=chat,
        media_catalog=media,
    )
    with _LOCK:
        config = load_config()
        registrations = _registrations(config)
        _assert_unique(registrations, registration_id, candidate)
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
    if not _ID_RE.match(str(registration_id or "")):
        raise ModelRegistrationNotFound("Registration not found")
    with _LOCK:
        initial = load_config()
        existing = _registrations(initial).get(registration_id)
        if not isinstance(existing, dict):
            raise ModelRegistrationNotFound("Registration not found")
        existing = dict(existing)
    kind = existing.get("kind")
    source = str(data.get("source") or existing.get("source") or "catalog").strip().lower()
    chat = _chat_catalog() if kind == "chat" and source != "custom" else None
    media = _media_catalog(str(kind)) if kind in {"image", "video"} else None
    merged = dict(existing)
    merged.update(data)
    candidate, provider_config, api_key = _normalize_request(
        merged,
        registration_id=registration_id,
        existing=existing,
        chat_catalog=chat,
        media_catalog=media,
    )
    with _LOCK:
        config = load_config()
        registrations = _registrations(config)
        current = registrations.get(registration_id)
        if not isinstance(current, dict):
            raise ModelRegistrationNotFound("Registration not found")
        if current.get("kind") != existing.get("kind"):
            raise ModelRegistrationConflict("Registration changed concurrently")
        _assert_unique(registrations, registration_id, candidate)
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
    with _LOCK:
        config = load_config()
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
        config = load_config()
        registrations = _registrations(config)
        item = registrations.get(registration_id)
        if not isinstance(item, dict):
            raise ModelRegistrationNotFound("Registration not found")
        kind = item.get("kind")
        if kind not in {"image", "video"}:
            raise ModelRegistrationError("Chat registrations must be activated through the session gateway")
        from hermes_cli.tools_config import select_media_model

        select_media_model(
            config,
            kind=kind,
            provider_name=str(item.get("provider") or ""),
            model=str(item.get("model") or ""),
            use_gateway=bool(item.get("use_gateway", False)),
        )
        save_config(config, preserve_keys={(f"{kind}_gen",)})
        return {
            "ok": True,
            "registration_id": registration_id,
            "kind": kind,
            "provider": item["provider"],
            "model": item["model"],
        }

