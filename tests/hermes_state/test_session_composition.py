import copy

import pytest
from starlette.exceptions import HTTPException

from agent.context_compressor import ContextCompressor
from agent.model_metadata import estimate_tokens_rough
from hermes_cli.session_api import list_sessions_payload, session_composition_payload
from hermes_state import SessionDB


def _message(role: str, index: int) -> dict:
    return {"role": role, "content": (f"message-{index} " * 500).strip()}


def test_compression_reconstruction_is_pure_and_uses_runtime_prompt_builder(monkeypatch):
    compressor = ContextCompressor(
        model="test", quiet_mode=True, config_context_length=32768,
        protect_first_n=1, protect_last_n=3, summary_target_ratio=0.10,
    )
    messages = [{"role": "system", "content": "system"}]
    messages.extend(_message("user" if index % 2 == 0 else "assistant", index) for index in range(20))
    original = copy.deepcopy(messages)

    def no_network(*args, **kwargs):
        raise AssertionError("analytics reconstruction must not access a provider")

    monkeypatch.setattr("agent.context_compressor.call_llm", no_network)
    reconstruction = compressor.reconstruct_next_compression_request(
        messages, today="2026-08-01"
    )

    assert reconstruction["availability"] == "available"
    assert reconstruction["serialized_turns"] in reconstruction["prompt"]
    assert reconstruction["serialized_components"]["user"]
    assert reconstruction["serialized_components"]["assistant"]
    assert "The current date is 2026-08-01" in reconstruction["prompt"]
    assert messages == original


def test_composition_counts_full_lineage_and_rejects_unknown_ids(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    try:
        db.create_session("root", source="gui", model="test", system_prompt="stored")
        db.append_message("root", "user", "before")
        db.append_message("root", "assistant", "answer")
        db.end_session("root", "compression")
        db.create_session("tip", source="gui", model="test", parent_session_id="root")
        db.append_message("tip", "user", "after")

        payload = session_composition_payload(db, ids=["tip"])

        assert payload["schema_version"] == 1
        assert payload["scope"]["canonical_root_ids"] == ["root"]
        assert payload["scope"]["canonical_tip_ids"] == ["tip"]
        exact = next(chart for chart in payload["charts"] if chart["id"] == "db_messages")
        assert exact["total"] == 3
        assert {segment["id"]: segment["value"] for segment in exact["segments"]} == {
            "user": 2,
            "assistant": 1,
            "tool": 0,
        }
        request = next(chart for chart in payload["charts"] if chart["id"] == "main_model_request")
        tools = next(segment for segment in request["segments"] if segment["id"] == "tool_definitions")
        assert tools["value"] is None
        assert tools["status"] == "unavailable"

        with pytest.raises(HTTPException) as exc:
            session_composition_payload(db, ids=["missing"])
        assert exc.value.status_code == 404
    finally:
        db.close()


@pytest.mark.parametrize(
    "ids, detail",
    [
        ([""], "blanks"),
        (["same", "same"], "duplicates"),
        (["x" * 257], "256"),
        ([str(index) for index in range(51)], "50"),
        ([f"{index:02d}" + "x" * 98 for index in range(42)], "4096"),
    ],
)
def test_composition_id_validation(tmp_path, ids, detail):
    db = SessionDB(tmp_path / "state.db")
    try:
        with pytest.raises(HTTPException) as exc:
            session_composition_payload(db, ids=ids)
        assert exc.value.status_code == 400
        assert detail in str(exc.value.detail)
    finally:
        db.close()


def test_effective_activity_bounds_filter_before_count_and_pagination(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    try:
        db.create_session("old-root", source="gui")
        db.append_message("old-root", "user", "old")
        db.end_session("old-root", "compression")
        db.create_session("recent-tip", source="gui", parent_session_id="old-root")
        db.append_message("recent-tip", "assistant", "recent")
        db.create_session("outside", source="gui")
        db.append_message("outside", "user", "outside")
        db._conn.execute("UPDATE sessions SET started_at = 10 WHERE id = 'old-root'")
        db._conn.execute("UPDATE sessions SET started_at = 20 WHERE id = 'recent-tip'")
        db._conn.execute("UPDATE sessions SET started_at = 40 WHERE id = 'outside'")
        db._conn.execute("UPDATE messages SET timestamp = 10 WHERE session_id = 'old-root'")
        db._conn.execute("UPDATE messages SET timestamp = 30 WHERE session_id = 'recent-tip'")
        db._conn.execute("UPDATE messages SET timestamp = 40 WHERE session_id = 'outside'")
        db._conn.commit()

        payload = list_sessions_payload(
            db, order="recent", active_from=30.0, active_before=40.0, limit=1
        )

        assert payload["total"] == 1
        assert [session["id"] for session in payload["sessions"]] == ["recent-tip"]
    finally:
        db.close()


def test_shared_text_estimator_keeps_ceiling_behavior():
    assert estimate_tokens_rough("") == 0
    assert estimate_tokens_rough("abcde") == 2
