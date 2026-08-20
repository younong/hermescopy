from pathlib import Path
from unittest.mock import patch

from hermes_cli.dashboard_owner_payloads import active_dashboard_plugin_payload
from hermes_cli.dashboard_plugins import discover_dashboard_plugins


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


def test_discovery_validates_api_target_and_keeps_project_python_untrusted(tmp_path):
    user_root = tmp_path / "home" / "plugins"
    dashboard_dir = user_root / "worker-api" / "dashboard"
    dashboard_dir.mkdir(parents=True)
    dashboard_dir.joinpath("manifest.json").write_text(
        '{"name":"worker-api","entry":"index.js","api":"plugin_api.py",'
        '"api_target":"owner-worker"}',
        encoding="utf-8",
    )
    dashboard_dir.joinpath("index.js").write_text("// ui", encoding="utf-8")
    dashboard_dir.joinpath("plugin_api.py").write_text("router = None", encoding="utf-8")

    invalid_dir = user_root / "invalid-target" / "dashboard"
    invalid_dir.mkdir(parents=True)
    invalid_dir.joinpath("manifest.json").write_text(
        '{"name":"invalid-target","entry":"index.js","api":"plugin_api.py",'
        '"api_target":"cron"}',
        encoding="utf-8",
    )

    with patch.dict("os.environ", {"HERMES_HOME": str(tmp_path / "home")}), patch(
        "hermes_cli.plugins.get_bundled_plugins_dir", return_value=tmp_path / "bundled"
    ):
        plugins = discover_dashboard_plugins()

    by_name = {plugin["name"]: plugin for plugin in plugins}
    assert by_name["worker-api"]["api_target"] == "owner-worker"
    assert by_name["worker-api"]["has_api"] is True
    assert by_name["invalid-target"]["has_api"] is False


def test_user_control_plane_plugin_cannot_opt_into_authenticated_api(tmp_path):
    user_root = tmp_path / "home" / "plugins"
    dashboard_dir = user_root / "operator-api" / "dashboard"
    dashboard_dir.mkdir(parents=True)
    dashboard_dir.joinpath("manifest.json").write_text(
        '{"name":"operator-api","entry":"index.js","api":"plugin_api.py",'
        '"api_target":"control-plane","authenticated_api":"dashboard-session"}',
        encoding="utf-8",
    )

    with patch.dict("os.environ", {"HERMES_HOME": str(tmp_path / "home")}), patch(
        "hermes_cli.plugins.get_bundled_plugins_dir", return_value=tmp_path / "bundled"
    ):
        plugin = discover_dashboard_plugins()[0]

    assert plugin["_authenticated_control_plane_api"] is False


def test_bundled_control_plane_plugin_can_declare_authenticated_api(tmp_path):
    bundled_root = tmp_path / "bundled"
    dashboard_dir = bundled_root / "shared-api" / "dashboard"
    dashboard_dir.mkdir(parents=True)
    dashboard_dir.joinpath("manifest.json").write_text(
        '{"name":"shared-api","entry":"index.js","api":"plugin_api.py",'
        '"api_target":"control-plane","authenticated_api":"dashboard-session"}',
        encoding="utf-8",
    )

    with patch.dict("os.environ", {"HERMES_HOME": str(tmp_path / "home")}), patch(
        "hermes_cli.plugins.get_bundled_plugins_dir", return_value=bundled_root
    ):
        plugin = discover_dashboard_plugins()[0]

    assert plugin["_authenticated_control_plane_api"] is True


def test_plugin_route_registry_replaces_stale_app_routes():
    from hermes_cli.dashboard_auth.api_availability import (
        AuthenticatedApiBucket,
        classify_authenticated_api,
        clear_plugin_api_routes,
        register_plugin_api_routes,
    )

    class _Route:
        path = "/jobs/{job_id}"
        methods = {"GET"}

    clear_plugin_api_routes()
    register_plugin_api_routes(
        "first",
        api_target="owner-worker",
        routes=[_Route()],
        prefix="/api/plugins/first",
    )
    assert classify_authenticated_api(
        "/api/plugins/first/jobs/id", method="GET"
    ).bucket == AuthenticatedApiBucket.OWNER_WORKER

    clear_plugin_api_routes()
    assert classify_authenticated_api(
        "/api/plugins/first/jobs/id", method="GET"
    ).allowed is False


def test_control_plane_mount_registers_only_explicit_authenticated_api(tmp_path):
    from fastapi import FastAPI

    from hermes_cli.dashboard_auth.api_availability import (
        AuthenticatedApiBucket,
        classify_authenticated_api,
        clear_plugin_api_routes,
    )
    from hermes_cli.dashboard_plugins import mount_dashboard_plugin_apis

    dashboard_dir = tmp_path / "dashboard"
    dashboard_dir.mkdir()
    dashboard_dir.joinpath("plugin_api.py").write_text(
        "from fastapi import APIRouter\nrouter = APIRouter()\n"
        "@router.get('/status')\ndef status(): return {'ok': True}\n",
        encoding="utf-8",
    )
    plugins = [
        {
            "name": "private",
            "source": "bundled",
            "api_target": "control-plane",
            "_authenticated_control_plane_api": False,
            "_dir": str(dashboard_dir),
            "_api_file": "plugin_api.py",
        },
        {
            "name": "shared",
            "source": "bundled",
            "api_target": "control-plane",
            "_authenticated_control_plane_api": True,
            "_dir": str(dashboard_dir),
            "_api_file": "plugin_api.py",
        },
    ]

    clear_plugin_api_routes()
    with patch("hermes_cli.plugins_cmd._get_disabled_set", return_value=set()), patch(
        "hermes_cli.plugins_cmd._get_enabled_set", return_value=set()
    ):
        mount_dashboard_plugin_apis(
            FastAPI(),
            plugins,
            runtime_target="control-plane",
        )

    assert classify_authenticated_api(
        "/api/plugins/private/status", method="GET"
    ).allowed is False
    assert classify_authenticated_api(
        "/api/plugins/shared/status", method="GET"
    ).bucket == AuthenticatedApiBucket.PLUGIN_API
    clear_plugin_api_routes()


def test_owner_worker_mount_requires_capability_dependency():
    import pytest
    from fastapi import FastAPI

    from hermes_cli.dashboard_plugins import mount_dashboard_plugin_apis

    with pytest.raises(ValueError, match="capability dependency"):
        mount_dashboard_plugin_apis(
            FastAPI(),
            [],
            runtime_target="owner-worker",
        )


def test_owner_worker_plugin_websocket_routes_fail_closed():
    from hermes_cli.dashboard_auth.api_availability import (
        clear_plugin_api_routes,
        register_plugin_api_routes,
        registered_plugin_websocket_route,
    )

    class _WebSocketRoute:
        path = "/events"
        methods = None

    clear_plugin_api_routes()
    register_plugin_api_routes(
        "worker-events",
        api_target="owner-worker",
        routes=[_WebSocketRoute()],
        prefix="/api/plugins/worker-events",
    )

    assert registered_plugin_websocket_route(
        "/api/plugins/worker-events/events"
    ) is False
