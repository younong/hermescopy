from __future__ import annotations

import pytest

from tui_gateway import server


def _owner_env(monkeypatch, tmp_path):
    owner = tmp_path / "owner"
    root = owner / "workspaces"
    default = root / "default"
    default.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(owner))
    monkeypatch.setenv("HERMES_OWNER_KEY", "ok1_owner")
    monkeypatch.setenv("HERMES_WORKSPACE_ROOT", str(root))
    monkeypatch.delenv("TERMINAL_CWD", raising=False)
    return owner, root, default


def test_owner_completion_cwd_defaults_when_omitted(monkeypatch, tmp_path):
    _owner, _root, default = _owner_env(monkeypatch, tmp_path)

    assert server._completion_cwd({}) == str(default.resolve())


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
