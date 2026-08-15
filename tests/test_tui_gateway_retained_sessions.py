"""Owner Worker JSON-RPC session source fencing."""

from __future__ import annotations

import asyncio
import base64
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import threading

import pytest

from hermes_cli.authenticated_file_context import AuthenticatedWorkspaceContext
from hermes_cli.controlled_roots import controlled_roots_for
from hermes_cli.owner_runtime import ensure_owner_runtime_dirs, owner_worker_runtime_paths
from hermes_session_queries import DB_PERSISTED_MARKER
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
    (paths.default_workspace / "employees" / "emp_legacy").mkdir()
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


class _CollaborationTransport(_Transport):
    def __init__(
        self,
        mutation_error: str | None = None,
        *,
        purpose: str = "interactive",
    ):
        super().__init__(purpose)
        self.mutation_error = mutation_error
        self.frames = []
        self.attached = True

    def dashboard_owner_mutation_error(self):
        return self.mutation_error if self.attached else "dashboard mutation requires an active session"

    def write(self, obj):
        self.frames.append(obj)
        return True


def _call(
    runtime: server.OwnerWorkerGatewayRuntime,
    method: str,
    params: dict | None = None,
    *,
    transport=None,
):
    request = {"id": "request", "method": method, "params": params or {}}
    if transport is not None:
        return server.dispatch(request, transport=transport, runtime=runtime)
    with server.owner_worker_gateway_runtime(runtime):
        return server.handle_request(request)


def _dispatch(runtime: server.OwnerWorkerGatewayRuntime, method: str, params: dict, *, purpose: str):
    return server.dispatch(
        {"id": "request", "method": method, "params": params},
        transport=_Transport(purpose),
        runtime=runtime,
    )


def test_authenticated_image_attach_materializes_durable_upload(owner_gateway, monkeypatch):
    from hermes_cli.controlled_roots import RootKind

    _db, runtime, workspace_root = owner_gateway
    source = Path(workspace_root) / "default" / "incoming.png"
    payload = b"\x89PNG\r\n\x1a\nvalid-test-image"
    runtime.filesystem_context.roots.replace_bytes(
        RootKind.WORKSPACE, "default/incoming.png", payload
    )
    session = {"attached_images": [], "pending_attachments": []}
    monkeypatch.setenv("TERMINAL_CWD", str(Path(workspace_root) / "default"))
    monkeypatch.setattr(server, "_sess", lambda _params, _rid: (session, None))

    with server.owner_worker_gateway_runtime(runtime):
        response = server._methods["image.attach"](
            "request", {"path": str(source)}
        )

    assert "error" not in response
    assert response["result"]["path"] == "uploads/incoming.png"
    assert session["attached_images"] == [
        str(Path(workspace_root) / "default" / "uploads" / "incoming.png")
    ]
    assert Path(session["attached_images"][0]).read_bytes() == payload
    assert session["pending_attachments"][0]["path"] == session["attached_images"][0]


def test_authenticated_image_hint_uses_tool_visible_workspace_path(
    owner_gateway, monkeypatch
):
    _db, runtime, workspace_root = owner_gateway
    image = Path(workspace_root) / "default" / "uploads" / "attached.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"\x89PNG\r\n\x1a\nvalid-test-image")
    monkeypatch.setattr(
        "tools.vision_tools.vision_analyze_tool",
        lambda **_kwargs: asyncio.sleep(0, result='{"success":false}'),
    )

    with server.owner_worker_gateway_runtime(runtime):
        enriched = server._enrich_with_attached_images("describe it", [str(image)])

    assert "/workspace/uploads/attached.png" in enriched
    assert str(image) not in enriched


def test_owner_worker_codex_prompt_uses_deployment_native_vision(
    owner_gateway, monkeypatch
):
    from hermes_cli.controlled_roots import RootKind
    from hermes_cli.deployment_inference import DeploymentInferenceDescriptor

    _db, runtime, _workspace_root = owner_gateway
    transport = _CollaborationTransport()
    image = runtime.filesystem_context.workspace_path / "uploads" / "attached.png"
    runtime.filesystem_context.roots.replace_bytes(
        RootKind.WORKSPACE,
        "default/uploads/attached.png",
        b"\x89PNG\r\n\x1a\nvalid-test-image",
    )
    captured = {}
    turn_started = threading.Event()

    class _Agent:
        provider = "custom:codex"
        model = "gpt-5.6-sol"
        api_mode = "codex_app_server"
        session_id = "stored-image"

        def clear_interrupt(self):
            return None

        def run_conversation(self, message, **_kwargs):
            captured["message"] = message
            turn_started.set()
            return {
                "final_response": "visible",
                "messages": [{"role": "assistant", "content": "visible"}],
            }

    session = {
        "agent": _Agent(),
        "agent_ready": threading.Event(),
        "session_key": "stored-image",
        "history": [],
        "history_lock": threading.Lock(),
        "history_version": 0,
        "inflight_turn": None,
        "running": False,
        "attached_images": [str(image)],
        "pending_attachments": [],
        "cwd": str(runtime.filesystem_context.workspace_path),
        "cols": 80,
        "transport": transport,
        "source": "dashboard-gui",
    }
    session["agent_ready"].set()
    runtime.mutable_state.sessions["live-image"] = session
    descriptor = DeploymentInferenceDescriptor(
        provider="custom:codex",
        model="gpt-5.6-sol",
        api_mode="chat_completions",
        policy_id="policy-v1",
        allowed_models=("gpt-5.6-sol",),
        supports_vision=True,
    )
    monkeypatch.setattr(server, "_emit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server, "_sync_agent_model_with_config", lambda *_args: None)
    monkeypatch.setattr(server, "_register_session_cwd", lambda *_args: None)
    monkeypatch.setattr(server, "_complete_prompt_turn_receipt", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server, "_session_info", lambda *_args: {})
    monkeypatch.setattr(server, "_get_usage", lambda *_args: {})
    monkeypatch.setattr(server, "render_message", lambda *_args: "")
    monkeypatch.setattr(server, "_voice_tts_enabled", lambda: False)
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {})
    monkeypatch.setattr(
        "hermes_cli.deployment_inference.deployment_descriptor_from_environment",
        lambda: descriptor,
    )
    monkeypatch.setattr(
        server,
        "_enrich_with_attached_images",
        lambda *_args: (_ for _ in ()).throw(AssertionError("aux vision must not run")),
    )
    monkeypatch.setattr(
        "tools.process_registry.process_registry.drain_notifications",
        lambda: [],
    )

    try:
        response = _call(
            runtime,
            "prompt.submit",
            {"session_id": "live-image", "text": "describe it"},
            transport=transport,
        )
        assert response["result"] == {"status": "streaming"}
        assert turn_started.wait(timeout=2)
        session["_run_thread"].join(timeout=2)
        assert not session["_run_thread"].is_alive()
    finally:
        runtime.mutable_state.sessions.pop("live-image", None)

    assert isinstance(captured["message"], list)
    assert any(part.get("type") == "image_url" for part in captured["message"])
    text_part = next(part for part in captured["message"] if part.get("type") == "text")
    assert "/workspace/uploads/attached.png" in text_part["text"]
    assert str(image) not in text_part["text"]


def test_authenticated_image_attach_rejects_stale_workspace_path(owner_gateway, monkeypatch):
    _db, runtime, workspace_root = owner_gateway
    session = {"attached_images": [], "pending_attachments": []}
    stale = Path(workspace_root) / "default" / "old" / "missing.png"
    monkeypatch.setenv("TERMINAL_CWD", str(Path(workspace_root) / "default"))
    monkeypatch.setattr(server, "_sess", lambda _params, _rid: (session, None))

    with server.owner_worker_gateway_runtime(runtime):
        response = server._methods["image.attach"](
            "request", {"path": str(stale)}
        )

    assert response["error"] == {
        "code": 4016,
        "message": "authenticated image is unavailable",
    }
    assert str(stale) not in str(response)
    assert session["attached_images"] == []


def test_attachment_ref_rejects_external_path_in_non_authenticated_mode(monkeypatch, tmp_path):
    session = {"cwd": str(tmp_path)}
    target = tmp_path.parent / "outside.txt"
    monkeypatch.setattr(server, "_authenticated_workspace_context", lambda: None)

    with pytest.raises(ValueError, match="attachment path is unavailable"):
        server._attachment_ref_path(session, target)


def test_owner_worker_create_rejects_legacy_source_before_live_registration(owner_gateway):
    _db, runtime, _workspace_root = owner_gateway

    response = _call(runtime, "session.create", {"source": "tui"})

    assert response["error"] == {"code": 4002, "message": "session source is not available"}
    assert runtime.mutable_state.sessions == {}


def test_owner_worker_code_session_isolated_from_chat_config(owner_gateway, monkeypatch):
    from hermes_cli.controlled_roots import RootKind

    _db, runtime, _workspace_root = owner_gateway
    monkeypatch.setattr(
        server,
        "_resolve_code_startup_runtime",
        lambda params=None: (
            str((params or {}).get("model") or "code-default"),
            str((params or {}).get("provider") or "code-provider"),
        ),
    )

    response = _call(
        runtime,
        "session.create",
        {
            "source": "dashboard-gui",
            "kind": "code",
            "model": "code-model",
            "provider": "code-provider",
            "cwd": None,
        },
    )

    assert "error" not in response
    result = response["result"]
    assert result["info"]["model_kind"] == "code"
    assert result["info"]["runtime_profile"] == "coding"
    assert result["info"]["runtime_toolset"] == "coding"
    session = runtime.mutable_state.sessions[result["session_id"]]
    assert session["model_kind"] == "code"
    assert session["runtime_profile"] == "coding"
    assert session["runtime_toolset"] == "coding"
    assert session["model_override"] == {
        "model": "code-model",
        "provider": "code-provider",
    }


def test_employee_reasoning_config_uses_pinned_policy():
    assert server._employee_reasoning_config({"reasoning_effort": "max"}) == {
        "enabled": True,
        "effort": "max",
    }
    assert server._employee_reasoning_config({"reasoning_effort": ""}) is None


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


def test_legacy_employee_policy_uses_canonical_workspace(owner_gateway, monkeypatch):
    _db, runtime, _workspace_root = owner_gateway
    source_policy = {
        "schema_version": 1,
        "model_registration_id": "registration-a",
        "system_prompt": "You are an analyst.",
        "toolsets": [],
        "skills": [],
        "mcp_servers": [],
        "workspace_relative_path": "employees/new-employee",
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
                "employee_id": "emp_legacy",
                "profile_revision": 3,
                "profile_fingerprint": "sha256:" + "a" * 64,
                "source_policy": source_policy,
            },
        },
        purpose="retained-channel",
    )

    assert "error" not in response
    session = runtime.mutable_state.sessions[response["result"]["session_id"]]
    assert (
        session["employee_policy"]["workspace_relative_path"]
        == "employees/emp_legacy"
    )


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
                "employee_id": "ca_employee",
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


def test_web_direct_employee_selection_is_server_resolved_and_context_bound(
    owner_gateway, monkeypatch
):
    _db, runtime, _workspace_root = owner_gateway
    from hermes_cli.collaboration.models import CollaborationMemberProfile
    from hermes_cli.collaboration.resolver import ResolvedCollaborationEmployee

    policy = {
        "schema_version": 1,
        "employee_id": "employee-a",
        "profile_revision": 3,
        "source_profile_fingerprint": "sha256:" + "a" * 64,
        "system_prompt": "Server-authoritative policy",
        "model": {"provider": "openai", "model": "test-model"},
        "toolsets": [],
        "skills": [],
        "mcp_servers": [],
        "workspace_relative_path": "employees/analyst",
        "knowledge_relative_paths": [],
        "max_iterations": 20,
        "max_tokens": 2000,
        "reasoning_effort": "max",
    }

    class _Resolver:
        def resolve_current(self, employee_id):
            assert employee_id == "employee-a"
            return ResolvedCollaborationEmployee(
                member=CollaborationMemberProfile(
                    "employee-a", 3, "sha256:" + "a" * 64
                ),
                employee_policy=policy,
                may_participate=False,
                may_create_groups=True,
                invite_quota=5,
            )

    class _Service:
        resolver = _Resolver()

        def source_agent_context(self, **kwargs):
            from hermes_cli.collaboration.agent_tools import CollaborationAgentContext

            assert kwargs["creator_employee_id"] == "employee-a"
            assert kwargs["source_kind"] == "web_direct"
            assert kwargs["require_participation"] is False
            return CollaborationAgentContext(
                service=self,
                creator_employee_id="employee-a",
                source_kind="web_direct",
                source_conversation_id=kwargs["source_conversation_id"],
                may_create_authorized=True,
            )

    class _DashboardTransport:
        def begin_dashboard_attach(self, _generation, **_kwargs):
            return None

        def commit_dashboard_attach(self, _generation, _sid, *, on_commit):
            return on_commit()

        def abort_dashboard_attach(self, _generation):
            return None

        def write(self, _payload):
            return None

    server.bind_collaboration_service(runtime, _Service())
    monkeypatch.setattr(
        server, "_dashboard_attach_transport", lambda: _DashboardTransport()
    )
    override = _call(
        runtime,
        "session.create",
        {
            "source": "dashboard-gui",
            "employee_id": "employee-a",
            "model": "forged-model",
            "switch_generation": 0,
        },
    )
    assert override["error"] == {
        "code": 4002,
        "message": "employee policy cannot be combined with runtime overrides",
    }
    assert runtime.mutable_state.sessions == {}

    response = _call(
        runtime,
        "session.create",
        {
            "source": "dashboard-gui",
            "employee_id": "employee-a",
            "switch_generation": 1,
        },
    )

    assert "error" not in response
    session = runtime.mutable_state.sessions[response["result"]["session_id"]]
    assert session["employee_policy"] == policy
    context = session["collaboration_context"]
    assert context.creator_employee_id == "employee-a"
    assert context.source_kind == "web_direct"
    assert context.source_conversation_id == response["result"]["stored_session_id"]
    assert context.source_depth == 0
    assert context.may_create_authorized is True

    built = []

    def _capture_build(*_args, **kwargs):
        built.append(kwargs)
        raise RuntimeError("stop after trusted build arguments are captured")

    monkeypatch.setattr(server, "_make_agent", _capture_build)
    with server.owner_worker_gateway_runtime(runtime):
        server._start_agent_build(response["result"]["session_id"], session)
    assert session["agent_ready"].wait(timeout=2)
    assert built == [
        {
            "collaboration_context": context,
            "employee_policy": policy,
            "model_override": session["model_override"],
            "platform": "webui",
        }
    ]


def test_builtin_web_direct_policy_pins_tool_snapshot_and_rechecks_authority(
    owner_gateway, monkeypatch
):
    _db, runtime, _workspace_root = owner_gateway
    from hermes_cli.collaboration.models import CollaborationMemberProfile
    from hermes_cli.collaboration.resolver import ResolvedCollaborationEmployee

    base_policy = {
        "schema_version": 1,
        "employee_id": "builtin-a",
        "profile_revision": 1,
        "source_profile_fingerprint": "builtin-assistant-v1",
        "system_prompt": "Built-in prompt",
        "model": {"provider": "openai", "model": "test-model"},
        "toolsets": [],
        "skills": [],
        "mcp_servers": [],
        "workspace_relative_path": "",
        "knowledge_relative_paths": [],
        "max_iterations": 90,
        "max_tokens": None,
        "builtin_assistant": True,
    }

    class _Resolver:
        may_manage_employees = True

        def resolve_current(self, employee_id):
            assert employee_id == "builtin-a"
            return ResolvedCollaborationEmployee(
                member=CollaborationMemberProfile(
                    "builtin-a", 1, "builtin-assistant-v1"
                ),
                employee_policy=base_policy,
                may_participate=True,
                may_create_groups=True,
                may_manage_employees=self.may_manage_employees,
                invite_quota=None,
            )

    class _Service:
        resolver = _Resolver()

        def source_agent_context(self, **kwargs):
            from hermes_cli.collaboration.agent_tools import CollaborationAgentContext

            resolved = self.resolver.resolve_current(kwargs["creator_employee_id"])
            return CollaborationAgentContext(
                service=self,
                creator_employee_id=resolved.member.employee_id,
                source_kind=kwargs["source_kind"],
                source_conversation_id=kwargs["source_conversation_id"],
                may_create_authorized=resolved.may_create_groups,
                may_manage_employees=resolved.may_manage_employees,
            )

    class _DashboardTransport:
        def begin_dashboard_attach(self, _generation, **_kwargs):
            return None

        def commit_dashboard_attach(self, _generation, _sid, *, on_commit):
            return on_commit()

        def write(self, _payload):
            return None

    service = _Service()
    server.bind_collaboration_service(runtime, service)
    monkeypatch.setattr(
        server, "_dashboard_attach_transport", lambda: _DashboardTransport()
    )
    monkeypatch.setattr(
        server, "_load_enabled_toolsets", lambda: ["terminal", "project"]
    )

    response = _call(
        runtime,
        "session.create",
        {
            "source": "dashboard-gui",
            "employee_id": "builtin-a",
            "switch_generation": 1,
        },
    )

    assert "error" not in response
    session = runtime.mutable_state.sessions[response["result"]["session_id"]]
    pinned_policy = session["employee_policy"]
    assert pinned_policy["runtime_toolsets"] == ["terminal", "project"]
    assert pinned_policy["system_prompt"] == "Built-in prompt"
    server._ensure_session_db_row(session)
    row = _db.get_session(response["result"]["stored_session_id"])
    with server.owner_worker_gateway_runtime(runtime):
        restored = server._stored_session_runtime_overrides(row)
    assert restored["employee_policy"] == pinned_policy
    assert restored["employee_policy"]["runtime_toolsets"] == [
        "terminal",
        "project",
    ]
    assert restored["collaboration_context"].may_manage_employees is True

    service.resolver.may_manage_employees = False
    with server.owner_worker_gateway_runtime(runtime):
        with pytest.raises(RuntimeError, match="authority is unavailable"):
            server._stored_session_runtime_overrides(row)


def _web_direct_test_service(policy):
    from hermes_cli.collaboration.agent_tools import CollaborationAgentContext
    from hermes_cli.collaboration.models import CollaborationMemberProfile
    from hermes_cli.collaboration.resolver import ResolvedCollaborationEmployee

    class _Resolver:
        def resolve_current(self, employee_id):
            if employee_id != policy["employee_id"]:
                raise RuntimeError("employee not found")
            return ResolvedCollaborationEmployee(
                member=CollaborationMemberProfile(
                    employee_id,
                    int(policy.get("profile_revision") or 1),
                    str(policy.get("source_profile_fingerprint") or "sha256:" + "a" * 64),
                ),
                employee_policy=policy,
                may_participate=True,
                may_create_groups=False,
            )

    class _Service:
        resolver = _Resolver()

        def source_agent_context(self, **kwargs):
            return CollaborationAgentContext(
                service=self,
                creator_employee_id=kwargs["creator_employee_id"],
                source_kind=kwargs["source_kind"],
                source_conversation_id=kwargs["source_conversation_id"],
                may_create_authorized=False,
            )

    return _Service()


class _EmployeeDashboardTransport:
    def __init__(self):
        self.active_session_id = None

    def begin_dashboard_attach(self, _generation, **_kwargs):
        return None

    def commit_dashboard_attach(self, _generation, sid, *, on_commit):
        if not on_commit():
            return False
        self.active_session_id = sid
        return True

    def dashboard_attach_is_current(self, _generation):
        return True

    def abort_dashboard_attach(self, _generation):
        return None

    def write(self, _payload):
        return None


def _open_employee(runtime, transport, employee_id, generation):
    return _call(
        runtime,
        "session.create",
        {
            "source": "dashboard-gui",
            "employee_id": employee_id,
            "browser_id": "browser-a",
            "switch_generation": generation,
        },
        transport=transport,
    )


def test_web_direct_open_reuses_live_and_cold_persisted_conversation(
    owner_gateway, monkeypatch
):
    db, runtime, _workspace_root = owner_gateway
    policy = {
        "employee_id": "employee-a",
        "profile_revision": 1,
        "source_profile_fingerprint": "sha256:" + "a" * 64,
        "system_prompt": "Pinned employee prompt",
        "model": {"provider": "openai", "model": "test-model"},
        "toolsets": [],
        "skills": [],
        "mcp_servers": [],
        "workspace_relative_path": "employees/analyst",
        "knowledge_relative_paths": [],
        "max_iterations": 20,
        "max_tokens": 2000,
    }
    server.bind_collaboration_service(runtime, _web_direct_test_service(policy))
    transport = _EmployeeDashboardTransport()
    monkeypatch.setattr(server, "_reopen_resume_session", lambda *_args, **_kwargs: None)

    first = _open_employee(runtime, transport, "employee-a", 1)
    first_session = runtime.mutable_state.sessions[first["result"]["session_id"]]
    original_binding = dict(first_session["web_direct_binding"])
    second = _open_employee(runtime, transport, "employee-a", 2)

    assert "error" not in first
    assert second["result"]["session_key"] == first["result"]["stored_session_id"]
    assert second["result"]["session_id"] == first["result"]["session_id"]
    assert second["result"]["resume_kind"] == "live"
    assert first_session["web_direct_binding"] == original_binding

    server._ensure_session_db_row(first_session)
    db.append_message(first["result"]["stored_session_id"], "user", "remember this")

    live = _open_employee(runtime, transport, "employee-a", 3)

    assert [message["text"] for message in live["result"]["messages"]] == [
        "remember this"
    ]
    assert live["result"]["history_page"] == {
        "cursor": None,
        "has_more": False,
        "returned_count": 1,
        "truncated_count": 0,
    }

    runtime.mutable_state.sessions.clear()
    cold = _open_employee(runtime, transport, "employee-a", 4)

    assert cold["result"]["resumed"] == first["result"]["stored_session_id"]
    assert cold["result"]["resume_kind"] == "cold"
    assert [message["text"] for message in cold["result"]["messages"]] == [
        "remember this"
    ]
    assert cold["result"]["history_page"] == {
        "cursor": None,
        "has_more": False,
        "returned_count": 1,
        "truncated_count": 0,
    }
    resumed = runtime.mutable_state.sessions[cold["result"]["session_id"]]
    assert [message["content"] for message in resumed["history"]] == ["remember this"]
    assert resumed["employee_policy"] == policy


def test_web_direct_open_follows_compression_and_delete_fences_stale_runtime(
    owner_gateway, monkeypatch
):
    db, runtime, workspace_root = owner_gateway
    policy = {
        "employee_id": "employee-a",
        "profile_revision": 1,
        "source_profile_fingerprint": "sha256:" + "a" * 64,
        "system_prompt": "Pinned employee prompt",
        "model": {"provider": "openai", "model": "test-model"},
        "toolsets": [],
        "skills": [],
        "mcp_servers": [],
        "workspace_relative_path": "employees/analyst",
        "knowledge_relative_paths": [],
        "max_iterations": 20,
        "max_tokens": 2000,
    }
    server.bind_collaboration_service(runtime, _web_direct_test_service(policy))
    transport = _EmployeeDashboardTransport()
    monkeypatch.setattr(server, "_reopen_resume_session", lambda *_args, **_kwargs: None)

    opened = _open_employee(runtime, transport, "employee-a", 1)
    root = opened["result"]["stored_session_id"]
    stale = runtime.mutable_state.sessions[opened["result"]["session_id"]]
    server._ensure_session_db_row(stale)
    db.append_message(root, "user", "before compression")
    db.end_session(root, "compression")
    db.create_session(
        "compression-tip",
        source="dashboard-gui",
        owner_key="owner-a",
        workspace_root=workspace_root,
        worker_generation=2,
        parent_session_id=root,
        model_config={
            server._EMPLOYEE_POLICY_CONFIG_KEY: policy,
            server._WEB_DIRECT_EMPLOYEE_CONFIG_KEY: "employee-a",
        },
    )
    db.append_message("compression-tip", "assistant", "after compression")
    runtime.mutable_state.sessions.clear()

    compressed = _open_employee(runtime, transport, "employee-a", 2)
    assert compressed["result"]["resumed"] == "compression-tip"
    assert [message["content"] for message in runtime.mutable_state.sessions[
        compressed["result"]["session_id"]
    ]["history"]] == ["after compression"]

    assert db.delete_session(
        "compression-tip",
        recovery_scope={
            "owner_key": "owner-a",
            "workspace_root": workspace_root,
            "worker_generation": 2,
            "historical_resume": True,
        },
    )
    assert server._web_direct_binding_is_current(stale) is False
    stale["running"] = True
    stale["queued_prompt"] = None
    stale_sid = opened["result"]["session_id"]
    runtime.mutable_state.sessions[stale_sid] = stale
    rejected = _call(
        runtime,
        "prompt.submit",
        {"session_id": stale_sid, "text": "stale prompt"},
        transport=transport,
    )
    assert rejected["error"] == {
        "code": 4093,
        "message": "employee conversation was reset — reopen the contact",
    }
    assert stale["queued_prompt"] is None

    runtime.mutable_state.sessions.clear()
    replacement = _open_employee(runtime, transport, "employee-a", 3)
    assert replacement["result"]["stored_session_id"] != root


def test_ordinary_session_create_remains_non_idempotent(owner_gateway):
    _db, runtime, _workspace_root = owner_gateway

    first = _call(runtime, "session.create", {"source": "dashboard-gui"})
    second = _call(runtime, "session.create", {"source": "dashboard-gui"})

    assert first["result"]["stored_session_id"] != second["result"]["stored_session_id"]
    assert first["result"]["session_id"] != second["result"]["session_id"]




def test_feishu_direct_context_requires_exact_managed_origin_and_group_gets_no_tool(
    owner_gateway,
):
    _db, runtime, _workspace_root = owner_gateway
    from hermes_cli.collaboration.agent_tools import CollaborationAgentContext

    policy = {"employee_id": "employee-a"}

    class _Resolver:
        def validate_feishu_origin(self, **kwargs):
            assert kwargs["employee_id"] == "employee-a"
            assert kwargs["binding_id"] == "binding-a"
            assert kwargs["conversation_id"] == "oc_direct"
            if kwargs["employee_id"] != "employee-a":
                raise RuntimeError("forged account")

    class _Service:
        resolver = _Resolver()

        def source_agent_context(self, **kwargs):
            return CollaborationAgentContext(
                service=self,
                creator_employee_id=kwargs["creator_employee_id"],
                source_kind=kwargs["source_kind"],
                source_conversation_id=kwargs["source_conversation_id"],
                source_provider=kwargs["source_provider"],
                source_connector_account_id=kwargs["source_connector_account_id"],
                source_binding_id=kwargs["source_binding_id"],
                source_session_id=kwargs["source_session_id"],
                may_create_authorized=True,
            )

    service = _Service()
    server.bind_collaboration_service(runtime, service)
    direct = {
        "provider": "feishu",
        "source_kind": "feishu_direct",
        "employee_id": "employee-a",
        "connector_account_id": "ca-employee-a",
        "binding_id": "binding-a",
        "conversation_id": "oc_direct",
        "thread_id": "",
        "dispatch_scope": "",
    }
    with server.owner_worker_gateway_runtime(runtime):
        token = server.bind_transport(_Transport("retained-channel"))
        try:
            context, error = server._trusted_retained_collaboration_context(
                {"retained_source_context": direct},
                employee_policy=policy,
                session_key="session-a",
            )
            assert error is None
            assert context.source_kind == "feishu_direct"
            assert context.source_binding_id == "binding-a"
            assert context.allowed_origin_attachment_ids == ()

            group_context, error = server._trusted_retained_collaboration_context(
                {
                    "retained_source_context": {
                        **direct,
                        "source_kind": "feishu_group",
                    }
                },
                employee_policy=policy,
                session_key="session-group",
            )
            assert error is None
            assert group_context is None

            forged, error = server._trusted_retained_collaboration_context(
                {
                    "retained_source_context": {
                        **direct,
                        "employee_id": "employee-b",
                    }
                },
                employee_policy=policy,
                session_key="session-forged",
            )
            assert forged is None
            assert "inconsistent" in error
        finally:
            server.reset_transport(token)


def test_web_direct_employee_metadata_propagates_to_compression_children():
    from types import SimpleNamespace

    agent = SimpleNamespace(_session_init_model_config={"max_iterations": 20})
    policy = {"employee_id": "employee-a", "system_prompt": "Pinned"}
    context = SimpleNamespace(source_kind="web_direct")

    server._stamp_employee_compression_metadata(
        agent,
        employee_policy=policy,
        collaboration_context=context,
    )

    assert agent._session_init_model_config[server._EMPLOYEE_POLICY_CONFIG_KEY] == policy
    assert (
        agent._session_init_model_config[server._WEB_DIRECT_EMPLOYEE_CONFIG_KEY]
        == "employee-a"
    )


def test_collaboration_tool_injection_requires_trusted_role_and_creation_authority():
    from hermes_cli.collaboration.agent_tools import CollaborationAgentContext

    class _Agent:
        def __init__(self):
            self.tools = []
            self.valid_tool_names = set()

    def names(context):
        agent = _Agent()
        server._inject_collaboration_agent_tools(agent, context)
        return agent.valid_tool_names

    base = {
        "service": object(),
        "creator_employee_id": "employee-a",
        "source_kind": "web_direct",
        "source_conversation_id": "session-a",
    }
    assert names(CollaborationAgentContext(**base, may_create_authorized=True)) == {
        "create_internal_group"
    }
    assert names(CollaborationAgentContext(**base, may_create_authorized=False)) == set()
    assert names(
        CollaborationAgentContext(
            **base,
            source_depth=1,
            may_create_authorized=True,
        )
    ) == set()
    assert names(
        CollaborationAgentContext(
            **base,
            role="member",
            may_create_authorized=True,
        )
    ) == set()


def test_web_direct_employee_selection_rejects_policy_and_runtime_forgery(
    owner_gateway, monkeypatch
):
    _db, runtime, _workspace_root = owner_gateway
    monkeypatch.setattr(server, "_dashboard_attach_transport", lambda: object())

    policy = _call(
        runtime,
        "session.create",
        {
            "source": "dashboard-gui",
            "employee_id": "employee-a",
            "employee_policy": {},
            "switch_generation": 1,
        },
    )

    assert policy["error"] == {
        "code": 4002,
        "message": "employee policy requires a retained channel connection",
    }
    assert runtime.mutable_state.sessions == {}


def test_web_direct_employee_selection_requires_dashboard_transport(owner_gateway):
    _db, runtime, _workspace_root = owner_gateway

    response = _call(
        runtime,
        "session.create",
        {
            "source": "dashboard-gui",
            "employee_id": "employee-a",
            "switch_generation": 1,
        },
    )

    assert response["error"] == {
        "code": 4002,
        "message": "employee direct chat requires a dashboard WebSocket",
    }
    assert runtime.mutable_state.sessions == {}


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


def test_web_direct_resume_rebuilds_live_authority_and_rejects_identity_mismatch(
    owner_gateway,
):
    _db, runtime, _workspace_root = owner_gateway
    from hermes_cli.collaboration.models import CollaborationMemberProfile
    from hermes_cli.collaboration.resolver import ResolvedCollaborationEmployee

    policy = {
        "employee_id": "employee-a",
        "system_prompt": "Pinned policy",
    }

    class _Resolver:
        may_create_groups = False
        may_participate = True

        def resolve_current(self, employee_id):
            assert employee_id == "employee-a"
            return ResolvedCollaborationEmployee(
                member=CollaborationMemberProfile(
                    "employee-a", 1, "sha256:" + "a" * 64
                ),
                employee_policy=policy,
                may_participate=self.may_participate,
                may_create_groups=self.may_create_groups,
                invite_quota=5,
            )

    class _Service:
        resolver = _Resolver()

        def source_agent_context(self, **kwargs):
            from hermes_cli.collaboration.agent_tools import CollaborationAgentContext

            resolved = self.resolver.resolve_current(kwargs["creator_employee_id"])
            if not resolved.may_participate:
                raise RuntimeError("collaboration participation is revoked")
            return CollaborationAgentContext(
                service=self,
                creator_employee_id=resolved.member.employee_id,
                source_kind=kwargs["source_kind"],
                source_conversation_id=kwargs["source_conversation_id"],
                may_create_authorized=resolved.may_create_groups,
            )

    service = _Service()
    server.bind_collaboration_service(runtime, service)
    row = {
        "id": "session-a",
        "model_config": {
            server._EMPLOYEE_POLICY_CONFIG_KEY: policy,
            server._WEB_DIRECT_EMPLOYEE_CONFIG_KEY: "employee-a",
        },
    }

    with server.owner_worker_gateway_runtime(runtime):
        denied = server._stored_session_runtime_overrides(row)
        assert denied["employee_policy"] == policy
        assert denied["collaboration_context"].may_create_authorized is False

        service.resolver.may_create_groups = True
        allowed = server._stored_session_runtime_overrides(row)
        assert allowed["collaboration_context"].may_create_authorized is True

        service.resolver.may_participate = False
        with pytest.raises(RuntimeError, match="participation is revoked"):
            server._stored_session_runtime_overrides(row)
        service.resolver.may_participate = True

        with pytest.raises(RuntimeError, match="identity is inconsistent"):
            server._stored_session_runtime_overrides(
                {
                    **row,
                    "model_config": {
                        **row["model_config"],
                        server._WEB_DIRECT_EMPLOYEE_CONFIG_KEY: "employee-b",
                    },
                }
            )


def test_owner_worker_resume_marks_reconstructed_history_as_persisted(
    owner_gateway, monkeypatch
):
    db, runtime, workspace_root = owner_gateway
    _create_owned(db, workspace_root, "retained", source="dashboard-gui", generation=2)
    db.append_message("retained", "user", "draw an image")
    tool_calls = [{
        "id": "call-image-1",
        "type": "function",
        "function": {
            "name": "image_generate",
            "arguments": '{"prompt":"draw"}',
        },
    }]
    db.append_message(
        "retained", "assistant", "", tool_calls=tool_calls,
    )
    db.append_message(
        "retained",
        "tool",
        '{"success":true,"image":"/workspace/generated/result.png"}',
        tool_name="image_generate",
        tool_call_id="call-image-1",
    )
    monkeypatch.setattr(server, "_reopen_resume_session", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        server, "_stored_session_runtime_overrides", lambda *_args, **_kwargs: {}
    )
    monkeypatch.setattr(server, "_notify_session_boundary", lambda *_args, **_kwargs: None)

    response = _call(
        runtime,
        "session.resume",
        {"session_id": "retained", "source": "dashboard-gui"},
    )

    assert "error" not in response
    live_id = response["result"]["session_id"]
    history = runtime.mutable_state.sessions[live_id]["history"]
    assert [message["role"] for message in history] == ["user", "assistant", "tool"]
    assert all(message[DB_PERSISTED_MARKER] is True for message in history)


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


def test_queued_prompt_retains_owner_runtime_for_approval(
    owner_gateway, monkeypatch
):
    from hermes_cli.controlled_roots import RootKind
    from tools import approval

    _db, runtime, _workspace_root = owner_gateway
    transport = _CollaborationTransport()
    first_started = threading.Event()
    release_first = threading.Event()
    approval_requested = threading.Event()
    protected = {"exists": True}

    class _Agent:
        model = "test-model"
        provider = "test-provider"
        session_id = "stored-a"

        def clear_interrupt(self):
            return None

        def interrupt(self):
            return None

        def run_conversation(self, message, **_kwargs):
            if message == "safe":
                first_started.set()
                assert release_first.wait(timeout=2)
                text = "safe complete"
            else:
                decision = approval.check_all_command_guards(
                    "rm -r /workspace/protected",
                    "local",
                )
                if decision.get("approved"):
                    protected["exists"] = False
                    text = "unsafe"
                else:
                    text = decision.get("message") or "denied"
            return {
                "final_response": text,
                "messages": [{"role": "assistant", "content": text}],
            }

    agent = _Agent()
    session = {
        "agent": agent,
        "agent_ready": threading.Event(),
        "session_key": "stored-a",
        "history": [],
        "history_lock": threading.Lock(),
        "history_version": 0,
        "inflight_turn": None,
        "running": False,
        "attached_images": [],
        "pending_attachments": [],
        "cwd": str(
            runtime.filesystem_context.roots.get(RootKind.WORKSPACE).canonical_path
            / runtime.filesystem_context.workspace_prefix
        ),
        "cols": 80,
        "transport": transport,
        "source": "dashboard-gui",
    }
    session["agent_ready"].set()
    runtime.mutable_state.sessions["live-a"] = session
    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "queue")
    monkeypatch.setattr(server, "_sync_agent_model_with_config", lambda *_args: None)
    monkeypatch.setattr(server, "_register_session_cwd", lambda *_args: None)
    monkeypatch.setattr(server, "_complete_prompt_turn_receipt", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server, "_session_info", lambda *_args: {})
    monkeypatch.setattr(server, "_get_usage", lambda *_args: {})
    monkeypatch.setattr(server, "render_message", lambda *_args: "")
    monkeypatch.setattr(server, "_load_cfg", lambda: {})
    monkeypatch.setattr(server, "_voice_tts_enabled", lambda: False)
    monkeypatch.setattr(
        "tools.process_registry.process_registry.drain_notifications",
        lambda: [],
    )
    monkeypatch.setenv("HERMES_GATEWAY_SESSION", "1")
    monkeypatch.setenv("HERMES_EXEC_ASK", "1")
    monkeypatch.setattr(approval, "_get_approval_mode", lambda: "manual")
    monkeypatch.setattr(
        "tools.tirith_security.check_command_security",
        lambda _command: {"action": "allow", "findings": [], "summary": ""},
    )

    def notify(data):
        server._emit_approval_request("live-a", data)
        approval_requested.set()

    approval.register_gateway_notify("stored-a", notify)
    try:
        first = _call(
            runtime,
            "prompt.submit",
            {"session_id": "live-a", "text": "safe"},
            transport=transport,
        )
        assert first["result"] == {"status": "streaming"}
        assert first_started.wait(timeout=2)

        second = _call(
            runtime,
            "prompt.submit",
            {"session_id": "live-a", "text": "approval-deny"},
            transport=transport,
        )
        assert second["result"] == {"status": "queued"}
        release_first.set()

        assert approval_requested.wait(timeout=2)
        approval_response = _call(
            runtime,
            "approval.respond",
            {"session_id": "live-a", "choice": "deny"},
            transport=transport,
        )
        assert approval_response["result"] == {"resolved": 1}
        session["_run_thread"].join(timeout=2)
        assert not session["_run_thread"].is_alive()
    finally:
        release_first.set()
        approval.resolve_gateway_approval("stored-a", "deny", resolve_all=True)
        approval.unregister_gateway_notify("stored-a")
        runtime.mutable_state.sessions.pop("live-a", None)

    events = [frame["params"] for frame in transport.frames if frame.get("method") == "event"]
    approval_events = [event for event in events if event.get("type") == "approval.request"]
    assert len(approval_events) == 1
    assert approval_events[0]["session_id"] == "live-a"
    assert protected == {"exists": True}
    completed = [event["payload"] for event in events if event.get("type") == "message.complete"]
    assert completed[-1]["status"] == "complete"
    assert "denied" in completed[-1]["text"].lower()


def test_verified_artifact_emits_before_complete_and_failed_turn_emits_none(
    owner_gateway, monkeypatch
):
    from hermes_cli.controlled_roots import RootKind

    _db, runtime, _workspace_root = owner_gateway
    transport = _CollaborationTransport()
    turn_started = threading.Event()
    outcomes = [
        {
            "artifacts": [
                {
                    "id": "artifact-zip",
                    "mime_type": "application/zip",
                    "name": "tool.zip",
                    "path": "/workspace/tool.zip",
                    "size_bytes": 7,
                }
            ],
            "final_response": "ready",
            "messages": [{"role": "assistant", "content": "ready"}],
        },
        {
            "artifacts": [
                {
                    "id": "must-not-emit",
                    "name": "bad.zip",
                    "path": "/workspace/bad.zip",
                }
            ],
            "error": "failed",
            "failed": True,
            "final_response": "failed",
            "messages": [{"role": "assistant", "content": "failed"}],
        },
    ]

    class _Agent:
        model = "test-model"
        provider = "test-provider"
        session_id = "stored-artifact"

        def clear_interrupt(self):
            return None

        def interrupt(self):
            return None

        def run_conversation(self, *_args, **_kwargs):
            turn_started.set()
            return outcomes.pop(0)

    agent = _Agent()
    session = {
        "agent": agent,
        "agent_ready": threading.Event(),
        "session_key": "stored-artifact",
        "history": [],
        "history_lock": threading.Lock(),
        "history_version": 0,
        "inflight_turn": None,
        "running": False,
        "attached_images": [],
        "pending_attachments": [],
        "cwd": str(
            runtime.filesystem_context.roots.get(RootKind.WORKSPACE).canonical_path
            / runtime.filesystem_context.workspace_prefix
        ),
        "cols": 80,
        "transport": transport,
        "source": "dashboard-gui",
    }
    session["agent_ready"].set()
    runtime.mutable_state.sessions["live-artifact"] = session
    monkeypatch.setattr(server, "_required_gateway_transport", lambda: transport)
    monkeypatch.setattr(server, "_sync_agent_model_with_config", lambda *_args: None)
    monkeypatch.setattr(server, "_register_session_cwd", lambda *_args: None)
    monkeypatch.setattr(server, "_complete_prompt_turn_receipt", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server, "_session_info", lambda *_args: {})
    monkeypatch.setattr(server, "_get_usage", lambda *_args: {})
    monkeypatch.setattr(server, "render_message", lambda *_args: "")
    monkeypatch.setattr(server, "_voice_tts_enabled", lambda: False)
    monkeypatch.setattr(
        "tools.process_registry.process_registry.drain_notifications",
        lambda: [],
    )

    try:
        for text in ("success", "failure"):
            response = _call(
                runtime,
                "prompt.submit",
                {"session_id": "live-artifact", "text": text},
                transport=transport,
            )
            assert response["result"] == {"status": "streaming"}
            assert turn_started.wait(timeout=2)
            run_thread = session["_run_thread"]
            run_thread.join(timeout=2)
            assert not run_thread.is_alive()
            turn_started.clear()
    finally:
        runtime.mutable_state.sessions.pop("live-artifact", None)

    events = [frame["params"] for frame in transport.frames if frame.get("method") == "event"]
    relevant = [
        event["type"]
        for event in events
        if event["type"] in {"artifact.created", "message.complete"}
    ]
    assert relevant == ["artifact.created", "message.complete", "message.complete"]
    artifacts = [event for event in events if event["type"] == "artifact.created"]
    assert artifacts[0]["payload"]["id"] == "artifact-zip"


def test_websocket_teardown_binds_owner_runtime(owner_gateway, monkeypatch):
    from tui_gateway import ws as gateway_ws

    _db, runtime, _workspace_root = owner_gateway
    observed = []

    class _WebSocket:
        query_params = {}
        claims = None
        scope = {}

        async def accept(self):
            return None

        async def send_text(self, _value):
            return None

        async def receive_text(self):
            raise gateway_ws._WebSocketDisconnect(code=1000)

        async def close(self, **_kwargs):
            return None

    monkeypatch.setattr(
        "hermes_cli.mcp_startup.start_background_mcp_discovery",
        lambda **_kwargs: None,
    )

    def close_sessions(_transport, *, end_reason):
        observed.append(
            (
                server.current_owner_worker_gateway_runtime(),
                end_reason,
            )
        )
        return 0, 0

    monkeypatch.setattr(server, "_close_sessions_for_transport", close_sessions)

    asyncio.run(gateway_ws.handle_ws(_WebSocket(), runtime=runtime))

    assert observed == [(runtime, "ws_disconnect")]


def test_collaboration_runner_rebuilds_from_matching_persisted_policy(
    owner_gateway, monkeypatch
):
    db, runtime, _workspace_root = owner_gateway
    from hermes_cli.collaboration.models import CollaborationMembership
    from hermes_cli.employee_policy import canonical_employee_snapshot

    membership = CollaborationMembership(
        membership_id="membership-a",
        group_id="group-a",
        employee_id="employee-a",
        profile_revision=1,
        profile_fingerprint="fingerprint-a",
        hidden_session_id="hidden-a",
        stored_session_id="stored-a",
        role="member",
        join_sequence=1,
        leave_sequence=None,
        created_at=1.0,
        left_at=None,
    )
    policy = canonical_employee_snapshot({
        "employee_id": "employee-a",
        "profile_revision": 1,
        "source_profile_fingerprint": "fingerprint-a",
        "system_prompt": "Pinned policy",
        "model": {"provider": "openai", "model": "test-model"},
        "workspace_relative_path": "employees/analyst",
        "knowledge_relative_paths": ["collaboration-attachments/membership-a"],
        "reasoning_effort": "max",
    })[0]
    runner = server.CollaborationAgentRunner(db, runtime)
    runner.ensure_member_session(membership=membership, employee_policy=policy)
    built = []

    class _Agent:
        def run_conversation(self, *_args, **_kwargs):
            return {"final_response": "done"}

        def close(self):
            return None

        def interrupt(self):
            return None

    def _make_agent(*_args, **kwargs):
        built.append(kwargs["employee_policy"])
        return _Agent()

    monkeypatch.setattr(server, "_make_agent", _make_agent)
    first = runner.run(
        stored_session_id="stored-a",
        hidden_session_id="hidden-a",
        employee_policy=policy,
        prompt="hello",
        target_id="target-a",
        external_receipt_key="receipt-a",
        on_delta=lambda _text: None,
        on_approval=lambda _data: None,
    )
    runner._agents.clear()
    second = runner.run(
        stored_session_id="stored-a",
        hidden_session_id="hidden-a",
        employee_policy=policy,
        prompt="again",
        target_id="target-b",
        external_receipt_key="receipt-b",
        on_delta=lambda _text: None,
        on_approval=lambda _data: None,
    )

    assert first["status"] == second["status"] == "complete"
    assert built == [policy, policy]
    unsafe_snapshot = {
        key: value
        for key, value in policy.items()
        if key != "snapshot_fingerprint"
    }
    unsafe_snapshot["system_prompt"] = "Browser override"
    unsafe_policy = canonical_employee_snapshot(unsafe_snapshot)[0]
    with pytest.raises(RuntimeError, match="snapshot is inconsistent"):
        runner.run(
            stored_session_id="stored-a",
            hidden_session_id="other-hidden",
            employee_policy=unsafe_policy,
            prompt="unsafe",
            target_id="target-c",
            external_receipt_key="receipt-c",
            on_delta=lambda _text: None,
            on_approval=lambda _data: None,
        )
    runner.close()


def test_collaboration_runner_resumes_policy_from_before_reasoning_levels(
    owner_gateway, monkeypatch
):
    db, runtime, _workspace_root = owner_gateway
    from hermes_cli.collaboration.models import CollaborationMembership
    from hermes_cli.collaboration.resolver import collaboration_member_policy
    from hermes_cli.employee_policy import (
        EmployeePolicyInvalid,
        canonical_employee_snapshot,
        normalize_employee_snapshot_for_resume,
    )

    membership = CollaborationMembership(
        membership_id="membership-a",
        group_id="group-a",
        employee_id="employee-a",
        profile_revision=4,
        profile_fingerprint="profile-fingerprint-a",
        hidden_session_id="hidden-a",
        stored_session_id="stored-a",
        role="member",
        join_sequence=1,
        leave_sequence=None,
        created_at=1.0,
        left_at=None,
    )
    legacy_employee_policy = canonical_employee_snapshot({
        "employee_id": "employee-a",
        "profile_revision": 4,
        "source_profile_fingerprint": "profile-fingerprint-a",
        "system_prompt": "Pinned policy",
        "model": {"provider": "openai", "model": "test-model"},
        "workspace_relative_path": "employees/analyst",
        "knowledge_relative_paths": [],
    })[0]
    legacy_payload = {
        **legacy_employee_policy,
        "knowledge_relative_paths": [
            f"collaboration-attachments/{membership.membership_id}"
        ],
    }
    serialized_legacy_payload = json.dumps(
        legacy_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    legacy_policy = {
        **legacy_payload,
        "snapshot_fingerprint": "sha256:"
        + hashlib.sha256(serialized_legacy_payload.encode("utf-8")).hexdigest(),
    }
    current_employee_policy = canonical_employee_snapshot({
        **{
            key: value
            for key, value in legacy_employee_policy.items()
            if key != "snapshot_fingerprint"
        },
        "reasoning_effort": "",
    })[0]
    current_policy = collaboration_member_policy(
        current_employee_policy,
        membership.membership_id,
    )
    runner = server.CollaborationAgentRunner(db, runtime)
    runner.ensure_member_session(membership=membership, employee_policy=legacy_policy)
    built = []

    class _Agent:
        def run_conversation(self, *_args, **_kwargs):
            return {"final_response": "upgraded reply"}

        def close(self):
            return None

        def interrupt(self):
            return None

    monkeypatch.setattr(
        server,
        "_make_agent",
        lambda *_args, **kwargs: (built.append(kwargs["employee_policy"]) or _Agent()),
    )

    result = runner.run(
        stored_session_id="stored-a",
        hidden_session_id="hidden-a",
        employee_policy=current_policy,
        prompt="hello after upgrade",
        target_id="target-a",
        external_receipt_key="receipt-a",
        on_delta=lambda _text: None,
        on_approval=lambda _data: None,
    )

    assert result == {"status": "complete", "text": "upgraded reply"}
    assert built == [current_policy]
    assert legacy_policy["snapshot_fingerprint"] != canonical_employee_snapshot(
        legacy_policy
    )[1]
    assert current_policy == canonical_employee_snapshot(current_policy)[0]

    for changed_field, changed_value in (
        ("employee_id", "employee-b"),
        ("profile_revision", 5),
        ("source_profile_fingerprint", "profile-fingerprint-b"),
        ("knowledge_relative_paths", ["collaboration-attachments/membership-b"]),
    ):
        changed = dict(legacy_policy)
        changed[changed_field] = changed_value
        with pytest.raises(EmployeePolicyInvalid, match="fingerprint is invalid"):
            normalize_employee_snapshot_for_resume(changed)

        runner._agents.clear()
        current_snapshot = {
            key: value
            for key, value in current_policy.items()
            if key != "snapshot_fingerprint"
        }
        current_snapshot[changed_field] = changed_value
        current_changed = canonical_employee_snapshot(current_snapshot)[0]
        with pytest.raises(RuntimeError, match="snapshot is inconsistent"):
            runner.run(
                stored_session_id="stored-a",
                hidden_session_id=f"hidden-{changed_field}",
                employee_policy=current_changed,
                prompt="unsafe",
                target_id=f"target-{changed_field}",
                external_receipt_key=f"receipt-{changed_field}",
                on_delta=lambda _text: None,
                on_approval=lambda _data: None,
            )

    runner.close()


def test_collaboration_runner_refreshes_dynamic_context_but_pins_identity(
    owner_gateway, monkeypatch
):
    db, runtime, _workspace_root = owner_gateway
    from hermes_cli.collaboration.agent_tools import CollaborationAgentContext
    from hermes_cli.collaboration.models import CollaborationMembership
    from hermes_cli.employee_policy import canonical_employee_snapshot

    membership = CollaborationMembership(
        membership_id="membership-a",
        group_id="group-a",
        employee_id="employee-a",
        profile_revision=1,
        profile_fingerprint="fingerprint-a",
        hidden_session_id="hidden-a",
        stored_session_id="stored-a",
        role="member",
        join_sequence=1,
        leave_sequence=None,
        created_at=1.0,
        left_at=None,
    )
    policy = canonical_employee_snapshot({
        "employee_id": "employee-a",
        "profile_revision": 1,
        "source_profile_fingerprint": "fingerprint-a",
        "system_prompt": "Pinned policy",
        "model": {"provider": "openai", "model": "test-model"},
        "workspace_relative_path": "employees/analyst",
        "knowledge_relative_paths": ["collaboration-attachments/membership-a"],
        "reasoning_effort": "max",
    })[0]
    runner = server.CollaborationAgentRunner(db, runtime)
    runner.ensure_member_session(membership=membership, employee_policy=policy)
    service = object()
    first_context = CollaborationAgentContext(
        service=service,
        creator_employee_id="employee-a",
        source_kind="web_group",
        source_conversation_id="group-a",
        source_group_id="group-a",
        source_event_id="event-a",
        allowed_origin_attachment_ids=("attachment-a",),
    )
    contexts = [
        first_context,
        replace(
            first_context,
            source_event_id="event-b",
            allowed_origin_attachment_ids=("attachment-b",),
        ),
    ]

    class _Agent:
        def __init__(self, context):
            self.collaboration_context = context
            self.observed = []

        def run_conversation(self, *_args, **_kwargs):
            self.observed.append(self.collaboration_context)
            return {"final_response": "done"}

        def close(self):
            return None

        def interrupt(self):
            return None

    built = []

    def _make_agent(*_args, **kwargs):
        agent = _Agent(kwargs["collaboration_context"])
        built.append(agent)
        return agent

    monkeypatch.setattr(server, "_make_agent", _make_agent)
    for index, context in enumerate(contexts):
        result = runner.run(
            stored_session_id="stored-a",
            hidden_session_id="hidden-a",
            employee_policy=policy,
            prompt=f"message-{index}",
            target_id=f"target-{index}",
            external_receipt_key=f"receipt-{index}",
            collaboration_context=context,
            on_delta=lambda _text: None,
            on_approval=lambda _data: None,
        )
        assert result["status"] == "complete"

    assert len(built) == 1
    assert built[0].observed == contexts
    incompatible = replace(
        contexts[-1], source_group_id="group-b", source_event_id="event-c"
    )
    with pytest.raises(RuntimeError, match="identity is inconsistent"):
        runner.run(
            stored_session_id="stored-a",
            hidden_session_id="hidden-a",
            employee_policy=policy,
            prompt="unsafe",
            target_id="target-c",
            external_receipt_key="receipt-c",
            collaboration_context=incompatible,
            on_delta=lambda _text: None,
            on_approval=lambda _data: None,
        )
    runner.close()


def test_collaboration_rpc_uses_bound_owner_service_and_redacts_internal_ids(owner_gateway):
    _db, runtime, _workspace_root = owner_gateway

    class _Service:
        def get_group(self, group_id, *, after_sequence=None):
            assert group_id == "group-a"
            assert after_sequence is None
            return {
                "group": {"group_id": group_id},
                "memberships": [{"membership_id": "member-a"}],
            }

    server.bind_collaboration_service(runtime, _Service())
    response = server.dispatch(
        {
            "id": "request",
            "method": "collaboration.group.get",
            "params": {"group_id": "group-a"},
        },
        transport=_CollaborationTransport(),
        runtime=runtime,
    )

    assert "error" not in response, response
    assert response["result"]["memberships"] == [{"membership_id": "member-a"}]


def test_direct_collaboration_origin_persists_and_emits_typed_card(owner_gateway, monkeypatch):
    from hermes_cli.display_transcript import format_display_transcript

    db, runtime, _workspace_root = owner_gateway
    db.create_session("direct-origin", source="dashboard-gui")
    session = {
        "history": [],
        "history_lock": threading.Lock(),
        "history_version": 0,
        "session_key": "direct-origin",
    }
    emitted = []
    monkeypatch.setattr(
        server,
        "_emit",
        lambda event, sid, payload: emitted.append((event, sid, payload)),
    )
    with server.owner_worker_gateway_runtime(runtime):
        server._sessions["live-origin"] = session
        try:
            server.deliver_web_collaboration_origin(
                runtime,
                db,
                task={
                    "conversation_id": "direct-origin",
                    "description": "Review safely",
                    "group_id": "group-a",
                    "source_kind": "web_direct",
                    "summary_text": None,
                    "task_id": "task-a",
                    "title": "Safety review",
                },
                completion=False,
            )
        finally:
            server._sessions.pop("live-origin", None)

    assert db.get_messages_as_conversation(
        "direct-origin", include_ancestors=False
    ) == []
    display = format_display_transcript(
        db.get_display_messages("direct-origin", include_ancestors=False)
    )
    assert display[0]["collaboration_card"] == {
        "brief": "Review safely",
        "group_id": "group-a",
        "status": "created",
        "summary": "",
        "task_id": "task-a",
        "title": "Safety review",
    }
    assert session["history"] == []
    card_id = display[0]["id"]
    assert emitted == [
        (
            "collaboration.origin.card",
            "live-origin",
            {
                "brief": "Review safely",
                "card_id": card_id,
                "group_id": "group-a",
                "status": "created",
                "summary": "",
                "task_id": "task-a",
                "title": "Safety review",
            },
        )
    ]


def test_group_collaboration_origin_persists_typed_card_without_targets(owner_gateway):
    db, runtime, _workspace_root = owner_gateway
    from hermes_cli.collaboration.models import CollaborationMemberProfile
    from hermes_cli.collaboration.store import CollaborationStore

    store = CollaborationStore(db, owner_key="owner-a")
    source = store.create_group(
        "Source group",
        members=[
            CollaborationMemberProfile(
                "source-employee", 1, "fingerprint-source-employee-r1"
            )
        ],
    )
    emitted = []

    class _Service:
        def __init__(self):
            self.store = store

        def emit(self, event, payload):
            emitted.append((event, payload))

    server.bind_collaboration_service(runtime, _Service())
    task = {
        "conversation_id": source.group_id,
        "description": "Review safely",
        "group_id": "ai-group-a",
        "source_group_id": source.group_id,
        "source_kind": "web_group",
        "summary_text": "Completed; @source-employee is plain summary text.",
        "task_id": "task-a",
        "title": "Safety review",
    }
    server.deliver_web_collaboration_origin(runtime, db, task=task, completion=False)
    server.deliver_web_collaboration_origin(runtime, db, task=task, completion=False)
    server.deliver_web_collaboration_origin(runtime, db, task=task, completion=True)
    server.deliver_web_collaboration_origin(runtime, db, task=task, completion=True)

    snapshot = store.snapshot_payload(source.group_id)
    cards = [
        event for event in snapshot["events"]
        if event["event_kind"] == "collaboration.origin.card"
    ]
    assert len(cards) == 2
    assert [card["body"] for card in cards] == [
        {
            "group_id": "ai-group-a",
            "status": "created",
            "task_id": "task-a",
            "text": "Review safely",
            "title": "Safety review",
        },
        {
            "group_id": "ai-group-a",
            "status": "completed",
            "task_id": "task-a",
            "text": "Completed; @source-employee is plain summary text.",
            "title": "Safety review",
        },
    ]
    assert snapshot["turns"] == []
    assert snapshot["targets"] == []
    assert len(emitted) == 2


def test_collaboration_events_use_gateway_payload_shape(owner_gateway):
    _db, runtime, _workspace_root = owner_gateway
    transport = _CollaborationTransport()

    class _Service:
        def list_groups(self, *, include_archived=False):
            assert include_archived is False
            return {"groups": []}

    server.bind_collaboration_service(runtime, _Service())
    response = server.dispatch(
        {
            "id": "request",
            "method": "collaboration.groups.list",
            "params": {},
        },
        transport=transport,
        runtime=runtime,
    )
    assert response["result"] == {"groups": []}
    runtime.mutable_state.collaboration_transports.add(transport)

    server.emit_collaboration_event(
        runtime,
        "collaboration.execution.delta",
        {
            "group_id": "group-a",
            "target_id": "target-a",
            "execution_id": "execution-a",
            "text": "chunk",
        },
    )

    assert transport.frames == [
        {
            "jsonrpc": "2.0",
            "method": "event",
            "params": {
                "type": "collaboration.execution.delta",
                "payload": {
                    "group_id": "group-a",
                    "target_id": "target-a",
                    "execution_id": "execution-a",
                    "text": "chunk",
                },
            },
        }
    ]


def test_dashboard_owner_attach_allows_collaboration_without_chat_session(
    owner_gateway, monkeypatch
):
    from tui_gateway import ws as gateway_ws

    _db, runtime, _workspace_root = owner_gateway

    class _Service:
        def list_groups(self, **_kwargs):
            return {"groups": []}

    loop = asyncio.new_event_loop()
    transport = gateway_ws.WSTransport(object(), loop, connection_purpose="interactive")
    server.bind_collaboration_service(runtime, _Service())
    monkeypatch.setattr(server, "_required_gateway_transport", lambda: transport)
    try:
        attached = server.dispatch(
            {
                "id": "attach",
                "method": "session.owner_attach",
                "params": {"browser_id": "browser-a"},
            },
            transport=transport,
            runtime=runtime,
        )
        groups = server.dispatch(
            {"id": "groups", "method": "collaboration.groups.list", "params": {}},
            transport=transport,
            runtime=runtime,
        )

        assert attached["result"] == {"attached": True}
        assert groups["result"] == {"groups": []}
        assert transport in runtime.mutable_state.collaboration_transports
        assert transport._dashboard_frame_allowed(
            {
                "method": "event",
                "params": {
                    "type": "collaboration.event.appended",
                    "payload": {"group_id": "group-a"},
                },
            }
        )
        assert not transport._dashboard_frame_allowed(
            {"method": "event", "params": {"type": "message.delta", "payload": {}}}
        )
    finally:
        loop.close()


@pytest.mark.parametrize(
    ("method", "params", "service_method", "expected_args", "expected_kwargs"),
    [
        (
            "collaboration.group.create",
            {"name": "Group", "employee_ids": ["employee-a"], "client_idempotency_key": "create-a"},
            "create_group",
            (),
            {"name": "Group", "employee_ids": ["employee-a"], "client_idempotency_key": "create-a"},
        ),
        (
            "collaboration.group.archive",
            {"group_id": "group-a"},
            "archive_group",
            ("group-a",),
            {},
        ),
        (
            "collaboration.members.update",
            {"group_id": "group-a", "employee_ids": ["employee-a"]},
            "update_members",
            ("group-a",),
            {"employee_ids": ["employee-a"]},
        ),
    ],
)
def test_collaboration_mutations_succeed_from_attached_dashboard(
    owner_gateway, monkeypatch, method, params, service_method, expected_args, expected_kwargs
):
    _db, runtime, _workspace_root = owner_gateway
    calls = []

    class _Service:
        def __getattr__(self, name):
            assert name == service_method
            return lambda *args, **kwargs: calls.append((args, kwargs)) or {"ok": True}

    transport = _CollaborationTransport()
    monkeypatch.setattr(server, "_required_gateway_transport", lambda: transport)
    server.bind_collaboration_service(runtime, _Service())
    response = server.dispatch(
        {"id": "request", "method": method, "params": params},
        transport=transport,
        runtime=runtime,
    )

    assert response["result"] == {"ok": True}
    assert calls == [(expected_args, expected_kwargs)]
    assert transport in runtime.mutable_state.collaboration_transports


def test_collaboration_rpc_rejects_interactive_socket_without_dashboard_scope(owner_gateway):
    _db, runtime, _workspace_root = owner_gateway

    class _Service:
        def list_groups(self, **_kwargs):
            pytest.fail("unattached interactive collaboration RPC must not reach service")

    transport = _CollaborationTransport()
    transport.attached = False
    server.bind_collaboration_service(runtime, _Service())
    response = server.dispatch(
        {"id": "request", "method": "collaboration.groups.list", "params": {}},
        transport=transport,
        runtime=runtime,
    )

    assert response["error"] == {
        "code": 4092,
        "message": "dashboard mutation requires an active session",
    }
    assert transport not in runtime.mutable_state.collaboration_transports


def test_collaboration_mutation_uses_dashboard_owner_authorization(owner_gateway):
    _db, runtime, _workspace_root = owner_gateway

    class _Service:
        def archive_group(self, _group_id):
            pytest.fail("unauthorized collaboration mutation must not reach service")

    server.bind_collaboration_service(runtime, _Service())
    response = server.dispatch(
        {
            "id": "request",
            "method": "collaboration.group.archive",
            "params": {"group_id": "group-a"},
        },
        transport=_CollaborationTransport("dashboard session switch in progress"),
        runtime=runtime,
    )

    assert response["error"] == {
        "code": 4092,
        "message": "dashboard session switch in progress",
    }


@pytest.mark.parametrize(
    ("method", "params"),
    [
        ("collaboration.groups.list", {}),
        ("collaboration.group.get", {"group_id": "group-a"}),
        (
            "collaboration.group.create",
            {"name": "Group", "employee_ids": [], "client_idempotency_key": "create-a"},
        ),
        ("collaboration.group.archive", {"group_id": "group-a"}),
        ("collaboration.members.update", {"group_id": "group-a", "employee_ids": []}),
    ],
)
def test_browser_collaboration_rpcs_reject_retained_channel(
    owner_gateway, method, params
):
    _db, runtime, _workspace_root = owner_gateway

    class _Service:
        def __getattr__(self, _name):
            return lambda *_args, **_kwargs: pytest.fail(
                "retained collaboration RPC must not reach service"
            )

    transport = _CollaborationTransport(purpose="retained-channel")
    server.bind_collaboration_service(runtime, _Service())
    response = server.dispatch(
        {"id": "request", "method": method, "params": params},
        transport=transport,
        runtime=runtime,
    )

    assert response["error"] == {
        "code": 4092,
        "message": "browser collaboration requires an interactive connection",
    }
    assert transport not in runtime.mutable_state.collaboration_transports


def test_collaboration_group_get_forwards_incremental_sequence(owner_gateway):
    _db, runtime, _workspace_root = owner_gateway

    class _Service:
        def get_group(self, group_id, *, after_sequence=None):
            assert group_id == "group-a"
            assert after_sequence == 7
            return {"events": [], "reconciliation": {"after_sequence": 7}}

    server.bind_collaboration_service(runtime, _Service())
    response = server.dispatch(
        {
            "id": "request",
            "method": "collaboration.group.get",
            "params": {"group_id": "group-a", "after_sequence": 7},
        },
        transport=_CollaborationTransport(),
        runtime=runtime,
    )

    assert response["result"]["reconciliation"]["after_sequence"] == 7


def test_collaboration_byte_upload_and_approval_rpc(owner_gateway):
    _db, runtime, _workspace_root = owner_gateway
    calls = []

    class _Service:
        def attach(self, group_id, **kwargs):
            calls.append(("attach", group_id, kwargs))
            return {"attachment": {"attachment_id": "attachment-a"}}

        def respond_approval(self, approval_id, choice):
            calls.append(("approval", approval_id, choice))
            return {"approval": {"approval_id": approval_id, "status": "approved"}}

    server.bind_collaboration_service(runtime, _Service())
    content = base64.b64encode(b"hello").decode("ascii")
    upload = server.dispatch(
        {
            "id": "request",
            "method": "collaboration.file.attach",
            "params": {
                "group_id": "group-a",
                "filename": "note.txt",
                "content_base64": content,
                "media_type": "text/plain",
            },
        },
        transport=_CollaborationTransport(),
        runtime=runtime,
    )
    approval = server.dispatch(
        {
            "id": "request",
            "method": "collaboration.approval.respond",
            "params": {"approval_id": "approval-a", "choice": "once"},
        },
        transport=_CollaborationTransport(),
        runtime=runtime,
    )

    assert upload["result"]["attachment"]["attachment_id"] == "attachment-a"
    assert approval["result"]["approval"]["status"] == "approved"
    assert calls == [
        (
            "attach",
            "group-a",
            {
                "kind": "file",
                "filename": "note.txt",
                "content_base64": content,
                "media_type": "text/plain",
            },
        ),
        ("approval", "approval-a", "once"),
    ]


def test_collaboration_browser_methods_reject_policy_input(owner_gateway):
    _db, runtime, _workspace_root = owner_gateway

    class _Service:
        def get_group(self, *_args, **_kwargs):
            pytest.fail("policy-bearing collaboration input must fail before service")

    server.bind_collaboration_service(runtime, _Service())
    response = server.dispatch(
        {
            "id": "request",
            "method": "collaboration.group.get",
            "params": {
                "group_id": "group-a",
                "employee_policy": {"system_prompt": "unsafe"},
            },
        },
        transport=_CollaborationTransport(),
        runtime=runtime,
    )

    assert response["error"] == {
        "code": -32602,
        "message": "collaboration group get params are invalid",
    }


def test_collaboration_message_rejects_browser_attachment_paths(owner_gateway):
    _db, runtime, _workspace_root = owner_gateway

    class _Service:
        def submit_message(self, *_args, **_kwargs):
            pytest.fail("browser attachment input must fail before service")

    server.bind_collaboration_service(runtime, _Service())
    response = server.dispatch(
        {
            "id": "request",
            "method": "collaboration.message.submit",
            "params": {
                "group_id": "group-a",
                "text": "hello",
                "attachments": [{"source_path": "/tmp/unsafe"}],
            },
        },
        transport=_CollaborationTransport(),
        runtime=runtime,
    )

    assert response["error"] == {
        "code": -32602,
        "message": "collaboration message params are invalid",
    }
