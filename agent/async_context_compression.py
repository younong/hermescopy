"""Durable background preparation for built-in context compression."""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Optional

from agent.context_compressor import PreparedCompression
from agent.conversation_compression import commit_prepared_context

logger = logging.getLogger(__name__)

_PREPARATION_DEADLINE_SECONDS = 360.0
_PREPARATION_LEASE_SECONDS = 420.0


class AsyncCompressionAction(Enum):
    NONE = "none"
    PREPARING = "preparing"
    READY = "ready"
    COMMITTED = "committed"
    FAILED = "failed"
    STALE = "stale"


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


@dataclass(frozen=True)
class AsyncCompressionOutcome:
    action: AsyncCompressionAction
    messages: list
    system_prompt: str


_orchestrators: dict[str, "_CompressionOrchestrator"] = {}
_orchestrators_lock = threading.Lock()


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


def _route_fingerprint(*parts: Any) -> str:
    """Hash a route identity without persisting provider URLs or credentials."""
    identity = "\x1f".join(str(part or "").strip().lower() for part in parts)
    return hashlib.sha256(identity.encode("utf-8", errors="replace")).hexdigest()


def _main_route_fingerprint(agent: Any) -> str:
    compressor = agent.context_compressor
    return _route_fingerprint(
        getattr(agent, "provider", None) or getattr(compressor, "provider", None),
        getattr(agent, "base_url", None) or getattr(compressor, "base_url", None),
        getattr(agent, "model", None) or getattr(compressor, "model", None),
        getattr(agent, "api_mode", None) or getattr(compressor, "api_mode", None),
    )


def _auxiliary_route(agent: Any) -> tuple[str, Optional[int]]:
    """Return the configured auxiliary identity without provider I/O.

    Automatic turn handling must remain non-blocking. The detached compressor
    resolves authoritative capacity in its background worker before chunking.
    """
    compressor = agent.context_compressor
    return (
        _route_fingerprint(
            getattr(compressor, "provider", None),
            getattr(compressor, "base_url", None),
            getattr(compressor, "summary_model", None)
            or getattr(compressor, "model", None),
            getattr(compressor, "api_mode", None),
        ),
        None,
    )


def _emit_compression_status(agent: Any, kind: str, message: str) -> None:
    emit = getattr(agent, "_emit_status", None)
    if emit is None:
        return
    try:
        emit(message, kind=kind)
    except Exception:
        logger.debug("compression status callback failed", exc_info=True)


def _serialize_prepared(prepared: PreparedSnapshot) -> str:
    return json.dumps(
        {
            "session_key": list(prepared.session_key),
            "session_id": prepared.session_id,
            "model": prepared.model,
            "snapshot_length": prepared.snapshot_length,
            "snapshot_digest": prepared.snapshot_digest,
            "compression": asdict(prepared.compression),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _deserialize_prepared(payload: str) -> PreparedSnapshot:
    value = json.loads(payload)
    compression = value["compression"]
    return PreparedSnapshot(
        session_key=tuple(value["session_key"]),
        session_id=str(value["session_id"]),
        model=str(value["model"]),
        snapshot_length=int(value["snapshot_length"]),
        snapshot_digest=str(value["snapshot_digest"]),
        compression=PreparedCompression(
            compressed_messages=compression["compressed_messages"],
            compressor_state=compression["compressor_state"],
            aborted=bool(compression["aborted"]),
            applied=bool(compression.get("applied", True)),
            auxiliary_route_fingerprint=compression.get(
                "auxiliary_route_fingerprint"
            ),
            auxiliary_context_length=compression.get(
                "auxiliary_context_length"
            ),
            auxiliary_input_budget=compression.get("auxiliary_input_budget"),
        ),
    )


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
    prepare = max(
        int(getattr(compressor, "threshold_tokens", 0) or 0),
        configured_prepare,
    )
    feasibility_cap = getattr(agent, "_compression_prepare_token_cap", None)
    if feasibility_cap is not None:
        prepare = min(prepare, max(1, int(feasibility_cap)))
    commit = max(
        prepare,
        int(usable * float(getattr(agent, "compression_commit_threshold", 0.80))),
    )
    emergency = max(
        commit,
        int(usable * float(getattr(agent, "compression_emergency_threshold", 0.88))),
    )
    return CompressionThresholds(prepare=prepare, commit=commit, emergency=emergency)


def async_compression_enabled(agent: Any) -> bool:
    db = getattr(agent, "_session_db", None)
    return bool(
        getattr(agent, "compression_enabled", True)
        and getattr(agent, "_using_builtin_context_compressor", False)
        and getattr(agent, "session_id", "")
        and db is not None
        and hasattr(db, "enqueue_compression_job")
        and hasattr(getattr(agent, "context_compressor", None), "prepare_compression")
    )


class _CompressionOrchestrator:
    def __init__(self, db_path: str) -> None:
        from tools.daemon_pool import DaemonThreadPoolExecutor

        self.db_path = db_path
        self.executor = DaemonThreadPoolExecutor(
            max_workers=4,
            thread_name_prefix="context-prepare",
        )
        self.active: set[tuple[str, str]] = set()
        self.lock = threading.Lock()
        self.holder = f"{os.getpid()}:{uuid.uuid4().hex}"

    def submit(self, agent: Any, session_id: str, job_id: str) -> None:
        key = (session_id, job_id)
        with self.lock:
            if key in self.active:
                return
            self.active.add(key)
        self.executor.submit(self._run, agent, session_id, job_id, key)

    def _run(
        self,
        agent: Any,
        session_id: str,
        job_id: str,
        active_key: tuple[str, str],
    ) -> None:
        db = agent._session_db
        try:
            claimed = db.claim_compression_job(
                session_id=session_id,
                job_id=job_id,
                holder=self.holder,
                lease_seconds=_PREPARATION_LEASE_SECONDS,
            )
            if claimed is None:
                return
            snapshot_payload = claimed.get("snapshot_payload")
            if not snapshot_payload:
                db.mark_compression_job_stale(
                    session_id,
                    job_id,
                    failure_code="snapshot_unavailable",
                )
                return
            snapshot = json.loads(snapshot_payload)
            lease_version = int(claimed["lease_version"])
            deadline_at = float(
                claimed.get("deadline_at")
                or time.time() + _PREPARATION_DEADLINE_SECONDS
            )
            if deadline_at <= time.time():
                db.mark_compression_job_degraded(
                    session_id=session_id,
                    job_id=job_id,
                    holder=self.holder,
                    lease_version=lease_version,
                    failure_code="deadline_exceeded",
                )
                _emit_compression_status(
                    agent,
                    "compression.degraded",
                    "Context compression preparation expired without changing the session.",
                )
                return
            if claimed.get("main_route_fingerprint") != _main_route_fingerprint(agent):
                db.mark_compression_job_stale(session_id, job_id)
                return
            auxiliary_fingerprint, _ = _auxiliary_route(agent)
            if claimed.get("auxiliary_route_fingerprint") != auxiliary_fingerprint:
                db.mark_compression_job_stale(session_id, job_id)
                return

            def checkpoint(chunk_cursor: int, rolling_summary: str) -> None:
                if not db.refresh_compression_job_lease(
                    session_id=session_id,
                    job_id=job_id,
                    holder=self.holder,
                    lease_version=lease_version,
                    lease_seconds=_PREPARATION_LEASE_SECONDS,
                ):
                    raise RuntimeError("compression job lease lost during refresh")
                if not db.checkpoint_compression_job(
                    session_id=session_id,
                    job_id=job_id,
                    holder=self.holder,
                    lease_version=lease_version,
                    chunk_cursor=chunk_cursor,
                    rolling_summary=rolling_summary,
                ):
                    raise RuntimeError("compression job lease lost during checkpoint")

            prepared = _prepare(
                agent.context_compressor,
                snapshot,
                int(claimed.get("main_budget_tokens") or 0),
                _session_key(agent),
                session_id,
                str(getattr(agent, "model", "") or ""),
                str(claimed["snapshot_digest"]),
                chunk_cursor=int(claimed.get("chunk_cursor") or 0),
                rolling_summary=claimed.get("rolling_summary"),
                checkpoint=checkpoint,
                deadline_at=deadline_at,
            )
            if prepared.compression.aborted or not prepared.compression.applied:
                failure_code = "summary_failure"
                error = str(
                    prepared.compression.compressor_state.get(
                        "_last_summary_error", ""
                    )
                    or ""
                ).lower()
                if "atomic tool group" in error:
                    db.mark_compression_job_degraded(
                        session_id=session_id,
                        job_id=job_id,
                        holder=self.holder,
                        lease_version=lease_version,
                        failure_code="atomic_group_too_large",
                    )
                    _emit_compression_status(
                        agent,
                        "compression.degraded",
                        "Context compression cannot safely summarize one tool exchange. No messages were dropped.",
                    )
                else:
                    db.mark_compression_job_cooldown(
                        session_id=session_id,
                        job_id=job_id,
                        holder=self.holder,
                        lease_version=lease_version,
                        retry_at=time.time() + 60.0,
                        failure_code=failure_code,
                    )
                    _emit_compression_status(
                        agent,
                        "compression.cooldown",
                        "Context compression is cooling down after a temporary preparation failure.",
                    )
                return
            ready = db.mark_compression_job_ready(
                session_id=session_id,
                job_id=job_id,
                holder=self.holder,
                lease_version=lease_version,
                prepared_payload=_serialize_prepared(prepared),
                auxiliary_route_fingerprint=(
                    prepared.compression.auxiliary_route_fingerprint
                ),
                auxiliary_context_length=(
                    prepared.compression.auxiliary_context_length
                ),
                auxiliary_input_budget=(
                    prepared.compression.auxiliary_input_budget
                ),
            )
            if ready:
                _emit_compression_status(
                    agent,
                    "compression.ready",
                    "Context compression is prepared and ready to apply.",
                )
        except Exception as exc:
            logger.warning(
                "durable compression preparation failed: session=%s error_type=%s",
                session_id,
                type(exc).__name__,
            )
            job = db.get_compression_job(session_id)
            if job and job["job_id"] == job_id and job["state"] == "preparing":
                db.mark_compression_job_cooldown(
                    session_id=session_id,
                    job_id=job_id,
                    holder=self.holder,
                    lease_version=int(job["lease_version"]),
                    retry_at=time.time() + min(900.0, 30.0 * 2 ** min(5, int(job["attempt_count"]))),
                    failure_code="preparation_failure",
                )
                _emit_compression_status(
                    agent,
                    "compression.cooldown",
                    "Context compression is cooling down after a temporary preparation failure.",
                )
        finally:
            with self.lock:
                self.active.discard(active_key)

    def close(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=True)


def _orchestrator(agent: Any) -> _CompressionOrchestrator:
    db = agent._session_db
    path = str(getattr(db, "db_path", None) or getattr(db, "_db_path", None) or id(db))
    with _orchestrators_lock:
        orchestrator = _orchestrators.get(path)
        if orchestrator is None:
            orchestrator = _CompressionOrchestrator(path)
            _orchestrators[path] = orchestrator
        return orchestrator


def _prepare(
    compressor: Any,
    snapshot: list,
    current_tokens: int,
    session_key: tuple[str, str],
    session_id: str,
    model: str,
    snapshot_digest: str,
    *,
    chunk_cursor: int = 0,
    rolling_summary: Optional[str] = None,
    checkpoint: Any = None,
    deadline_at: Optional[float] = None,
) -> PreparedSnapshot:
    remaining = max(0.0, float(deadline_at or time.time()) - time.time())
    compression = compressor.prepare_compression(
        snapshot,
        current_tokens=current_tokens,
        deadline_monotonic=time.monotonic() + remaining,
        chunk_cursor=chunk_cursor,
        rolling_summary=rolling_summary,
        checkpoint=checkpoint,
    )
    return PreparedSnapshot(
        session_key=session_key,
        session_id=session_id,
        model=model,
        snapshot_length=len(snapshot),
        snapshot_digest=snapshot_digest,
        compression=compression,
    )


def invalidate_compression_runtime(agent: Any, *, reason: str = "model changed") -> None:
    invalidate_preparation(agent, reason=reason)
    agent._compression_feasibility_checked = False
    agent._compression_prepare_token_cap = None
    agent._compression_warning = None


def invalidate_preparation(agent: Any, *, reason: str = "session changed") -> None:
    db = getattr(agent, "_session_db", None)
    session_id = str(getattr(agent, "session_id", "") or "")
    if db is not None and session_id and hasattr(db, "cancel_compression_job"):
        db.cancel_compression_job(session_id, failure_code="invalidated")


def _enqueue(agent: Any, messages: list, current_tokens: int) -> dict[str, Any]:
    db = agent._session_db
    session_id = str(agent.session_id)
    snapshot = copy.deepcopy(messages)
    digest = _message_digest(snapshot)
    auxiliary_fingerprint, auxiliary_budget = _auxiliary_route(agent)
    job = db.enqueue_compression_job(
        session_id=session_id,
        job_id=uuid.uuid4().hex,
        fence_id=uuid.uuid4().hex,
        snapshot_message_id=None,
        snapshot_message_count=len(snapshot),
        snapshot_digest=digest,
        snapshot_payload=json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"), default=str),
        main_route_fingerprint=_main_route_fingerprint(agent),
        auxiliary_route_fingerprint=auxiliary_fingerprint,
        main_budget_tokens=current_tokens,
        auxiliary_budget_tokens=auxiliary_budget,
        deadline_at=time.time() + _PREPARATION_DEADLINE_SECONDS,
    )
    _emit_compression_status(
        agent,
        "compression.preparing",
        "Preparing context compression in the background.",
    )
    _orchestrator(agent).submit(agent, session_id, str(job["job_id"]))
    return job


def maybe_handle_async_compression(
    agent: Any,
    messages: list,
    system_message: str,
    *,
    current_tokens: int,
    task_id: str = "default",
    emit_abort_warning: bool = True,
) -> AsyncCompressionOutcome:
    """Enqueue or observe durable preparation without blocking the turn."""
    if not async_compression_enabled(agent):
        return AsyncCompressionOutcome(AsyncCompressionAction.NONE, messages, system_message)

    thresholds = compression_thresholds(agent)
    if not getattr(agent, "_compression_feasibility_checked", False):
        from agent.model_metadata import MINIMUM_CONTEXT_LENGTH

        if current_tokens >= min(thresholds.prepare, MINIMUM_CONTEXT_LENGTH):
            from agent.conversation_compression import check_compression_model_feasibility

            check_compression_model_feasibility(agent)
            agent._compression_feasibility_checked = True
            thresholds = compression_thresholds(agent)

    if current_tokens < thresholds.prepare:
        return AsyncCompressionOutcome(AsyncCompressionAction.NONE, messages, system_message)
    if not agent.context_compressor.should_compress(current_tokens):
        return AsyncCompressionOutcome(AsyncCompressionAction.NONE, messages, system_message)

    db = agent._session_db
    session_id = str(agent.session_id)
    job = db.get_compression_job(session_id)
    if job is None or job["state"] in {"completed", "cancelled", "stale", "degraded"}:
        job = _enqueue(agent, messages, current_tokens)
    elif job["state"] == "cooldown":
        if float(job.get("retry_at") or 0) > time.time():
            return AsyncCompressionOutcome(AsyncCompressionAction.FAILED, messages, system_message)
        if db.requeue_due_compression_job(session_id, str(job["job_id"])):
            job = db.get_compression_job(session_id)
        _orchestrator(agent).submit(agent, session_id, str(job["job_id"]))
    elif job["state"] in {"queued", "preparing"}:
        _orchestrator(agent).submit(agent, session_id, str(job["job_id"]))

    state = str(job["state"])
    if state == "ready":
        prepared_payload = job.get("prepared_payload")
        if not prepared_payload:
            db.mark_compression_job_stale(session_id, str(job["job_id"]))
            return AsyncCompressionOutcome(AsyncCompressionAction.STALE, messages, system_message)
        prepared = _deserialize_prepared(prepared_payload)
        auxiliary_fingerprint, _ = _auxiliary_route(agent)
        prepared_route = prepared.compression.auxiliary_route_fingerprint
        persisted_route = job.get("prepared_auxiliary_route_fingerprint")
        prepared_context_length = prepared.compression.auxiliary_context_length
        prepared_input_budget = prepared.compression.auxiliary_input_budget
        if (
            job.get("main_route_fingerprint") != _main_route_fingerprint(agent)
            or job.get("auxiliary_route_fingerprint") != auxiliary_fingerprint
            or not prepared_route
            or prepared_route != persisted_route
            or prepared_context_length
            != job.get("prepared_auxiliary_context_length")
            or prepared_input_budget != job.get("prepared_auxiliary_input_budget")
            or not prepared_snapshot_is_current(prepared, agent, messages)
        ):
            db.mark_compression_job_stale(session_id, str(job["job_id"]))
            return AsyncCompressionOutcome(AsyncCompressionAction.STALE, messages, system_message)
        if current_tokens < thresholds.commit:
            return AsyncCompressionOutcome(AsyncCompressionAction.READY, messages, system_message)
        holder = f"commit:{os.getpid()}:{threading.get_ident()}:{uuid.uuid4().hex}"
        committing = db.claim_ready_compression_job(
            session_id=session_id,
            job_id=str(job["job_id"]),
            fence_id=str(job["fence_id"]),
            holder=holder,
        )
        if committing is None:
            return AsyncCompressionOutcome(AsyncCompressionAction.PREPARING, messages, system_message)
        try:
            compressed, prompt = commit_prepared_context(
                agent=agent,
                messages=messages,
                system_message=system_message,
                prepared=prepared,
                approx_tokens=current_tokens,
                task_id=task_id,
                emit_abort_warning=emit_abort_warning,
                compression_job=committing,
            )
        except (RuntimeError, ValueError):
            db.mark_compression_job_stale(session_id, str(job["job_id"]))
            return AsyncCompressionOutcome(AsyncCompressionAction.STALE, messages, system_message)
        return AsyncCompressionOutcome(AsyncCompressionAction.COMMITTED, compressed, prompt)
    if state in {"cooldown", "degraded"}:
        return AsyncCompressionOutcome(AsyncCompressionAction.FAILED, messages, system_message)
    if state in {"stale", "cancelled"}:
        return AsyncCompressionOutcome(AsyncCompressionAction.STALE, messages, system_message)
    return AsyncCompressionOutcome(AsyncCompressionAction.PREPARING, messages, system_message)


def _reset_async_compression_for_tests() -> None:
    with _orchestrators_lock:
        orchestrators = tuple(_orchestrators.values())
        _orchestrators.clear()
    for orchestrator in orchestrators:
        orchestrator.close()
