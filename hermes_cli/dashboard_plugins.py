"""Validated dashboard plugin discovery and backend route mounting."""
from __future__ import annotations

import importlib.util
import json
import logging
import re
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable

from hermes_constants import get_hermes_home
from utils import env_var_enabled


_log = logging.getLogger(__name__)
_API_TARGETS = frozenset({"control-plane", "owner-worker"})
_PLUGIN_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_WORKSPACE_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def safe_plugin_relpath(value: Any, *, dashboard_dir: Path) -> str | None:
    """Return a relative manifest path only when it stays under dashboard_dir."""
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = Path(value)
    if candidate.is_absolute():
        return None
    try:
        resolved = (dashboard_dir / candidate).resolve()
        resolved.relative_to(dashboard_dir.resolve())
    except (OSError, RuntimeError, ValueError):
        return None
    return value


def _nonempty_string(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _optional_string(value: Any, *, field: str, default: str) -> str:
    if value is None:
        return default
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    return value.strip()


def _normalized_route(value: Any, *, field: str, prefix: str = "/") -> str:
    path = _nonempty_string(value, field=field)
    normalized_path = PurePosixPath(path)
    parts = normalized_path.parts
    if (
        not path.startswith(prefix)
        or (path == prefix and prefix != "/")
        or (path.endswith("/") and path != "/")
        or normalized_path.as_posix() != path
        or "?" in path
        or "#" in path
        or "." in parts
        or ".." in parts
    ):
        scope = f"under {prefix.rstrip('/')}" if prefix != "/" else "an absolute normalized route"
        raise ValueError(f"{field} must be {scope}")
    return path


def _normalize_tab(raw: Any, *, name: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("tab must be an object")

    position = _nonempty_string(raw.get("position", "end"), field="tab.position")
    if position != "end":
        relation, separator, target = position.partition(":")
        if (
            separator != ":"
            or relation not in {"before", "after"}
            or not _WORKSPACE_ID_PATTERN.fullmatch(target)
        ):
            raise ValueError("tab.position must be end, before:<id>, or after:<id>")

    tab = {
        "path": _normalized_route(raw.get("path", f"/{name}"), field="tab.path"),
        "position": position,
    }
    override = raw.get("override")
    if override is not None:
        tab["override"] = _normalized_route(override, field="tab.override")
    hidden = raw.get("hidden")
    if hidden is not None:
        if not isinstance(hidden, bool):
            raise ValueError("tab.hidden must be a boolean")
        if hidden:
            tab["hidden"] = True
    return tab


def _normalize_chat_workspace(raw: Any, *, index: int) -> dict[str, Any]:
    field = f"chat.workspaces[{index}]"
    if not isinstance(raw, dict):
        raise ValueError(f"{field} must be an object")

    workspace_id = _nonempty_string(raw.get("id"), field=f"{field}.id")
    if not _WORKSPACE_ID_PATTERN.fullmatch(workspace_id):
        raise ValueError(f"{field}.id must contain lowercase letters, digits, or hyphens")

    path = _normalized_route(raw.get("path"), field=f"{field}.path", prefix="/chat/")

    position = raw.get("position", "end")
    if isinstance(position, bool) or not isinstance(position, (str, int)):
        raise ValueError(f"{field}.position must be end, before:<id>, after:<id>, or a non-negative integer")
    if isinstance(position, int):
        if position < 0:
            raise ValueError(f"{field}.position integer must be non-negative")
    else:
        position = _nonempty_string(position, field=f"{field}.position")
        if position != "end":
            relation, separator, target = position.partition(":")
            if (
                separator != ":"
                or relation not in {"before", "after"}
                or not _WORKSPACE_ID_PATTERN.fullmatch(target)
                or target == workspace_id
            ):
                raise ValueError(
                    f"{field}.position must be end, before:<id>, after:<id>, or a non-negative integer"
                )

    admin_only = raw.get("admin_only", False)
    if not isinstance(admin_only, bool):
        raise ValueError(f"{field}.admin_only must be a boolean")

    return {
        "id": workspace_id,
        "path": path,
        "label": _optional_string(raw.get("label"), field=f"{field}.label", default=workspace_id),
        "description": _optional_string(raw.get("description"), field=f"{field}.description", default=""),
        "icon": _optional_string(raw.get("icon"), field=f"{field}.icon", default="Puzzle"),
        "position": position,
        "admin_only": admin_only,
    }


def normalize_dashboard_plugin_manifest(
    data: Any,
    *,
    default_name: str,
    dashboard_dir: Path,
    source: str,
) -> dict[str, Any]:
    """Validate and normalize one dashboard plugin manifest."""
    if not isinstance(data, dict):
        raise ValueError("manifest must be an object")

    name = _nonempty_string(data.get("name", default_name), field="name")
    if not _PLUGIN_NAME_RE.fullmatch(name):
        raise ValueError(
            "name must start with a letter or digit and contain only letters, digits, dots, underscores, or hyphens"
        )

    raw_tab = data.get("tab")
    tab_info = _normalize_tab(raw_tab, name=name) if raw_tab is not None else None

    raw_chat = data.get("chat")
    chat_info: dict[str, Any] | None = None
    if raw_chat is not None:
        if not isinstance(raw_chat, dict):
            raise ValueError("chat must be an object")
        raw_workspaces = raw_chat.get("workspaces")
        if not isinstance(raw_workspaces, list) or not raw_workspaces:
            raise ValueError("chat.workspaces must be a non-empty list")
        workspaces = [
            _normalize_chat_workspace(workspace, index=index)
            for index, workspace in enumerate(raw_workspaces)
        ]
        ids = [workspace["id"] for workspace in workspaces]
        paths = [workspace["path"] for workspace in workspaces]
        if len(ids) != len(set(ids)):
            raise ValueError("chat.workspaces contains duplicate ids")
        if len(paths) != len(set(paths)):
            raise ValueError("chat.workspaces contains duplicate paths")
        chat_info = {"workspaces": workspaces}

    raw_api = data.get("api")
    has_api = bool(raw_api) and isinstance(raw_api, str)
    # Reject bare manifests (no tab, no chat.workspaces, no api). Legacy
    # api-only plugins (e.g. #259's owner-worker / control-plane API plugins)
    # declare just ``api`` + ``api_target`` and must still be discovered.
    if tab_info is None and chat_info is None and not has_api:
        raise ValueError("manifest must declare tab or chat.workspaces")

    slots_src = data.get("slots")
    slots = [slot for slot in slots_src if isinstance(slot, str) and slot] if isinstance(slots_src, list) else []
    raw_api = data.get("api")
    safe_api = safe_plugin_relpath(raw_api, dashboard_dir=dashboard_dir)
    if raw_api and safe_api is None:
        _log.warning(
            "Plugin %s: refusing unsafe api path %r (must be a relative file "
            "inside the plugin's dashboard directory)",
            name,
            raw_api,
        )
    plugin = {
        "name": name,
        "label": data.get("label", name),
        "description": data.get("description", ""),
        "icon": data.get("icon", "Puzzle"),
        "version": data.get("version", "0.0.0"),
        "slots": slots,
        "entry": data.get("entry", "dist/index.js"),
        "css": data.get("css"),
        "has_api": bool(safe_api),
        "source": source,
        "_dir": str(dashboard_dir),
        "_api_file": safe_api,
    }
    if tab_info is not None:
        plugin["tab"] = tab_info
    if chat_info is not None:
        plugin["chat"] = chat_info
    return plugin


def discover_dashboard_plugins() -> list[dict[str, Any]]:
    """Discover and validate dashboard manifests in precedence order.

    Each manifest is normalized via :func:`normalize_dashboard_plugin_manifest`,
    which preserves both the legacy ``tab`` field (dashboard plugin tabs) and
    the ``chat.workspaces`` array introduced for chat-workspace plugins
    (PR #261). ``api_target`` and the authenticated-control-plane policy flag
    are layered on after normalization so both fields reach the dashboard.
    """
    from hermes_cli.plugins import get_bundled_plugins_dir

    bundled_root = get_bundled_plugins_dir()
    search_dirs = [
        (get_hermes_home() / "plugins", "user"),
        (bundled_root / "memory", "bundled"),
        (bundled_root, "bundled"),
    ]
    if env_var_enabled("HERMES_ENABLE_PROJECT_PLUGINS"):
        search_dirs.append((Path.cwd() / ".hermes" / "plugins", "project"))

    plugins: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for plugins_root, source in search_dirs:
        if not plugins_root.is_dir():
            continue
        for child in sorted(plugins_root.iterdir()):
            if not child.is_dir():
                continue
            manifest_file = child / "dashboard" / "manifest.json"
            if not manifest_file.is_file():
                continue
            try:
                data = json.loads(manifest_file.read_text(encoding="utf-8"))
                plugin = normalize_dashboard_plugin_manifest(
                    data,
                    default_name=child.name,
                    dashboard_dir=child / "dashboard",
                    source=source,
                )
                name = plugin["name"]
                if name in seen_names:
                    _log.warning(
                        "Ignoring duplicate dashboard plugin name %r from %s",
                        name,
                        manifest_file,
                    )
                    continue
                seen_names.add(name)

                raw_target = data.get("api_target", "control-plane")
                api_target = raw_target if raw_target in _API_TARGETS else None
                if api_target is None and plugin.get("has_api"):
                    _log.warning(
                        "Plugin %s: refusing invalid api_target %r (expected control-plane or owner-worker)",
                        plugin["name"],
                        raw_target,
                    )
                    plugin["_api_file"] = None
                    plugin["has_api"] = False
                authenticated_api = data.get("authenticated_api")
                plugin["api_target"] = api_target or "control-plane"
                plugin["_authenticated_control_plane_api"] = (
                    source == "bundled" and authenticated_api == "dashboard-session"
                )
                plugins.append(plugin)
            except Exception as exc:
                _log.warning("Bad dashboard plugin manifest %s: %s", manifest_file, exc)
                continue
    return plugins


def plugin_api_enabled(
    plugin: dict[str, Any],
    *,
    allow_disabled_bundled: bool = False,
) -> bool:
    """Apply the existing trusted-source and activation policy for Python APIs."""
    name = str(plugin.get("name") or "")
    source = plugin.get("source")
    if source == "project":
        return False
    if source not in {"bundled", "user"}:
        return False
    try:
        from hermes_cli.plugins_cmd import _get_disabled_set, _get_enabled_set

        disabled = _get_disabled_set()
        enabled = _get_enabled_set()
    except Exception:
        disabled = set()
        enabled = set()
    if name in disabled and not (source == "bundled" and allow_disabled_bundled):
        return False
    return source == "bundled" or name in enabled


def _load_plugin_router(plugin: dict[str, Any], *, runtime_target: str):
    api_file = plugin.get("_api_file")
    if not api_file or not plugin_api_enabled(
        plugin,
        allow_disabled_bundled=runtime_target == "owner-worker",
    ):
        return None
    dashboard_dir = Path(plugin["_dir"])
    safe_api = safe_plugin_relpath(api_file, dashboard_dir=dashboard_dir)
    if safe_api is None:
        _log.warning("Plugin %s: refusing API path outside dashboard directory", plugin.get("name"))
        return None
    api_path = (dashboard_dir / safe_api).resolve()
    if not api_path.is_file():
        _log.warning("Plugin %s declares api=%s but file was not found", plugin.get("name"), api_file)
        return None

    module_name = f"hermes_dashboard_plugin_{plugin['name'].replace('-', '_')}_{runtime_target.replace('-', '_')}"
    spec = importlib.util.spec_from_file_location(module_name, api_path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return getattr(module, "router", None)


def mount_dashboard_plugin_apis(
    app: Any,
    plugins: Iterable[dict[str, Any]],
    *,
    runtime_target: str,
    owner_dependency: Callable[..., Any] | None = None,
) -> None:
    """Mount trusted plugin routers and register their authenticated route policy.

    The Control Plane mounts both targets so local dashboards retain their direct,
    multi-profile behavior. In authenticated mode, the exact owner-worker-targeted
    methods are intercepted and proxied before these local handlers can execute.
    Owner Workers mount only routers explicitly targeted at them.
    """
    if runtime_target not in _API_TARGETS:
        raise ValueError(f"invalid dashboard plugin runtime target: {runtime_target}")
    if runtime_target == "owner-worker" and owner_dependency is None:
        raise ValueError("owner-worker plugin APIs require a capability dependency")

    from fastapi import Depends
    from hermes_cli.dashboard_auth.api_availability import (
        clear_plugin_api_routes,
        register_plugin_api_routes,
    )

    if runtime_target == "control-plane":
        clear_plugin_api_routes()
    for plugin in plugins:
        api_target = plugin.get("api_target", "control-plane")
        if not plugin.get("_api_file"):
            continue
        if runtime_target == "owner-worker" and api_target != "owner-worker":
            continue
        try:
            router = _load_plugin_router(plugin, runtime_target=runtime_target)
            if router is None:
                _log.warning("Plugin %s API file has no 'router' attribute", plugin.get("name"))
                continue
            prefix = f"/api/plugins/{plugin['name']}"
            dependencies = [Depends(owner_dependency)] if owner_dependency is not None else None
            app.include_router(router, prefix=prefix, dependencies=dependencies)
            if runtime_target == "control-plane" and (
                api_target == "owner-worker"
                or plugin.get("_authenticated_control_plane_api") is True
            ):
                register_plugin_api_routes(
                    plugin["name"],
                    api_target=api_target,
                    routes=router.routes,
                    prefix=prefix,
                )
            _log.info("Mounted %s dashboard plugin API routes: %s/", api_target, prefix)
        except Exception as exc:
            _log.warning("Failed to load plugin %s API routes: %s", plugin.get("name"), exc)
