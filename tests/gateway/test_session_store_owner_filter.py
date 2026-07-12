import json
from datetime import datetime
from unittest.mock import patch

import pytest

from gateway.config import GatewayConfig, Platform, SessionResetPolicy
from gateway.session import SessionEntry, SessionStore


def _entry(session_key: str, session_id: str, *, owner_key: str | None = None) -> dict:
    entry = SessionEntry(
        session_key=session_key,
        session_id=session_id,
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
        owner_key=owner_key,
    ).to_dict()
    if owner_key is None:
        entry.pop("owner_key", None)
    return entry


def _load_entries(tmp_path, monkeypatch, *, owner_key: str | None, strict_scope: bool = False):
    if owner_key is None:
        monkeypatch.delenv("HERMES_OWNER_KEY", raising=False)
    else:
        monkeypatch.setenv("HERMES_OWNER_KEY", owner_key)
    if not strict_scope:
        monkeypatch.delenv("HERMES_WORKSPACE_ROOT", raising=False)
        monkeypatch.delenv("HERMES_WORKER_GENERATION", raising=False)
    config = GatewayConfig(default_reset_policy=SessionResetPolicy(mode="none"))
    with patch("hermes_state.SessionDB", side_effect=RuntimeError("no db")):
        store = SessionStore(sessions_dir=tmp_path, config=config)
    store._ensure_loaded()
    return store._entries


def test_owner_worker_loads_only_matching_sessions_json_entries(tmp_path, monkeypatch):
    data = {
        "mine": _entry("mine", "sid_mine", owner_key="ok1_mine"),
        "other": _entry("other", "sid_other", owner_key="ok1_other"),
        "legacy": _entry("legacy", "sid_legacy", owner_key=None),
    }
    (tmp_path / "sessions.json").write_text(json.dumps(data), encoding="utf-8")

    entries = _load_entries(tmp_path, monkeypatch, owner_key="ok1_mine")

    assert set(entries) == {"mine"}
    assert entries["mine"].session_id == "sid_mine"


def test_owner_worker_accepts_origin_owner_metadata(tmp_path, monkeypatch):
    entry = _entry("origin-owned", "sid_origin", owner_key=None)
    entry["origin"] = {
        "platform": "telegram",
        "chat_id": "chat-1",
        "chat_type": "dm",
        "owner_key": "ok1_mine",
    }
    (tmp_path / "sessions.json").write_text(json.dumps({"origin-owned": entry}), encoding="utf-8")

    entries = _load_entries(tmp_path, monkeypatch, owner_key="ok1_mine")

    assert set(entries) == {"origin-owned"}


def test_local_mode_keeps_legacy_sessions_json_entries(tmp_path, monkeypatch):
    data = {
        "mine": _entry("mine", "sid_mine", owner_key="ok1_mine"),
        "legacy": _entry("legacy", "sid_legacy", owner_key=None),
    }
    (tmp_path / "sessions.json").write_text(json.dumps(data), encoding="utf-8")

    entries = _load_entries(tmp_path, monkeypatch, owner_key=None)

    assert set(entries) == {"mine", "legacy"}


def test_owner_worker_accepts_matching_workspace_generation_assertions(tmp_path, monkeypatch):
    workspace = tmp_path / "workspaces"
    workspace.mkdir()
    entry = _entry("mine", "sid_mine", owner_key="ok1_mine")
    entry.update({"workspace_root": str(workspace), "worker_generation": 5})
    (tmp_path / "sessions.json").write_text(json.dumps({"mine": entry}), encoding="utf-8")
    monkeypatch.setenv("HERMES_OWNER_KEY", "ok1_mine")
    monkeypatch.setenv("HERMES_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("HERMES_WORKER_GENERATION", "5")

    entries = _load_entries(tmp_path, monkeypatch, owner_key="ok1_mine", strict_scope=True)

    assert set(entries) == {"mine"}


@pytest.mark.parametrize(
    ("entry_scope", "reason"),
    [
        ({}, "persisted_scope_assertion_missing"),
        ({"workspace_root": "", "worker_generation": 5}, "persisted_scope_assertion_missing"),
        ({"workspace_root": "not-a-generation", "worker_generation": "bad"}, "persisted_scope_assertion_invalid"),
    ],
)
def test_owner_worker_treats_missing_or_invalid_scope_as_absent_without_raw_audit(
    tmp_path, monkeypatch, entry_scope, reason
):
    workspace = tmp_path / "workspaces"
    workspace.mkdir()
    entry = _entry("mine", "sid_mine", owner_key="ok1_mine")
    entry.update(entry_scope)
    (tmp_path / "sessions.json").write_text(json.dumps({"mine": entry}), encoding="utf-8")
    events = []
    monkeypatch.setenv("HERMES_OWNER_KEY", "ok1_mine")
    monkeypatch.setenv("HERMES_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("HERMES_WORKER_GENERATION", "5")
    monkeypatch.setattr(
        "hermes_cli.dashboard_auth.audit.audit_authority",
        lambda event, **fields: events.append((event, fields)),
    )

    entries = _load_entries(tmp_path, monkeypatch, owner_key="ok1_mine", strict_scope=True)

    assert entries == {}
    assert events[0][0].value == "persisted_scope_rejected"
    assert events[0][1]["reason"] == reason
    assert "sid_mine" not in repr(events[0][1])
    assert str(workspace) not in repr(events[0][1])


def test_owner_worker_rejects_workspace_or_generation_mismatch_without_raw_audit(tmp_path, monkeypatch):
    workspace = tmp_path / "workspaces"
    workspace.mkdir()
    entry = _entry("mine", "sid_mine", owner_key="ok1_mine")
    entry.update({"workspace_root": str(workspace), "worker_generation": 4})
    (tmp_path / "sessions.json").write_text(json.dumps({"mine": entry}), encoding="utf-8")
    events = []
    monkeypatch.setenv("HERMES_OWNER_KEY", "ok1_mine")
    monkeypatch.setenv("HERMES_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("HERMES_WORKER_GENERATION", "5")
    monkeypatch.setattr(
        "hermes_cli.dashboard_auth.audit.audit_authority",
        lambda event, **fields: events.append((event, fields)),
    )

    entries = _load_entries(tmp_path, monkeypatch, owner_key="ok1_mine", strict_scope=True)

    assert entries == {}
    assert len(events) == 1
    event, fields = events[0]
    assert event.value == "persisted_scope_rejected"
    assert fields["reason"] == "persisted_scope_assertion_mismatch"
    assert fields["audience_class"] == "owner-persisted-scope"
    assert fields["worker_generation"] == 5
    assert str(workspace) not in repr(fields)
    assert "sid_mine" not in repr(fields)


def test_session_entry_writes_current_workspace_and_generation(monkeypatch, tmp_path):
    workspace = tmp_path / "workspaces"
    workspace.mkdir()
    monkeypatch.setenv("HERMES_OWNER_KEY", "ok1_mine")
    monkeypatch.setenv("HERMES_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("HERMES_WORKER_GENERATION", "5")

    entry = SessionEntry(
        session_key="mine",
        session_id="sid_mine",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    ).to_dict()

    assert entry["owner_key"] == "ok1_mine"
    assert entry["workspace_root"] == str(workspace.resolve())
    assert entry["worker_generation"] == 5
