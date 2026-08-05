from pathlib import Path
from unittest.mock import patch

from hermes_cli.dashboard_owner_payloads import active_dashboard_plugin_payload


def _plugin(dashboard_dir: Path, name: str, **overrides):
    plugin = {
        "name": name,
        "source": "bundled",
        "entry": "dist/index.js",
        "css": None,
        "_dir": str(dashboard_dir),
    }
    plugin.update(overrides)
    return plugin


def test_active_dashboard_plugins_require_declared_assets(tmp_path):
    dashboard_dir = tmp_path / "dashboard"
    (dashboard_dir / "dist").mkdir(parents=True)
    (dashboard_dir / "dist" / "index.js").write_text("// plugin", encoding="utf-8")
    (dashboard_dir / "dist" / "index.css").write_text("/* plugin */", encoding="utf-8")

    plugins = [
        _plugin(dashboard_dir, "entry-only"),
        _plugin(dashboard_dir, "with-css", css="dist/index.css"),
        _plugin(dashboard_dir, "missing-entry", entry="dist/missing.js"),
        _plugin(dashboard_dir, "missing-css", css="dist/missing.css"),
        _plugin(dashboard_dir, "escaping-entry", entry="../outside.js"),
    ]

    with patch("hermes_cli.dashboard_owner_payloads.load_config", return_value={}), \
         patch("hermes_cli.plugins_cmd._get_enabled_set", return_value=set()), \
         patch("hermes_cli.plugins_cmd._get_disabled_set", return_value=set()):
        payload = active_dashboard_plugin_payload(plugins)

    assert [plugin["name"] for plugin in payload] == ["entry-only", "with-css"]
    assert all("_dir" not in plugin for plugin in payload)


def test_asset_check_preserves_plugin_activation_policy(tmp_path):
    dashboard_dir = tmp_path / "dashboard"
    (dashboard_dir / "dist").mkdir(parents=True)
    (dashboard_dir / "dist" / "index.js").write_text("// plugin", encoding="utf-8")
    plugins = [
        _plugin(dashboard_dir, "bundled-active"),
        _plugin(dashboard_dir, "bundled-disabled"),
        _plugin(dashboard_dir, "bundled-hidden"),
        _plugin(dashboard_dir, "user-enabled", source="user"),
        _plugin(dashboard_dir, "user-not-enabled", source="user"),
    ]

    with patch(
        "hermes_cli.dashboard_owner_payloads.load_config",
        return_value={"dashboard": {"hidden_plugins": ["bundled-hidden"]}},
    ), patch(
        "hermes_cli.plugins_cmd._get_enabled_set", return_value={"user-enabled"}
    ), patch(
        "hermes_cli.plugins_cmd._get_disabled_set", return_value={"bundled-disabled"}
    ):
        payload = active_dashboard_plugin_payload(plugins)

    assert [plugin["name"] for plugin in payload] == ["bundled-active", "user-enabled"]
