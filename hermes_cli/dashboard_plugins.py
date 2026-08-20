"""Validated dashboard plugin discovery and backend route mounting."""
from __future__ import annotations

import importlib.util
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any, Callable, Iterable

from hermes_constants import get_hermes_home
from utils import env_var_enabled


_log = logging.getLogger(__name__)
_API_TARGETS = frozenset({"control-plane", "owner-worker"})
_PLUGIN_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


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


def discover_dashboard_plugins() -> list[dict[str, Any]]:
    """Discover and validate dashboard manifests in precedence order."""
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
                name = data.get("name", child.name)
                if (
                    not isinstance(name, str)
                    or not _PLUGIN_NAME_RE.fullmatch(name)
                    or name in seen_names
                ):
                    continue
                seen_names.add(name)
                dashboard_dir = child / "dashboard"
                raw_api = data.get("api")
                safe_api = safe_plugin_relpath(raw_api, dashboard_dir=dashboard_dir)
                if raw_api and safe_api is None:
                    _log.warning(
                        "Plugin %s: refusing unsafe api path %r; backend routes will not be mounted",
                        name,
                        raw_api,
                    )
                raw_target = data.get("api_target", "control-plane")
                api_target = raw_target if raw_target in _API_TARGETS else None
                authenticated_api = data.get("authenticated_api")
                authenticated_control_plane_api = (
                    source == "bundled"
                    and authenticated_api == "dashboard-session"
                )
                if safe_api and api_target is None:
                    _log.warning(
                        "Plugin %s: refusing invalid api_target %r (expected control-plane or owner-worker)",
                        name,
                        raw_target,
                    )
                    safe_api = None

                raw_tab = data.get("tab", {}) if isinstance(data.get("tab"), dict) else {}
                tab_info = {
                    "path": raw_tab.get("path", f"/{name}"),
                    "position": raw_tab.get("position", "end"),
                }
                override_path = raw_tab.get("override")
                if isinstance(override_path, str) and override_path.startswith("/"):
                    tab_info["override"] = override_path
                if bool(raw_tab.get("hidden")):
                    tab_info["hidden"] = True
                raw_slots = data.get("slots")
                slots = (
                    [slot for slot in raw_slots if isinstance(slot, str) and slot]
                    if isinstance(raw_slots, list)
                    else []
                )
                plugins.append({
                    "name": name,
                    "label": data.get("label", name),
                    "description": data.get("description", ""),
                    "icon": data.get("icon", "Puzzle"),
                    "version": data.get("version", "0.0.0"),
                    "tab": tab_info,
                    "slots": slots,
                    "entry": data.get("entry", "dist/index.js"),
                    "css": data.get("css"),
                    "has_api": bool(safe_api),
                    "api_target": api_target or "control-plane",
                    "source": source,
                    "_authenticated_control_plane_api": authenticated_control_plane_api,
                    "_dir": str(dashboard_dir),
                    "_api_file": safe_api,
                })
            except Exception as exc:
                _log.warning("Bad dashboard plugin manifest %s: %s", manifest_file, exc)
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
