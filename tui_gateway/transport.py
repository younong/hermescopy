"""Transport abstraction for the tui_gateway JSON-RPC server.

The gateway is driven over the authenticated Owner Worker WebSocket path. This
module decouples the I/O sink from handler logic so concurrent requests and
session events stay bound to the correct peer.

A :class:`Transport` is anything that can accept a JSON-serialisable dict and
forward it to its peer.  The active transport for the current request is
tracked in a :class:`contextvars.ContextVar` so handlers — including those
dispatched onto the worker pool — route their writes to the right peer.

No ambient transport fallback exists: callers must bind the exact admitted
Owner Worker peer before dispatching an RPC.
"""

from __future__ import annotations

import contextvars
import errno
import json
import logging
import os
import threading
from typing import Any, Callable, Optional, Protocol, runtime_checkable

# Errno values that mean "the peer is gone" rather than "the host has a
# real I/O problem".  Anything outside this set re-raises so it surfaces
# in the crash log instead of looking like a clean disconnect.
_PEER_GONE_ERRNOS = frozenset({
    errno.EPIPE,        # write to closed pipe (POSIX)
    errno.ECONNRESET,   # peer reset the connection
    errno.EBADF,        # fd closed under us
    errno.ESHUTDOWN,    # transport endpoint shut down
    getattr(errno, "WSAECONNRESET", -1),  # win32 mapping (no-op on POSIX)
    getattr(errno, "WSAESHUTDOWN", -1),
} - {-1})

logger = logging.getLogger(__name__)

# Optional knob: when true, StdioTransport does not call ``stream.flush``
# after writing.  Use this on environments where a half-closed pipe (TUI
# Node parent quit while the gateway is still emitting events) makes
# flush block long enough to starve the rest of the worker pool.
#
# IMPORTANT: Python text stdout is fully buffered when attached to a
# pipe (the TUI case), so this knob ONLY makes sense when the gateway
# is launched with ``-u`` or ``PYTHONUNBUFFERED=1``.  Without one of
# those, JSON-RPC frames will accumulate in the buffer and the TUI
# will hang waiting for ``gateway.ready``.  Default stays off so the
# existing flush-after-write behaviour is unchanged.
_DISABLE_FLUSH = (os.environ.get("HERMES_TUI_GATEWAY_NO_FLUSH", "") or "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}


@runtime_checkable
class Transport(Protocol):
    """Minimal interface every transport implements."""

    def write(self, obj: dict) -> bool:
        """Emit one JSON frame. Return ``False`` when the peer is gone."""

    def close(self) -> None:
        """Release any resources owned by this transport."""


_current_transport: contextvars.ContextVar[Optional[Transport]] = (
    contextvars.ContextVar(
        "hermes_gateway_transport",
        default=None,
    )
)


def current_transport() -> Optional[Transport]:
    """Return the transport bound for the current request, if any."""
    return _current_transport.get()


def bind_transport(transport: Optional[Transport]):
    """Bind *transport* for the current context. Returns a token for :func:`reset_transport`."""
    return _current_transport.set(transport)


def reset_transport(token) -> None:
    """Restore the transport binding captured by :func:`bind_transport`."""
    _current_transport.reset(token)


class StdioTransport:
    """Writes JSON frames to a stream (usually ``sys.stdout``).

    The stream is resolved via a callable so runtime monkey-patches of the
    underlying stream continue to work — this preserves the behaviour the
    existing test suite relies on (``monkeypatch.setattr(server, "_real_stdout", ...)``).
    """

    __slots__ = ("_stream_getter", "_lock")

    def __init__(self, stream_getter: Callable[[], Any], lock: threading.Lock) -> None:
        self._stream_getter = stream_getter
        self._lock = lock

    def write(self, obj: dict) -> bool:
        """Return ``True`` on success, ``False`` ONLY when the peer is gone.

        Returning ``False`` is the dispatcher's "broken stdout pipe" signal
        — ``entry.py`` calls ``sys.exit(0)`` when ``write_json`` reports
        ``False``.  So programming errors (non-JSON-safe payloads, encoding
        misconfig, unexpected ValueErrors, host I/O bugs like ENOSPC) MUST
        NOT return ``False``, otherwise a real bug looks like a clean
        disconnect and is harder to diagnose.  Those re-raise so the
        existing crash-log infrastructure records the traceback.

        Peer-gone branches:
          * ``BrokenPipeError``
          * ``ValueError("...closed file...")``
          * ``OSError`` whose errno is in :data:`_PEER_GONE_ERRNOS`
            (EPIPE / ECONNRESET / EBADF / ESHUTDOWN; plus WSA mappings
            on Windows).  Other OSError errnos (ENOSPC, EACCES, ...) are
            real host problems and re-raise.
        """
        # Serialization is OUTSIDE the lock so a large payload can't
        # block other threads emitting their own frames.  A non-JSON-safe
        # payload is a programming error: re-raise so the crash log
        # captures it instead of silently exiting via the False path.
        line = json.dumps(obj, ensure_ascii=False) + "\n"

        with self._lock:
            stream = self._stream_getter()
            try:
                stream.write(line)
            except BrokenPipeError:
                return False
            except ValueError as e:
                # ValueError("I/O operation on closed file") is the
                # ONLY ValueError that means "peer gone".  Anything
                # else — including UnicodeEncodeError, which is a
                # ValueError subclass for misconfigured locales —
                # is a real bug; re-raise so it surfaces in the crash log.
                if isinstance(e, UnicodeEncodeError) or "closed file" not in str(e):
                    raise
                return False
            except OSError as e:
                if e.errno not in _PEER_GONE_ERRNOS:
                    raise
                logger.debug("StdioTransport write peer gone: %s", e)
                return False

            # A flush that *raises* with a peer-gone errno means the
            # dispatcher should exit cleanly.  A flush that *hangs* on
            # a half-closed pipe holds the lock until it returns — see
            # ``_DISABLE_FLUSH`` for the "skip flush entirely" escape
            # hatch.
            if not _DISABLE_FLUSH:
                try:
                    stream.flush()
                except BrokenPipeError:
                    return False
                except ValueError as e:
                    if isinstance(e, UnicodeEncodeError) or "closed file" not in str(e):
                        raise
                    return False
                except OSError as e:
                    if e.errno not in _PEER_GONE_ERRNOS:
                        raise
                    logger.debug("StdioTransport flush peer gone: %s", e)
                    return False

        return True

    def close(self) -> None:
        return None


class FanoutTransport:
    """Broadcasts session events to every currently attached peer.

    The hub owns only its subscriber registry, not the underlying transports.
    WebSocket lifecycle remains with ``handle_ws`` so detaching one browser tab
    cannot close the sockets used by other sessions or clients.
    """

    __slots__ = (
        "_closed",
        "_generation",
        "_lock",
        "_on_detached",
        "_subscribers",
    )

    def __init__(self) -> None:
        self._closed = False
        self._generation = 0
        self._lock = threading.Lock()
        self._on_detached: Callable[["FanoutTransport"], None] | None = None
        self._subscribers: dict[Transport, int] = {}

    def set_on_detached(
        self, callback: Callable[["FanoutTransport"], None]
    ) -> None:
        """Register the lifecycle hook run when write failures empty the hub."""
        with self._lock:
            self._on_detached = callback

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    @property
    def is_detached(self) -> bool:
        with self._lock:
            return self._closed or not self._subscribers

    def attach(self, transport: Transport) -> bool:
        """Attach *transport*, returning whether it was newly subscribed."""
        with self._lock:
            if self._closed or transport in self._subscribers:
                return False
            self._generation += 1
            self._subscribers[transport] = self._generation
            return True

    def detach(self, transport: Transport) -> bool:
        """Detach *transport*, returning whether it was subscribed."""
        with self._lock:
            return self._subscribers.pop(transport, None) is not None

    def contains(self, transport: Transport) -> bool:
        with self._lock:
            return transport in self._subscribers

    def write(self, obj: dict) -> bool:
        with self._lock:
            if self._closed:
                return False
            subscribers = tuple(self._subscribers.items())

        delivered = False
        failed: list[tuple[Transport, int]] = []
        for transport, generation in subscribers:
            try:
                if transport.write(obj):
                    delivered = True
                else:
                    failed.append((transport, generation))
            except Exception:
                logger.debug("FanoutTransport subscriber write failed", exc_info=True)
                failed.append((transport, generation))

        became_detached = False
        if failed:
            with self._lock:
                had_subscribers = bool(self._subscribers)
                for transport, generation in failed:
                    if self._subscribers.get(transport) == generation:
                        self._subscribers.pop(transport, None)
                became_detached = had_subscribers and not self._subscribers
        if became_detached:
            with self._lock:
                on_detached = self._on_detached
            if on_detached is not None:
                try:
                    on_detached(self)
                except Exception:
                    logger.debug("FanoutTransport detach callback failed", exc_info=True)
        return delivered

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._subscribers.clear()


class TeeTransport:
    """Mirrors writes to one primary plus N best-effort secondaries.

    The primary's return value (and exceptions) determine the result —
    secondaries swallow failures so a wedged sidecar never stalls the
    main IO path.  Used by the PTY child so every dispatcher emit lands
    on stdio (Ink) AND on a back-WS feeding the dashboard sidebar.
    """

    __slots__ = ("_primary", "_secondaries")

    def __init__(self, primary: "Transport", *secondaries: "Transport") -> None:
        self._primary = primary
        self._secondaries = secondaries

    def write(self, obj: dict) -> bool:
        # Primary first so a slow sidecar (WS publisher) never delays Ink/stdio.
        ok = self._primary.write(obj)
        for sec in self._secondaries:
            try:
                sec.write(obj)
            except Exception:
                pass
        return ok

    def close(self) -> None:
        try:
            self._primary.close()
        finally:
            for sec in self._secondaries:
                try:
                    sec.close()
                except Exception:
                    pass
