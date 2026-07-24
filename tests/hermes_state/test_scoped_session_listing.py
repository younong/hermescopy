import time

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


def test_compact_listing_skips_exact_display_counts(tmp_path, monkeypatch):
    db = SessionDB(tmp_path / "state.db")
    try:
        db.create_session(
            "compact",
            source="gui",
            model="test-model",
            model_config={"large": "x" * 10_000},
            system_prompt="secret prompt material",
        )
        db.append_message("compact", "user", "hello")

        def fail_display_count(*args, **kwargs):
            raise AssertionError("compact listing must not calculate exact display counts")

        monkeypatch.setattr(db, "display_message_count", fail_display_count)

        payload = list_sessions_payload(db, order="recent", compact=True)

        assert [session["id"] for session in payload["sessions"]] == ["compact"]
        session = payload["sessions"][0]
        assert session["message_count"] == 1
        assert session["model"] == "test-model"
        assert "system_prompt" not in session
        assert "model_config" not in session
    finally:
        db.close()


def test_rich_listing_keeps_lineage_aware_display_count(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    try:
        db.create_session("root", source="gui")
        db.append_message("root", "user", "before compression")
        db.end_session("root", "compression")
        db.create_session("tip", source="gui", parent_session_id="root")
        db.append_message("tip", "assistant", "after compression")

        payload = list_sessions_payload(db, order="recent")

        assert [session["id"] for session in payload["sessions"]] == ["tip"]
        assert payload["sessions"][0]["message_count"] == 2
    finally:
        db.close()


def test_compact_recent_listing_stays_below_300ms_with_compression_chains(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    try:
        base = 1_700_000_000.0
        sessions = []
        messages = []
        message_id = 1
        for index in range(3_000):
            chain_length = 3 if index % 10 == 0 else 1
            parent_id = None
            for chain_index in range(chain_length):
                session_id = (
                    f"session-{index}-root"
                    if chain_index == 0
                    else f"session-{index}-tip-{chain_index}"
                )
                started_at = base + index * 10 + chain_index
                compressed = chain_index < chain_length - 1
                sessions.append(
                    (
                        session_id,
                        "gui",
                        parent_id,
                        started_at,
                        started_at + 0.5 if compressed else None,
                        "compression" if compressed else None,
                        3,
                        0,
                    )
                )
                for message_index in range(3):
                    messages.append(
                        (
                            message_id,
                            session_id,
                            "user" if message_index == 0 else "assistant",
                            f"message {index} {chain_index} {message_index}",
                            started_at + message_index / 10,
                        )
                    )
                    message_id += 1
                parent_id = session_id
        db._conn.executemany(
            """INSERT INTO sessions (
                   id, source, parent_session_id, started_at, ended_at,
                   end_reason, message_count, archived
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            sessions,
        )
        db._conn.executemany(
            """INSERT INTO messages (id, session_id, role, content, timestamp)
               VALUES (?, ?, ?, ?, ?)""",
            messages,
        )
        db._conn.commit()

        started = time.perf_counter()
        payload = list_sessions_payload(
            db,
            limit=30,
            order="recent",
            compact=True,
        )
        elapsed = time.perf_counter() - started

        assert payload["total"] == 3_000
        assert len(payload["sessions"]) == 30
        assert payload["sessions"][0]["id"] == "session-2999-root"
        assert elapsed < 0.3
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
