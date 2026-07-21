"""Focused tests for bounded, lineage-aware display history pages."""

import time

import pytest

from hermes_state import SessionDB


@pytest.fixture
def db(tmp_path):
    value = SessionDB(tmp_path / "state.db")
    yield value
    value.close()


def _scope(owner="ok1_owner", generation=1, historical=False):
    result = {
        "owner_key": owner,
        "workspace_root": "/workspace/owner",
        "worker_generation": generation,
    }
    if historical:
        result["historical_resume"] = True
    return result


def _compression_chain(db):
    root_scope = _scope(generation=1)
    tip_scope = _scope(generation=2)
    db.create_session("root", source="tui", **root_scope)
    db.append_message("root", role="user", content="root user")
    db.append_message("root", role="assistant", content="root answer")
    db.end_session("root", "compression")
    db.create_session("tip", source="tui", parent_session_id="root", **tip_scope)
    db.append_message("tip", role="user", content="tip user")
    db.append_message("tip", role="assistant", content="tip answer")
    base = time.time() - 100
    db._conn.execute(
        "UPDATE sessions SET started_at = ?, ended_at = ? WHERE id = 'root'",
        (base, base + 10),
    )
    db._conn.execute(
        "UPDATE sessions SET started_at = ? WHERE id = 'tip'", (base + 20,)
    )
    db._conn.commit()


def test_pages_backward_without_duplicates_or_omissions(db):
    db.create_session("s1", source="tui")
    for index in range(11):
        db.append_message("s1", role="user", content=f"message-{index}")

    cursor = None
    contents = []
    while True:
        page = db.get_conversation_page("s1", before_cursor=cursor, limit=3)
        contents = [message["content"] for message in page["messages"]] + contents
        if not page["has_more"]:
            break
        cursor = page["next_cursor"]

    assert contents == [f"message-{index}" for index in range(11)]


def test_cursor_snapshot_excludes_new_appends(db):
    db.create_session("s1", source="tui")
    for index in range(6):
        db.append_message("s1", role="user", content=f"old-{index}")

    newest = db.get_conversation_page("s1", limit=2)
    db.append_message("s1", role="assistant", content="new-after-snapshot")
    older = db.get_conversation_page(
        "s1", before_cursor=newest["next_cursor"], limit=10
    )

    assert "new-after-snapshot" not in [m["content"] for m in older["messages"]]
    assert [m["content"] for m in older["messages"]] == [
        "old-0",
        "old-1",
        "old-2",
        "old-3",
    ]


def test_historical_scope_pages_compression_lineage(db):
    _compression_chain(db)
    historical = _scope(generation=2, historical=True)

    page = db.get_conversation_page(
        "tip", limit=10, include_ancestors=True, recovery_scope=historical
    )

    assert [message["content"] for message in page["messages"]] == [
        "root user",
        "root answer",
        "tip user",
        "tip answer",
    ]
    assert all("_row_id" in message for message in page["messages"])


def test_cursor_rejected_for_other_session_or_scope(db):
    db.create_session("owned", source="tui", **_scope())
    db.create_session(
        "other",
        source="tui",
        owner_key="ok1_other",
        workspace_root="/workspace/other",
        worker_generation=1,
    )
    for index in range(3):
        db.append_message("owned", role="user", content=f"owned-{index}")
    db.append_message("other", role="user", content="private")
    cursor = db.get_conversation_page(
        "owned", limit=1, recovery_scope=_scope()
    )["next_cursor"]

    with pytest.raises(ValueError, match="does not match session"):
        db.get_conversation_page("other", before_cursor=cursor, limit=1)
    with pytest.raises(ValueError, match="recovery scope"):
        db.get_conversation_page(
            "owned", before_cursor=cursor, limit=1, recovery_scope=_scope("ok1_other")
        )


def test_cross_page_tool_result_recovers_tool_metadata(db):
    db.create_session("s1", source="tui")
    db.append_message(
        "s1",
        role="assistant",
        content="",
        tool_calls=[
            {
                "id": "call-1",
                "type": "function",
                "function": {"name": "search", "arguments": '{"q":"safe"}'},
            }
        ],
    )
    db.append_message(
        "s1", role="tool", content="result", tool_call_id="call-1"
    )

    page = db.get_conversation_page("s1", limit=1)

    assert len(page["messages"]) == 1
    assert page["messages"][0]["_display_tool_name"] == "search"
    assert page["messages"][0]["_display_tool_args"] == {"q": "safe"}


def test_malformed_cursor_rejected(db):
    db.create_session("s1", source="tui")
    with pytest.raises(ValueError, match="invalid conversation history cursor"):
        db.get_conversation_page("s1", before_cursor="not-a-cursor")


def test_page_clamps_fields_attachments_and_serialized_budget(db, monkeypatch):
    db.create_session("s1", source="tui")
    db.append_message(
        "s1",
        role="user",
        content="first-large-message",
        attachments=[{"name": str(index)} for index in range(5)],
    )
    db.append_message("s1", role="assistant", content="latest")
    monkeypatch.setattr(SessionDB, "_CONVERSATION_PAGE_MAX_TEXT_CHARS", 8)
    monkeypatch.setattr(SessionDB, "_CONVERSATION_PAGE_MAX_ATTACHMENTS", 2)
    monkeypatch.setattr(SessionDB, "_CONVERSATION_PAGE_MAX_SERIALIZED_BYTES", 180)

    page = db.get_conversation_page("s1", limit=2)

    assert page["messages"][-1]["content"] == "latest"
    assert page["has_more"] is True
    assert page["next_cursor"] is not None
    for message in page["messages"]:
        assert len(message.get("content") or "") <= 8
        assert len(message.get("attachments") or []) <= 2


def _contents(messages):
    return [message["content"] for message in messages]


def test_in_place_compaction_displays_originals_but_model_loads_projection(db):
    db.create_session("compact", source="tui")
    for role, content in [
        ("user", "original question"),
        ("assistant", "original answer"),
        ("user", "follow-up"),
        ("assistant", "follow-up answer"),
    ]:
        db.append_message("compact", role=role, content=content)

    db.archive_and_compact(
        "compact",
        [
            {"role": "user", "content": "[CONTEXT COMPACTION] summary"},
            {"role": "assistant", "content": "recent reply"},
        ],
    )

    assert _contents(db.get_messages_as_conversation("compact")) == [
        "[CONTEXT COMPACTION] summary",
        "recent reply",
    ]
    assert _contents(db.get_conversation_page("compact", limit=20)["messages"]) == [
        "original question",
        "original answer",
        "follow-up",
        "follow-up answer",
    ]
    assert db.display_message_count("compact") == 4


def test_repeated_compaction_hides_old_projections_and_keeps_new_turns(db):
    db.create_session("repeated", source="tui")
    db.append_message("repeated", role="user", content="original")
    db.append_message("repeated", role="assistant", content="answer")
    db.archive_and_compact(
        "repeated",
        [
            {"role": "user", "content": "summary-one"},
            {"role": "assistant", "content": "tail-copy"},
        ],
    )
    db.append_message("repeated", role="user", content="new question")
    db.append_message("repeated", role="assistant", content="new answer")
    db.archive_and_compact(
        "repeated",
        [
            {"role": "user", "content": "summary-two"},
            {"role": "assistant", "content": "latest-tail-copy"},
        ],
    )

    display = _contents(db.get_conversation_page("repeated", limit=20)["messages"])
    assert display == ["original", "answer", "new question", "new answer"]
    assert _contents(db.get_messages_as_conversation("repeated")) == [
        "summary-two",
        "latest-tail-copy",
    ]


def test_display_history_hides_rewound_rows_after_compaction(db):
    db.create_session("rewound", source="tui")
    first = db.append_message("rewound", role="user", content="keep")
    db.append_message("rewound", role="assistant", content="keep answer")
    removed = db.append_message("rewound", role="user", content="remove")
    db.append_message("rewound", role="assistant", content="remove answer")
    db.rewind_to_message("rewound", removed)
    db.archive_and_compact(
        "rewound",
        [
            {"role": "user", "content": "summary"},
            {"role": "assistant", "content": "tail"},
        ],
    )

    assert first > 0
    assert _contents(db.get_conversation_page("rewound", limit=20)["messages"]) == [
        "keep",
        "keep answer",
    ]


def test_display_history_pages_across_archived_and_active_rows(db):
    db.create_session("mixed-pages", source="tui")
    for index in range(6):
        db.append_message("mixed-pages", role="user", content=f"old-{index}")
    db.archive_and_compact(
        "mixed-pages",
        [
            {"role": "user", "content": "summary"},
            {"role": "assistant", "content": "tail"},
        ],
    )
    db.append_message("mixed-pages", role="user", content="new-user")
    db.append_message("mixed-pages", role="assistant", content="new-answer")

    cursor = None
    contents = []
    while True:
        page = db.get_conversation_page(
            "mixed-pages", before_cursor=cursor, limit=3
        )
        contents = _contents(page["messages"]) + contents
        if not page["has_more"]:
            break
        cursor = page["next_cursor"]

    assert contents == [
        "old-0", "old-1", "old-2", "old-3", "old-4", "old-5",
        "new-user", "new-answer",
    ]


def test_previous_cursor_version_is_rejected(db):
    db.create_session("cursor-version", source="tui")
    db.append_message("cursor-version", role="user", content="one")
    stale = db._encode_conversation_page_cursor(
        {"v": 1, "tip": "cursor-version", "lineage": "old", "snapshot": 1, "before": 1}
    )

    with pytest.raises(ValueError, match="unsupported conversation history cursor"):
        db.get_conversation_page("cursor-version", before_cursor=stale, limit=1)
