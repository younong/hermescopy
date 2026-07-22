"""Content-free diagnostics for provider streaming attempts.

The hot path keeps only counters, timestamps, and fixed-bucket histograms. Raw
chunks, deltas, reasoning, tool payloads, and request identifiers are never
retained by the aggregate metrics.
"""

from __future__ import annotations

import logging
import math
import time
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)


# Failure-forensics-only response headers. These are never emitted by the
# aggregate stream summary below. In particular, do not collect forwarded IPs.
STREAM_DIAG_HEADERS = (
    "cf-ray",
    "cf-cache-status",
    "x-openrouter-provider",
    "x-openrouter-model",
    "x-openrouter-id",
    "x-request-id",
    "x-vercel-id",
    "via",
    "server",
)

STREAM_GAP_BUCKETS_MS: tuple[float, ...] = (
    5,
    10,
    25,
    50,
    100,
    250,
    500,
    1000,
    2500,
    5000,
    10000,
    30000,
)
_STREAM_OUTCOMES = frozenset({"success", "error", "interrupted"})


def fixed_histogram(bounds: Sequence[float]) -> list[int]:
    """Return zeroed fixed buckets, including one overflow bucket."""
    return [0] * (len(bounds) + 1)


def fixed_histogram_observe(
    counts: list[int], bounds: Sequence[float], value: float
) -> None:
    """Record one finite non-negative sample into fixed buckets."""
    if not math.isfinite(value) or value < 0:
        return
    for index, upper in enumerate(bounds):
        if value <= upper:
            counts[index] += 1
            return
    counts[-1] += 1


def fixed_histogram_percentile(
    counts: Sequence[int], bounds: Sequence[float], maximum: float, percentile: float
) -> float:
    """Return the covering bucket upper bound, or exact max for overflow."""
    total = sum(max(0, int(count)) for count in counts)
    if total <= 0:
        return 0.0
    rank = max(1, math.ceil(percentile * total))
    cumulative = 0
    for index, count in enumerate(counts):
        cumulative += max(0, int(count))
        if cumulative >= rank:
            return float(bounds[index]) if index < len(bounds) else max(0.0, maximum)
    return max(0.0, maximum)


def stream_diag_init() -> Dict[str, Any]:
    """Return fresh, constant-memory state for one provider stream attempt."""
    return {
        # Preserve wall-clock fields consumed by the existing retry UI/logging.
        "started_at": time.time(),
        "first_chunk_at": None,
        # Monotonic fields drive aggregate durations and are immune to clock jumps.
        "started_monotonic": time.monotonic(),
        "response_opened_monotonic": None,
        "first_event_monotonic": None,
        "first_visible_monotonic": None,
        "last_event_monotonic": None,
        "event_gap_max_ms": 0.0,
        "event_gap_histogram": fixed_histogram(STREAM_GAP_BUCKETS_MS),
        "chunks": 0,
        "bytes": 0,
        "headers": {},
        "http_status": None,
        "finalized": False,
    }


def stream_diag_capture_response(agent: Any, diag: Dict[str, Any], http_response: Any) -> None:
    """Mark response-open and snapshot failure-forensics HTTP metadata."""
    if not isinstance(diag, dict):
        return
    if diag.get("response_opened_monotonic") is None:
        diag["response_opened_monotonic"] = time.monotonic()
    if http_response is None:
        return
    try:
        diag["http_status"] = getattr(http_response, "status_code", None)
    except Exception:
        pass
    try:
        headers = getattr(http_response, "headers", None) or {}
        captured: Dict[str, str] = {}
        target_headers = getattr(agent, "_STREAM_DIAG_HEADERS", STREAM_DIAG_HEADERS)
        for name in target_headers:
            try:
                val = headers.get(name)
                if val:
                    captured[name] = str(val)[:120]
            except Exception:
                continue
        diag["headers"] = captured
    except Exception:
        pass


def stream_diag_record_event(
    diag: Dict[str, Any], event: Any = None, *, now: float | None = None
) -> None:
    """Record one raw SDK event without retaining its content."""
    if not isinstance(diag, dict) or diag.get("finalized"):
        return
    observed = time.monotonic() if now is None else now
    previous = diag.get("last_event_monotonic")
    if previous is not None:
        # Round sub-nanosecond float noise so exact bucket boundaries remain
        # stable (for example 50 ms must not spill into the 100 ms bucket).
        gap_ms = round(max(0.0, (observed - float(previous)) * 1000.0), 6)
        diag["event_gap_max_ms"] = max(float(diag.get("event_gap_max_ms") or 0.0), gap_ms)
        fixed_histogram_observe(
            diag["event_gap_histogram"], STREAM_GAP_BUCKETS_MS, gap_ms
        )
    else:
        diag["first_event_monotonic"] = observed
        # Preserve the existing wall-clock TTFB field.
        diag["first_chunk_at"] = time.time()
    diag["last_event_monotonic"] = observed
    diag["chunks"] = int(diag.get("chunks") or 0) + 1
    try:
        diag["bytes"] = int(diag.get("bytes") or 0) + len(repr(event))
    except Exception:
        pass


def stream_diag_mark_visible(diag: Dict[str, Any], *, now: float | None = None) -> None:
    """Mark the first delta that reaches an existing display callback seam."""
    if not isinstance(diag, dict) or diag.get("finalized"):
        return
    if diag.get("first_visible_monotonic") is None:
        diag["first_visible_monotonic"] = time.monotonic() if now is None else now


def _elapsed_ms(diag: Dict[str, Any], key: str) -> float | None:
    value = diag.get(key)
    if value is None:
        return None
    return max(0.0, (float(value) - float(diag["started_monotonic"])) * 1000.0)


def stream_diag_finalize(
    diag: Dict[str, Any], *, outcome: str, now: float | None = None
) -> Dict[str, Any] | None:
    """Log and return one identifier-free summary; subsequent calls are no-ops."""
    if not isinstance(diag, dict) or diag.get("finalized"):
        return None
    if outcome not in _STREAM_OUTCOMES:
        outcome = "error"
    diag["finalized"] = True
    ended = time.monotonic() if now is None else now
    counts = tuple(int(count) for count in diag.get("event_gap_histogram") or ())
    maximum = float(diag.get("event_gap_max_ms") or 0.0)
    summary = {
        "outcome": outcome,
        "duration_ms": max(
            0.0, (ended - float(diag.get("started_monotonic") or ended)) * 1000.0
        ),
        "response_open_ms": _elapsed_ms(diag, "response_opened_monotonic"),
        "first_event_ms": _elapsed_ms(diag, "first_event_monotonic"),
        "first_visible_ms": _elapsed_ms(diag, "first_visible_monotonic"),
        "events": int(diag.get("chunks") or 0),
        "estimated_event_bytes": int(diag.get("bytes") or 0),
        "event_gap_max_ms": maximum,
        "event_gap_p95_ms": fixed_histogram_percentile(
            counts, STREAM_GAP_BUCKETS_MS, maximum, 0.95
        ),
    }
    logger.info(
        "stream aggregate outcome=%s duration_ms=%.1f response_open_ms=%s "
        "first_event_ms=%s first_visible_ms=%s events=%d "
        "estimated_event_bytes=%d event_gap_max_ms=%.1f event_gap_p95_ms=%.1f",
        summary["outcome"],
        summary["duration_ms"],
        _format_optional_ms(summary["response_open_ms"]),
        _format_optional_ms(summary["first_event_ms"]),
        _format_optional_ms(summary["first_visible_ms"]),
        summary["events"],
        summary["estimated_event_bytes"],
        summary["event_gap_max_ms"],
        summary["event_gap_p95_ms"],
    )
    return summary


def _format_optional_ms(value: float | None) -> str:
    return "-" if value is None else f"{value:.1f}"


def flatten_exception_chain(error: BaseException) -> str:
    """Return a compact ``Outer(msg) <- Inner(msg) <- ...`` rendering."""
    seen: List[BaseException] = []
    link: Optional[BaseException] = error
    while link is not None and len(seen) < 4:
        if link in seen:
            break
        seen.append(link)
        nxt = getattr(link, "__cause__", None) or getattr(link, "__context__", None)
        if nxt is None or nxt is link:
            break
        link = nxt
    parts: List[str] = []
    for error_item in seen:
        msg = str(error_item).strip().replace("\n", " ")
        if len(msg) > 140:
            msg = msg[:140] + "…"
        parts.append(
            f"{type(error_item).__name__}({msg})" if msg else type(error_item).__name__
        )
    return " <- ".join(parts) if parts else type(error).__name__


def log_stream_retry(
    agent: Any,
    *,
    kind: str,
    error: BaseException,
    attempt: int,
    max_attempts: int,
    mid_tool_call: bool,
    diag: Optional[Dict[str, Any]] = None,
) -> None:
    """Record a transient stream drop with failure-forensics detail."""
    try:
        try:
            summary = agent._summarize_api_error(error)
        except Exception:
            summary = str(error)
        if summary and len(summary) > 240:
            summary = summary[:240] + "…"
        try:
            chain = flatten_exception_chain(error)
        except Exception:
            chain = type(error).__name__

        current = time.time()
        estimated_bytes = 0
        chunks = 0
        elapsed = 0.0
        ttfb = None
        headers_repr = "-"
        http_status = "-"
        if isinstance(diag, dict):
            try:
                estimated_bytes = int(diag.get("bytes") or 0)
                chunks = int(diag.get("chunks") or 0)
                started = float(diag.get("started_at") or current)
                elapsed = max(0.0, current - started)
                first = diag.get("first_chunk_at")
                if first is not None:
                    ttfb = max(0.0, float(first) - started)
                headers = diag.get("headers") or {}
                if isinstance(headers, dict) and headers:
                    headers_repr = " ".join(f"{k}={v}" for k, v in headers.items())
                if diag.get("http_status") is not None:
                    http_status = str(diag.get("http_status"))
            except Exception:
                pass

        logger.warning(
            "Stream %s on attempt %s/%s — retrying. "
            "subagent_id=%s depth=%s provider=%s base_url=%s "
            "error_type=%s error=%s chain=%s "
            "http_status=%s estimated_event_bytes=%d chunks=%d elapsed=%.2fs ttfb=%s "
            "upstream=[%s]",
            kind,
            attempt,
            max_attempts,
            getattr(agent, "_subagent_id", None) or "-",
            getattr(agent, "_delegate_depth", 0),
            agent.provider or "-",
            agent.base_url or "-",
            type(error).__name__,
            summary,
            chain,
            http_status,
            estimated_bytes,
            chunks,
            elapsed,
            f"{ttfb:.2f}s" if ttfb is not None else "-",
            headers_repr,
            extra={"mid_tool_call": mid_tool_call},
        )
    except Exception:
        logger.debug("stream-retry log emit failed", exc_info=True)


def emit_stream_drop(
    agent: Any,
    *,
    error: BaseException,
    attempt: int,
    max_attempts: int,
    mid_tool_call: bool,
    diag: Optional[Dict[str, Any]] = None,
) -> None:
    """Emit the existing compact user-visible stream drop status."""
    kind = "drop mid tool-call" if mid_tool_call else "drop"
    log_stream_retry(
        agent,
        kind=kind,
        error=error,
        attempt=attempt,
        max_attempts=max_attempts,
        mid_tool_call=mid_tool_call,
        diag=diag,
    )
    provider = agent.provider or "provider"
    suffix = ""
    if isinstance(diag, dict):
        try:
            started = diag.get("started_at")
            if started is not None:
                suffix = f" after {max(0.0, time.time() - float(started)):.1f}s"
        except Exception:
            pass
    try:
        agent._buffer_status(
            f"⚠️ {provider} stream {kind} ({type(error).__name__}){suffix} "
            f"— reconnecting, retry {attempt}/{max_attempts}"
        )
        agent._touch_activity(
            f"stream retry {attempt}/{max_attempts} after {type(error).__name__}"
        )
    except Exception:
        pass


__all__ = [
    "STREAM_DIAG_HEADERS",
    "STREAM_GAP_BUCKETS_MS",
    "fixed_histogram",
    "fixed_histogram_observe",
    "fixed_histogram_percentile",
    "stream_diag_init",
    "stream_diag_capture_response",
    "stream_diag_record_event",
    "stream_diag_mark_visible",
    "stream_diag_finalize",
    "flatten_exception_chain",
    "log_stream_retry",
    "emit_stream_drop",
]
