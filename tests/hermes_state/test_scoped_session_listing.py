import sqlite3
import time

import pytest
from starlette.exceptions import HTTPException

from hermes_cli.session_api import (
    delete_session_payload,
    latest_descendant_payload,
    list_sessions_payload,
    rename_session_payload,
    session_detail_payload,
    session_messages_payload,
)
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


def test_listing_uses_bounded_sql_for_page_and_display_counts(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    try:
        for index in range(30):
            session_id = f"session-{index}"
            db.create_session(session_id, source="gui")
            db.append_message(session_id, "user", f"message {index}")

        statements = []
        db._conn.set_trace_callback(statements.append)
        payload = list_sessions_payload(db, limit=20, order="recent")
        db._conn.set_trace_callback(None)

        assert len(payload["sessions"]) == 20
        selects = [sql for sql in statements if sql.lstrip().upper().startswith(("SELECT", "WITH"))]
        assert len(selects) <= 6
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
    from hermes_cli.session_reader.performance_contract import (
        STANDARDS,
        expected_latest_session_id,
        populate_large_session_history,
    )

    db = SessionDB(tmp_path / "state.db")
    try:
        populate_large_session_history(db)
        query_plan = [
            row[3]
            for row in db._conn.execute(
                "EXPLAIN QUERY PLAN "
                "SELECT child.id FROM sessions parent "
                "JOIN sessions child INDEXED BY idx_sessions_parent "
                "ON child.parent_session_id = parent.id "
                "WHERE parent.end_reason = 'compression'"
            ).fetchall()
        ]
        assert any("idx_sessions_parent" in detail for detail in query_plan)

        started = time.perf_counter()
        payload = list_sessions_payload(
            db,
            limit=30,
            order="recent",
            compact=True,
        )
        elapsed = time.perf_counter() - started

        assert payload["total"] == STANDARDS.visible_sessions
        assert len(payload["sessions"]) == STANDARDS.page_size
        assert payload["sessions"][0]["id"] == expected_latest_session_id()
        assert elapsed * 1000 < STANDARDS.local_list_max_ms
    finally:
        db.close()


def test_internal_collaboration_sessions_are_hidden_from_ordinary_session_api(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    try:
        db.create_session("ordinary", source="gui")
        db.append_message("ordinary", "user", "visible")
        db.create_session(
            "collaboration-internal",
            source="gui",
            session_kind="internal_collaboration_member",
            visibility="internal",
        )
        db.append_message("collaboration-internal", "user", "private")
        db.create_session(
            "collaboration-empty",
            source="gui",
            session_kind="internal_collaboration_coordinator",
            visibility="internal",
        )
        db.end_session("collaboration-empty", "completed")
        with pytest.raises(ValueError, match="inconsistent"):
            db.create_session(
                "internal-visible",
                source="gui",
                session_kind="internal_collaboration_member",
                visibility="visible",
            )
        with pytest.raises(ValueError, match="inconsistent"):
            db.create_session(
                "ordinary-internal",
                source="gui",
                session_kind="conversation",
                visibility="internal",
            )
        with pytest.raises(sqlite3.IntegrityError, match="inconsistent"):
            db._conn.execute(
                "UPDATE sessions SET visibility='visible' WHERE id=?",
                ("collaboration-internal",),
            )

        payload = list_sessions_payload(db, archived="include")
        assert [session["id"] for session in payload["sessions"]] == ["ordinary"]
        assert payload["total"] == 1
        assert db.resolve_session_id("collaboration-internal") is None
        assert db.resolve_resume_session_id("collaboration-internal") == ""
        assert db.find_resume_recovery_scope("collaboration-internal") is None
        assert db.get_session_for_recovery("collaboration-internal") is None
        assert db.search_messages("private") == []

        for operation in (
            lambda: session_detail_payload(db, "collaboration-internal"),
            lambda: session_messages_payload(db, "collaboration-internal"),
            lambda: latest_descendant_payload(db, "collaboration-internal"),
            lambda: rename_session_payload(
                db, "collaboration-internal", title="should-not-change"
            ),
        ):
            with pytest.raises(HTTPException) as exc:
                operation()
            assert exc.value.status_code == 404
        assert delete_session_payload(db, "collaboration-internal") == {
            "ok": True,
            "already_absent": True,
        }
        assert db.count_empty_sessions() == 0
        assert db.delete_empty_sessions() == 0
        assert db.get_session("collaboration-empty")["session_kind"] == (
            "internal_collaboration_coordinator"
        )
        assert db.get_session("collaboration-internal")["session_kind"] == (
            "internal_collaboration_member"
        )
        assert db.get_session("collaboration-internal")["title"] is None
        with pytest.raises(RuntimeError, match="mismatch"):
            db.ensure_session("collaboration-internal", source="unknown")
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
