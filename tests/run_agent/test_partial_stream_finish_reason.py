"""Regression tests for issue #30963 — partial-stream stub finish_reason.

Pins the contract:

- text-only partial stream → stub.finish_reason == "length" so the
  conversation loop can identify the interrupted transport and finalize
  the assistant content already delivered to the user.
- partial mid-tool-call → stub.finish_reason == "length" so the loop
  triggers continuation machinery with targeted chunking guidance
  instead of ending the turn immediately.
- text-only network interruptions are logged and finalized from delivered
  assistant content without a synthetic continuation message.
- genuine output-length truncation and dropped-tool recovery retain targeted
  continuation prompts.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hermes_constants import PARTIAL_STREAM_STUB_ID, FINISH_REASON_LENGTH
from agent.conversation_loop import _get_continuation_prompt


# ── Helpers (mirrors test_streaming.py) ────────────────────────────────────

def _make_stream_chunk(content=None, tool_calls=None, finish_reason=None):
    delta = SimpleNamespace(
        content=content, tool_calls=tool_calls,
        reasoning_content=None, reasoning=None,
    )
    choice = SimpleNamespace(index=0, delta=delta, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], model=None, usage=None)


def _make_tool_call_delta(index=0, tc_id=None, name=None, arguments=None):
    func = SimpleNamespace(name=name, arguments=arguments)
    return SimpleNamespace(index=index, id=tc_id, function=func)


def _make_agent():
    from run_agent import AIAgent
    agent = AIAgent(
        api_key="test-key",
        base_url="https://example.com/v1",
        model="test/model",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )
    agent.api_mode = "chat_completions"
    agent._interrupt_requested = False
    return agent


# ── Stub finish_reason ────────────────────────────────────────────────────

class TestPartialStreamStubFinishReason:
    """The stub returned by interruptible_streaming_api_call when the
    upstream connection dies mid-flight."""

    @patch("run_agent.AIAgent._create_request_openai_client")
    @patch("run_agent.AIAgent._close_request_api_client")
    def test_text_only_partial_returns_length(self, _mock_close, mock_create, monkeypatch):
        """#30963: text-only partials must classify as length so the loop
        keeps continuing instead of exiting with budget remaining."""

        def _stalling_stream():
            yield _make_stream_chunk(content="Here's my answer so far")
            raise RuntimeError("simulated upstream stall")

        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = lambda *a, **kw: _stalling_stream()
        mock_create.return_value = mock_client

        agent = _make_agent()
        agent._current_streamed_assistant_text = "Here's my answer so far"

        monkeypatch.setenv("HERMES_STREAM_RETRIES", "0")
        response = agent._interruptible_streaming_api_call({})

        assert response.id == PARTIAL_STREAM_STUB_ID
        assert response.choices[0].finish_reason == FINISH_REASON_LENGTH, (
            "Text-only partial streams must use finish_reason=length so the "
            "conversation loop continues from where the network died "
            "(issue #30963)."
        )
        assert response.choices[0].message.content == "Here's my answer so far"
        assert response.choices[0].message.tool_calls is None

    @patch("run_agent.AIAgent._create_request_openai_client")
    @patch("run_agent.AIAgent._close_request_api_client")
    def test_partial_tool_call_uses_length(self, _mock_close, mock_create, monkeypatch):
        """Mid-tool-call partials now use finish_reason=length so the
        conversation loop's continuation machinery fires — bounded 3-retry
        with guidance to break output into smaller chunks (#31998).
        tool_calls=None is preserved, so no tool auto-executes."""

        def _stalling_stream():
            yield _make_stream_chunk(content="Let me write the audit: ")
            yield _make_stream_chunk(tool_calls=[
                _make_tool_call_delta(index=0, tc_id="call_1", name="write_file"),
            ])
            yield _make_stream_chunk(tool_calls=[
                _make_tool_call_delta(index=0, arguments='{"path": "/tmp/x", '),
            ])
            raise RuntimeError("simulated upstream stall")

        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = lambda *a, **kw: _stalling_stream()
        mock_create.return_value = mock_client

        agent = _make_agent()
        agent._fire_stream_delta = lambda text: None
        agent._current_streamed_assistant_text = "Let me write the audit: "

        monkeypatch.setenv("HERMES_STREAM_RETRIES", "0")
        response = agent._interruptible_streaming_api_call({})

        assert response.id == PARTIAL_STREAM_STUB_ID
        assert response.choices[0].finish_reason == FINISH_REASON_LENGTH, (
            "Partial mid-tool-call must use finish_reason=length so the "
            "continuation machinery fires instead of ending the turn "
            "immediately (#31998)."
        )
        assert response.choices[0].message.tool_calls is None, (
            "tool_calls must remain None (no auto-execution of side-effectful "
            "tool calls)."
        )
        # The stub should carry dropped tool names for continuation prompt
        assert getattr(response, "_dropped_tool_names", None) == ["write_file"]
        content = response.choices[0].message.content or ""
        assert "Stream stalled mid tool-call" in content
        assert "write_file" in content


# ── Clean stream-end without finish_reason ─────────────────────────────────

class TestCleanStreamEndText:
    """A text stream without a terminal finish_reason is an upstream drop."""

    @patch("run_agent.AIAgent._create_request_openai_client")
    @patch("run_agent.AIAgent._close_request_api_client")
    def test_no_finish_reason_text_routes_to_partial_stub(
        self, _mock_close, mock_create, monkeypatch,
    ):
        def _clean_ending_stream():
            yield _make_stream_chunk(content="Here's my answer so far")
            # The HTTP/SSE iterator ends without a terminal finish_reason.

        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = (
            lambda *a, **kw: _clean_ending_stream()
        )
        mock_create.return_value = mock_client

        agent = _make_agent()
        agent._current_streamed_assistant_text = "Here's my answer so far"
        monkeypatch.setenv("HERMES_STREAM_RETRIES", "0")

        response = agent._interruptible_streaming_api_call({})

        assert response.id == PARTIAL_STREAM_STUB_ID
        assert response.choices[0].finish_reason == FINISH_REASON_LENGTH
        assert response.choices[0].message.content == "Here's my answer so far"
        assert response.choices[0].message.tool_calls is None

class TestCleanStreamEndMidToolCall:
    """The upstream closes the SSE stream cleanly after delivering a tool
    name + the opening '{' of its arguments — NO exception, NO finish_reason,
    NO [DONE].  Observed live on NVIDIA Nemotron Ultra via the Nous dedicated
    endpoint: it stalls/drops during large tool-arg generation.

    The mock-builder must NOT stamp this as finish_reason='length' (which
    routes it through the max_tokens-boost truncation path and finally
    reports the misleading 'Response truncated due to output length limit').
    It must route through the partial-stream-stub path so the loop reports
    an honest mid-tool-call drop and asks the model to chunk its output.
    """

    @patch("run_agent.AIAgent._create_request_openai_client")
    @patch("run_agent.AIAgent._close_request_api_client")
    def test_no_finish_reason_partial_tool_args_routes_to_stub(
        self, _mock_close, mock_create, monkeypatch,
    ):
        def _clean_ending_stream():
            # Reasoning + tool name + the lone opening brace, then the
            # generator simply RETURNS (StopIteration) — no raise, no
            # finish_reason chunk, no [DONE].
            yield _make_stream_chunk(content="\n")
            yield _make_stream_chunk(tool_calls=[
                _make_tool_call_delta(index=0, tc_id="call_x", name="execute_code"),
            ])
            yield _make_stream_chunk(tool_calls=[
                _make_tool_call_delta(index=0, arguments="{"),
            ])
            # falls off the end — clean close, no terminator

        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = (
            lambda *a, **kw: _clean_ending_stream()
        )
        mock_create.return_value = mock_client

        agent = _make_agent()
        agent._fire_stream_delta = lambda text: None

        response = agent._interruptible_streaming_api_call({})

        assert response.id == PARTIAL_STREAM_STUB_ID, (
            "A clean stream-end mid tool-call (no finish_reason) must be "
            "tagged as a partial-stream stub, not a 'stream-<uuid>' "
            "truncation — otherwise the loop reports the false 'output "
            "length limit' error."
        )
        assert response.choices[0].finish_reason == FINISH_REASON_LENGTH
        assert response.choices[0].message.tool_calls is None, (
            "Incomplete tool args must never auto-execute."
        )
        assert getattr(response, "_dropped_tool_names", None) == ["execute_code"]

    @patch("run_agent.AIAgent._create_request_openai_client")
    @patch("run_agent.AIAgent._close_request_api_client")
    def test_real_length_truncation_still_uses_uuid_id(
        self, _mock_close, mock_create, monkeypatch,
    ):
        """Control: when the provider DOES send finish_reason='length' with
        partial tool args, it is a genuine output cap — keep the existing
        non-stub behaviour (boost max_tokens and retry)."""

        def _capped_stream():
            yield _make_stream_chunk(tool_calls=[
                _make_tool_call_delta(index=0, tc_id="call_y", name="execute_code"),
            ])
            yield _make_stream_chunk(tool_calls=[
                _make_tool_call_delta(index=0, arguments="{"),
            ])
            # Provider explicitly reports the output cap.
            yield _make_stream_chunk(finish_reason="length")

        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = (
            lambda *a, **kw: _capped_stream()
        )
        mock_create.return_value = mock_client

        agent = _make_agent()
        agent._fire_stream_delta = lambda text: None

        response = agent._interruptible_streaming_api_call({})

        assert response.id != PARTIAL_STREAM_STUB_ID, (
            "A provider-reported finish_reason='length' is a real output cap "
            "and must keep the existing truncation path, not the stream-drop "
            "stub path."
        )
        assert response.id.startswith("stream-")
        assert response.choices[0].finish_reason == FINISH_REASON_LENGTH


# ── Length-continuation prompt branching ──────────────────────────────────

class TestLengthContinuationPromptBranching:
    """Only genuine length caps and dropped-tool recovery prompt the model."""

    def test_real_truncation_uses_length_prompt(self):
        prompt = _get_continuation_prompt()
        assert "output length limit" in prompt
        assert "network error" not in prompt

    def test_dropped_tool_call_uses_truthful_incremental_prompt(self):
        """Dropped tool calls retain the latest targeted retry guidance."""
        prompt = _get_continuation_prompt(["write_file"])
        assert "upstream response stream ended" in prompt
        assert "did not run" in prompt
        assert "does not establish any fixed token or payload-size limit" in prompt
        assert "exactly one small, self-contained tool call" in prompt
        assert "write_file" in prompt
        assert "8K" not in prompt
        assert "too large" not in prompt
        assert "output length limit" not in prompt

    def test_repeated_drop_requires_smaller_scope_not_full_rewrite(self):
        prompt = _get_continuation_prompt(["write_file"], retry_attempt=2)
        assert "happened again" in prompt
        assert "reduce the scope further" in prompt
        assert "do not substitute another full-payload rewrite" in prompt


# ── Integration: live conversation loop ───────────────────────────────────

@pytest.fixture()
def loop_agent():
    """AIAgent with a mocked OpenAI client for conversation-loop responses."""
    from run_agent import AIAgent
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        a = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
        a.client = MagicMock()
        a._cached_system_prompt = "You are helpful."
        a._use_prompt_caching = False
        a.tool_delay = 0
        a.compression_enabled = False
        a.save_trajectories = False
        return a


class TestConversationLoopPartialStreamContinuation:
    """Text-only network interruption stays out of model and session history."""

    def test_submitted_network_marker_is_ignored_before_model_call(
        self, loop_agent,
    ):
        marker = (
            "[System: The previous response was cut off by a network error mid-stream. "
            "Continue exactly where you left off. Do not restart or repeat prior text. "
            "Finish the answer directly.]"
        )
        history = [
            {"role": "user", "content": "original question"},
            {"role": "assistant", "content": "partial answer"},
        ]

        result = loop_agent.run_conversation(marker, conversation_history=history)

        loop_agent.client.chat.completions.create.assert_not_called()
        assert result["final_response"] is None
        assert result["messages"] == history
        assert result["turn_exit_reason"] == "ignored_internal_network_recovery_marker"

    def test_partial_stream_stub_is_logged_without_synthetic_message(
        self, loop_agent, caplog, tmp_path,
    ):
        from hermes_state import SessionDB
        from tests.run_agent.test_run_agent import _mock_assistant_msg

        session_db = SessionDB(db_path=tmp_path / "state.db")
        loop_agent._session_db = session_db
        loop_agent._session_db_created = False

        partial_stub = SimpleNamespace(
            id=PARTIAL_STREAM_STUB_ID,
            model="test/model",
            choices=[SimpleNamespace(
                index=0,
                message=_mock_assistant_msg(content="The delivered partial answer."),
                finish_reason=FINISH_REASON_LENGTH,
            )],
            usage=None,
        )
        loop_agent.client.chat.completions.create.return_value = partial_stub

        try:
            with (
                patch.object(loop_agent, "_save_trajectory"),
                patch.object(loop_agent, "_cleanup_task_resources"),
                caplog.at_level("WARNING", logger="agent.conversation_loop"),
            ):
                result = loop_agent.run_conversation("ask me something")
            persisted = session_db.get_messages_as_conversation(loop_agent.session_id)
        finally:
            session_db.close()

        assert loop_agent.client.chat.completions.create.call_count == 1
        assert result["final_response"] == "The delivered partial answer."
        assert result["turn_exit_reason"] == "text_response(partial_stream_network_error)"
        assert result["response_previewed"] is True
        assert [message["role"] for message in result["messages"]] == [
            "user", "assistant",
        ]
        assert result["messages"][-1]["content"] == "The delivered partial answer."
        assert result["messages"][-1]["finish_reason"] == FINISH_REASON_LENGTH
        assert [message["role"] for message in persisted] == ["user", "assistant"]
        assert [message["content"] for message in persisted] == [
            "ask me something", "The delivered partial answer.",
        ]
        assert "keeping the delivered assistant content" in caplog.text


class TestContentFilterStallActivatesFallback:
    """Regression for #32421: a provider output-layer content safety filter
    (e.g. MiniMax ``output new_sensitive (1027)``) terminates a streaming
    response mid-delivery.  The raw error is swallowed into a
    finish_reason=length partial-stream stub, so before the fix the loop
    burned 3 continuation retries against the SAME primary (re-hitting the
    content-deterministic filter every time) and gave up with
    ``"Response remained truncated after 3 continuation attempts"`` — the
    configured fallback chain was never consulted.

    The fix has three layers:
      1. error_classifier classifies ``new_sensitive`` as
         ``content_policy_blocked``.
      2. interruptible_streaming_api_call runs the swallowed error through
         that classifier and stamps the stub ``_content_filter_terminated``.
      3. the conversation loop reads the tag and activates fallback BEFORE
         burning any continuation retries.
    """

    @patch("run_agent.AIAgent._create_request_openai_client")
    @patch("run_agent.AIAgent._close_request_api_client")
    def test_streaming_call_tags_content_filter_stub(
        self, _mock_close, mock_create, monkeypatch,
    ):
        """Layer 2: the real streaming path stamps _content_filter_terminated
        when the swallowed error matches a content-filter pattern."""

        def _minimax_stall():
            yield _make_stream_chunk(content="Writing the file: ")
            yield _make_stream_chunk(tool_calls=[
                _make_tool_call_delta(index=0, tc_id="call_1", name="write_file"),
            ])
            yield _make_stream_chunk(tool_calls=[
                _make_tool_call_delta(index=0, arguments='{"path": "/tmp/x", '),
            ])
            raise RuntimeError("output new_sensitive (1027) [MiniMax-M2.7]")

        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = (
            lambda *a, **kw: _minimax_stall()
        )
        mock_create.return_value = mock_client

        agent = _make_agent()
        agent._fire_stream_delta = lambda text: None
        agent._current_streamed_assistant_text = "Writing the file: "

        monkeypatch.setenv("HERMES_STREAM_RETRIES", "0")
        response = agent._interruptible_streaming_api_call({})

        assert response.id == PARTIAL_STREAM_STUB_ID
        assert getattr(response, "_content_filter_terminated", False) is True, (
            "MiniMax new_sensitive stream stall must tag the stub so the loop "
            "can route to fallback (#32421)."
        )

    @patch("run_agent.AIAgent._create_request_openai_client")
    @patch("run_agent.AIAgent._close_request_api_client")
    def test_plain_network_stall_not_tagged(
        self, _mock_close, mock_create, monkeypatch,
    ):
        """A plain network stall (no content-filter signature) must NOT be
        tagged — it should still use the normal continuation path, not
        switch providers."""

        def _network_stall():
            yield _make_stream_chunk(content="Writing the file: ")
            raise RuntimeError("connection reset by peer")

        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = (
            lambda *a, **kw: _network_stall()
        )
        mock_create.return_value = mock_client

        agent = _make_agent()
        agent._fire_stream_delta = lambda text: None
        agent._current_streamed_assistant_text = "Writing the file: "

        monkeypatch.setenv("HERMES_STREAM_RETRIES", "0")
        response = agent._interruptible_streaming_api_call({})

        assert response.id == PARTIAL_STREAM_STUB_ID
        assert getattr(response, "_content_filter_terminated", False) is False, (
            "A plain network stall must not be misclassified as a content "
            "filter — that would needlessly switch providers."
        )

    def test_tagged_stub_activates_fallback_first_pass(self, loop_agent):
        """Layer 3: a tagged stub activates fallback on the FIRST pass, with
        zero continuation retries burned, and the fallback provider then
        completes the turn."""
        from tests.run_agent.test_run_agent import _mock_assistant_msg, _mock_response

        def _filter_stub():
            return SimpleNamespace(
                id=PARTIAL_STREAM_STUB_ID,
                model="minimax/MiniMax-M2.7",
                choices=[SimpleNamespace(
                    index=0,
                    message=_mock_assistant_msg(content="Writing the file..."),
                    finish_reason=FINISH_REASON_LENGTH,
                )],
                usage=None,
                _dropped_tool_names=["write_file"],
                _content_filter_terminated=True,
            )

        recovery = _mock_response(
            content="Done on the fallback provider.", finish_reason="stop",
        )
        loop_agent.client.chat.completions.create.side_effect = [
            _filter_stub(), recovery,
        ]
        loop_agent._fallback_chain = [
            {"provider": "openrouter", "model": "anthropic/claude-sonnet-4.7"},
        ]
        loop_agent._fallback_index = 0
        fb_calls = {"n": 0}

        def _fake_activate(reason=None):
            fb_calls["n"] += 1
            loop_agent._fallback_index = len(loop_agent._fallback_chain)
            return True

        with (
            patch.object(loop_agent, "_persist_session"),
            patch.object(loop_agent, "_save_trajectory"),
            patch.object(loop_agent, "_cleanup_task_resources"),
            patch.object(loop_agent, "_try_activate_fallback",
                         side_effect=_fake_activate),
        ):
            result = loop_agent.run_conversation("write me a long file")

        assert fb_calls["n"] == 1, (
            "Content-filter-tagged stub must activate fallback exactly once, "
            "on the first pass — not after exhausting continuation retries."
        )
        assert result["final_response"] == "Done on the fallback provider."
        assert result["completed"] is True

    def test_tagged_stub_no_fallback_falls_through(self, loop_agent):
        """When no fallback chain is configured, a tagged stub falls through
        to the normal continuation path (best-effort) rather than crashing."""
        from tests.run_agent.test_run_agent import _mock_assistant_msg, _mock_response

        def _filter_stub():
            return SimpleNamespace(
                id=PARTIAL_STREAM_STUB_ID,
                model="minimax/MiniMax-M2.7",
                choices=[SimpleNamespace(
                    index=0,
                    message=_mock_assistant_msg(content="partial "),
                    finish_reason=FINISH_REASON_LENGTH,
                )],
                usage=None,
                _dropped_tool_names=["write_file"],
                _content_filter_terminated=True,
            )

        recovery = _mock_response(content="recovered text", finish_reason="stop")
        loop_agent.client.chat.completions.create.side_effect = [
            _filter_stub(), recovery,
        ]
        # No fallback chain configured.
        loop_agent._fallback_chain = []
        loop_agent._fallback_index = 0
        fb_calls = {"n": 0}

        def _fake_activate(reason=None):
            fb_calls["n"] += 1
            return False

        with (
            patch.object(loop_agent, "_persist_session"),
            patch.object(loop_agent, "_save_trajectory"),
            patch.object(loop_agent, "_cleanup_task_resources"),
            patch.object(loop_agent, "_try_activate_fallback",
                         side_effect=_fake_activate),
        ):
            result = loop_agent.run_conversation("write me a long file")

        # Fallback was not attempted (empty chain gates it out); the loop
        # continued normally and produced a response.
        assert fb_calls["n"] == 0, (
            "With an empty fallback chain, the loop must not even call "
            "_try_activate_fallback — it should fall through to continuation."
        )
        assert result["completed"] is True
        continuation = _get_continuation_prompt(["write_file"])
        retry_messages = [
            message
            for request in loop_agent.client.chat.completions.create.call_args_list[1:]
            for message in request.kwargs["messages"]
        ]
        assert any(
            message.get("role") == "user"
            and message.get("content") == continuation
            for message in retry_messages
        )
        assert all(
            message.get("content") != continuation
            for message in result["messages"]
        )
