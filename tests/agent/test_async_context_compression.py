"""Focused tests for speculative asynchronous context compression."""

from __future__ import annotations

from concurrent.futures import Future, TimeoutError
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent.async_context_compression import (
    AsyncCompressionAction,
    _reset_async_compression_for_tests,
    compression_thresholds,
    invalidate_compression_runtime,
    invalidate_preparation,
    maybe_handle_async_compression,
)
from agent.context_compressor import PreparedCompression


class _ControlledExecutor:
    def __init__(self):
        self.submissions = []

    def submit(self, fn, *args, **kwargs):
        future = Future()
        self.submissions.append((future, fn, args, kwargs))
        return future

    def complete(self, index=0):
        future, fn, args, kwargs = self.submissions[index]
        future.set_result(fn(*args, **kwargs))
        return future


class _FakeCompressor:
    def __init__(self):
        self.context_length = 256_000
        self.max_tokens = 0
        self.threshold_tokens = 128_000
        self.model = "test/model"
        self.prepare_calls = []
        self._last_compress_aborted = False

    def should_compress(self, tokens):
        return tokens >= self.threshold_tokens

    def prepare_compression(self, messages, *, current_tokens=None, **_kwargs):
        self.prepare_calls.append((messages, current_tokens))
        return PreparedCompression(
            compressed_messages=[
                {"role": "user", "content": "[SUMMARY] prior work"},
                messages[-1],
            ],
            compressor_state={"compression_count": 1},
            aborted=False,
        )


class _FakeAgent:
    def __init__(self, executor):
        self.session_id = "session-1"
        self.model = "test/model"
        self._session_db = SimpleNamespace(db_path="/tmp/state.db")
        self.context_compressor = _FakeCompressor()
        self.compression_enabled = True
        self._compression_feasibility_checked = True
        self._using_builtin_context_compressor = True
        self.compression_async_prepare = True
        self.compression_prepare_threshold = 0.50
        self.compression_commit_threshold = 0.80
        self.compression_emergency_threshold = 0.88
        self.compression_emergency_wait_seconds = 15.0
        self._async_compression_executor = executor
        self._compress_context = MagicMock(
            return_value=([{"role": "user", "content": "sync summary"}], "SYNC")
        )


@pytest.fixture(autouse=True)
def _clear_registry():
    _reset_async_compression_for_tests()
    yield
    _reset_async_compression_for_tests()


def _messages():
    return [
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "current question"},
    ]


def _handle(agent, messages, tokens):
    return maybe_handle_async_compression(
        agent,
        messages,
        "SYSTEM",
        current_tokens=tokens,
        task_id="default",
    )


def test_thresholds_use_effective_input_window_and_stay_monotonic():
    agent = _FakeAgent(_ControlledExecutor())
    agent.context_compressor.max_tokens = 16_000
    agent.context_compressor.threshold_tokens = 120_000

    thresholds = compression_thresholds(agent)

    assert thresholds.prepare == 120_000
    assert thresholds.commit == int(240_000 * 0.80)
    assert thresholds.emergency == int(240_000 * 0.88)


def test_explicit_prepare_threshold_can_raise_builtin_trigger():
    agent = _FakeAgent(_ControlledExecutor())
    agent.compression_prepare_threshold = 0.60

    thresholds = compression_thresholds(agent)

    assert thresholds.prepare == int(256_000 * 0.60)
    assert thresholds.commit == int(256_000 * 0.80)
    assert thresholds.emergency == int(256_000 * 0.88)


def test_feasibility_check_runs_before_first_worker_submission(monkeypatch):
    executor = _ControlledExecutor()
    agent = _FakeAgent(executor)
    agent._compression_feasibility_checked = False
    events = []

    def _check(candidate):
        assert candidate is agent
        assert executor.submissions == []
        events.append("feasibility")

    monkeypatch.setattr(
        "agent.conversation_compression.check_compression_model_feasibility",
        _check,
    )

    outcome = _handle(agent, _messages(), 128_000)

    assert outcome.action is AsyncCompressionAction.PREPARING
    assert events == ["feasibility"]
    assert agent._compression_feasibility_checked is True
    assert len(executor.submissions) == 1


def test_feasibility_lowered_threshold_starts_prepare_at_safe_cap(monkeypatch):
    executor = _ControlledExecutor()
    agent = _FakeAgent(executor)
    agent._compression_feasibility_checked = False
    checks = []

    def _check(candidate):
        checks.append(candidate)
        candidate.context_compressor.threshold_tokens = 80_000
        candidate._compression_prepare_token_cap = 80_000

    monkeypatch.setattr(
        "agent.conversation_compression.check_compression_model_feasibility",
        _check,
    )

    below = _handle(agent, _messages(), 64_000)
    at_cap = _handle(agent, _messages(), 80_000)

    assert below.action is AsyncCompressionAction.NONE
    assert at_cap.action is AsyncCompressionAction.PREPARING
    assert checks == [agent]
    assert compression_thresholds(agent).prepare == 80_000
    assert len(executor.submissions) == 1
    assert executor.submissions[0][2][2] == 80_000


def test_below_prepare_threshold_does_not_submit():
    executor = _ControlledExecutor()
    agent = _FakeAgent(executor)

    outcome = _handle(agent, _messages(), 127_999)

    assert outcome.action is AsyncCompressionAction.NONE
    assert executor.submissions == []
    agent._compress_context.assert_not_called()


def test_prepare_threshold_submits_once_without_changing_live_context():
    executor = _ControlledExecutor()
    agent = _FakeAgent(executor)
    messages = _messages()

    first = _handle(agent, messages, 128_000)
    second = _handle(agent, messages, 150_000)

    assert first.action is AsyncCompressionAction.PREPARING
    assert second.action is AsyncCompressionAction.PREPARING
    assert first.messages is messages
    assert first.system_prompt == "SYSTEM"
    assert len(executor.submissions) == 1
    assert agent.context_compressor.prepare_calls == []
    agent._compress_context.assert_not_called()


def test_ready_preparation_is_not_committed_before_commit_threshold(monkeypatch):
    executor = _ControlledExecutor()
    agent = _FakeAgent(executor)
    messages = _messages()
    _handle(agent, messages, 128_000)
    executor.complete()
    commit = MagicMock()
    monkeypatch.setattr("agent.async_context_compression.commit_prepared_context", commit)

    outcome = _handle(agent, messages, 190_000)

    assert outcome.action is AsyncCompressionAction.READY
    assert outcome.messages is messages
    commit.assert_not_called()


def test_commit_threshold_applies_ready_snapshot_plus_append_only_delta(monkeypatch):
    executor = _ControlledExecutor()
    agent = _FakeAgent(executor)
    snapshot = _messages()
    _handle(agent, snapshot, 128_000)
    executor.complete()
    current = snapshot + [{"role": "assistant", "content": "new delta"}]
    committed = [
        {"role": "user", "content": "[SUMMARY] prior work"},
        snapshot[-1],
        current[-1],
    ]
    commit = MagicMock(return_value=(committed, "COMMITTED"))
    monkeypatch.setattr("agent.async_context_compression.commit_prepared_context", commit)

    outcome = _handle(agent, current, 205_000)

    assert outcome.action is AsyncCompressionAction.COMMITTED
    assert outcome.messages == committed
    assert outcome.system_prompt == "COMMITTED"
    prepared = commit.call_args.kwargs["prepared"]
    assert prepared.snapshot_length == len(snapshot)
    assert prepared.compression.compressed_messages[-1] == snapshot[-1]
    assert commit.call_args.kwargs["messages"] is current
    agent._compress_context.assert_not_called()


def test_failed_preparation_is_discarded_and_can_retry(monkeypatch):
    executor = _ControlledExecutor()
    agent = _FakeAgent(executor)
    messages = _messages()
    _handle(agent, messages, 128_000)
    executor.submissions[0][0].set_exception(RuntimeError("provider failed"))

    failed = _handle(agent, messages, 150_000)
    retried = _handle(agent, messages, 150_000)

    assert failed.action is AsyncCompressionAction.FAILED
    assert retried.action is AsyncCompressionAction.PREPARING
    assert len(executor.submissions) == 2


def test_noop_preparation_is_discarded_and_can_retry(monkeypatch):
    executor = _ControlledExecutor()
    agent = _FakeAgent(executor)
    messages = _messages()
    _handle(agent, messages, 128_000)
    future = executor.complete()
    prepared = future.result()
    object.__setattr__(prepared.compression, "applied", False)

    failed = _handle(agent, messages, 205_000)
    retried = _handle(agent, messages, 205_000)

    assert failed.action is AsyncCompressionAction.FAILED
    assert retried.action is AsyncCompressionAction.PREPARING
    assert len(executor.submissions) == 2


def test_noop_preparation_is_not_committed_below_emergency(monkeypatch):
    executor = _ControlledExecutor()
    agent = _FakeAgent(executor)
    messages = _messages()
    _handle(agent, messages, 128_000)
    future = executor.complete()
    prepared = future.result()
    object.__setattr__(prepared.compression, "applied", False)
    commit = MagicMock()
    monkeypatch.setattr(
        "agent.async_context_compression.commit_prepared_context",
        commit,
    )

    outcome = _handle(agent, messages, 205_000)

    assert outcome.action is AsyncCompressionAction.FAILED
    commit.assert_not_called()
    agent._compress_context.assert_not_called()


def test_changed_snapshot_is_stale_and_not_committed_below_emergency(monkeypatch):
    executor = _ControlledExecutor()
    agent = _FakeAgent(executor)
    snapshot = _messages()
    _handle(agent, snapshot, 128_000)
    executor.complete()
    changed = [dict(message) for message in snapshot]
    changed[0]["content"] = "edited history"
    commit = MagicMock()
    monkeypatch.setattr("agent.async_context_compression.commit_prepared_context", commit)

    outcome = _handle(agent, changed, 205_000)

    assert outcome.action is AsyncCompressionAction.STALE
    commit.assert_not_called()
    agent._compress_context.assert_not_called()


def test_emergency_waits_on_same_future_then_commits(monkeypatch):
    executor = _ControlledExecutor()
    agent = _FakeAgent(executor)
    messages = _messages()
    _handle(agent, messages, 128_000)
    future, fn, args, kwargs = executor.submissions[0]
    prepared = fn(*args, **kwargs)
    waits = []

    def _result(timeout=None):
        waits.append(timeout)
        return prepared

    monkeypatch.setattr(future, "result", _result)
    commit = MagicMock(return_value=([{"role": "user", "content": "ready"}], "READY"))
    monkeypatch.setattr("agent.async_context_compression.commit_prepared_context", commit)

    outcome = _handle(agent, messages, 226_000)

    assert waits == [15.0]
    assert len(executor.submissions) == 1
    assert outcome.action is AsyncCompressionAction.COMMITTED
    agent._compress_context.assert_not_called()


def test_emergency_timeout_uses_existing_synchronous_fallback(monkeypatch):
    executor = _ControlledExecutor()
    agent = _FakeAgent(executor)
    messages = _messages()
    _handle(agent, messages, 128_000)
    future = executor.submissions[0][0]
    waits = []

    def _timeout(timeout=None):
        waits.append(timeout)
        raise TimeoutError()

    monkeypatch.setattr(future, "result", _timeout)

    outcome = _handle(agent, messages, 226_000)

    assert waits == [15.0]
    assert len(executor.submissions) == 1
    assert outcome.action is AsyncCompressionAction.SYNCHRONOUS_FALLBACK
    assert outcome.messages == [{"role": "user", "content": "sync summary"}]
    agent._compress_context.assert_called_once()


def test_runtime_invalidation_clears_feasibility_state():
    agent = _FakeAgent(_ControlledExecutor())
    agent._compression_prepare_token_cap = 80_000
    agent._compression_warning = "warning for old model"

    invalidate_compression_runtime(agent, reason="model changed")

    assert agent._compression_feasibility_checked is False
    assert agent._compression_prepare_token_cap is None
    assert agent._compression_warning is None


def test_session_invalidation_discards_ready_result(monkeypatch):
    executor = _ControlledExecutor()
    agent = _FakeAgent(executor)
    messages = _messages()
    _handle(agent, messages, 128_000)
    executor.complete()
    invalidate_preparation(agent, reason="manual compression")
    commit = MagicMock()
    monkeypatch.setattr("agent.async_context_compression.commit_prepared_context", commit)

    outcome = _handle(agent, messages, 205_000)

    assert outcome.action is AsyncCompressionAction.STALE
    commit.assert_not_called()


def test_invalidated_preparation_uses_synchronous_fallback_at_emergency(monkeypatch):
    executor = _ControlledExecutor()
    agent = _FakeAgent(executor)
    messages = _messages()
    _handle(agent, messages, 128_000)
    executor.complete()
    invalidate_preparation(agent, reason="manual compression")
    commit = MagicMock()
    monkeypatch.setattr(
        "agent.async_context_compression.commit_prepared_context",
        commit,
    )

    outcome = _handle(agent, messages, 226_000)

    assert outcome.action is AsyncCompressionAction.SYNCHRONOUS_FALLBACK
    assert outcome.messages == [{"role": "user", "content": "sync summary"}]
    commit.assert_not_called()
    agent._compress_context.assert_called_once()


def test_prepare_runs_on_detached_compressor_state(monkeypatch):
    from agent.context_compressor import ContextCompressor

    compressor = object.__new__(ContextCompressor)
    compressor._session_db = object()
    compressor._session_id = "live-session"
    compressor._previous_summary = "live summary"
    compressor.compression_count = 3
    compressor._PREPARED_STATE_FIELDS = ContextCompressor._PREPARED_STATE_FIELDS
    observed = {}

    def _compress(detached, messages, **_kwargs):
        observed["same"] = detached is compressor
        observed["db"] = detached._session_db
        observed["session"] = detached._session_id
        detached._previous_summary = "prepared summary"
        detached._last_compress_aborted = False
        return [{"role": "user", "content": "prepared"}]

    monkeypatch.setattr(ContextCompressor, "compress", _compress)
    prepared = ContextCompressor.prepare_compression(
        compressor,
        _messages(),
        current_tokens=128_000,
    )

    assert observed == {"same": False, "db": None, "session": ""}
    assert compressor._previous_summary == "live summary"
    assert compressor.compression_count == 3
    assert prepared.compressor_state["_previous_summary"] == "prepared summary"


def test_noop_preparation_does_not_increment_live_compression_count(monkeypatch):
    from agent.context_compressor import ContextCompressor

    compressor = object.__new__(ContextCompressor)
    compressor._session_db = object()
    compressor._session_id = "live-session"
    compressor._previous_summary = "live summary"
    compressor.compression_count = 3
    compressor._last_compress_aborted = False
    compressor._PREPARED_STATE_FIELDS = ContextCompressor._PREPARED_STATE_FIELDS

    def _compress(detached, messages, **_kwargs):
        detached._last_compress_aborted = False
        return messages

    monkeypatch.setattr(ContextCompressor, "compress", _compress)
    messages = _messages()
    prepared = ContextCompressor.prepare_compression(
        compressor,
        messages,
        current_tokens=128_000,
    )
    result = ContextCompressor.apply_prepared_compression(
        compressor,
        prepared,
        [],
        current_tokens=128_000,
    )

    assert result == messages
    assert compressor.compression_count == 3


def test_plugin_context_engine_never_uses_async_coordinator():
    executor = _ControlledExecutor()
    agent = _FakeAgent(executor)
    agent._using_builtin_context_compressor = False

    outcome = _handle(agent, _messages(), 226_000)

    assert outcome.action is AsyncCompressionAction.NONE
    assert executor.submissions == []
