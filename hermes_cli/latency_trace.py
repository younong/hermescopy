"""Small, content-free timing markers for correlated dashboard chat traces."""

from __future__ import annotations

import logging
import re
import time
from typing import Any

_TRACE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{7,127}$")


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
) -> None:
    """Emit one joinable timing marker without request, owner, or chat content."""
    clean_trace_id = clean_latency_trace_id(trace_id)
    if not clean_trace_id:
        return
    elapsed_ms = 0.0 if started_at is None else (time.monotonic() - started_at) * 1000
    logger.info(
        "latency trace_id=%s surface=%s stage=%s elapsed_ms=%.1f outcome=%s",
        clean_trace_id,
        surface,
        stage,
        elapsed_ms,
        outcome,
    )
