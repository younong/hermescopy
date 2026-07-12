from __future__ import annotations

import pytest

from hermes_cli.authenticated_file_context import AuthenticatedWorkspaceContext
from hermes_cli.controlled_roots import RootKind, controlled_roots_for
from hermes_cli.owner_runtime import ensure_owner_runtime_dirs, owner_worker_runtime_paths
from tui_gateway import server


def _owner_env(monkeypatch, tmp_path):
    owner = tmp_path / "owner"
    root = owner / "workspaces"
    default = root / "default"
    default.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(owner))
    monkeypatch.setenv("HERMES_OWNER_KEY", "ok1_owner")
    monkeypatch.setenv("HERMES_WORKSPACE_ROOT", str(root))
    # Existing cwd tests exercise owner-only compatibility; strict durable
    # recovery tests explicitly opt into a trusted worker generation.
    monkeypatch.delenv("HERMES_WORKER_GENERATION", raising=False)
    monkeypatch.delenv("TERMINAL_CWD", raising=False)
    return owner, root, default


def _runtime_paths(tmp_path):
    owner_home = ensure_owner_runtime_dirs(tmp_path / "owner")
    return owner_worker_runtime_paths(owner_home=owner_home, worker_generation=1)


@pytest.fixture
def authenticated_runtime(monkeypatch, tmp_path):
    import hermes_cli.controlled_roots as controlled_roots

    monkeypatch.setattr(controlled_roots.sys, "platform", "linux")
    monkeypatch.setattr(controlled_roots, "_openat2", lambda *_args: None)
    roots = controlled_roots_for(_runtime_paths(tmp_path))
    runtime = server.OwnerWorkerGatewayRuntime(
        owner_key="owner-a",
        worker_generation=1,
        worker_id="worker-a",
        lease_version=1,
        recovery_generation=0,
        filesystem_context=AuthenticatedWorkspaceContext(roots),
    )
    token = server._gateway_runtime.set(runtime)
    try:
        yield roots
    finally:
        server._gateway_runtime.reset(token)
        roots.close()


def test_owner_completion_cwd_defaults_when_omitted(monkeypatch, tmp_path):
    _owner, _root, default = _owner_env(monkeypatch, tmp_path)

    assert server._completion_cwd({}) == str(default.resolve())


def test_authenticated_runtime_completion_cwd_uses_bound_capability(authenticated_runtime, monkeypatch, tmp_path):
    selected = authenticated_runtime.get(RootKind.WORKSPACE).canonical_path / "default"
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setenv("HERMES_WORKSPACE_ROOT", str(outside))
    monkeypatch.setenv("TERMINAL_CWD", str(outside))
    monkeypatch.setattr(server, "_profile_configured_cwd", lambda *_args: pytest.fail("profile cwd must not be read"))

    assert server._completion_cwd({}) == str(selected)
    for resolve in (
        lambda: server._completion_cwd({"cwd": str(outside)}),
        lambda: server._session_cwd({"cwd": str(outside)}),
        lambda: server._terminal_task_cwd({"cwd": str(outside)}),
    ):
        with pytest.raises(ValueError, match="selected authenticated workspace"):
            resolve()


def test_authenticated_runtime_rejects_conflicting_cwd(authenticated_runtime, tmp_path):
    sibling = authenticated_runtime.get(RootKind.WORKSPACE).canonical_path / "sibling"
    sibling.mkdir()

    with pytest.raises(ValueError, match="selected authenticated workspace"):
        server._set_session_cwd({"session_key": "session"}, str(sibling))


def test_authenticated_runtime_registers_only_selected_workspace(authenticated_runtime, monkeypatch):
    selected = str(authenticated_runtime.get(RootKind.WORKSPACE).canonical_path / "default")
    registered = []
    monkeypatch.setattr(server, "_register_session_cwd", lambda session: registered.append(session["cwd"]))
    monkeypatch.setattr(server, "_persist_session_git_meta", lambda *_args: None)
    session = {"session_key": "session"}

    assert server._set_session_cwd(session, selected) == selected
    assert session["cwd"] == selected
    assert registered == [selected]


def test_authenticated_project_callback_cannot_bypass_selected_workspace(authenticated_runtime, monkeypatch):
    selected = authenticated_runtime.get(RootKind.WORKSPACE).canonical_path / "default"
    sibling = authenticated_runtime.get(RootKind.WORKSPACE).canonical_path / "sibling"
    sibling.mkdir()
    session = {"session_key": "session", "cwd": str(selected)}
    server._sessions["sid"] = session
    monkeypatch.setattr(
        server,
        "_register_session_cwd",
        lambda *_args: pytest.fail("conflicting project path must not register a terminal cwd"),
    )
    try:
        server._apply_project_workspace("session", str(sibling))
        assert session["cwd"] == str(selected)
    finally:
        server._sessions.pop("sid", None)


def test_authenticated_project_callback_accepts_selected_workspace(authenticated_runtime, monkeypatch):
    selected = str(authenticated_runtime.get(RootKind.WORKSPACE).canonical_path / "default")
    session = {"session_key": "session"}
    registered = []
    emitted = []
    server._sessions["sid"] = session
    monkeypatch.setattr(server, "_register_session_cwd", lambda value: registered.append(value["cwd"]))
    monkeypatch.setattr(server, "_persist_session_git_meta", lambda *_args: None)
    monkeypatch.setattr(server, "_emit", lambda *args: emitted.append(args))
    monkeypatch.setattr(server, "_git_branch_for_cwd", lambda *_args: "")
    try:
        server._apply_project_workspace("session", selected)
        assert session["cwd"] == selected
        assert registered == [selected]
        assert emitted and emitted[0][2]["cwd"] == selected
    finally:
        server._sessions.pop("sid", None)


def test_authenticated_runtime_missing_capability_fails_closed():
    runtime = server.OwnerWorkerGatewayRuntime(
        owner_key="owner-a",
        worker_generation=1,
        worker_id="worker-a",
        lease_version=1,
        recovery_generation=0,
    )
    token = server._gateway_runtime.set(runtime)
    try:
        with pytest.raises(RuntimeError, match="filesystem capability"):
            server._completion_cwd({})
    finally:
        server._gateway_runtime.reset(token)


def test_owner_completion_cwd_allows_explicit_workspace_path(monkeypatch, tmp_path):
    _owner, root, _default = _owner_env(monkeypatch, tmp_path)
    project = root / "project"
    project.mkdir()

    assert server._completion_cwd({"cwd": str(project)}) == str(project.resolve())


def test_owner_completion_cwd_rejects_explicit_escape(monkeypatch, tmp_path):
    _owner, _root, _default = _owner_env(monkeypatch, tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()

    with pytest.raises(ValueError):
        server._completion_cwd({"cwd": str(outside)})


def test_owner_session_cwd_rejects_explicit_escape(monkeypatch, tmp_path):
    _owner, _root, _default = _owner_env(monkeypatch, tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()

    with pytest.raises(ValueError):
        server._session_cwd({"cwd": str(outside)})


def test_owner_terminal_cwd_env_escape_rejected(monkeypatch, tmp_path):
    _owner, _root, _default = _owner_env(monkeypatch, tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setenv("TERMINAL_CWD", str(outside))

    with pytest.raises(ValueError):
        server._completion_cwd({})


def test_owner_config_save_uses_runtime_home_not_import_home(monkeypatch, tmp_path):
    owner, _root, _default = _owner_env(monkeypatch, tmp_path)
    import_home = tmp_path / "import-home"
    import_home.mkdir()
    monkeypatch.setattr(server, "_hermes_home", import_home)
    monkeypatch.setattr(server, "_cfg_cache", None)
    monkeypatch.setattr(server, "_cfg_mtime", None)
    monkeypatch.setattr(server, "_cfg_path", None)

    server._write_config_key("display.tui_compact", True)

    assert (owner / "config.yaml").read_text(encoding="utf-8")
    assert not (import_home / "config.yaml").exists()


def test_owner_resume_ignores_terminal_cwd_escape_when_no_session_cwd(monkeypatch, tmp_path):
    _owner, _root, default = _owner_env(monkeypatch, tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setenv("TERMINAL_CWD", str(outside))
    monkeypatch.setattr(server, "_owner_worker_mode", lambda: True)
    monkeypatch.setattr(server, "_owner_default_cwd", lambda: str(default.resolve()))
    monkeypatch.setattr(server, "_profile_configured_cwd", lambda profile_home: None)
    monkeypatch.setattr(server, "_profile_home", lambda profile: None)
    monkeypatch.setattr(server, "_get_db", lambda: _FakeResumeDb({"id": "sess-1"}))
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(server, "_resolve_model", lambda: "test/model")
    monkeypatch.setattr(server, "_git_branch_for_cwd", lambda cwd: "")
    monkeypatch.setattr(server, "_claim_active_session_slot", lambda *args, **kwargs: (None, None))
    monkeypatch.setattr(server, "_claim_or_reuse_live", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "_schedule_agent_build", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "_schedule_session_cap_enforcement", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "_enable_gateway_prompts", lambda: None)

    response = server._methods["session.resume"]("1", {"session_id": "sess-1"})

    assert response["result"]["info"]["cwd"] == str(default.resolve())


def test_owner_resume_rejects_persisted_cwd_escape(monkeypatch, tmp_path):
    _owner, _root, _default = _owner_env(monkeypatch, tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setattr(server, "_owner_worker_mode", lambda: True)
    monkeypatch.setattr(server, "_profile_configured_cwd", lambda profile_home: None)
    monkeypatch.setattr(server, "_profile_home", lambda profile: None)
    monkeypatch.setattr(server, "_get_db", lambda: _FakeResumeDb({"id": "sess-1", "cwd": str(outside)}))
    monkeypatch.setattr(server, "_claim_active_session_slot", lambda *args, **kwargs: (None, None))
    monkeypatch.setattr(server, "_enable_gateway_prompts", lambda: None)

    with pytest.raises(ValueError):
        server._methods["session.resume"]("1", {"session_id": "sess-1"})


def test_owner_resume_rejects_foreign_scope_before_recovery_reads(monkeypatch, tmp_path):
    _owner, root, _default = _owner_env(monkeypatch, tmp_path)
    monkeypatch.setenv("HERMES_WORKER_GENERATION", "7")
    calls: list[str] = []

    class _ForeignDb:
        def find_resume_recovery_scope(self, selector):
            calls.append("scope")
            return {
                "id": "foreign-session",
                "owner_key": "ok1_other",
                "workspace_root": str(root),
                "worker_generation": 7,
            }

        def get_session_for_recovery(self, *args, **kwargs):
            calls.append("full")
            raise AssertionError("full row must not be read")

        def resolve_resume_session_id(self, *args, **kwargs):
            calls.append("lineage")
            raise AssertionError("lineage must not be read")

        def reopen_session(self, *args, **kwargs):
            calls.append("reopen")
            raise AssertionError("row must not be reopened")

        def get_messages_as_conversation(self, *args, **kwargs):
            calls.append("history")
            raise AssertionError("history must not be read")

    monkeypatch.setattr(server, "_get_db", lambda: _ForeignDb())
    monkeypatch.setattr(server, "_child_run_active", lambda _: (_ for _ in ()).throw(AssertionError("liveness read")))
    monkeypatch.setattr(server, "_find_live_session_by_key", lambda _: (_ for _ in ()).throw(AssertionError("live read")))

    response = server._methods["session.resume"]("1", {"session_id": "foreign-session"})

    assert response["error"]["code"] == 4007
    assert calls == ["scope"]


def test_owner_resume_accepts_matching_scope(monkeypatch, tmp_path):
    _owner, root, default = _owner_env(monkeypatch, tmp_path)
    monkeypatch.setenv("HERMES_WORKER_GENERATION", "7")

    class _ScopedDb:
        def find_resume_recovery_scope(self, selector):
            return {
                "id": "owned-session",
                "owner_key": "ok1_owner",
                "workspace_root": str(root.resolve()),
                "worker_generation": 7,
            }

        def get_session_for_recovery(self, session_id, *, recovery_scope):
            assert session_id == "owned-session"
            assert recovery_scope["worker_generation"] == 7
            return {
                "id": session_id,
                "owner_key": "ok1_owner",
                "workspace_root": str(root.resolve()),
                "worker_generation": 7,
            }

        def resolve_resume_session_id(self, session_id, *, recovery_scope):
            return session_id

        def reopen_session(self, session_id, *, recovery_scope):
            assert session_id == "owned-session"

        def get_messages_as_conversation(self, session_id, **kwargs):
            assert session_id == "owned-session"
            assert kwargs["recovery_scope"]["worker_generation"] == 7
            return []

    monkeypatch.setattr(server, "_get_db", lambda: _ScopedDb())
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(server, "_claim_active_session_slot", lambda *args, **kwargs: (None, None))
    monkeypatch.setattr(server, "_claim_or_reuse_live", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "_schedule_agent_build", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "_schedule_session_cap_enforcement", lambda: None)
    monkeypatch.setattr(server, "_enable_gateway_prompts", lambda: None)
    monkeypatch.setattr(server, "_profile_configured_cwd", lambda _: None)

    response = server._methods["session.resume"]("1", {"session_id": "owned-session"})

    assert response["result"]["resumed"] == "owned-session"
    assert response["result"]["info"]["cwd"] == str(default.resolve())


class _FakeResumeDb:
    def __init__(self, row):
        self.row = row

    def get_session(self, session_id):
        return dict(self.row) if session_id == self.row.get("id") else None

    def get_session_by_title(self, title):
        return None

    def resolve_resume_session_id(self, session_id):
        return session_id

    def reopen_session(self, session_id):
        return None

    def get_messages_as_conversation(self, session_id, include_ancestors=False):
        return []
