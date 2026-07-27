"""Small, content-free timing markers for correlated dashboard chat traces."""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

_TRACE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{7,127}$")
_LATENCY_PATH_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_LatencyObserver = Callable[..., None]
_latency_observer: ContextVar[_LatencyObserver | None] = ContextVar(
    "latency_observer", default=None
)


def clean_latency_trace_id(value: Any) -> str:
    """Return a safe opaque trace id, or an empty string for untrusted input."""
    trace_id = str(value or "").strip()
    return trace_id if _TRACE_ID_RE.fullmatch(trace_id) else ""


def log_latency_stage(
    logger: logging.Logger,
    *,
    trace_id: Any,
    surface: str,
    stage: str,
    started_at: float | None = None,
    outcome: str = "ok",
    path: str | None = None,
) -> None:
    """Emit one joinable timing marker without request, owner, or chat content."""
    clean_trace_id = clean_latency_trace_id(trace_id)
    if not clean_trace_id:
        return
    if path is not None and not _LATENCY_PATH_RE.fullmatch(path):
        return
    elapsed_ms = 0.0 if started_at is None else (time.monotonic() - started_at) * 1000
    logger.info(
        "latency trace_id=%s surface=%s stage=%s elapsed_ms=%.1f outcome=%s%s",
        clean_trace_id,
        surface,
        stage,
        elapsed_ms,
        outcome,
        "" if path is None else f" path={path}",
    )


@contextmanager
def latency_trace_scope(
    logger: logging.Logger, *, trace_id: Any, surface: str
) -> Iterator[None]:
    """Bind one validated trace observer across async thread boundaries."""
    clean_trace_id = clean_latency_trace_id(trace_id)
    if not clean_trace_id:
        yield
        return

    def observe(**fields: Any) -> None:
        log_latency_stage(
            logger,
            trace_id=clean_trace_id,
            surface=surface,
            **fields,
        )

    token = _latency_observer.set(observe)
    try:
        yield
    finally:
        _latency_observer.reset(token)


def observe_latency_stage(
    *,
    stage: str,
    started_at: float | None = None,
    outcome: str = "ok",
    path: str | None = None,
) -> None:
    """Emit through the active trace observer without affecting its caller."""
    observer = _latency_observer.get()
    if observer is None:
        return
    try:
        observer(stage=stage, started_at=started_at, outcome=outcome, path=path)
    except Exception:
        pass


@contextmanager
def observed_latency_stage(*, stage: str, path: str) -> Iterator[None]:
    """Time one stage and preserve the exact result or exception."""
    started_at = time.monotonic()
    try:
        yield
    except BaseException:
        observe_latency_stage(
            stage=stage,
            started_at=started_at,
            outcome="error",
            path=path,
        )
        raise
    observe_latency_stage(stage=stage, started_at=started_at, path=path)
