"""Speculative background preparation for built-in context compression."""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

from agent.conversation_compression import commit_prepared_context

logger = logging.getLogger(__name__)


class AsyncCompressionAction(Enum):
    NONE = "none"
    PREPARING = "preparing"
    READY = "ready"
    COMMITTED = "committed"
    FAILED = "failed"
    STALE = "stale"
    SYNCHRONOUS_FALLBACK = "synchronous_fallback"


@dataclass(frozen=True)
class CompressionThresholds:
    prepare: int
    commit: int
    emergency: int


@dataclass(frozen=True)
class PreparedSnapshot:
    session_key: tuple[str, str]
    session_id: str
    model: str
    snapshot_length: int
    snapshot_digest: str
    compression: Any


@dataclass
class _PreparationRecord:
    session_id: str
    model: str
    snapshot_length: int
    snapshot_digest: str
    future: Optional[Future] = None
    stale: bool = False
    stale_reason: str = ""


@dataclass(frozen=True)
class AsyncCompressionOutcome:
    action: AsyncCompressionAction
    messages: list
    system_prompt: str


_registry: dict[tuple[str, str], _PreparationRecord] = {}
_registry_lock = threading.Lock()
_executor = None
_executor_lock = threading.Lock()


def _session_key(agent: Any) -> tuple[str, str]:
    db = getattr(agent, "_session_db", None)
    path = getattr(db, "db_path", None) or getattr(db, "_db_path", None)
    owner = str(path) if path else f"agent:{id(agent)}"
    return owner, str(getattr(agent, "session_id", "") or "")


def _message_digest(messages: list) -> str:
    payload = json.dumps(
        messages,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8", errors="replace")
    return hashlib.sha256(payload).hexdigest()


def prepared_snapshot_is_current(prepared: PreparedSnapshot, agent: Any, messages: list) -> bool:
    if _session_key(agent) != prepared.session_key:
        return False
    if str(getattr(agent, "session_id", "") or "") != prepared.session_id:
        return False
    if str(getattr(agent, "model", "") or "") != prepared.model:
        return False
    if len(messages) < prepared.snapshot_length:
        return False
    return _message_digest(messages[: prepared.snapshot_length]) == prepared.snapshot_digest


def compression_thresholds(agent: Any) -> CompressionThresholds:
    compressor = agent.context_compressor
    usable = max(
        1,
        int(compressor.context_length)
        - int(getattr(compressor, "max_tokens", 0) or 0),
    )
    configured_prepare = int(
        usable * float(getattr(agent, "compression_prepare_threshold", 0.50))
    )
    # The live compressor threshold carries model/provider route overrides.
    # A user may raise async preparation above that floor. Auxiliary-model
    # feasibility may then impose a lower safety cap so the detached summary
    # request never exceeds the model selected to produce it.
    prepare = max(
        int(getattr(compressor, "threshold_tokens", 0) or 0),
        configured_prepare,
    )
    feasibility_cap = getattr(
        agent,
        "_compression_prepare_token_cap",
        None,
    )
    if feasibility_cap is not None:
        prepare = min(prepare, max(1, int(feasibility_cap)))
    commit = max(
        prepare,
        int(
            usable
            * float(getattr(agent, "compression_commit_threshold", 0.80))
        ),
    )
    emergency = max(
        commit,
        int(
            usable
            * float(getattr(agent, "compression_emergency_threshold", 0.88))
        ),
    )
    return CompressionThresholds(
        prepare=prepare,
        commit=commit,
        emergency=emergency,
    )


def async_compression_enabled(agent: Any) -> bool:
    return bool(
        getattr(agent, "compression_enabled", True)
        and getattr(agent, "compression_async_prepare", False)
        and getattr(agent, "_using_builtin_context_compressor", False)
        and hasattr(getattr(agent, "context_compressor", None), "prepare_compression")
    )


def _get_executor(agent: Any):
    injected = getattr(agent, "_async_compression_executor", None)
    if injected is not None:
        return injected
    global _executor
    if _executor is None:
        with _executor_lock:
            if _executor is None:
                from tools.daemon_pool import DaemonThreadPoolExecutor

                _executor = DaemonThreadPoolExecutor(
                    max_workers=1,
                    thread_name_prefix="context-prepare",
                )
    return _executor


def _prepare(
    compressor: Any,
    snapshot: list,
    current_tokens: int,
    session_key: tuple[str, str],
    session_id: str,
    model: str,
    snapshot_digest: str,
) -> PreparedSnapshot:
    deadline = time.monotonic() + 360.0
    compression = compressor.prepare_compression(
        snapshot,
        current_tokens=current_tokens,
        deadline_monotonic=deadline,
    )
    return PreparedSnapshot(
        session_key=session_key,
        session_id=session_id,
        model=model,
        snapshot_length=len(snapshot),
        snapshot_digest=snapshot_digest,
        compression=compression,
    )


def _start(agent: Any, messages: list, current_tokens: int) -> _PreparationRecord:
    key = _session_key(agent)
    snapshot = copy.deepcopy(messages)
    digest = _message_digest(snapshot)
    record = _PreparationRecord(
        session_id=str(getattr(agent, "session_id", "") or ""),
        model=str(getattr(agent, "model", "") or ""),
        snapshot_length=len(snapshot),
        snapshot_digest=digest,
    )
    future = _get_executor(agent).submit(
        _prepare,
        agent.context_compressor,
        snapshot,
        current_tokens,
        key,
        record.session_id,
        record.model,
        digest,
    )
    record.future = future
    _registry[key] = record
    logger.info(
        "async compression prepare started: session=%s messages=%d tokens=~%d",
        record.session_id or "none",
        len(snapshot),
        current_tokens,
    )
    return record


def invalidate_compression_runtime(
    agent: Any,
    *,
    reason: str = "model changed",
) -> None:
    """Invalidate a snapshot and force auxiliary feasibility to be re-probed."""
    invalidate_preparation(agent, reason=reason)
    agent._compression_feasibility_checked = False
    agent._compression_prepare_token_cap = None
    agent._compression_warning = None


def invalidate_preparation(agent: Any, *, reason: str = "session changed") -> None:
    key = _session_key(agent)
    with _registry_lock:
        record = _registry.get(key)
        if record is None:
            return
        record.stale = True
        record.stale_reason = reason
        if record.future is not None:
            record.future.cancel()


def _sync_fallback(
    agent: Any,
    messages: list,
    system_message: str,
    current_tokens: int,
    task_id: str,
    emit_abort_warning: bool,
) -> AsyncCompressionOutcome:
    compressed, prompt = agent._compress_context(
        messages,
        system_message,
        approx_tokens=current_tokens,
        task_id=task_id,
        emit_abort_warning=emit_abort_warning,
    )
    return AsyncCompressionOutcome(
        AsyncCompressionAction.SYNCHRONOUS_FALLBACK,
        compressed,
        prompt,
    )


def maybe_handle_async_compression(
    agent: Any,
    messages: list,
    system_message: str,
    *,
    current_tokens: int,
    task_id: str = "default",
    emit_abort_warning: bool = True,
) -> AsyncCompressionOutcome:
    """Advance prepare/commit state at a caller-provided safe boundary."""
    if not async_compression_enabled(agent):
        return AsyncCompressionOutcome(AsyncCompressionAction.NONE, messages, system_message)

    thresholds = compression_thresholds(agent)
    # Probe once the transcript reaches the lowest auxiliary context Hermes
    # supports. Waiting until the configured prepare threshold would discover a
    # smaller auxiliary window too late; probing earlier would add startup cost
    # to short sessions that never approach compression pressure.
    if not getattr(agent, "_compression_feasibility_checked", False):
        from agent.model_metadata import MINIMUM_CONTEXT_LENGTH

        if current_tokens >= min(thresholds.prepare, MINIMUM_CONTEXT_LENGTH):
            from agent.conversation_compression import (
                check_compression_model_feasibility,
            )

            check_compression_model_feasibility(agent)
            agent._compression_feasibility_checked = True
            thresholds = compression_thresholds(agent)

    if current_tokens < thresholds.prepare:
        return AsyncCompressionOutcome(AsyncCompressionAction.NONE, messages, system_message)
    if not agent.context_compressor.should_compress(current_tokens):
        return AsyncCompressionOutcome(AsyncCompressionAction.NONE, messages, system_message)

    key = _session_key(agent)
    with _registry_lock:
        record = _registry.get(key)
        stale_record = record is not None and record.stale
        if stale_record:
            _registry.pop(key, None)
    if stale_record:
        if current_tokens >= thresholds.emergency:
            return _sync_fallback(
                agent,
                messages,
                system_message,
                current_tokens,
                task_id,
                emit_abort_warning,
            )
        return AsyncCompressionOutcome(
            AsyncCompressionAction.STALE,
            messages,
            system_message,
        )

    if record is None:
        with _registry_lock:
            record = _registry.get(key)
            stale_record = record is not None and record.stale
            if record is None:
                record = _start(agent, messages, current_tokens)
            elif stale_record:
                _registry.pop(key, None)
        if stale_record:
            if current_tokens >= thresholds.emergency:
                return _sync_fallback(
                    agent,
                    messages,
                    system_message,
                    current_tokens,
                    task_id,
                    emit_abort_warning,
                )
            return AsyncCompressionOutcome(
                AsyncCompressionAction.STALE,
                messages,
                system_message,
            )

    if record.future is None:
        with _registry_lock:
            _registry.pop(key, None)
        return _sync_fallback(
            agent, messages, system_message, current_tokens, task_id, emit_abort_warning
        )

    prepared: Optional[PreparedSnapshot] = None
    if current_tokens >= thresholds.emergency:
        try:
            prepared = record.future.result(
                timeout=float(getattr(agent, "compression_emergency_wait_seconds", 15.0))
            )
        except Exception as exc:
            logger.warning("async compression emergency wait failed: %s", exc)
            with _registry_lock:
                _registry.pop(key, None)
            return _sync_fallback(
                agent, messages, system_message, current_tokens, task_id, emit_abort_warning
            )
    elif record.future.done():
        try:
            prepared = record.future.result()
        except Exception as exc:
            logger.warning("async compression preparation failed: %s", exc)
            with _registry_lock:
                _registry.pop(key, None)
            return AsyncCompressionOutcome(AsyncCompressionAction.FAILED, messages, system_message)
    else:
        return AsyncCompressionOutcome(AsyncCompressionAction.PREPARING, messages, system_message)

    if prepared is None:
        return AsyncCompressionOutcome(AsyncCompressionAction.PREPARING, messages, system_message)
    if not prepared_snapshot_is_current(prepared, agent, messages):
        with _registry_lock:
            _registry.pop(key, None)
        if current_tokens >= thresholds.emergency:
            return _sync_fallback(
                agent, messages, system_message, current_tokens, task_id, emit_abort_warning
            )
        return AsyncCompressionOutcome(AsyncCompressionAction.STALE, messages, system_message)
    if prepared.compression.aborted or not prepared.compression.applied:
        with _registry_lock:
            _registry.pop(key, None)
        if current_tokens >= thresholds.emergency:
            return _sync_fallback(
                agent, messages, system_message, current_tokens, task_id, emit_abort_warning
            )
        return AsyncCompressionOutcome(AsyncCompressionAction.FAILED, messages, system_message)
    if current_tokens < thresholds.commit:
        return AsyncCompressionOutcome(AsyncCompressionAction.READY, messages, system_message)

    try:
        compressed, prompt = commit_prepared_context(
            agent=agent,
            messages=messages,
            system_message=system_message,
            prepared=prepared,
            approx_tokens=current_tokens,
            task_id=task_id,
            emit_abort_warning=emit_abort_warning,
        )
    except ValueError:
        with _registry_lock:
            _registry.pop(key, None)
        if current_tokens >= thresholds.emergency:
            return _sync_fallback(
                agent, messages, system_message, current_tokens, task_id, emit_abort_warning
            )
        return AsyncCompressionOutcome(AsyncCompressionAction.STALE, messages, system_message)

    with _registry_lock:
        _registry.pop(key, None)
    return AsyncCompressionOutcome(AsyncCompressionAction.COMMITTED, compressed, prompt)


def _reset_async_compression_for_tests() -> None:
    global _executor
    with _registry_lock:
        for record in _registry.values():
            if record.future is not None:
                record.future.cancel()
        _registry.clear()
    if _executor is not None:
        _executor.shutdown(wait=False, cancel_futures=True)
        _executor = None
