import json
from pathlib import Path
from unittest.mock import patch

import pytest

from hermes_cli.dashboard_owner_payloads import (
    active_dashboard_plugin_payload,
    discover_dashboard_plugins,
    normalize_dashboard_plugin_manifest,
)


def _write_manifest(root: Path, directory: str, manifest: dict) -> Path:
    dashboard_dir = root / directory / "dashboard"
    dashboard_dir.mkdir(parents=True)
    (dashboard_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return dashboard_dir


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


def test_manifest_normalizer_preserves_legacy_tab_defaults(tmp_path):
    plugin = normalize_dashboard_plugin_manifest(
        {"name": "legacy", "tab": {}},
        default_name="fallback",
        dashboard_dir=tmp_path / "dashboard",
        source="bundled",
    )

    assert plugin["tab"] == {"path": "/legacy", "position": "end"}
    assert "chat" not in plugin


@pytest.mark.parametrize(
    ("tab", "message"),
    [
        ({"path": "settings"}, "absolute normalized route"),
        ({"path": "/settings/"}, "absolute normalized route"),
        ({"path": "/settings", "position": 1}, "non-empty string"),
        ({"path": "/settings", "position": "start"}, "tab.position"),
        ({"path": "/settings", "override": "settings"}, "absolute normalized route"),
        ({"path": "/settings", "hidden": "yes"}, "boolean"),
    ],
)
def test_manifest_normalizer_rejects_malformed_tabs(tmp_path, tab, message):
    with pytest.raises(ValueError, match=message):
        normalize_dashboard_plugin_manifest(
            {"name": "bad", "tab": tab},
            default_name="bad",
            dashboard_dir=tmp_path / "dashboard",
            source="user",
        )


def test_manifest_normalizer_accepts_chat_workspaces_without_dashboard_tab(tmp_path):
    dashboard_dir = tmp_path / "dashboard"
    plugin = normalize_dashboard_plugin_manifest(
        {
            "name": "chat-tools",
            "label": "Chat tools",
            "entry": "dist/index.js",
            "chat": {
                "workspaces": [
                    {
                        "id": "kanban",
                        "path": "/chat/kanban",
                        "label": "Board",
                        "description": "Plan collaborative work",
                        "icon": "SquareKanban",
                        "position": 0,
                    },
                    {
                        "id": "statistics",
                        "path": "/chat/statistics",
                        "position": "after:kanban",
                    },
                ],
            },
        },
        default_name="fallback",
        dashboard_dir=dashboard_dir,
        source="bundled",
    )

    assert "tab" not in plugin
    assert plugin["chat"] == {
        "workspaces": [
            {
                "id": "kanban",
                "path": "/chat/kanban",
                "label": "Board",
                "description": "Plan collaborative work",
                "icon": "SquareKanban",
                "position": 0,
                "admin_only": False,
            },
            {
                "id": "statistics",
                "path": "/chat/statistics",
                "label": "statistics",
                "description": "",
                "icon": "Puzzle",
                "position": "after:kanban",
                "admin_only": False,
            },
        ],
    }


@pytest.mark.parametrize(
    ("workspaces", "message"),
    [
        ([], "non-empty list"),
        ([{"id": "Bad Id", "path": "/chat/bad"}], "lowercase letters"),
        ([{"id": "bad-path", "path": "/settings"}], "under /chat"),
        ([{"id": "bad-position", "path": "/chat/bad-position", "position": "start"}], "position must be"),
        ([{"id": "bad-position", "path": "/chat/bad-position", "position": -1}], "non-negative"),
        ([{"id": "self", "path": "/chat/self", "position": "after:self"}], "position must be"),
        (
            [
                {"id": "same", "path": "/chat/one"},
                {"id": "same", "path": "/chat/two"},
            ],
            "duplicate ids",
        ),
        (
            [
                {"id": "one", "path": "/chat/same"},
                {"id": "two", "path": "/chat/same"},
            ],
            "duplicate paths",
        ),
    ],
)
def test_manifest_normalizer_rejects_malformed_or_duplicate_workspaces(
    tmp_path, workspaces, message
):
    with pytest.raises(ValueError, match=message):
        normalize_dashboard_plugin_manifest(
            {"name": "bad", "chat": {"workspaces": workspaces}},
            default_name="bad",
            dashboard_dir=tmp_path / "dashboard",
            source="user",
        )


def test_owner_discovery_rejects_entire_malformed_manifest_deterministically(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_ENABLE_PROJECT_PLUGINS", raising=False)
    _write_manifest(
        tmp_path / "plugins",
        "a-invalid",
        {
            "name": "a-invalid",
            "entry": "dist/index.js",
            "chat": {
                "workspaces": [
                    {"id": "duplicate", "path": "/chat/one"},
                    {"id": "duplicate", "path": "/chat/two"},
                ]
            },
        },
    )
    valid_dir = _write_manifest(
        tmp_path / "plugins",
        "b-valid",
        {
            "name": "b-valid",
            "entry": "dist/index.js",
            "chat": {"workspaces": [{"id": "board", "path": "/chat/board"}]},
        },
    )

    with patch("hermes_cli.plugins.get_bundled_plugins_dir", return_value=tmp_path / "bundled"):
        plugins = discover_dashboard_plugins()

    assert [plugin["name"] for plugin in plugins] == ["b-valid"]
    assert plugins[0]["_dir"] == str(valid_dir)
    assert "tab" not in plugins[0]


def test_manifest_without_tab_or_chat_workspace_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="must declare tab or chat.workspaces"):
        normalize_dashboard_plugin_manifest(
            {"name": "asset-only"},
            default_name="asset-only",
            dashboard_dir=tmp_path / "dashboard",
            source="user",
        )


def test_discovery_keeps_first_plugin_name_by_sorted_source_precedence(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_ENABLE_PROJECT_PLUGINS", raising=False)
    first_dir = _write_manifest(
        tmp_path / "plugins",
        "a-first",
        {"name": "duplicate", "tab": {"path": "/first"}, "entry": "dist/index.js"},
    )
    _write_manifest(
        tmp_path / "plugins",
        "z-second",
        {"name": "duplicate", "tab": {"path": "/second"}, "entry": "dist/index.js"},
    )

    with patch("hermes_cli.plugins.get_bundled_plugins_dir", return_value=tmp_path / "bundled"):
        plugins = discover_dashboard_plugins()

    duplicate = next(plugin for plugin in plugins if plugin["name"] == "duplicate")
    assert duplicate["tab"]["path"] == "/first"
    assert duplicate["_dir"] == str(first_dir)


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
