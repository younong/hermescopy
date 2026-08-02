"""Tests for payload/context-length → compression retry logic in AIAgent.

Verifies that:
- HTTP 413 errors trigger history compression and retry
- HTTP 400 context-length errors trigger compression (not generic 4xx abort)
- Prepared-request accounting proactively compresses oversized sessions before dispatch
"""

import pytest
#pytestmark = pytest.mark.skip(reason="Hangs in non-interactive environments")



from types import SimpleNamespace
from unittest.mock import MagicMock, patch


from agent.context_compressor import SUMMARY_PREFIX
from run_agent import AIAgent
import run_agent


# ---------------------------------------------------------------------------
# Fast backoff for compression retry tests
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_compression_sleep(monkeypatch):
    """Short-circuit the 2s time.sleep between compression retries.

    Production code has ``time.sleep(2)`` in multiple places after a 413/context
    compression, for rate-limit smoothing. Tests assert behavior, not timing.
    """
    import time as _time
    monkeypatch.setattr(_time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(run_agent, "jittered_backoff", lambda *a, **k: 0.0)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_tool_defs(*names: str) -> list:
    return [
        {
            "type": "function",
            "function": {
                "name": n,
                "description": f"{n} tool",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for n in names
    ]


def _mock_response(content="Hello", finish_reason="stop", tool_calls=None, usage=None):
    msg = SimpleNamespace(
        content=content,
        tool_calls=tool_calls,
        reasoning_content=None,
        reasoning=None,
    )
    choice = SimpleNamespace(message=msg, finish_reason=finish_reason)
    resp = SimpleNamespace(choices=[choice], model="test/model")
    resp.usage = SimpleNamespace(**usage) if usage else None
    return resp


def _make_413_error(*, use_status_code=True, message="Request entity too large"):
    """Create an exception that mimics a 413 HTTP error."""
    err = Exception(message)
    if use_status_code:
        err.status_code = 413
    return err


@pytest.fixture()
def agent():
    with (
        patch("run_agent.get_tool_definitions", return_value=_make_tool_defs("web_search")),
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
        # Default matches production (`compression.enabled` defaults to True).
        # Overflow-recovery tests below verify that 413 / context-overflow
        # errors DO trigger compression; the disabled-path behavior is
        # covered explicitly by TestOverflowWithCompactionDisabled.
        a.compression_enabled = True
        a.save_trajectories = False
        return a


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_current_user_turn_is_persisted_before_provider_call(agent):
    """The inbound user turn is flushed before provider/tool work can crash."""
    observed = []

    def _record_persist(messages, conversation_history):
        observed.append(("persist", list(messages), list(conversation_history or [])))

    def _provider_crash(*_args, **_kwargs):
        observed.append(("provider", [], []))
        raise RuntimeError("provider died after turn-start persistence")

    agent.client.chat.completions.create.side_effect = _provider_crash

    with (
        patch.object(agent, "_persist_session", side_effect=_record_persist),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation(
            "new message that must survive a crash",
            conversation_history=[{"role": "user", "content": "old message"}],
        )

    assert result.get("failed") is True
    assert observed[0][0] == "persist"
    assert observed[1][0] == "provider"
    persisted_messages = observed[0][1]
    assert persisted_messages[-1] == {
        "role": "user",
        "content": "new message that must survive a crash",
    }


class TestHTTP413Compression:
    """Provider overflows synchronously compress before any retry."""

    @staticmethod
    def _assert_lossless_block(result, mock_compress):
        mock_compress.assert_called_once()
        assert result["failed"] is True
        assert result["completed"] is False
        assert result["final_response"] is None
        assert result["turn_exit_reason"] == "compression_hard_blocked"
        assert result["failure_reason"] == "compression_failed"
        assert result["messages"][-1]["role"] == "user"

    @pytest.mark.parametrize(
        "error",
        [
            _make_413_error(),
            _make_413_error(use_status_code=False, message="error code: 413"),
            Exception("Error code: 400 - Please reduce the length of the messages"),
            Exception(
                "Error code: 400 - This endpoint's maximum context length is "
                "128000 tokens. However, you requested about 270460 tokens."
            ),
        ],
    )
    def test_overflow_enqueues_and_blocks_without_assistant_error(
        self, agent, error
    ):
        if "400" in str(error):
            error.status_code = 400
        agent.client.chat.completions.create.side_effect = [error]
        events = []
        agent.status_callback = lambda kind, message: events.append((kind, message))
        prefill = [
            {"role": "user", "content": "previous question"},
            {"role": "assistant", "content": "previous answer"},
        ]

        with (
            patch.object(agent, "_compress_context") as mock_compress,
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
        ):
            result = agent.run_conversation("hello", conversation_history=prefill)

        self._assert_lossless_block(result, mock_compress)
        assert "compression.blocked" in {kind for kind, _ in events}

    def test_overflow_retries_only_after_verified_safe_compression(self, agent):
        error = _make_413_error()
        response = _mock_response(content="Recovered", finish_reason="stop")
        agent.client.chat.completions.create.side_effect = [error, response]
        compacted = [{"role": "user", "content": "compacted summary"}]

        with (
            patch.object(
                agent,
                "_compress_context",
                return_value=(compacted, "compressed prompt"),
            ) as mock_compress,
            patch(
                "agent.conversation_compression.estimate_request_tokens_rough",
                side_effect=[80, 40],
            ),
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
        ):
            result = agent.run_conversation(
                "hello",
                conversation_history=[
                    {"role": "user", "content": "old"},
                    {"role": "assistant", "content": "answer"},
                ],
            )

        mock_compress.assert_called_once()
        assert result["completed"] is True
        assert result["final_response"] == "Recovered"
        assert agent.client.chat.completions.create.call_count == 2

    def test_overflow_preserves_vision_payload_when_projection_not_ready(self, agent):
        error = _make_413_error()
        agent.client.chat.completions.create.side_effect = [error]
        image = "data:image/png;base64," + ("a" * 2000)
        prefill = [
            {"role": "user", "content": "inspect"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call_vision",
                    "type": "function",
                    "function": {"name": "browser_vision", "arguments": "{}"},
                }],
            },
            {
                "role": "tool",
                "tool_call_id": "call_vision",
                "name": "browser_vision",
                "content": [{"type": "image_url", "image_url": {"url": image}}],
            },
        ]

        with (
            patch.object(agent, "_compress_context") as mock_compress,
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
        ):
            result = agent.run_conversation("continue", conversation_history=prefill)

        self._assert_lossless_block(result, mock_compress)
        assert image in str(result["messages"])


class TestPreparedRequestCompression:
    """Prepared-request accounting should compress before provider dispatch."""

    def test_compress_context_emits_lifecycle_status_before_work(self, agent):
        """Direct context compression should tell gateway users why the turn paused."""
        # This test calls _compress_context directly and asserts the FIRST
        # status event is the lifecycle "Compacting context" message. With
        # compaction enabled the lazy feasibility probe would emit an
        # aux-provider warning first (no aux key in the hermetic test env),
        # displacing events[0]. The flag value is irrelevant to what this
        # test asserts, so disable it to suppress the probe.
        agent.compression_enabled = False
        events = []
        agent.status_callback = lambda ev, msg: events.append((ev, msg))

        def _fake_compress(messages, current_tokens=None, focus_topic=None):
            events.append(("compress", "started"))
            return [{"role": "user", "content": f"{SUMMARY_PREFIX}\nPrevious conversation"}]

        with (
            patch.object(agent.context_compressor, "compress", side_effect=_fake_compress),
            patch.object(agent, "_build_system_prompt", return_value="new system prompt"),
            patch("run_agent.estimate_request_tokens_rough", return_value=42),
        ):
            compressed, new_system_prompt = agent._compress_context(
                [{"role": "user", "content": "hello"}],
                "system prompt",
                approx_tokens=1234,
            )

        assert compressed == [{"role": "user", "content": f"{SUMMARY_PREFIX}\nPrevious conversation"}]
        assert new_system_prompt == "new system prompt"
        assert events[0][0] == "compression.preparing"
        assert "Compacting context" in events[0][1]
        assert events[1] == ("compress", "started")

    def test_prepared_request_compresses_at_threshold(self, agent, monkeypatch):
        """The middleware-final prepared payload is the automatic compression gate."""
        agent.compression_enabled = True
        agent.context_compressor.context_length = 200_000
        agent.context_compressor.threshold_tokens = 100_000
        big_history = [
            {"role": role, "content": "x" * 20_000}
            for _ in range(20)
            for role in ("user", "assistant")
        ]
        ok_resp = _mock_response(content="Used prepared fit", finish_reason="stop")
        agent.client.chat.completions.create.side_effect = [ok_resp]

        compressed_once = {"done": False}

        def _compress(_messages, *_args, **_kwargs):
            compressed_once["done"] = True
            return ([{"role": "user", "content": "compacted"}], "compressed prompt")

        monkeypatch.setattr(agent, "_compress_context", _compress)
        monkeypatch.setattr(agent, "_persist_session", lambda *a, **k: None)
        monkeypatch.setattr(agent, "_save_trajectory", lambda *a, **k: None)
        monkeypatch.setattr(agent, "_cleanup_task_resources", lambda *a, **k: None)

        result = agent.run_conversation("hello", conversation_history=big_history)

        assert compressed_once["done"] is True
        assert result["completed"] is True
        assert result["final_response"] == "Used prepared fit"
        assert agent._prepared_model_request.payload == agent.client.chat.completions.create.call_args.kwargs

    def test_no_progress_below_hard_limit_dispatches_prepared_snapshot(self, agent, monkeypatch):
        agent.compression_enabled = True
        agent.context_compressor.context_length = 200_000
        agent.context_compressor.threshold_tokens = 1
        agent.context_compressor.max_tokens = 4_096
        agent.client.chat.completions.create.side_effect = [
            _mock_response(content="Continued safely", finish_reason="stop")
        ]
        monkeypatch.setattr(
            agent,
            "_compress_context",
            lambda messages, *_args, **_kwargs: (messages, agent._cached_system_prompt),
        )
        monkeypatch.setattr(agent, "_persist_session", lambda *a, **k: None)
        monkeypatch.setattr(agent, "_save_trajectory", lambda *a, **k: None)
        monkeypatch.setattr(agent, "_cleanup_task_resources", lambda *a, **k: None)

        result = agent.run_conversation("hello", conversation_history=[])

        assert result["completed"] is True
        assert result["final_response"] == "Continued safely"
        assert agent.client.chat.completions.create.call_count == 1
        assert (
            agent.client.chat.completions.create.call_args.kwargs
            == agent._prepared_model_request.payload
        )

    def test_no_progress_at_hard_limit_blocks_without_dispatch(self, agent, monkeypatch):
        agent.compression_enabled = True
        agent.context_compressor.context_length = 5
        agent.context_compressor.threshold_tokens = 1
        agent.context_compressor.max_tokens = 4
        agent.max_tokens = 4
        monkeypatch.setattr(
            agent,
            "_compress_context",
            lambda messages, *_args, **_kwargs: (messages, agent._cached_system_prompt),
        )
        monkeypatch.setattr(agent, "_persist_session", lambda *a, **k: None)
        monkeypatch.setattr(agent, "_save_trajectory", lambda *a, **k: None)
        monkeypatch.setattr(agent, "_cleanup_task_resources", lambda *a, **k: None)

        result = agent.run_conversation("hello", conversation_history=[])

        assert result["completed"] is False
        assert result["turn_exit_reason"] == "compression_hard_blocked"
        assert agent.client.chat.completions.create.call_count == 0

    def test_no_compression_when_prepared_request_is_under_threshold(self, agent):
        agent.compression_enabled = True
        agent.context_compressor.context_length = 1_000_000
        agent.context_compressor.threshold_tokens = 850_000
        agent.client.chat.completions.create.side_effect = [
            _mock_response(content="No compression needed", finish_reason="stop")
        ]
        with (
            patch.object(agent, "_compress_context") as mock_compress,
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
        ):
            result = agent.run_conversation("hello", conversation_history=[])
        mock_compress.assert_not_called()
        assert result["completed"] is True

    def test_request_middleware_runs_before_prepared_accounting(self, agent, monkeypatch):
        agent.compression_enabled = False
        agent.client.chat.completions.create.side_effect = [
            _mock_response(content="OK", finish_reason="stop")
        ]
        monkeypatch.setattr(agent, "_persist_session", lambda *a, **k: None)
        monkeypatch.setattr(agent, "_save_trajectory", lambda *a, **k: None)
        monkeypatch.setattr(agent, "_cleanup_task_resources", lambda *a, **k: None)

        result = agent.run_conversation("hello")

        assert result["completed"] is True
        prepared = agent._prepared_model_request
        assert prepared.payload == agent.client.chat.completions.create.call_args.kwargs
        assert prepared.accounting.effective_input_tokens > 0

    def test_no_automatic_compression_when_disabled(self, agent):
        agent.compression_enabled = False
        agent.context_compressor.context_length = 100
        agent.context_compressor.threshold_tokens = 85
        history = [{"role": "user", "content": "x" * 1000}]
        agent.client.chat.completions.create.side_effect = [
            _mock_response(content="OK", finish_reason="stop")
        ]
        with (
            patch.object(agent, "_compress_context") as mock_compress,
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
        ):
            agent.run_conversation("hello", conversation_history=history)
        mock_compress.assert_not_called()


class TestToolResultCompression:
    """Compression should trigger when tool results push prepared context past threshold."""

    def test_anthropic_prompt_too_long_blocks_losslessly(self, agent):
        """Anthropic overflow synchronously compresses and emits no assistant error row."""
        error = Exception(
            "Error code: 400 - {'type': 'error', 'error': {'type': "
            "'invalid_request_error', 'message': 'prompt is too long: "
            "233153 tokens > 200000 maximum'}}"
        )
        error.status_code = 400
        agent.client.chat.completions.create.side_effect = [error]

        with (
            patch.object(agent, "_compress_context") as mock_compress,
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
        ):
            result = agent.run_conversation(
                "hello",
                conversation_history=[
                    {"role": "user", "content": "previous"},
                    {"role": "assistant", "content": "answer"},
                ],
            )

        mock_compress.assert_called_once()
        assert result["turn_exit_reason"] == "compression_hard_blocked"
        assert result["final_response"] is None
        assert result["messages"][-1]["role"] == "user"


class TestOverflowWithCompactionDisabled:
    """When ``compression.enabled`` is False, NO automatic compaction may
    fire — including the provider/request-size overflow recovery paths.

    Ported from anomalyco/opencode#30749: the proactive token-threshold
    path already honoured the setting, but provider overflow errors
    (413 payload-too-large, context-overflow, long-context-tier 429) still
    silently compressed + rotated the session. The fix surfaces a terminal
    error so the user can compact manually, start fresh, or switch models.
    """

    @staticmethod
    def _prefill():
        return [
            {"role": "user", "content": "previous question"},
            {"role": "assistant", "content": "previous answer"},
        ]

    def test_413_does_not_compress_when_disabled(self, agent):
        """413 must NOT call _compress_context when compaction is disabled."""
        agent.compression_enabled = False
        err_413 = _make_413_error()
        # If the guard fails, a second (success) response would be consumed.
        agent.client.chat.completions.create.side_effect = [err_413, _mock_response()]

        with (
            patch.object(agent, "_compress_context") as mock_compress,
            patch.object(agent, "_persist_session") as mock_persist,
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
        ):
            result = agent.run_conversation("hello", conversation_history=self._prefill())

        mock_compress.assert_not_called()
        mock_persist.assert_called()
        assert result.get("failed") is True
        assert result.get("compaction_disabled") is True
        assert "auto-compaction is disabled" in result["error"]

    def test_context_overflow_does_not_compress_when_disabled(self, agent):
        """400 'prompt is too long' must NOT compress when compaction disabled."""
        agent.compression_enabled = False
        err_400 = Exception(
            "Error code: 400 - {'type': 'error', 'error': {'type': "
            "'invalid_request_error', 'message': 'prompt is too long: "
            "233153 tokens > 200000 maximum'}}"
        )
        err_400.status_code = 400
        agent.client.chat.completions.create.side_effect = [err_400, _mock_response()]

        with (
            patch.object(agent, "_compress_context") as mock_compress,
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
        ):
            result = agent.run_conversation("hello", conversation_history=self._prefill())

        mock_compress.assert_not_called()
        assert result.get("compaction_disabled") is True

    def test_413_uses_synchronous_block_when_enabled(self, agent):
        """Enabled compression attempts synchronous recovery before blocking."""
        agent.compression_enabled = True
        agent.client.chat.completions.create.side_effect = [_make_413_error()]

        with (
            patch.object(agent, "_compress_context") as mock_compress,
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
        ):
            result = agent.run_conversation("hello", conversation_history=self._prefill())

        mock_compress.assert_called_once()
        assert result["turn_exit_reason"] == "compression_hard_blocked"
        assert result.get("compaction_disabled") is not True
