"""Lease-bound deployment inference relay.

The Dashboard process owns the upstream policy and credential resolver.  Each
owner worker receives one end of a socketpair; it hosts a loopback HTTP adapter
for its own SDK and forwards requests over that inherited descriptor.  The
Control Plane validates the exact durable lease before every upstream call and
adds credentials only after rejecting caller supplied authorization headers.
"""
from __future__ import annotations

import base64
import json
import os
import socket
import struct
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from hermes_cli.dashboard_auth.authority import AuthorityStore, AuthorizationRejected, OwnerWorkerAuthorityLease, WorkerLeaseState
from hermes_cli.deployment_inference import DeploymentInferencePolicy

_MAX_FRAME_BYTES = 64 * 1024 * 1024
_ALLOWED_PATHS = frozenset({"/v1/chat/completions", "/v1/messages"})
_HOP_BY_HOP_HEADERS = frozenset({
    "authorization",
    "connection",
    "host",
    "content-length",
    "proxy-authorization",
    "transfer-encoding",
})


class DeploymentInferenceRelayError(RuntimeError):
    """The worker-to-control-plane inference relay rejected a request."""


def _send_frame(conn: socket.socket, value: dict[str, Any]) -> None:
    encoded = json.dumps(value, separators=(",", ":")).encode("utf-8")
    if len(encoded) > _MAX_FRAME_BYTES:
        raise DeploymentInferenceRelayError("relay frame is too large")
    conn.sendall(struct.pack("!I", len(encoded)) + encoded)


def _recv_exact(conn: socket.socket, count: int) -> bytes:
    chunks: list[bytes] = []
    remaining = count
    while remaining:
        chunk = conn.recv(remaining)
        if not chunk:
            raise DeploymentInferenceRelayError("relay peer closed")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _recv_frame(conn: socket.socket) -> dict[str, Any]:
    size = struct.unpack("!I", _recv_exact(conn, 4))[0]
    if not size or size > _MAX_FRAME_BYTES:
        raise DeploymentInferenceRelayError("relay frame is invalid")
    try:
        value = json.loads(_recv_exact(conn, size))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DeploymentInferenceRelayError("relay frame is malformed") from exc
    if not isinstance(value, dict):
        raise DeploymentInferenceRelayError("relay frame is malformed")
    return value


@dataclass
class _RelayPeer:
    lease: OwnerWorkerAuthorityLease
    connection: socket.socket
    lock: threading.Lock
    thread: threading.Thread


class DeploymentInferenceBroker:
    """Control-plane-only broker for one active worker generation at a time."""

    def __init__(
        self,
        *,
        policy: DeploymentInferencePolicy,
        authority_store: AuthorityStore,
    ) -> None:
        self._policy = policy
        self._authority_store = authority_store
        self._peers: dict[tuple[str, int, str, int, int], _RelayPeer] = {}
        self._lock = threading.RLock()
        self._closed = False

    @staticmethod
    def _key(lease: OwnerWorkerAuthorityLease) -> tuple[str, int, str, int, int]:
        return (
            lease.owner_key,
            lease.worker_generation,
            lease.worker_id,
            lease.lease_version,
            lease.recovery_generation,
        )

    def register(self, lease: OwnerWorkerAuthorityLease) -> int:
        """Create and register a private worker endpoint for this exact lease."""
        self._authority_store.assert_worker_lease(lease, states=frozenset({WorkerLeaseState.STARTING}))
        parent, child = socket.socketpair()
        child_fd = child.detach()
        parent.settimeout(None)
        key = self._key(lease)
        thread = threading.Thread(target=self._serve_peer, args=(key,), daemon=True, name=f"inference-relay-{lease.worker_generation}")
        peer = _RelayPeer(lease=lease, connection=parent, lock=threading.Lock(), thread=thread)
        with self._lock:
            if self._closed or key in self._peers:
                parent.close()
                os.close(child_fd)
                raise DeploymentInferenceRelayError("relay registration is unavailable")
            self._peers[key] = peer
        thread.start()
        return child_fd

    def activate(self, lease: OwnerWorkerAuthorityLease) -> None:
        """Update the exact relay fence after worker promotion.

        A durable transition increments the lease version, so the broker must
        replace its pre-activation STARTING lease with the returned ACTIVE
        lease before processing requests. A child may still close its endpoint
        during startup; that remains an inference-only failure, not a worker
        health failure.
        """
        with self._lock:
            current_key = next(
                (
                    key
                    for key, peer in self._peers.items()
                    if (
                        peer.lease.owner_key,
                        peer.lease.worker_generation,
                        peer.lease.worker_id,
                        peer.lease.recovery_generation,
                    ) == (
                        lease.owner_key,
                        lease.worker_generation,
                        lease.worker_id,
                        lease.recovery_generation,
                    )
                ),
                None,
            )
            if current_key is not None:
                peer = self._peers.pop(current_key)
                peer.lease = lease
                self._peers[self._key(lease)] = peer

    def revoke(self, lease: OwnerWorkerAuthorityLease) -> None:
        """Immediately close a relay endpoint once its generation drains."""
        with self._lock:
            peer = self._peers.pop(self._key(lease), None)
        if peer is not None:
            try:
                peer.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            peer.connection.close()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            peers = tuple(self._peers.values())
            self._peers.clear()
        for peer in peers:
            try:
                peer.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            peer.connection.close()

    def _serve_peer(self, key: tuple[str, int, str, int, int]) -> None:
        with self._lock:
            peer = self._peers.get(key)
        if peer is None:
            return
        try:
            while True:
                request = _recv_frame(peer.connection)
                with peer.lock:
                    try:
                        self._stream_request(
                            peer.lease,
                            request,
                            lambda frame: _send_frame(peer.connection, frame),
                        )
                    except DeploymentInferenceRelayError as exc:
                        _send_frame(peer.connection, {
                            "type": "error",
                            "message": str(exc),
                        })
        except (DeploymentInferenceRelayError, OSError):
            pass
        finally:
            with self._lock:
                for candidate_key, candidate in tuple(self._peers.items()):
                    if candidate is peer:
                        self._peers.pop(candidate_key, None)
                        break
            try:
                peer.connection.close()
            except OSError:
                pass

    def _request_parts(
        self,
        lease: OwnerWorkerAuthorityLease,
        request: dict[str, Any],
    ) -> tuple[str, bytes, dict[str, str]]:
        try:
            self._authority_store.assert_worker_lease(lease, states=frozenset({WorkerLeaseState.ACTIVE}))
        except AuthorizationRejected as exc:
            raise DeploymentInferenceRelayError("relay worker lease is not active") from exc
        method = str(request.get("method") or "").upper()
        path = str(request.get("path") or "")
        if method != "POST" or path not in _ALLOWED_PATHS:
            raise DeploymentInferenceRelayError("relay request is not allowed")
        expected_path = "/v1/messages" if self._policy.api_mode == "anthropic_messages" else "/v1/chat/completions"
        if path != expected_path:
            raise DeploymentInferenceRelayError("relay request API mode does not match policy")
        try:
            body = base64.b64decode(str(request.get("body") or ""), validate=True)
            payload = json.loads(body)
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise DeploymentInferenceRelayError("relay request body is invalid") from exc
        if not isinstance(payload, dict) or not self._policy.descriptor().allows_model(str(payload.get("model") or "")):
            raise DeploymentInferenceRelayError("relay request model is not allowed")
        incoming_headers = request.get("headers")
        if not isinstance(incoming_headers, dict):
            raise DeploymentInferenceRelayError("relay request headers are invalid")
        headers = {
            str(name): str(value)
            for name, value in incoming_headers.items()
            if str(name).lower() not in _HOP_BY_HOP_HEADERS
        }
        runtime = self._policy.resolve_runtime()
        if self._policy.api_mode == "anthropic_messages":
            headers["x-api-key"] = str(runtime["api_key"])
        else:
            headers["Authorization"] = f"Bearer {runtime['api_key']}"
        headers.setdefault("Content-Type", "application/json")
        upstream_base_url = str(runtime["base_url"]).rstrip("/")
        if upstream_base_url.endswith("/v1"):
            upstream_base_url = upstream_base_url[:-3]
        return upstream_base_url + path, body, headers

    def _handle_request(
        self,
        lease: OwnerWorkerAuthorityLease,
        request: dict[str, Any],
    ) -> dict[str, Any]:
        """Return a buffered response for focused callers outside the socket loop.

        The live worker path uses :meth:`_stream_request`; this narrow adapter
        keeps the request validation behavior directly testable without making
        the streaming protocol itself part of the policy API.
        """
        frames: list[dict[str, Any]] = []
        self._stream_request(lease, request, frames.append)
        if not frames or frames[0].get("type") != "response_start":
            raise DeploymentInferenceRelayError("relay response is invalid")
        body = b"".join(
            base64.b64decode(str(frame.get("body") or ""), validate=True)
            for frame in frames[1:]
            if frame.get("type") == "response_chunk"
        )
        return {
            "status": frames[0]["status"],
            "headers": frames[0]["headers"],
            "body": base64.b64encode(body).decode("ascii"),
        }

    def _stream_request(
        self,
        lease: OwnerWorkerAuthorityLease,
        request: dict[str, Any],
        emit: Any,
    ) -> None:
        upstream, body, headers = self._request_parts(lease, request)
        try:
            import httpx

            with httpx.Client(timeout=900.0) as client:
                with client.stream("POST", upstream, content=body, headers=headers) as response:
                    safe_headers = {
                        name: value
                        for name, value in response.headers.items()
                        if name.lower() not in _HOP_BY_HOP_HEADERS
                    }
                    emit({
                        "type": "response_start",
                        "status": response.status_code,
                        "headers": safe_headers,
                    })
                    # Preserve provider chunks (including SSE event framing) rather
                    # than collecting the response before the worker can consume it.
                    for chunk in response.iter_raw(chunk_size=48 * 1024):
                        if chunk:
                            emit({
                                "type": "response_chunk",
                                "body": base64.b64encode(chunk).decode("ascii"),
                            })
                    emit({"type": "response_end"})
        except DeploymentInferenceRelayError:
            raise
        except Exception as exc:
            raise DeploymentInferenceRelayError("deployment inference upstream is unavailable") from exc


class OwnerInferenceRelay:
    """Worker-local loopback HTTP server backed by its inherited descriptor."""

    def __init__(self, inherited_fd: int) -> None:
        if inherited_fd < 0:
            raise DeploymentInferenceRelayError("relay descriptor is invalid")
        self._connection = socket.socket(fileno=inherited_fd)
        # The descriptor is consumed by this relay only.  Future PTY/tool
        # subprocesses must not inherit it even if a caller forgets an env scrub.
        self._connection.set_inheritable(False)
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        if self._server is None:
            raise DeploymentInferenceRelayError("relay has not started")
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/v1"

    def start(self) -> None:
        if self._server is not None:
            return
        relay = self

        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                relay._handle_http(self)

            def log_message(self, _format: str, *_args: Any) -> None:
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True, name="owner-inference-relay")
        self._thread.start()

    def close(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        try:
            self._connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._connection.close()

    def _handle_http(self, handler: BaseHTTPRequestHandler) -> None:
        try:
            path = urlparse(handler.path).path
            length = int(handler.headers.get("Content-Length", "0"))
            if length < 0 or length > _MAX_FRAME_BYTES:
                raise DeploymentInferenceRelayError("relay request is too large")
            request = {
                "method": handler.command,
                "path": path,
                "headers": dict(handler.headers.items()),
                "body": base64.b64encode(handler.rfile.read(length)).decode("ascii"),
            }
            with self._lock:
                _send_frame(self._connection, request)
                response = _recv_frame(self._connection)
                if response.get("type") == "error":
                    raise DeploymentInferenceRelayError("relay request was rejected")
                if response.get("type") != "response_start":
                    raise DeploymentInferenceRelayError("relay response is invalid")
                status = int(response["status"])
                headers = response.get("headers")
                if not isinstance(headers, dict):
                    raise DeploymentInferenceRelayError("relay response is invalid")
                handler.send_response(status)
                for name, value in headers.items():
                    if str(name).lower() not in _HOP_BY_HOP_HEADERS:
                        handler.send_header(str(name), str(value))
                # Framing is intentionally delegated to the HTTP server.  Do not
                # add Content-Length: provider SSE responses are streamed as they
                # arrive over the private broker connection.
                handler.end_headers()
                while True:
                    response = _recv_frame(self._connection)
                    response_type = response.get("type")
                    if response_type == "response_end":
                        break
                    if response_type == "error":
                        raise DeploymentInferenceRelayError("relay response failed")
                    if response_type != "response_chunk":
                        raise DeploymentInferenceRelayError("relay response is invalid")
                    body = base64.b64decode(str(response.get("body") or ""), validate=True)
                    if body:
                        handler.wfile.write(body)
                        handler.wfile.flush()
        except Exception:
            handler.send_error(502, "deployment inference relay unavailable")
