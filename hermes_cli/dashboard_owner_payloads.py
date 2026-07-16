"""Owner-local payload builders shared by dashboard HTTP surfaces."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home
from hermes_cli.config import cfg_get, load_config
from utils import env_var_enabled


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


def owner_singleton_profile_payload(owner_home: Path) -> dict[str, Any]:
    """Describe only the immutable owner home, never host-level sibling profiles."""
    from hermes_cli import profiles as profiles_mod

    home = Path(owner_home).expanduser().resolve()
    if home != get_hermes_home().resolve():
        raise RuntimeError("owner profile payload home does not match HERMES_HOME")
    model, provider = profiles_mod._read_config_model(home)
    distribution_name, distribution_version, distribution_source = profiles_mod._read_distribution_meta(home)
    metadata = profiles_mod.read_profile_meta(home)
    return {
        "management_mode": "owner_singleton",
        "profiles": [{
            "name": "default",
            "path": None,
            "is_default": True,
            "model": model,
            "provider": provider,
            "has_env": (home / ".env").is_file(),
            "skill_count": profiles_mod._count_skills(home),
            "gateway_running": True,
            "description": metadata.get("description", ""),
            "description_auto": bool(metadata.get("description_auto", False)),
            "distribution_name": distribution_name,
            "distribution_version": distribution_version,
            "distribution_source": distribution_source,
            "has_alias": False,
        }],
    }


def safe_plugin_api_relpath(api_field: Any, *, dashboard_dir: Path) -> str | None:
    if not isinstance(api_field, str) or not api_field.strip():
        return None
    candidate = Path(api_field)
    if candidate.is_absolute():
        return None
    try:
        resolved = (dashboard_dir / candidate).resolve()
        base = dashboard_dir.resolve()
        resolved.relative_to(base)
    except (OSError, RuntimeError, ValueError):
        return None
    return api_field


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
                name = data.get("name", child.name)
                if not isinstance(name, str) or not name or name in seen_names:
                    continue
                seen_names.add(name)
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
                slots_src = data.get("slots")
                slots = [slot for slot in slots_src if isinstance(slot, str) and slot] if isinstance(slots_src, list) else []
                dashboard_dir = child / "dashboard"
                safe_api = safe_plugin_api_relpath(data.get("api"), dashboard_dir=dashboard_dir)
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
                    "source": source,
                    "_dir": str(dashboard_dir),
                    "_api_file": safe_api,
                })
            except Exception:
                continue
    return plugins


def active_dashboard_plugin_payload(plugins: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Filter manifests using the current owner config and plugin enablement."""
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
        return plugin.get("source") != "user" or name in enabled

    return [
        {key: value for key, value in plugin.items() if not key.startswith("_")}
        for plugin in discovered
        if active(plugin)
    ]
