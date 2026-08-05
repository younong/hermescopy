"""Minimal read-only owner Session Reader process entrypoint."""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from queue import Empty, Queue
from typing import Any, Iterator
from urllib.parse import parse_qs, unquote, urlsplit

from hermes_cli.session_sources import RETAINED_SESSION_SOURCES, retained_recovery_scope

from .runtime import (
    FORBIDDEN_OWNER_WORKER_ENV_KEYS,
    session_reader_socket_path,
    validate_session_reader_runtime_environment,
)

_MAX_HEADER_BYTES = 32 * 1024
_MAX_REQUEST_LINE_BYTES = 8 * 1024
_MAX_WORKERS = 8
_MAX_IN_FLIGHT = 16
_DB_POOL_SIZE = 4
_SOCKET_READ_TIMEOUT = 5.0
_LITERAL_SESSION_PATHS = frozenset({
    "/api/sessions",
    "/api/sessions/search",
    "/api/sessions/composition",
    "/api/sessions/empty/count",
    "/api/sessions/stats",
})
_SESSION_ITEM_SUFFIXES = frozenset({
    "latest-descendant",
    "messages",
    "export",
})
_STRICT_PERCENT_ESCAPE_RE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_STATUS_TEXT = {
    200: "OK",
    400: "Bad Request",
    401: "Unauthorized",
    404: "Not Found",
    405: "Method Not Allowed",
    422: "Unprocessable Entity",
    500: "Internal Server Error",
    503: "Service Unavailable",
}
_log = logging.getLogger(__name__)


class _ReaderLease:
    __slots__ = (
        "owner_key",
        "reader_generation",
        "reader_id",
        "state",
        "lease_version",
        "recovery_generation",
    )

    def __init__(
        self,
        *,
        owner_key: str,
        reader_generation: int,
        reader_id: str,
        state: str,
        lease_version: int,
        recovery_generation: int,
    ) -> None:
        self.owner_key = owner_key
        self.reader_generation = reader_generation
        self.reader_id = reader_id
        self.state = state
        self.lease_version = lease_version
        self.recovery_generation = recovery_generation


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a Hermes owner Session Reader")
    parser.add_argument("--owner-key", required=True)
    parser.add_argument("--owner-home", required=True)
    parser.add_argument("--socket", required=True)
    parser.add_argument("--control-home", required=True)
    parser.add_argument("--reader-generation", required=True, type=int)
    parser.add_argument("--reader-id", required=True)
    return parser.parse_args()


def _lease_from_env(owner_key: str) -> _ReaderLease:
    try:
        return _ReaderLease(
            owner_key=owner_key,
            reader_generation=int(os.environ["HERMES_READER_GENERATION"]),
            reader_id=str(os.environ["HERMES_READER_ID"]),
            state="starting",
            lease_version=int(os.environ["HERMES_READER_LEASE_VERSION"]),
            recovery_generation=int(os.environ["HERMES_READER_RECOVERY_GENERATION"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("session reader lease environment is incomplete") from exc


def _prepare_env(args: argparse.Namespace) -> tuple[str, Path, Path]:
    owner_key = str(args.owner_key).strip()
    owner_home = Path(args.owner_home).expanduser().resolve()
    socket_path = Path(args.socket).expanduser().resolve()
    generation = int(args.reader_generation)
    reader_id = str(args.reader_id).strip()
    if not owner_key or generation < 1 or not reader_id:
        raise SystemExit("owner_key, reader_generation, and reader_id are required")
    if socket_path != session_reader_socket_path(owner_home, generation):
        raise SystemExit("reader socket does not match owner generation")
    validate_session_reader_runtime_environment(
        owner_home=owner_home,
        owner_key=owner_key,
        reader_generation=generation,
        reader_id=reader_id,
        socket_path=socket_path,
    )
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    return owner_key, owner_home, socket_path


def _single_query_value(query: dict[str, list[str]], name: str, default: Any) -> Any:
    values = query.get(name)
    if not values:
        return default
    if len(values) != 1:
        raise ValueError(f"{name} must be specified once")
    return values[0]


def _int_query(query: dict[str, list[str]], name: str, default: int) -> int:
    try:
        return int(_single_query_value(query, name, default))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _float_query(
    query: dict[str, list[str]], name: str, default: float | None = None
) -> float | None:
    value = _single_query_value(query, name, default)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number") from exc


def _bool_query(query: dict[str, list[str]], name: str, default: bool) -> bool:
    value = str(_single_query_value(query, name, str(default).lower())).lower()
    if value in {"true", "1"}:
        return True
    if value in {"false", "0"}:
        return False
    raise ValueError(f"{name} must be a boolean")


def _session_route(path: str) -> tuple[str, str | None] | None:
    if path in _LITERAL_SESSION_PATHS:
        return path, None
    parts = path.split("/")
    if len(parts) not in {4, 5} or parts[:3] != ["", "api", "sessions"]:
        return None
    encoded_id = parts[3]
    if (
        not encoded_id
        or _STRICT_PERCENT_ESCAPE_RE.search(encoded_id)
        or any(ord(char) < 32 or ord(char) == 127 for char in encoded_id)
    ):
        return None
    try:
        session_id = unquote(encoded_id, errors="strict")
    except (UnicodeDecodeError, ValueError):
        return None
    if (
        session_id in {".", ".."}
        or "/" in session_id
        or "\\" in session_id
        or any(ord(char) < 32 or ord(char) == 127 for char in session_id)
    ):
        return None
    if len(parts) == 5 and parts[4] not in _SESSION_ITEM_SUFFIXES:
        return None
    return path, session_id


def _response(status: int, payload: dict[str, Any]) -> bytes:
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    reason = _STATUS_TEXT.get(status, "Error")
    return (
        f"HTTP/1.1 {status} {reason}\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n\r\n"
    ).encode("ascii") + body


class SessionReaderQueryRuntime:
    """Reuse a bounded set of owner-local read-only connections."""

    def __init__(self, state_db: Path, *, pool_size: int = _DB_POOL_SIZE) -> None:
        from .db import ReadOnlySessionDB

        self.state_db = Path(state_db)
        self.pool_size = max(1, int(pool_size))
        self._available: Queue[Any] = Queue(maxsize=self.pool_size)
        self._opened = 0
        if self.state_db.exists():
            self._available.put(ReadOnlySessionDB(self.state_db))
            self._opened = 1
        self._lock = threading.Lock()
        self._closed = False

    @contextmanager
    def borrow(self) -> Iterator[Any]:
        from .db import ReadOnlySessionDB

        if self._closed:
            raise RuntimeError("session reader query runtime is closed")
        try:
            db = self._available.get_nowait()
        except Empty:
            with self._lock:
                if self._opened < self.pool_size:
                    db = ReadOnlySessionDB(self.state_db)
                    self._opened += 1
                else:
                    db = None
            if db is None:
                db = self._available.get()
        try:
            yield db
        finally:
            if self._closed:
                db.close()
            else:
                self._available.put(db)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        while True:
            try:
                self._available.get_nowait().close()
            except Empty:
                return


def _create_handler(
    owner_key: str,
    owner_home: Path,
    *,
    reader_generation: int,
    reader_id: str,
    socket_path: Path,
):
    from hermes_cli import session_api
    from .tokens import (
        SessionReaderCapabilityInvalid,
        prepare_session_reader_capability_verifier,
        verify_session_reader_capability,
    )

    paths = validate_session_reader_runtime_environment(
        owner_home=owner_home,
        owner_key=owner_key,
        reader_generation=reader_generation,
        reader_id=reader_id,
        socket_path=socket_path,
    )
    lease = _lease_from_env(owner_key)
    Path(os.environ["HERMES_CONTROL_HOME"]).resolve()
    verifier = prepare_session_reader_capability_verifier(
        public_key=os.environ["HERMES_SESSION_READER_CAPABILITY_PUBLIC_KEY"],
        issuer_key_version=os.environ["HERMES_SESSION_READER_CAPABILITY_ISSUER"],
        retained_public_keys=os.environ[
            "HERMES_SESSION_READER_CAPABILITY_RETAINED_PUBLIC_KEYS"
        ],
    )
    queries = SessionReaderQueryRuntime(paths.state_db)

    def handle(method: str, target: str, headers: dict[str, str]) -> tuple[int, dict[str, Any]]:
        parsed = urlsplit(target)
        path = parsed.path or "/"
        if method != "GET":
            return 405, {"detail": "Method not allowed"}
        route = (path, None) if path == "/internal/health" else _session_route(path)
        if route is None:
            return 404, {"detail": "Not found"}
        authorization = headers.get("authorization", "")
        token = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
        try:
            verify_session_reader_capability(
                token,
                expected_lease=lease,
                path=path,
                # The exact lease is inherited from the fenced Reader launch. The
                # Control Plane reasserts durable authority before every request;
                # keeping the subprocess off authority.db avoids a second shared
                # database trust boundary on the latency-critical read path.
                authority_store=None,
                verifier=verifier,
            )
        except (SessionReaderCapabilityInvalid, RuntimeError):
            return 401, {"detail": "invalid session reader capability"}
        if path == "/internal/health":
            return 200, {
                "ready": True,
                "owner_key": owner_key,
                "owner_home": str(paths.owner_home),
                "reader_generation": reader_generation,
                "reader_id": reader_id,
                "lease_version": lease.lease_version,
                "recovery_generation": lease.recovery_generation,
                "pid": os.getpid(),
                "hermes_home": str(Path(os.environ["HERMES_HOME"]).resolve()),
                "forbidden_env_present": [
                    key for key in FORBIDDEN_OWNER_WORKER_ENV_KEYS if os.environ.get(key, "").strip()
                ],
            }
        query = parse_qs(parsed.query, keep_blank_values=True)
        if any(any(str(value).strip() for value in query.get(key, [])) for key in ("owner", "owner_home", "owner_key")):
            return 400, {"detail": "owner selection is not available in authenticated mode"}
        route_path, session_id = route
        try:
            profile = str(_single_query_value(query, "profile", "")).strip().lower()
            if profile and profile not in {"current", "default"}:
                return 400, {"detail": "profile selection is not available in authenticated mode"}
            limit = _int_query(query, "limit", 20)
            offset = _int_query(query, "offset", 0)
            min_messages = _int_query(query, "min_messages", 0)
            archived = str(_single_query_value(query, "archived", "exclude"))
            order = str(_single_query_value(query, "order", "created"))
            source = str(_single_query_value(query, "source", "")) or None
            exclude_sources = str(_single_query_value(query, "exclude_sources", "")) or None
            cwd_prefix = str(_single_query_value(query, "cwd_prefix", "")) or None
            compact = _bool_query(query, "compact", False)
            active_from = _float_query(query, "active_from")
            active_before = _float_query(query, "active_before")
            composition_ids = query.get("ids", [])
            search_query = str(_single_query_value(query, "q", ""))
            before = str(_single_query_value(query, "before", "")) or None
        except ValueError as exc:
            return 422, {"detail": str(exc)}
        if not paths.state_db.exists():
            if route_path == "/api/sessions":
                return 200, {"sessions": [], "total": 0, "limit": limit, "offset": offset}
            if route_path == "/api/sessions/search":
                return 200, {"results": []}
            if route_path == "/api/sessions/composition":
                return 404, {"detail": "Session not found"}
            if route_path == "/api/sessions/empty/count":
                return 200, {"count": 0}
            if route_path == "/api/sessions/stats":
                return 200, {
                    "total": 0,
                    "active_store": 0,
                    "archived": 0,
                    "messages": 0,
                    "by_source": {},
                }
            return 404, {"detail": "Session not found"}
        recovery_scope = retained_recovery_scope({
            "owner_key": owner_key,
            "workspace_root": str((paths.owner_home / "workspaces").resolve()),
            "historical_resume": True,
        })
        try:
            with queries.borrow() as db:
                if route_path == "/api/sessions":
                    payload = session_api.list_sessions_payload(
                        db,
                        limit=limit,
                        offset=offset,
                        min_messages=min_messages,
                        archived=archived,
                        order=order,
                        source=source,
                        exclude_sources=exclude_sources,
                        cwd_prefix=cwd_prefix,
                        active_from=active_from,
                        active_before=active_before,
                        recovery_scope=recovery_scope,
                        compact=compact,
                        allowed_sources=sorted(RETAINED_SESSION_SOURCES),
                        latency_trace_id=headers.get("x-request-id", ""),
                    )
                elif route_path == "/api/sessions/composition":
                    payload = session_api.session_composition_payload(
                        db,
                        ids=composition_ids,
                        recovery_scope=recovery_scope,
                    )
                elif route_path == "/api/sessions/search":
                    payload = session_api.search_sessions_payload(
                        db,
                        q=search_query,
                        limit=limit,
                        recovery_scope=recovery_scope,
                        allowed_sources=sorted(RETAINED_SESSION_SOURCES),
                    )
                elif route_path == "/api/sessions/empty/count":
                    payload = session_api.empty_count_payload(
                        db, recovery_scope=recovery_scope
                    )
                elif route_path == "/api/sessions/stats":
                    payload = session_api.stats_payload(
                        db, recovery_scope=recovery_scope
                    )
                elif route_path.endswith("/latest-descendant"):
                    payload = session_api.latest_descendant_payload(
                        db,
                        str(session_id),
                        recovery_scope=recovery_scope,
                    )
                elif route_path.endswith("/messages"):
                    payload = session_api.session_messages_payload(
                        db,
                        str(session_id),
                        limit=limit if "limit" in query else None,
                        before=before,
                        recovery_scope=recovery_scope,
                    )
                elif route_path.endswith("/export"):
                    payload = session_api.export_session_payload(
                        db,
                        str(session_id),
                        recovery_scope=recovery_scope,
                    )
                else:
                    payload = session_api.session_detail_payload(
                        db,
                        str(session_id),
                        recovery_scope=recovery_scope,
                    )
            return 200, payload
        except Exception as exc:
            status = int(getattr(exc, "status_code", 500))
            detail = getattr(exc, "detail", None) or "Internal server error"
            if status >= 500:
                _log.exception("session reader request failed")
            return status, {"detail": detail}

    handle.close = queries.close
    return handle


def _handle_connection(
    connection: socket.socket,
    handler: Any,
    admission: threading.BoundedSemaphore | None = None,
) -> None:
    with connection:
        connection.settimeout(_SOCKET_READ_TIMEOUT)
        status, payload = 400, {"detail": "Bad request"}
        try:
            chunks = bytearray()
            while b"\r\n\r\n" not in chunks:
                chunk = connection.recv(4096)
                if not chunk:
                    break
                chunks.extend(chunk)
                if len(chunks) > _MAX_HEADER_BYTES:
                    raise ValueError("headers too large")
            lines = bytes(chunks).decode("latin-1").split("\r\n")
            if not lines or len(lines[0]) > _MAX_REQUEST_LINE_BYTES:
                raise ValueError("request line too large")
            method, target, _version = lines[0].split(" ", 2)
            headers: dict[str, str] = {}
            for line in lines[1:]:
                if not line:
                    continue
                name, value = line.split(":", 1)
                headers[name.strip().lower()] = value.strip()
            status, payload = handler(method.upper(), target, headers)
        except (UnicodeError, ValueError):
            pass
        except Exception:
            _log.exception("session reader connection failed")
            status, payload = 500, {"detail": "Internal server error"}
        try:
            connection.sendall(_response(status, payload))
        finally:
            if admission is not None:
                admission.release()


def _serve(
    socket_path: Path,
    handler: Any,
    *,
    ready_path: Path,
    ready_payload: dict[str, Any],
) -> None:
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(socket_path))
    server.listen(64)
    temporary_ready = ready_path.with_suffix(".tmp")
    temporary_ready.write_text(
        json.dumps(ready_payload, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    os.replace(temporary_ready, ready_path)
    admission = threading.BoundedSemaphore(_MAX_IN_FLIGHT)
    executor = ThreadPoolExecutor(
        max_workers=_MAX_WORKERS,
        thread_name_prefix="session-reader",
    )
    try:
        while True:
            connection, _address = server.accept()
            if not admission.acquire(blocking=False):
                with connection:
                    connection.sendall(
                        _response(503, {"detail": "Session reader is busy"})
                    )
                continue
            try:
                executor.submit(_handle_connection, connection, handler, admission)
            except Exception:
                admission.release()
                connection.close()
                raise
    finally:
        server.close()
        executor.shutdown(wait=True, cancel_futures=True)
        close_handler = getattr(handler, "close", None)
        if close_handler is not None:
            close_handler()


def main() -> None:
    args = _parse_args()
    owner_key, owner_home, socket_path = _prepare_env(args)
    try:
        socket_path.unlink()
    except FileNotFoundError:
        pass
    handler = _create_handler(
        owner_key,
        owner_home,
        reader_generation=int(args.reader_generation),
        reader_id=str(args.reader_id),
        socket_path=socket_path,
    )
    ready_path = socket_path.with_name("reader.ready.json")
    ready_payload = {
        "ready": True,
        "owner_key": owner_key,
        "owner_home": str(owner_home),
        "reader_generation": int(args.reader_generation),
        "reader_id": str(args.reader_id),
        "lease_version": int(os.environ["HERMES_READER_LEASE_VERSION"]),
        "recovery_generation": int(os.environ["HERMES_READER_RECOVERY_GENERATION"]),
        "pid": os.getpid(),
        "hermes_home": str(Path(os.environ["HERMES_HOME"]).resolve()),
        "forbidden_env_present": [
            key for key in FORBIDDEN_OWNER_WORKER_ENV_KEYS if os.environ.get(key, "").strip()
        ],
    }
    try:
        _serve(
            socket_path,
            handler,
            ready_path=ready_path,
            ready_payload=ready_payload,
        )
    finally:
        for path in (socket_path, ready_path):
            try:
                path.unlink()
            except FileNotFoundError:
                pass


if __name__ == "__main__":
    main()
