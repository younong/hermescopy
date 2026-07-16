from hermes_cli.session_api import list_sessions_payload
from hermes_state import SessionDB


def _historical_scope():
    return {
        "owner_key": "ok1_owner",
        "workspace_root": "/workspace/owner",
        "worker_generation": 9,
        "historical_resume": True,
    }


def test_historical_scope_lists_only_resumable_owner_history(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    scope = _historical_scope()
    try:
        db.create_session(
            "owned-root",
            source="tui",
            owner_key="ok1_owner",
            workspace_root="/workspace/owner",
            worker_generation=7,
        )
        db.append_message("owned-root", "user", "before compression")
        db.end_session("owned-root", "compression")
        db.create_session(
            "owned-tip",
            source="tui",
            parent_session_id="owned-root",
            owner_key="ok1_owner",
            workspace_root="/workspace/owner",
            worker_generation=8,
        )
        db.append_message("owned-tip", "assistant", "after compression")
        db.create_session(
            "foreign",
            source="tui",
            owner_key="ok1_other",
            workspace_root="/workspace/other",
            worker_generation=8,
        )
        db.append_message("foreign", "user", "private")
        db.create_session("legacy", source="tui")
        db.append_message("legacy", "user", "unattributed")

        payload = list_sessions_payload(db, order="recent", recovery_scope=scope)

        assert [session["id"] for session in payload["sessions"]] == ["owned-tip"]
        assert payload["total"] == 1
    finally:
        db.close()


def test_historical_scope_does_not_project_foreign_compression_child(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    scope = _historical_scope()
    try:
        db.create_session(
            "owned-root",
            source="tui",
            owner_key="ok1_owner",
            workspace_root="/workspace/owner",
            worker_generation=7,
        )
        db.append_message("owned-root", "user", "owner message")
        db.end_session("owned-root", "compression")
        db.create_session(
            "foreign-tip",
            source="tui",
            parent_session_id="owned-root",
            owner_key="ok1_other",
            workspace_root="/workspace/other",
            worker_generation=8,
        )
        db.append_message("foreign-tip", "assistant", "private continuation")

        payload = list_sessions_payload(db, order="recent", recovery_scope=scope)

        assert [session["id"] for session in payload["sessions"]] == ["owned-root"]
        assert payload["sessions"][0]["preview"] == "owner message"
        assert payload["total"] == 1
    finally:
        db.close()
