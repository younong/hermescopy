"""Minimal read-only owner Session Reader process entrypoint."""
from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from hermes_cli.owner_runtime import (
    FORBIDDEN_OWNER_WORKER_ENV_KEYS,
    session_reader_socket_path,
    validate_session_reader_runtime_environment,
)

_MAX_HEADER_BYTES = 32 * 1024
_MAX_REQUEST_LINE_BYTES = 8 * 1024
_STATUS_TEXT = {
    200: "OK",
    400: "Bad Request",
    401: "Unauthorized",
    404: "Not Found",
    405: "Method Not Allowed",
    422: "Unprocessable Entity",
    500: "Internal Server Error",
}
_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class _ReaderLease:
    owner_key: str
    reader_generation: int
    reader_id: str
    state: str
    lease_version: int
    recovery_generation: int


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


def _bool_query(query: dict[str, list[str]], name: str, default: bool) -> bool:
    value = str(_single_query_value(query, name, str(default).lower())).lower()
    if value in {"true", "1"}:
        return True
    if value in {"false", "0"}:
        return False
    raise ValueError(f"{name} must be a boolean")


def _response(status: int, payload: dict[str, Any]) -> bytes:
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    reason = _STATUS_TEXT.get(status, "Error")
    return (
        f"HTTP/1.1 {status} {reason}\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n\r\n"
    ).encode("ascii") + body


def _create_handler(
    owner_key: str,
    owner_home: Path,
    *,
    reader_generation: int,
    reader_id: str,
    socket_path: Path,
):
    from hermes_cli import session_api
    from hermes_state import SessionDB
    from .tokens import SessionReaderCapabilityInvalid, verify_session_reader_capability

    paths = validate_session_reader_runtime_environment(
        owner_home=owner_home,
        owner_key=owner_key,
        reader_generation=reader_generation,
        reader_id=reader_id,
        socket_path=socket_path,
    )
    lease = _lease_from_env(owner_key)
    Path(os.environ["HERMES_CONTROL_HOME"]).resolve()
    verifier = {
        "public_key": os.environ["HERMES_SESSION_READER_CAPABILITY_PUBLIC_KEY"],
        "issuer_key_version": os.environ["HERMES_SESSION_READER_CAPABILITY_ISSUER"],
        "retained_public_keys": os.environ["HERMES_SESSION_READER_CAPABILITY_RETAINED_PUBLIC_KEYS"],
    }

    def handle(method: str, target: str, headers: dict[str, str]) -> tuple[int, dict[str, Any]]:
        parsed = urlsplit(target)
        path = parsed.path or "/"
        if method != "GET":
            return 405, {"detail": "Method not allowed"}
        if path not in {"/internal/health", "/api/sessions"}:
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
                **verifier,
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
        try:
            profile = str(_single_query_value(query, "profile", "")).strip().lower()
            if profile and profile != "default":
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
        except ValueError as exc:
            return 422, {"detail": str(exc)}
        if not paths.state_db.exists():
            return 200, {"sessions": [], "total": 0, "limit": limit, "offset": offset}
        db = SessionDB(db_path=paths.state_db, read_only=True)
        try:
            return 200, session_api.list_sessions_payload(
                db,
                limit=limit,
                offset=offset,
                min_messages=min_messages,
                archived=archived,
                order=order,
                source=source,
                exclude_sources=exclude_sources,
                cwd_prefix=cwd_prefix,
                recovery_scope={
                    "owner_key": owner_key,
                    "workspace_root": str((paths.owner_home / "workspaces").resolve()),
                    "worker_generation": 1,
                    "historical_resume": True,
                },
                compact=compact,
                latency_trace_id=headers.get("x-request-id", ""),
            )
        except Exception as exc:
            status = int(getattr(exc, "status_code", 500))
            detail = getattr(exc, "detail", None) or "Internal server error"
            if status >= 500:
                _log.exception("session reader request failed")
            return status, {"detail": detail}
        finally:
            db.close()

    return handle


def create_app(
    owner_key: str,
    owner_home: Path,
    *,
    reader_generation: int,
    reader_id: str,
    socket_path: Path,
):
    """Compatibility ASGI app for focused in-process tests; subprocesses use the lean server."""
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse

    handler = _create_handler(
        owner_key,
        owner_home,
        reader_generation=reader_generation,
        reader_id=reader_id,
        socket_path=socket_path,
    )
    app = FastAPI(title="Hermes Session Reader")

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def dispatch(request: Request, path: str):
        status, payload = handler(request.method, request.url.path + (f"?{request.url.query}" if request.url.query else ""), dict(request.headers))
        return JSONResponse(status_code=status, content=payload)

    return app


def _handle_connection(connection: socket.socket, handler: Any) -> None:
    with connection:
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
        connection.sendall(_response(status, payload))


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
    try:
        while True:
            connection, _address = server.accept()
            threading.Thread(
                target=_handle_connection,
                args=(connection, handler),
                daemon=True,
            ).start()
    finally:
        server.close()


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
