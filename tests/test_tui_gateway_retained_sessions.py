"""Owner Worker JSON-RPC session source fencing."""

from __future__ import annotations

import threading

import pytest

from hermes_cli.authenticated_file_context import AuthenticatedWorkspaceContext
from hermes_cli.controlled_roots import controlled_roots_for
from hermes_cli.owner_runtime import ensure_owner_runtime_dirs, owner_worker_runtime_paths
from hermes_state import SessionDB
from tui_gateway import server


@pytest.fixture()
def owner_gateway(monkeypatch, tmp_path):
    import hermes_cli.controlled_roots as controlled_roots

    monkeypatch.setattr(controlled_roots.sys, "platform", "linux")
    monkeypatch.setattr(controlled_roots, "_openat2", lambda *_args: None)
    owner_home = tmp_path / "owner"
    workspace_root = owner_home / "workspaces"
    workspace_root.mkdir(parents=True)
    paths = owner_worker_runtime_paths(
        owner_home=ensure_owner_runtime_dirs(owner_home),
        worker_generation=2,
    )
    paths.default_workspace.mkdir(parents=True, exist_ok=True)
    (paths.default_workspace / "employees" / "analyst").mkdir(parents=True)
    roots = controlled_roots_for(paths)
    db = SessionDB(db_path=owner_home / "state.db")
    runtime = server.OwnerWorkerGatewayRuntime(
        "owner-a",
        2,
        "worker-a",
        1,
        0,
        filesystem_context=AuthenticatedWorkspaceContext(roots),
    )
    env = {
        "HERMES_HOME": str(owner_home),
        "HERMES_OWNER_KEY": "owner-a",
        "HERMES_WORKSPACE_ROOT": str(workspace_root),
        "HERMES_WORKER_GENERATION": "2",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(server, "_db", db)
    monkeypatch.setattr(server, "_db_error", None)
    monkeypatch.setattr(server, "_schedule_agent_build", lambda _sid: None)
    monkeypatch.setattr(server, "_schedule_session_cap_enforcement", lambda: None)
    monkeypatch.setattr(server, "_claim_active_session_slot", lambda *_args, **_kwargs: (None, None))
    monkeypatch.setattr(server, "_enable_gateway_prompts", lambda: None)
    monkeypatch.setattr(server, "_load_show_reasoning", lambda: False)
    monkeypatch.setattr(server, "_load_tool_progress_mode", lambda: "compact")
    monkeypatch.setattr(server, "_resolve_model", lambda: "test-model")
    monkeypatch.setattr(server, "_required_gateway_transport", lambda: object())

    yield db, runtime, str(workspace_root)

    db.close()
    roots.close()


def test_tui_default_toolsets_use_coding_posture(monkeypatch):
    monkeypatch.delenv("HERMES_TUI_TOOLSETS", raising=False)
    monkeypatch.setattr(
        "agent.coding_context.coding_selection",
        lambda **_kwargs: ["coding", "mcp-github"],
    )

    assert server._load_enabled_toolsets() == ["coding", "mcp-github", "project"]


def test_tui_explicit_toolset_pin_precedes_coding_posture(monkeypatch):
    monkeypatch.setenv("HERMES_TUI_TOOLSETS", "terminal")
    monkeypatch.setattr(
        "agent.coding_context.coding_selection",
        lambda **_kwargs: pytest.fail("coding posture must not override an explicit pin"),
    )

    assert server._load_enabled_toolsets() == ["terminal"]


def test_tui_coding_posture_preserves_disabled_toolsets(monkeypatch):
    cfg = {"agent": {"disabled_toolsets": ["browser", "memory"]}}
    monkeypatch.setattr(server, "_load_cfg", lambda: cfg)

    assert server._load_disabled_toolsets() == ["browser", "memory"]

    class ExistingAgent:
        enabled_toolsets = ["coding"]
        disabled_toolsets = ["browser", "memory"]
        model = "test-model"

    kwargs = server._background_agent_kwargs(ExistingAgent(), "task-a")
    assert kwargs["enabled_toolsets"] == ["coding"]
    assert kwargs["disabled_toolsets"] == ["browser", "memory"]

    class FreshAgent:
        enabled_toolsets = ["coding"]
        disabled_toolsets = None
        model = "test-model"

    fresh_kwargs = server._background_agent_kwargs(FreshAgent(), "task-b")
    assert fresh_kwargs["disabled_toolsets"] == ["browser", "memory"]

    from model_tools import get_tool_definitions

    tools = get_tool_definitions(
        enabled_toolsets=kwargs["enabled_toolsets"],
        disabled_toolsets=kwargs["disabled_toolsets"],
        quiet_mode=True,
        skip_tool_search_assembly=True,
    )
    names = {tool["function"]["name"] for tool in tools}
    assert "browser_navigate" not in names
    assert "memory" not in names
    assert "patch" in names


def _create_owned(
    db: SessionDB,
    workspace_root: str,
    session_id: str,
    *,
    source: str,
    generation: int = 1,
) -> None:
    db.create_session(
        session_id,
        source,
        owner_key="owner-a",
        workspace_root=workspace_root,
        worker_generation=generation,
    )


class _Transport:
    def __init__(self, purpose: str):
        self.connection_purpose = purpose

    def write(self, _obj):
        return True

    def close(self):
        return None


def _call(runtime: server.OwnerWorkerGatewayRuntime, method: str, params: dict | None = None):
    with server.owner_worker_gateway_runtime(runtime):
        return server.handle_request({"id": "request", "method": method, "params": params or {}})


def _dispatch(runtime: server.OwnerWorkerGatewayRuntime, method: str, params: dict, *, purpose: str):
    return server.dispatch(
        {"id": "request", "method": method, "params": params},
        transport=_Transport(purpose),
        runtime=runtime,
    )


def test_owner_worker_create_rejects_legacy_source_before_live_registration(owner_gateway):
    _db, runtime, _workspace_root = owner_gateway

    response = _call(runtime, "session.create", {"source": "tui"})

    assert response["error"] == {"code": 4002, "message": "session source is not available"}
    assert runtime.mutable_state.sessions == {}


def test_employee_policy_rejects_interactive_source_spoof(owner_gateway):
    _db, runtime, _workspace_root = owner_gateway

    response = _dispatch(
        runtime,
        "session.create",
        {"source": "feishu", "employee_policy": {}},
        purpose="interactive",
    )

    assert response["error"] == {
        "code": 4002,
        "message": "employee policy requires a retained channel connection",
    }
    assert runtime.mutable_state.sessions == {}


def test_employee_policy_accepts_only_retained_channel_and_rejects_runtime_overrides(
    owner_gateway, monkeypatch
):
    _db, runtime, _workspace_root = owner_gateway
    source_policy = {
        "schema_version": 1,
        "model_registration_id": "registration-a",
        "system_prompt": "You are an analyst.",
        "toolsets": [],
        "skills": [],
        "mcp_servers": [],
        "workspace_relative_path": "employees/analyst",
        "knowledge_relative_paths": [],
        "max_iterations": 20,
        "max_tokens": 2000,
    }
    monkeypatch.setattr(
        "hermes_cli.model_registrations.resolve_chat_model_registration",
        lambda _registration_id: {
            "registration_id": "registration-a",
            "provider": "openai",
            "model": "gpt-test",
            "source": "catalog",
        },
    )

    response = _dispatch(
        runtime,
        "session.create",
        {
            "source": "feishu",
            "employee_policy": {
                "account_id": "ca_employee",
                "profile_revision": 3,
                "profile_fingerprint": "sha256:" + "a" * 64,
                "source_policy": source_policy,
            },
            "model": "caller-model",
        },
        purpose="retained-channel",
    )

    assert response["error"] == {
        "code": 4002,
        "message": "employee policy cannot be combined with runtime overrides",
    }


def test_owner_worker_list_and_most_recent_hide_legacy_rows(owner_gateway):
    db, runtime, workspace_root = owner_gateway
    _create_owned(db, workspace_root, "legacy", source="cli")
    _create_owned(db, workspace_root, "retained", source="dashboard-gui", generation=2)

    listed = _call(runtime, "session.list")
    recent = _call(runtime, "session.most_recent")

    assert [row["id"] for row in listed["result"]["sessions"]] == ["retained"]
    assert recent["result"]["session_id"] == "retained"
    assert db.get_session("legacy") is not None


def test_owner_worker_live_routes_hide_legacy_records(owner_gateway):
    _db, runtime, _workspace_root = owner_gateway
    runtime.mutable_state.sessions.update(
        {
            "legacy-live": {
                "created_at": 1.0,
                "history": [],
                "session_key": "legacy",
                "source": "tui",
            },
            "retained-live": {
                "created_at": 2.0,
                "history": [],
                "session_key": "retained",
                "source": "dashboard-gui",
            },
        }
    )

    listed = _call(runtime, "session.active_list")
    hidden = _call(runtime, "session.activate", {"session_id": "legacy-live"})

    assert [row["id"] for row in listed["result"]["sessions"]] == ["retained-live"]
    assert hidden["error"] == {"code": 4001, "message": "session not found"}


def test_owner_worker_resume_cannot_override_legacy_stored_source(owner_gateway):
    db, runtime, workspace_root = owner_gateway
    _create_owned(db, workspace_root, "legacy", source="cli")
    db.append_message("legacy", "user", "private legacy content")

    response = _call(
        runtime,
        "session.resume",
        {"session_id": "legacy", "source": "dashboard-gui"},
    )

    assert response["error"] == {"code": 4007, "message": "session not found"}
    assert db.get_session("legacy") is not None


def test_owner_worker_resume_preserves_retained_stored_source(owner_gateway, monkeypatch):
    db, runtime, workspace_root = owner_gateway
    _create_owned(db, workspace_root, "retained", source="feishu")
    db.append_message("retained", "user", "hello")
    monkeypatch.setattr(server, "_reopen_resume_session", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        server,
        "_resume_history",
        lambda *_args, **_kwargs: [{"role": "user", "content": "hello"}],
    )
    monkeypatch.setattr(
        server,
        "_stored_session_runtime_overrides",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(server, "_notify_session_boundary", lambda *_args, **_kwargs: None)

    response = _call(
        runtime,
        "session.resume",
        {"session_id": "retained", "source": "dashboard-gui"},
    )

    assert "error" not in response
    live_id = response["result"]["session_id"]
    assert runtime.mutable_state.sessions[live_id]["source"] == "feishu"
