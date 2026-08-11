from __future__ import annotations

from copy import deepcopy

from agent.message_sanitization import (
    _HISTORICAL_TOOL_RECEIPT_MAX_CHARS,
    project_provider_history,
)


def _call(call_id: str, name: str = "read_file", arguments: str = '{"path":"a.py"}'):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def _old_episode(*, calls=None, results=None, final="done"):
    calls = calls or [_call("c1")]
    results = results or [
        {"role": "tool", "tool_call_id": call["id"], "content": "result"}
        for call in calls
    ]
    messages = [
        {"role": "user", "content": "old question"},
        {
            "role": "assistant",
            "content": None,
            "reasoning_details": [{"signature": "secret"}],
            "tool_calls": calls,
        },
        *results,
    ]
    if final is not None:
        messages.append({"role": "assistant", "content": final})
    messages.append({"role": "user", "content": "current"})
    return messages


def test_ordinary_history_and_current_turn_are_unchanged_by_identity():
    messages = [
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "answer"},
        {"role": "user", "content": "current"},
    ]

    projected = project_provider_history(messages, current_turn_index=2)

    assert projected is messages


def test_completed_episode_immediately_before_current_turn_becomes_plain_summary():
    messages = _old_episode()
    original = deepcopy(messages)

    projected = project_provider_history(
        messages,
        current_turn_index=len(messages) - 1,
    )

    assert messages == original
    assert [message["role"] for message in projected] == [
        "user",
        "assistant",
        "user",
    ]
    summary = projected[1]
    assert summary == {
        "role": "assistant",
        "content": "[Historical tool work: 1 call.]\ndone",
    }
    assert "read_file" not in summary["content"]
    assert "c1" not in summary["content"]
    assert "a.py" not in summary["content"]
    assert "result" not in summary["content"]
    assert "tool_calls" not in summary
    assert "reasoning_details" not in summary
    assert not any(message.get("role") == "tool" for message in projected)


def test_parallel_calls_keep_only_aggregate_error_and_media_outcomes():
    calls = [_call("c1", "read_file"), _call("c2", "vision_analyze")]
    results = [
        {
            "role": "tool",
            "tool_call_id": "c2",
            "content": [
                {"type": "text", "text": "found object"},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,AAA"},
                },
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "failed", "is_error": True},
    ]
    messages = _old_episode(calls=calls, results=results, final=None)

    projected = project_provider_history(
        messages,
        current_turn_index=len(messages) - 1,
    )

    content = projected[1]["content"]
    assert content == "[Historical tool work: 2 calls, 1 error, media omitted.]"
    assert "read_file" not in content
    assert "vision_analyze" not in content
    assert "c1" not in content
    assert "found object" not in content
    assert "failed" not in content
    assert "data:image" not in content


def test_consecutive_tool_rounds_collapse_atomically():
    messages = [
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": None, "tool_calls": [_call("c1")]},
        {"role": "tool", "tool_call_id": "c1", "content": "one"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [_call("c2", "search_files")],
        },
        {"role": "tool", "tool_call_id": "c2", "content": "two"},
        {"role": "assistant", "content": "finished"},
        {"role": "user", "content": "current"},
    ]

    projected = project_provider_history(messages, current_turn_index=6)

    assert len(projected) == 3
    assert projected[1]["content"] == "[Historical tool work: 2 calls.]\nfinished"


def test_whole_episode_receipt_has_one_hard_cap():
    calls = [_call(f"c{i}", f"tool_{i}", '{"secret":"value"}') for i in range(20)]
    results = [
        {"role": "tool", "tool_call_id": call["id"], "content": "x" * 2000}
        for call in calls
    ]
    messages = _old_episode(calls=calls, results=results, final="summary " + "y" * 2000)

    projected = project_provider_history(
        messages,
        current_turn_index=len(messages) - 1,
    )

    content = projected[1]["content"]
    assert len(content) == _HISTORICAL_TOOL_RECEIPT_MAX_CHARS
    assert content.endswith("...")
    assert "secret" not in content
    assert "tool_" not in content
    assert "c19" not in content
    assert set(projected[1]) == {"role", "content"}


def test_malformed_and_incomplete_historical_protocol_remains_structural():
    cases = [
        (
            [_call("c1"), _call("c2")],
            [{"role": "tool", "tool_call_id": "c1", "content": "one"}],
        ),
        (
            [_call("c1")],
            [
                {"role": "tool", "tool_call_id": "c1", "content": "one"},
                {"role": "tool", "tool_call_id": "c1", "content": "again"},
            ],
        ),
        (
            [_call("c1")],
            [{"role": "tool", "tool_call_id": "unknown", "content": "one"}],
        ),
        (
            [_call("c1"), _call("c1")],
            [{"role": "tool", "tool_call_id": "c1", "content": "one"}],
        ),
        (
            [{"id": "c1", "type": "function", "function": {"arguments": "{}"}}],
            [{"role": "tool", "tool_call_id": "c1", "content": "one"}],
        ),
    ]
    for calls, result_rows in cases:
        messages = _old_episode(calls=calls, results=result_rows)
        projected = project_provider_history(
            messages,
            current_turn_index=len(messages) - 1,
        )
        assert projected is messages


def test_orphan_tool_row_remains_structural():
    messages = [
        {"role": "user", "content": "old"},
        {"role": "tool", "tool_call_id": "orphan", "content": "result"},
        {"role": "assistant", "content": "answer"},
        {"role": "user", "content": "current"},
    ]

    projected = project_provider_history(messages, current_turn_index=3)

    assert projected is messages


def test_episode_crossing_current_turn_boundary_is_unchanged():
    messages = [
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": None, "tool_calls": [_call("c1")]},
        {"role": "user", "content": "current"},
        {"role": "tool", "tool_call_id": "c1", "content": "late"},
    ]

    projected = project_provider_history(messages, current_turn_index=2)

    assert projected is messages


def test_boundary_is_stable_when_current_tool_rows_are_appended():
    messages = _old_episode()
    current_index = len(messages) - 1
    first = project_provider_history(messages, current_turn_index=current_index)
    messages.extend(
        [
            {
                "role": "assistant",
                "content": None,
                "reasoning_details": [{"signature": "live"}],
                "tool_calls": [_call("live")],
            },
            {"role": "tool", "tool_call_id": "live", "content": "live result"},
        ]
    )

    second = project_provider_history(messages, current_turn_index=current_index)

    assert second[: len(first)] == first
    assert second[-2] is messages[-2]
    assert second[-1] is messages[-1]


def test_all_historical_multimodal_payloads_become_receipts():
    old_image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,OLD"}}
    recent_image = {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,RECENT"},
    }
    current_image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,NEW"}}
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "old"}, old_image]},
        {"role": "assistant", "content": "seen"},
        {
            "role": "user",
            "content": [{"type": "text", "text": "recent"}, recent_image],
        },
        {"role": "assistant", "content": "ack"},
        {
            "role": "user",
            "content": [{"type": "text", "text": "current"}, current_image],
        },
    ]

    projected = project_provider_history(messages, current_turn_index=4)

    assert "payload omitted" in str(projected[0]["content"])
    assert "payload omitted" in str(projected[2]["content"])
    assert "OLD" not in str(projected)
    assert "RECENT" not in str(projected)
    assert "NEW" in str(projected[4]["content"])
    assert messages[0]["content"][1] is old_image
    assert messages[2]["content"][1] is recent_image
    assert project_provider_history(projected, current_turn_index=4) is projected


def test_current_turn_tool_image_remains_available_for_next_iteration():
    historical_image = {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,OLD"},
    }
    current_image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,NEW"}}
    tool_image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,TOOL"}}
    messages = [
        {
            "role": "user",
            "content": [{"type": "text", "text": "old"}, historical_image],
        },
        {"role": "assistant", "content": "seen"},
        {
            "role": "user",
            "content": [{"type": "text", "text": "current"}, current_image],
        },
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [_call("vision", "vision_analyze")],
        },
        {
            "role": "tool",
            "tool_call_id": "vision",
            "content": [{"type": "text", "text": "result"}, tool_image],
        },
    ]

    projected = project_provider_history(messages, current_turn_index=2)

    assert "OLD" not in str(projected)
    assert "NEW" in str(projected)
    assert "TOOL" in str(projected)
