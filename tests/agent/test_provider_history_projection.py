from __future__ import annotations

from copy import deepcopy

from agent.message_sanitization import project_provider_history


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
    messages.extend(
        [
            {"role": "user", "content": "padding"},
            {"role": "assistant", "content": "ack"},
            {"role": "user", "content": "current"},
        ]
    )
    return messages


def test_recent_window_and_current_turn_are_unchanged_by_identity():
    messages = _old_episode()

    projected = project_provider_history(
        messages, current_turn_index=len(messages) - 1, protect_last_n=len(messages)
    )

    assert projected is messages


def test_completed_old_episode_becomes_plain_assistant_summary():
    messages = _old_episode()
    original = deepcopy(messages)

    projected = project_provider_history(
        messages, current_turn_index=len(messages) - 1, protect_last_n=2
    )

    assert messages == original
    assert [message["role"] for message in projected] == [
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
    ]
    summary = projected[1]
    assert summary["content"].startswith("[Historical tool episode summary]")
    assert "tool=read_file" in summary["content"]
    assert "done" in summary["content"]
    assert "tool_calls" not in summary
    assert "reasoning_details" not in summary
    assert not any(message.get("role") == "tool" for message in projected)


def test_parallel_calls_keep_original_order_and_media_error_flags():
    calls = [_call("c1", "read_file"), _call("c2", "vision_analyze")]
    results = [
        {"role": "tool", "tool_call_id": "c2", "content": [
            {"type": "text", "text": "found object"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
        ]},
        {"role": "tool", "tool_call_id": "c1", "content": "failed", "is_error": True},
    ]
    messages = _old_episode(calls=calls, results=results)

    projected = project_provider_history(
        messages, current_turn_index=len(messages) - 1, protect_last_n=2
    )

    content = projected[1]["content"]
    assert content.index("tool=read_file") < content.index("tool=vision_analyze")
    assert "flags=error" in content
    assert "flags=media_omitted" in content
    assert "data:image" not in content


def test_consecutive_tool_rounds_collapse_atomically():
    messages = [
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": None, "tool_calls": [_call("c1")]},
        {"role": "tool", "tool_call_id": "c1", "content": "one"},
        {"role": "assistant", "content": None, "tool_calls": [_call("c2", "search_files")]},
        {"role": "tool", "tool_call_id": "c2", "content": "two"},
        {"role": "assistant", "content": "finished"},
        {"role": "user", "content": "padding"},
        {"role": "assistant", "content": "ack"},
        {"role": "user", "content": "current"},
    ]

    projected = project_provider_history(messages, current_turn_index=8, protect_last_n=2)

    assert len(projected) == 5
    assert projected[1]["content"].count("- tool=") == 2
    assert projected[1]["content"].endswith("finished")


def test_missing_duplicate_and_unknown_results_leave_episode_unchanged():
    cases = [
        [_call("c1"), _call("c2")],
        [_call("c1")],
        [_call("c1")],
    ]
    results = [
        [{"role": "tool", "tool_call_id": "c1", "content": "one"}],
        [
            {"role": "tool", "tool_call_id": "c1", "content": "one"},
            {"role": "tool", "tool_call_id": "c1", "content": "again"},
        ],
        [{"role": "tool", "tool_call_id": "unknown", "content": "one"}],
    ]
    for calls, result_rows in zip(cases, results):
        messages = _old_episode(calls=calls, results=result_rows)
        projected = project_provider_history(
            messages, current_turn_index=len(messages) - 1, protect_last_n=2
        )
        assert projected is messages


def test_episode_crossing_protected_boundary_is_unchanged():
    messages = _old_episode()

    projected = project_provider_history(
        messages, current_turn_index=len(messages) - 1, protect_last_n=4
    )

    assert projected is messages


def test_boundary_is_stable_when_current_tool_rows_are_appended():
    messages = _old_episode()
    current_index = len(messages) - 1
    first = project_provider_history(messages, current_turn_index=current_index, protect_last_n=2)
    messages.extend(
        [
            {"role": "assistant", "content": None, "tool_calls": [_call("live")]},
            {"role": "tool", "tool_call_id": "live", "content": "live result"},
        ]
    )

    second = project_provider_history(
        messages, current_turn_index=current_index, protect_last_n=2
    )

    assert second[: len(first)] == first
    assert second[-2:] == messages[-2:]


def test_old_multimodal_payloads_become_receipts_but_recent_payloads_remain():
    old_image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,OLD"}}
    current_image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,NEW"}}
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "old"}, old_image]},
        {"role": "assistant", "content": "seen"},
        {"role": "user", "content": "padding"},
        {"role": "assistant", "content": "ack"},
        {"role": "user", "content": [{"type": "text", "text": "current"}, current_image]},
    ]

    projected = project_provider_history(messages, current_turn_index=4, protect_last_n=2)

    assert "payload omitted" in str(projected[0]["content"])
    assert "OLD" not in str(projected)
    assert "NEW" in str(projected[4]["content"])
    assert project_provider_history(projected, current_turn_index=4, protect_last_n=2) is projected
