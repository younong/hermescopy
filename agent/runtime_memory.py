"""Bound inference workers and reclaim idle glibc heap pages."""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
import time
from concurrent.futures import Future
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, TypeVar

from tools.daemon_pool import DaemonThreadPoolExecutor

logger = logging.getLogger(__name__)

_T = TypeVar("_T")
_REQUEST_WORKERS = 4
_TRIM_COOLDOWN_SECONDS = 300.0
_FALLBACK_TRIM_THRESHOLD_BYTES = 512 * 1024 * 1024
_MIN_TRIM_THRESHOLD_BYTES = 128 * 1024 * 1024

_executor_lock = threading.Lock()
_executor: DaemonThreadPoolExecutor | None = None
_activity = threading.Condition(threading.Lock())
_active_inference = 0
_trimming = False
_last_trim_at = float("-inf")


def _get_executor() -> DaemonThreadPoolExecutor:
    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = DaemonThreadPoolExecutor(
                max_workers=_REQUEST_WORKERS,
                thread_name_prefix="inference",
            )
        return _executor


@contextmanager
def _inference_activity() -> Iterator[None]:
    global _active_inference
    with _activity:
        while _trimming:
            _activity.wait()
        _active_inference += 1
    try:
        yield
    finally:
        with _activity:
            _active_inference -= 1
            _activity.notify_all()


def submit_inference(fn: Callable[..., _T], /, *args, **kwargs) -> Future[_T]:
    """Run provider work on the bounded daemon pool."""

    def _run() -> _T:
        with _inference_activity():
            return fn(*args, **kwargs)

    return _get_executor().submit(_run)


def current_rss_bytes() -> int | None:
    """Return current Linux RSS without allocating a process-memory snapshot."""
    if not sys.platform.startswith("linux"):
        return None
    try:
        for line in Path("/proc/self/status").read_text(encoding="ascii").splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def _cgroup_memory_max_bytes() -> int | None:
    if not sys.platform.startswith("linux"):
        return None
    try:
        relative = None
        for line in Path("/proc/self/cgroup").read_text(encoding="ascii").splitlines():
            if line.startswith("0::"):
                relative = line.partition("::")[2].lstrip("/")
                break
        if relative is None:
            return None
        raw = (Path("/sys/fs/cgroup") / relative / "memory.max").read_text(
            encoding="ascii"
        ).strip()
        if raw == "max":
            return None
        value = int(raw)
        return value if value > 0 else None
    except (OSError, ValueError):
        return None


def trim_threshold_bytes() -> int:
    limit = _cgroup_memory_max_bytes()
    if limit is None:
        return _FALLBACK_TRIM_THRESHOLD_BYTES
    return max(_MIN_TRIM_THRESHOLD_BYTES, limit // 2)


def maybe_trim_allocator(stage: str) -> bool:
    """Return idle glibc pages when RSS is high; otherwise do nothing."""
    global _last_trim_at, _trimming
    if not sys.platform.startswith("linux"):
        return False

    now = time.monotonic()
    with _activity:
        if (
            _active_inference
            or _trimming
            or now - _last_trim_at < _TRIM_COOLDOWN_SECONDS
        ):
            return False

    rss_before = current_rss_bytes()
    if rss_before is None or rss_before < trim_threshold_bytes():
        return False

    with _activity:
        if (
            _active_inference
            or _trimming
            or now - _last_trim_at < _TRIM_COOLDOWN_SECONDS
        ):
            return False
        _trimming = True
        _last_trim_at = now

    trimmed = False
    try:
        libc = ctypes.CDLL(None)
        malloc_trim = getattr(libc, "malloc_trim", None)
        if malloc_trim is None:
            return False
        malloc_trim.argtypes = [ctypes.c_size_t]
        malloc_trim.restype = ctypes.c_int
        trimmed = bool(malloc_trim(0))
        rss_after = current_rss_bytes()
        logger.info(
            "allocator trim stage=%s rss_before=%s rss_after=%s released=%s trimmed=%s",
            stage,
            rss_before,
            rss_after if rss_after is not None else -1,
            max(0, rss_before - rss_after) if rss_after is not None else -1,
            int(trimmed),
        )
        return trimmed
    except (AttributeError, OSError):
        return False
    finally:
        with _activity:
            _trimming = False
            _activity.notify_all()
