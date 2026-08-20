"""Owner-local payload builders shared by dashboard HTTP surfaces."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from hermes_cli.config import cfg_get, load_config
from hermes_cli.dashboard_plugins import (
    discover_dashboard_plugins,
    safe_plugin_relpath as safe_plugin_api_relpath,
)


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
            safe_asset = safe_plugin_api_relpath(asset, dashboard_dir=dashboard_dir)
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
