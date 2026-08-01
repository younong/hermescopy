"""Regression tests for iterative context-summary continuity."""

from unittest.mock import MagicMock, patch

from agent.context_compressor import ContextCompressor, SUMMARY_PREFIX


def _compressor() -> ContextCompressor:
    with patch("agent.context_compressor.get_model_context_length", return_value=100000):
        return ContextCompressor(
            model="test/model",
            threshold_percent=0.85,
            protect_first_n=1,
            protect_last_n=1,
            quiet_mode=True,
        )


def _response(content: str):
    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = content
    return mock_response


def _messages_with_handoff(summary_body: str):
    return [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": f"{SUMMARY_PREFIX}\n{summary_body}"},
        {"role": "assistant", "content": "handoff acknowledged after resume"},
        {"role": "user", "content": "new user turn after resume"},
        {"role": "assistant", "content": "new assistant work after resume"},
        {"role": "user", "content": "more new work after resume"},
        {"role": "assistant", "content": "latest tail response"},
        {"role": "user", "content": "final active request stays in protected tail"},
    ]


def test_existing_previous_summary_is_not_serialized_again_as_new_turn():
    """Same-process iterative compression should not feed the old handoff twice."""
    compressor = _compressor()
    old_summary = "OLD-SUMMARY-BODY unique continuity facts"
    compressor._previous_summary = old_summary

    with patch("agent.context_compressor.call_llm", return_value=_response("updated summary")) as mock_call:
        compressor.compress(_messages_with_handoff(old_summary))

    prompt = mock_call.call_args.kwargs["messages"][0]["content"]
    assert "PREVIOUS SUMMARY:" in prompt
    assert "NEW TURNS TO INCORPORATE:" in prompt
    assert prompt.count(old_summary) == 1
    assert f"[USER]: {SUMMARY_PREFIX}" not in prompt


def test_resume_rehydrates_previous_summary_from_handoff_message():
    """After restart/resume, the persisted handoff should regain summary identity."""
    compressor = _compressor()
    old_summary = "RESUMED-SUMMARY-BODY durable continuity facts"
    assert compressor._previous_summary is None

    with patch("agent.context_compressor.call_llm", return_value=_response("updated summary")) as mock_call:
        compressor.compress(_messages_with_handoff(old_summary))

    prompt = mock_call.call_args.kwargs["messages"][0]["content"]
    assert "PREVIOUS SUMMARY:" in prompt
    assert "NEW TURNS TO INCORPORATE:" in prompt
    assert "TURNS TO SUMMARIZE:" not in prompt
    assert prompt.count(old_summary) == 1
    assert f"[USER]: {SUMMARY_PREFIX}" not in prompt


def test_handoff_in_protected_head_populates_previous_summary_before_update():
    """A resumed protected-head handoff should restore iterative-summary state."""
    compressor = _compressor()
    old_summary = "PROTECTED-HEAD-SUMMARY durable facts from before restart"
    seen_turns = []

    def fake_generate_summary(
        turns_to_summarize, focus_topic=None, deadline_monotonic=None, **_kwargs
    ):
        seen_turns.extend(turns_to_summarize)
        return "new summary from resumed turns"

    with patch.object(compressor, "_generate_summary", side_effect=fake_generate_summary):
        compressor.compress(_messages_with_handoff(old_summary))

    assert compressor._previous_summary == "new summary from resumed turns"
    assert seen_turns
    assert all(old_summary not in str(msg.get("content", "")) for msg in seen_turns)


def test_each_changed_transcript_starts_its_own_chunk_plan_from_the_beginning():
    """A completed prior chunk plan must never skip chunks from later turns."""
    compressor = _compressor()
    first_groups = [
        [{"role": "user", "content": "first transcript chunk one"}],
        [{"role": "assistant", "content": "first transcript chunk two"}],
    ]
    second_groups = [
        [{"role": "user", "content": "NEW SECOND TRANSCRIPT CONTENT"}],
    ]
    seen = []

    compressor._auxiliary_summary_capacity = MagicMock(
        return_value=MagicMock(input_budget=25, max_output_tokens=100)
    )
    compressor._atomic_summary_groups = MagicMock(
        side_effect=[first_groups, second_groups]
    )

    def fake_generate_summary(turns, **_kwargs):
        seen.append([message["content"] for message in turns])
        return f"summary {len(seen)}"

    compressor._generate_summary = MagicMock(side_effect=fake_generate_summary)

    first_messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "first request"},
        {"role": "assistant", "content": "first response"},
        {"role": "user", "content": "second request"},
        {"role": "assistant", "content": "second response"},
        {"role": "user", "content": "third request"},
        {"role": "assistant", "content": "third response"},
        {"role": "user", "content": "active tail request"},
    ]
    first_compacted = compressor.compress(first_messages)
    second_messages = [
        *first_compacted,
        {"role": "assistant", "content": "work after first compaction"},
        {"role": "user", "content": "new request after first compaction"},
        {"role": "assistant", "content": "new response after first compaction"},
        {"role": "user", "content": "another new request"},
        {"role": "assistant", "content": "another new response"},
        {"role": "user", "content": "latest active request"},
    ]
    compressor.compress(second_messages)

    assert seen == [
        ["first transcript chunk one"],
        ["first transcript chunk two"],
        ["NEW SECOND TRANSCRIPT CONTENT"],
    ]
