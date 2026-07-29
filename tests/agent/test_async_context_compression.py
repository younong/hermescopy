"""Focused tests for durable asynchronous context compression."""

from __future__ import annotations

import json
import threading
import time
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
from hermes_state import SessionDB


class _ControlledExecutor:
    def __init__(self):
        self.submissions = []

    def submit(self, fn, *args, **kwargs):
        self.submissions.append((fn, args, kwargs))

    def complete(self, index=0):
        fn, args, kwargs = self.submissions[index]
        fn(*args, **kwargs)

    def shutdown(self, **_kwargs):
        return None


class _FakeCompressor:
    def __init__(self):
        self.context_length = 256_000
        self.max_tokens = 0
        self.threshold_tokens = 128_000
        self.model = "test/summary"
        self.prepare_calls = []
        self.block = None
        self.failure = None
        self.result = None

    def should_compress(self, tokens):
        return tokens >= self.threshold_tokens

    def prepare_compression(self, messages, *, current_tokens=None, **_kwargs):
        self.prepare_calls.append((messages, current_tokens))
        if self.block is not None:
            self.block.wait(timeout=5)
        if self.failure is not None:
            raise self.failure
        if self.result is not None:
            return self.result
        return PreparedCompression(
            compressed_messages=[
                {"role": "user", "content": "[SUMMARY] prior work"},
                messages[-1],
            ],
            compressor_state={"compression_count": 1},
            aborted=False,
            auxiliary_route_fingerprint="actual-route",
            auxiliary_context_length=256_000,
            auxiliary_input_budget=248_000,
        )


class _FakeAgent:
    def __init__(self, db):
        self.session_id = "session-1"
        self.model = "test/model"
        self._session_db = db
        self.context_compressor = _FakeCompressor()
        self.compression_enabled = True
        self._compression_feasibility_checked = True
        self._using_builtin_context_compressor = True
        self.compression_prepare_threshold = 0.50
        self.compression_commit_threshold = 0.80
        self.compression_emergency_threshold = 0.88
        self._compress_context = MagicMock()
        self.statuses = []

    def _emit_status(self, message, *, kind="lifecycle"):
        self.statuses.append((kind, message))


@pytest.fixture(autouse=True)
def _clear_orchestrators():
    _reset_async_compression_for_tests()
    yield
    _reset_async_compression_for_tests()


@pytest.fixture
def agent(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("session-1", "cli", model="test/model")
    candidate = _FakeAgent(db)
    yield candidate
    db.close()


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


def _wait_for_state(agent, expected, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = agent._session_db.get_compression_job(agent.session_id)
        if job and job["state"] in expected:
            return job
        time.sleep(0.01)
    raise AssertionError(f"job did not reach {expected}")


def test_thresholds_use_effective_input_window_and_stay_monotonic(agent):
    agent.context_compressor.max_tokens = 16_000
    agent.context_compressor.threshold_tokens = 120_000

    thresholds = compression_thresholds(agent)

    assert thresholds.prepare == 120_000
    assert thresholds.commit == int(240_000 * 0.80)
    assert thresholds.emergency == int(240_000 * 0.88)


def test_below_prepare_threshold_does_not_enqueue(agent):
    outcome = _handle(agent, _messages(), 127_999)

    assert outcome.action is AsyncCompressionAction.NONE
    assert agent._session_db.get_compression_job(agent.session_id) is None


def test_anti_thrash_veto_does_not_enqueue(agent):
    agent.context_compressor.should_compress = MagicMock(return_value=False)

    outcome = _handle(agent, _messages(), 128_000)

    assert outcome.action is AsyncCompressionAction.NONE
    agent.context_compressor.should_compress.assert_called_once_with(128_000)
    assert agent._session_db.get_compression_job(agent.session_id) is None


def test_prepare_threshold_enqueues_durable_snapshot_without_changing_context(agent):
    blocker = threading.Event()
    agent.context_compressor.block = blocker
    messages = _messages()

    outcome = _handle(agent, messages, 128_000)
    job = _wait_for_state(agent, {"queued", "preparing"})

    assert outcome.action is AsyncCompressionAction.PREPARING
    assert outcome.messages is messages
    assert json.loads(job["snapshot_payload"]) == messages
    assert job["snapshot_message_count"] == len(messages)
    assert "old question" not in repr({k: job[k] for k in ("job_id", "fence_id")})
    assert agent.statuses[0][0] == "compression.preparing"
    blocker.set()


def test_preparation_runs_in_background_and_never_waits_at_emergency(agent):
    blocker = threading.Event()
    agent.context_compressor.block = blocker

    started = time.monotonic()
    first = _handle(agent, _messages(), 128_000)
    emergency = _handle(agent, _messages(), 226_000)
    elapsed = time.monotonic() - started

    assert first.action is AsyncCompressionAction.PREPARING
    assert emergency.action is AsyncCompressionAction.PREPARING
    assert elapsed < 1.0
    agent._compress_context.assert_not_called()
    blocker.set()


def test_ready_job_commits_only_at_commit_threshold(agent, monkeypatch):
    messages = _messages()
    _handle(agent, messages, 128_000)
    job = _wait_for_state(agent, {"ready"})
    commit = MagicMock(return_value=([{"role": "user", "content": "ready"}], "READY"))
    monkeypatch.setattr("agent.async_context_compression.commit_prepared_context", commit)

    below = _handle(agent, messages, 190_000)
    committed = _handle(agent, messages, 205_000)

    assert below.action is AsyncCompressionAction.READY
    assert committed.action is AsyncCompressionAction.COMMITTED
    assert commit.call_args.kwargs["compression_job"]["job_id"] == job["job_id"]
    assert commit.call_args.kwargs["compression_job"]["state"] == "committing"
    agent._compress_context.assert_not_called()


def test_failed_preparation_enters_durable_cooldown_without_sync_fallback(agent):
    agent.context_compressor.failure = RuntimeError("provider failed")

    outcome = _handle(agent, _messages(), 128_000)
    job = _wait_for_state(agent, {"cooldown"})
    observed = _handle(agent, _messages(), 226_000)

    assert outcome.action is AsyncCompressionAction.PREPARING
    assert job["failure_code"] == "preparation_failure"
    assert job["retry_at"] > time.time()
    assert observed.action is AsyncCompressionAction.FAILED
    assert "compression.cooldown" in {kind for kind, _ in agent.statuses}
    agent._compress_context.assert_not_called()


def test_detached_atomic_group_failure_is_degraded_and_lossless(agent):
    agent.context_compressor.result = PreparedCompression(
        compressed_messages=_messages(),
        compressor_state={
            "_last_summary_error": (
                "atomic tool group exceeds auxiliary input budget"
            )
        },
        aborted=True,
        applied=False,
    )

    _handle(agent, _messages(), 128_000)
    job = _wait_for_state(agent, {"degraded"})

    assert job["failure_code"] == "atomic_group_too_large"
    assert "compression.degraded" in {kind for kind, _ in agent.statuses}


def test_ready_job_persists_actual_auxiliary_route_metadata(agent):
    _handle(agent, _messages(), 128_000)

    job = _wait_for_state(agent, {"ready"})

    assert job["prepared_auxiliary_route_fingerprint"] == "actual-route"
    assert job["prepared_auxiliary_context_length"] == 256_000
    assert job["prepared_auxiliary_input_budget"] == 248_000
    payload = json.loads(job["prepared_payload"])
    assert payload["compression"]["auxiliary_route_fingerprint"] == "actual-route"
    assert "base_url" not in payload["compression"]
    assert "api_key" not in payload["compression"]


def test_ready_transition_emits_structured_status(agent):
    _handle(agent, _messages(), 128_000)

    _wait_for_state(agent, {"ready"})

    assert "compression.ready" in {kind for kind, _ in agent.statuses}


def test_checkpoint_refreshes_exact_lease(agent, monkeypatch):
    def prepare(messages, *, current_tokens=None, checkpoint=None, **_kwargs):
        checkpoint(1, "rolling")
        return PreparedCompression(
            compressed_messages=[messages[-1]],
            compressor_state={"compression_count": 1},
            aborted=False,
            auxiliary_route_fingerprint="actual-route",
            auxiliary_context_length=256_000,
            auxiliary_input_budget=248_000,
        )

    refresh = MagicMock(
        wraps=agent._session_db.refresh_compression_job_lease
    )
    monkeypatch.setattr(
        agent._session_db, "refresh_compression_job_lease", refresh
    )
    agent.context_compressor.prepare_compression = prepare

    _handle(agent, _messages(), 128_000)
    job = _wait_for_state(agent, {"ready"})

    refresh.assert_called_once_with(
        session_id=agent.session_id,
        job_id=job["job_id"],
        holder=refresh.call_args.kwargs["holder"],
        lease_version=job["lease_version"],
        lease_seconds=420.0,
    )
    assert job["chunk_cursor"] == 1
    assert job["rolling_summary"] == "rolling"


def test_changed_snapshot_is_marked_stale_and_not_committed(agent, monkeypatch):
    snapshot = _messages()
    _handle(agent, snapshot, 128_000)
    _wait_for_state(agent, {"ready"})
    changed = [dict(message) for message in snapshot]
    changed[0]["content"] = "edited history"
    commit = MagicMock()
    monkeypatch.setattr("agent.async_context_compression.commit_prepared_context", commit)

    outcome = _handle(agent, changed, 205_000)

    assert outcome.action is AsyncCompressionAction.STALE
    assert agent._session_db.get_compression_job(agent.session_id)["state"] == "stale"
    commit.assert_not_called()


def test_invalidation_durably_cancels_job(agent):
    blocker = threading.Event()
    agent.context_compressor.block = blocker
    _handle(agent, _messages(), 128_000)
    _wait_for_state(agent, {"preparing"})

    invalidate_preparation(agent, reason="manual compression")

    assert agent._session_db.get_compression_job(agent.session_id)["state"] == "cancelled"
    blocker.set()


def test_expired_preparation_lease_is_reclaimed_after_restart(agent):
    blocker = threading.Event()
    agent.context_compressor.block = blocker
    _handle(agent, _messages(), 128_000)
    job = _wait_for_state(agent, {"preparing"})
    agent._session_db._conn.execute(
        "UPDATE compression_jobs SET lease_expires_at=? WHERE session_id=?",
        (time.time() - 1.0, agent.session_id),
    )
    agent._session_db._conn.commit()
    blocker.set()
    _wait_for_state(agent, {"preparing", "cooldown"})
    _reset_async_compression_for_tests()
    replacement = _FakeAgent(agent._session_db)

    _handle(replacement, _messages(), 150_000)
    ready = _wait_for_state(replacement, {"ready"})

    assert ready["job_id"] == job["job_id"]
    assert ready["lease_version"] >= 2


def test_tampered_actual_route_fence_stales_prepared_projection(agent, monkeypatch):
    messages = _messages()
    _handle(agent, messages, 128_000)
    _wait_for_state(agent, {"ready"})
    agent._session_db._conn.execute(
        "UPDATE compression_jobs SET prepared_auxiliary_route_fingerprint=? "
        "WHERE session_id=?",
        ("other-route", agent.session_id),
    )
    agent._session_db._conn.commit()
    commit = MagicMock()
    monkeypatch.setattr(
        "agent.async_context_compression.commit_prepared_context", commit
    )

    outcome = _handle(agent, messages, 205_000)

    assert outcome.action is AsyncCompressionAction.STALE
    assert agent._session_db.get_compression_job(agent.session_id)["state"] == "stale"
    commit.assert_not_called()


def test_route_change_stales_prepared_projection(agent, monkeypatch):
    messages = _messages()
    _handle(agent, messages, 128_000)
    _wait_for_state(agent, {"ready"})
    commit = MagicMock()
    monkeypatch.setattr(
        "agent.async_context_compression.commit_prepared_context", commit
    )

    agent.model = "test/changed"
    outcome = _handle(agent, messages, 205_000)

    assert outcome.action is AsyncCompressionAction.STALE
    assert agent._session_db.get_compression_job(agent.session_id)["state"] == "stale"
    commit.assert_not_called()


def test_runtime_invalidation_clears_feasibility_state(agent):
    agent._compression_prepare_token_cap = 80_000
    agent._compression_warning = "warning for old model"

    invalidate_compression_runtime(agent, reason="model changed")

    assert agent._compression_feasibility_checked is False
    assert agent._compression_prepare_token_cap is None
    assert agent._compression_warning is None


def test_plugin_context_engine_never_uses_durable_coordinator(agent):
    agent._using_builtin_context_compressor = False

    outcome = _handle(agent, _messages(), 226_000)

    assert outcome.action is AsyncCompressionAction.NONE
    assert agent._session_db.get_compression_job(agent.session_id) is None
