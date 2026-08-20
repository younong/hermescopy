"""Owner-local payload builders shared by dashboard HTTP surfaces."""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path, PurePosixPath
from typing import Any

from hermes_cli.config import cfg_get, load_config
from hermes_cli.dashboard_plugins import (
    discover_dashboard_plugins,
    safe_plugin_relpath as safe_plugin_api_relpath,
)


_log = logging.getLogger(__name__)

FONT_DEFAULT_ID = "theme"
FONT_CHOICES = frozenset({
    "system-sans", "system-serif", "system-mono",
    "inter", "ibm-plex-sans", "work-sans", "atkinson-hyperlegible", "dm-sans",
    "spectral", "fraunces", "source-serif",
    "jetbrains-mono", "ibm-plex-mono", "space-mono",
})


def normalize_config_for_web(config: dict[str, Any]) -> dict[str, Any]:
    """Return the config shape consumed by the dashboard."""
    normalized = dict(config)
    model_value = normalized.get("model")
    if isinstance(model_value, dict):
        context_length = model_value.get("context_length", 0)
        normalized["model"] = model_value.get("default", model_value.get("name", ""))
        normalized["model_context_length"] = context_length if isinstance(context_length, int) else 0
    else:
        normalized["model_context_length"] = 0
    return {key: value for key, value in normalized.items() if not key.startswith("_")}


def dashboard_font_payload(config: dict[str, Any] | None = None) -> dict[str, str]:
    font = cfg_get(config if config is not None else load_config(), "dashboard", "font", default=FONT_DEFAULT_ID)
    if font not in FONT_CHOICES:
        font = FONT_DEFAULT_ID
    return {"font": font}


def toolsets_payload(config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Describe configurable CLI toolsets using owner-local config and keys."""
    from hermes_cli.tools_config import (
        _get_effective_configurable_toolsets,
        _get_platform_tools,
        _toolset_has_keys,
        gui_toolset_label,
    )
    from toolsets import resolve_toolset

    owner_config = config if config is not None else load_config()
    enabled_toolsets = _get_platform_tools(
        owner_config,
        "cli",
        include_default_mcp_servers=False,
    )
    result = []
    for name, label, desc in _get_effective_configurable_toolsets():
        try:
            tools = sorted(set(resolve_toolset(name)))
        except Exception:
            tools = []
        is_enabled = name in enabled_toolsets
        result.append({
            "name": name,
            "label": gui_toolset_label(label),
            "description": desc,
            "enabled": is_enabled,
            "available": is_enabled,
            "configured": _toolset_has_keys(name, owner_config),
            "tools": tools,
        })
    return result


def safe_dashboard_relpath(value: Any, *, dashboard_dir: Path) -> str | None:
    """Return a relative path only when it remains inside dashboard_dir."""
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = Path(value)
    if candidate.is_absolute():
        return None
    try:
        resolved = (dashboard_dir / candidate).resolve()
        base = dashboard_dir.resolve()
        resolved.relative_to(base)
    except (OSError, RuntimeError, ValueError):
        return None
    return value


# Compatibility name retained for the web-server security tests and callers.
safe_plugin_api_relpath = safe_dashboard_relpath


_WORKSPACE_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


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

    if tab_info is None and chat_info is None:
        raise ValueError("manifest must declare tab or chat.workspaces")

    slots_src = data.get("slots")
    slots = [slot for slot in slots_src if isinstance(slot, str) and slot] if isinstance(slots_src, list) else []
    raw_api = data.get("api")
    safe_api = safe_dashboard_relpath(raw_api, dashboard_dir=dashboard_dir)
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
    """Discover dashboard manifests under owner, bundled, and opted-in project roots."""
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
            if not manifest_file.exists():
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
                    _log.warning("Ignoring duplicate dashboard plugin name %r from %s", name, manifest_file)
                    continue
                seen_names.add(name)
                plugins.append(plugin)
            except Exception as exc:
                _log.warning("Bad dashboard plugin manifest %s: %s", manifest_file, exc)
                continue
    return plugins


def active_dashboard_plugin_payload(plugins: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Filter manifests by owner activation policy and dashboard asset readiness."""
    discovered = plugins if plugins is not None else discover_dashboard_plugins()
    hidden = cfg_get(load_config(), "dashboard", "hidden_plugins", default=[]) or []
    try:
        from hermes_cli.plugins_cmd import _get_disabled_set, _get_enabled_set

        enabled = _get_enabled_set()
        disabled = _get_disabled_set()
    except Exception:
        enabled = set()
        disabled = set()

    def active(plugin: dict[str, Any]) -> bool:
        name = plugin.get("name", "")
        if name in hidden or name in disabled:
            return False
        if plugin.get("source") == "user" and name not in enabled:
            return False

        try:
            dashboard_dir = Path(plugin["_dir"])
        except (KeyError, TypeError):
            return False

        def asset_exists(asset: Any) -> bool:
            safe_asset = safe_dashboard_relpath(asset, dashboard_dir=dashboard_dir)
            return safe_asset is not None and (dashboard_dir / safe_asset).is_file()

        if not asset_exists(plugin.get("entry")):
            return False
        css = plugin.get("css")
        return css is None or asset_exists(css)

    return [
        {key: value for key, value in plugin.items() if not key.startswith("_")}
        for plugin in discovered
        if active(plugin)
    ]
