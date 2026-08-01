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
import logging
import os
import queue
import socket
import struct
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from agent.redact import redact_sensitive_text
from agent.stream_diag import flatten_exception_chain, stream_diag_init
from hermes_cli.dashboard_auth.authority import AuthorityStore, AuthorizationRejected, OwnerWorkerAuthorityLease, WorkerLeaseState
from hermes_cli.deployment_inference import (
    DeploymentInferencePolicy,
    deployment_routes_path,
    request_path_for_api_mode,
)

_PROTOCOL_VERSION = 2
_MAX_FRAME_BYTES = 64 * 1024 * 1024
_MAX_AGGREGATE_REQUEST_BYTES = 64 * 1024 * 1024
_RELAY_CONNECT_TIMEOUT_SECONDS = 30.0
_RELAY_READ_TIMEOUT_SECONDS = 360.0
_RELAY_MAX_DEADLINE_SECONDS = 420.0
_RELAY_DEADLINE_HEADER = "x-hermes-relay-deadline-seconds"
_MAX_CONCURRENT_BROKER_REQUESTS = 8
_MAX_CONCURRENT_WORKER_REQUESTS = 8
_MAX_IN_FLIGHT_RESPONSE_FRAMES = 16
_ALLOWED_PATHS = frozenset({"/v1/chat/completions", "/v1/messages"})
_PROVIDER_HEADER = "x-hermes-deployment-provider"
_HOP_BY_HOP_HEADERS = frozenset({
    "authorization",
    "connection",
    "host",
    "content-length",
    "proxy-authorization",
    "transfer-encoding",
    "x-api-key",
    _PROVIDER_HEADER,
    _RELAY_DEADLINE_HEADER,
})
_SAFE_RESPONSE_HEADERS = frozenset({
    "cache-control",
    "content-encoding",
    "content-type",
    "retry-after",
    "x-request-id",
})
_RELAY_DIAG_HEADERS = (
    "cf-ray",
    "x-request-id",
    "x-openrouter-id",
    "x-vercel-id",
)

logger = logging.getLogger(__name__)


def _capture_relay_response_diag(diag: dict[str, Any], response: Any) -> None:
    """Capture bounded, allowlisted provider metadata without response content."""
    diag["http_status"] = getattr(response, "status_code", None)
    response_headers = getattr(response, "headers", None) or {}
    diag["headers"] = {
        name: str(value)[:120]
        for name in _RELAY_DIAG_HEADERS
        if (value := response_headers.get(name))
    }


def _log_relay_outcome(
    outcome: str,
    diag: dict[str, Any],
    *,
    error: BaseException | None = None,
) -> None:
    """Emit one content-free diagnostic record for an upstream relay attempt."""
    now = time.time()
    started = float(diag.get("started_at") or now)
    first_chunk_at = diag.get("first_chunk_at")
    ttfb = (
        max(0.0, float(first_chunk_at) - started)
        if first_chunk_at is not None
        else None
    )
    headers = diag.get("headers") or {}
    request_ids = " ".join(f"{name}={value}" for name, value in headers.items()) or "-"
    chain = "-"
    if error is not None:
        chain = redact_sensitive_text(
            flatten_exception_chain(error), force=True, file_read=True
        )[:600]
    log = logger.info if outcome == "complete" else logger.warning
    log(
        "deployment inference relay outcome=%s http_status=%s elapsed=%.2fs "
        "ttfb=%s bytes=%d chunks=%d request_ids=[%s] error_chain=%s",
        outcome,
        diag.get("http_status") if diag.get("http_status") is not None else "-",
        max(0.0, now - started),
        f"{ttfb:.2f}s" if ttfb is not None else "-",
        int(diag.get("bytes") or 0),
        int(diag.get("chunks") or 0),
        request_ids,
        chain,
    )


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
class _BrokerRequest:
    body_bytes: int
    cancelled: threading.Event = field(default_factory=threading.Event)
    response_slots: threading.BoundedSemaphore = field(
        default_factory=lambda: threading.BoundedSemaphore(
            _MAX_IN_FLIGHT_RESPONSE_FRAMES
        )
    )
    response: Any = None
    response_lock: threading.Lock = field(default_factory=threading.Lock)

    def cancel(self) -> None:
        self.cancelled.set()

    def abort(self) -> None:
        self.cancel()
        with self.response_lock:
            response = self.response
        if response is not None:
            try:
                response.close()
            except Exception:
                pass


@dataclass
class _RelayPeer:
    lease: OwnerWorkerAuthorityLease
    connection: socket.socket
    send_lock: threading.Lock
    thread: threading.Thread
    executor: ThreadPoolExecutor
    slots: threading.BoundedSemaphore
    requests: dict[str, _BrokerRequest] = field(default_factory=dict)
    requests_lock: threading.Lock = field(default_factory=threading.Lock)
    request_bytes: int = 0
    handshake_complete: threading.Event = field(default_factory=threading.Event)


@dataclass
class _WorkerRequest:
    frames: queue.Queue[dict[str, Any] | DeploymentInferenceRelayError] = field(
        default_factory=lambda: queue.Queue(maxsize=_MAX_IN_FLIGHT_RESPONSE_FRAMES)
    )
    failed: threading.Event = field(default_factory=threading.Event)
    next_sequence: int = 0
    state: str = "awaiting_start"


class DeploymentInferenceBroker:
    """Control-plane-only broker for one active worker generation at a time."""

    def __init__(
        self,
        *,
        policy: DeploymentInferencePolicy,
        authority_store: AuthorityStore,
        policy_resolver: Any | None = None,
    ) -> None:
        policy.descriptor()
        self._policy_resolver = policy_resolver or (lambda: policy)
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
        peer = _RelayPeer(
            lease=lease,
            connection=parent,
            send_lock=threading.Lock(),
            thread=thread,
            executor=ThreadPoolExecutor(
                max_workers=_MAX_CONCURRENT_BROKER_REQUESTS,
                thread_name_prefix="inference-relay-request",
            ),
            slots=threading.BoundedSemaphore(_MAX_CONCURRENT_BROKER_REQUESTS),
        )
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

    @staticmethod
    def _cancel_peer_requests(peer: _RelayPeer) -> None:
        with peer.requests_lock:
            requests = tuple(peer.requests.values())
        for request in requests:
            request.abort()

    def revoke(self, lease: OwnerWorkerAuthorityLease) -> None:
        """Immediately close a relay endpoint once its generation drains."""
        with self._lock:
            peer = self._peers.pop(self._key(lease), None)
        if peer is not None:
            self._cancel_peer_requests(peer)
            try:
                peer.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            peer.connection.close()
            peer.executor.shutdown(wait=False, cancel_futures=True)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            peers = tuple(self._peers.values())
            self._peers.clear()
        for peer in peers:
            self._cancel_peer_requests(peer)
            try:
                peer.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            peer.connection.close()
            peer.executor.shutdown(wait=False, cancel_futures=True)

    @staticmethod
    def _send_peer_frame(peer: _RelayPeer, frame: dict[str, Any]) -> None:
        with peer.send_lock:
            _send_frame(peer.connection, frame)

    def _dispatch_request(
        self,
        peer: _RelayPeer,
        request_id: str,
        request: dict[str, Any],
        broker_request: _BrokerRequest,
    ) -> None:
        try:
            sequence = 0

            def emit(frame: dict[str, Any]) -> None:
                nonlocal sequence
                broker_request.response_slots.acquire()
                if broker_request.cancelled.is_set():
                    broker_request.response_slots.release()
                    raise DeploymentInferenceRelayError("relay request was cancelled")
                try:
                    self._send_peer_frame(peer, {
                        **frame,
                        "request_id": request_id,
                        "sequence": sequence,
                    })
                except BaseException:
                    broker_request.response_slots.release()
                    raise
                sequence += 1

            try:
                self._stream_request(
                    peer.lease,
                    request,
                    emit,
                    broker_request=broker_request,
                )
            except (AuthorizationRejected, DeploymentInferenceRelayError) as exc:
                if not broker_request.cancelled.is_set():
                    emit({
                        "type": "error",
                        "message": str(exc),
                    })
        except (DeploymentInferenceRelayError, OSError):
            pass
        finally:
            with peer.requests_lock:
                peer.requests.pop(request_id, None)
                peer.request_bytes -= broker_request.body_bytes
            peer.slots.release()

    def _serve_peer(self, key: tuple[str, int, str, int, int]) -> None:
        with self._lock:
            peer = self._peers.get(key)
        if peer is None:
            return
        try:
            hello = _recv_frame(peer.connection)
            if hello != {"type": "hello", "version": _PROTOCOL_VERSION}:
                raise DeploymentInferenceRelayError("relay protocol handshake is invalid")
            self._send_peer_frame(
                peer,
                {"type": "hello_ack", "version": _PROTOCOL_VERSION},
            )
            peer.handshake_complete.set()
            while True:
                frame = _recv_frame(peer.connection)
                request_id = frame.get("request_id")
                if not isinstance(request_id, str) or not request_id:
                    raise DeploymentInferenceRelayError("relay request identifier is invalid")
                frame_type = frame.get("type")
                if frame_type in {"cancel", "response_consumed"}:
                    with peer.requests_lock:
                        request = peer.requests.get(request_id)
                    if request is not None:
                        if frame_type == "cancel":
                            request.cancel()
                        request.response_slots.release()
                    continue
                if frame_type != "request_start":
                    raise DeploymentInferenceRelayError("relay frame type is invalid")
                body_bytes = len(str(frame.get("body") or ""))
                if not peer.slots.acquire(blocking=False):
                    self._send_peer_frame(peer, {
                        "type": "error",
                        "request_id": request_id,
                        "sequence": 0,
                        "message": "relay request capacity is exhausted",
                    })
                    continue
                with peer.requests_lock:
                    if (
                        request_id in peer.requests
                        or peer.request_bytes + body_bytes > _MAX_AGGREGATE_REQUEST_BYTES
                    ):
                        admitted = False
                    else:
                        broker_request = _BrokerRequest(body_bytes=body_bytes)
                        peer.requests[request_id] = broker_request
                        peer.request_bytes += body_bytes
                        admitted = True
                if not admitted:
                    peer.slots.release()
                    self._send_peer_frame(peer, {
                        "type": "error",
                        "request_id": request_id,
                        "sequence": 0,
                        "message": "relay request admission is unavailable",
                    })
                    continue
                peer.executor.submit(
                    self._dispatch_request,
                    peer,
                    request_id,
                    frame,
                    broker_request,
                )
        except (DeploymentInferenceRelayError, OSError):
            pass
        finally:
            self._cancel_peer_requests(peer)
            with self._lock:
                for candidate_key, candidate in tuple(self._peers.items()):
                    if candidate is peer:
                        self._peers.pop(candidate_key, None)
                        break
            try:
                peer.connection.close()
            except OSError:
                pass
            peer.executor.shutdown(wait=False, cancel_futures=True)

    def _request_parts(
        self,
        lease: OwnerWorkerAuthorityLease,
        request: dict[str, Any],
    ) -> tuple[str, bytes, dict[str, str]]:
        method = str(request.get("method") or "").upper()
        path = str(request.get("path") or "")
        if method == "GET" and path == deployment_routes_path():
            raise DeploymentInferenceRelayError("relay metadata request requires dedicated handling")
        if method != "POST" or path not in _ALLOWED_PATHS:
            raise DeploymentInferenceRelayError("relay request is not allowed")
        try:
            body = base64.b64decode(str(request.get("body") or ""), validate=True)
            payload = json.loads(body)
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise DeploymentInferenceRelayError("relay request body is invalid") from exc
        if not isinstance(payload, dict):
            raise DeploymentInferenceRelayError("relay request model is not allowed")
        incoming_headers = request.get("headers")
        if not isinstance(incoming_headers, dict):
            raise DeploymentInferenceRelayError("relay request headers are invalid")
        selected_provider = next(
            (
                str(value or "").strip().lower()
                for name, value in incoming_headers.items()
                if str(name).lower() == _PROVIDER_HEADER
            ),
            "",
        )
        selected_model = str(payload.get("model") or "")
        if not selected_provider:
            raise DeploymentInferenceRelayError("relay provider/model route is not allowed")
        try:
            self._authority_store.assert_worker_lease(
                lease,
                states=frozenset({WorkerLeaseState.ACTIVE}),
            )
        except AuthorizationRejected as exc:
            raise DeploymentInferenceRelayError("relay worker lease is not active") from exc
        try:
            policy = self._policy_resolver()
        except Exception as exc:
            raise DeploymentInferenceRelayError("relay routing policy is unavailable") from exc
        route = policy.route_for(selected_model, provider=selected_provider)
        if route is None:
            raise DeploymentInferenceRelayError("relay provider/model route is not allowed")
        expected_path = request_path_for_api_mode(route.api_mode)
        if path != expected_path:
            raise DeploymentInferenceRelayError("relay request API mode does not match policy")
        headers = {
            str(name): str(value)
            for name, value in incoming_headers.items()
            if str(name).lower() not in _HOP_BY_HOP_HEADERS
        }
        # Resolve the credential only after all untrusted request validation and
        # the exact durable ACTIVE lease check have succeeded.
        runtime = policy.resolve_route_runtime(route)
        if route.api_mode == "anthropic_messages":
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
        *,
        broker_request: _BrokerRequest | None = None,
    ) -> None:
        if (
            str(request.get("method") or "").upper() == "GET"
            and str(request.get("path") or "") == deployment_routes_path()
        ):
            try:
                self._authority_store.assert_worker_lease(
                    lease,
                    states=frozenset({WorkerLeaseState.ACTIVE}),
                )
                policy = self._policy_resolver()
            except Exception as exc:
                raise DeploymentInferenceRelayError("relay routing policy is unavailable") from exc
            body = json.dumps(
                [route.payload() for route in policy.route_descriptors()],
                separators=(",", ":"),
            ).encode("utf-8")
            emit({
                "type": "response_start",
                "status": 200,
                "headers": {"Content-Type": "application/json"},
            })
            emit({"type": "response_chunk", "body": base64.b64encode(body).decode("ascii")})
            emit({"type": "response_end"})
            return
        upstream, body, headers = self._request_parts(lease, request)
        diag = stream_diag_init()
        response_started = False
        try:
            import httpx

            timeout = httpx.Timeout(
                connect=_RELAY_CONNECT_TIMEOUT_SECONDS,
                read=_RELAY_READ_TIMEOUT_SECONDS,
                write=_RELAY_READ_TIMEOUT_SECONDS,
                pool=_RELAY_CONNECT_TIMEOUT_SECONDS,
            )
            with httpx.Client(timeout=timeout) as client:
                with client.stream("POST", upstream, content=body, headers=headers) as response:
                    if broker_request is not None:
                        with broker_request.response_lock:
                            broker_request.response = response
                        if broker_request.cancelled.is_set():
                            raise DeploymentInferenceRelayError("relay request was cancelled")
                    response_started = True
                    _capture_relay_response_diag(diag, response)
                    safe_headers = {
                        name: value
                        for name, value in response.headers.items()
                        if name.lower() in _SAFE_RESPONSE_HEADERS
                    }
                    emit({
                        "type": "response_start",
                        "status": response.status_code,
                        "headers": safe_headers,
                    })
                    # Forward each transport chunk as soon as httpx yields it. A
                    # fixed chunk_size makes ByteChunker buffer small SSE events
                    # until the threshold or EOF, defeating streaming semantics.
                    for chunk in response.iter_raw(chunk_size=None):
                        if chunk:
                            if diag["first_chunk_at"] is None:
                                diag["first_chunk_at"] = time.time()
                            diag["chunks"] += 1
                            diag["bytes"] += len(chunk)
                            emit({
                                "type": "response_chunk",
                                "body": base64.b64encode(chunk).decode("ascii"),
                            })
                    emit({"type": "response_end"})
                if broker_request is not None:
                    with broker_request.response_lock:
                        broker_request.response = None
        except DeploymentInferenceRelayError as exc:
            _log_relay_outcome(
                "midstream_failure" if response_started else "pre_header_transport_failure",
                diag,
                error=exc,
            )
            raise
        except Exception as exc:
            _log_relay_outcome(
                "midstream_failure" if response_started else "pre_header_transport_failure",
                diag,
                error=exc,
            )
            raise DeploymentInferenceRelayError("deployment inference upstream is unavailable") from exc
        else:
            _log_relay_outcome(
                "upstream_http_error"
                if int(diag.get("http_status") or 0) >= 400
                else "complete",
                diag,
            )



class OwnerInferenceRelay:
    """Worker-local loopback HTTP server backed by its inherited descriptor."""

    def __init__(self, inherited_fd: int) -> None:
        if inherited_fd < 0:
            raise DeploymentInferenceRelayError("relay descriptor is invalid")
        self._connection = socket.socket(fileno=inherited_fd)
        # The descriptor is consumed by this relay only.  Future PTY/tool
        # subprocesses must not inherit it even if a caller forgets an env scrub.
        self._connection.set_inheritable(False)
        self._send_lock = threading.Lock()
        self._requests: dict[str, _WorkerRequest] = {}
        self._requests_lock = threading.Lock()
        self._request_slots = threading.BoundedSemaphore(_MAX_CONCURRENT_WORKER_REQUESTS)
        self._connection_error: DeploymentInferenceRelayError | None = None
        self._handshake_complete = threading.Event()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._reader_thread = threading.Thread(
            target=self._read_responses,
            daemon=True,
            name="owner-inference-relay-reader",
        )
        self._reader_thread.start()
        self._send({"type": "hello", "version": _PROTOCOL_VERSION})
        if not self._handshake_complete.wait(timeout=_RELAY_CONNECT_TIMEOUT_SECONDS):
            self.close()
            raise DeploymentInferenceRelayError("relay protocol handshake timed out")
        if self._connection_error is not None:
            self.close()
            raise self._connection_error

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
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:  # noqa: N802
                relay._handle_http(self)

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
        self._reader_thread.join(timeout=2)

    def _send(self, frame: dict[str, Any]) -> None:
        with self._send_lock:
            _send_frame(self._connection, frame)

    def _fail_requests(self, error: DeploymentInferenceRelayError) -> None:
        self._connection_error = error
        with self._requests_lock:
            requests = tuple(self._requests.values())
        for request in requests:
            request.failed.set()
            try:
                request.frames.put_nowait(error)
            except queue.Full:
                pass

    def _read_responses(self) -> None:
        try:
            frame = _recv_frame(self._connection)
            if frame != {"type": "hello_ack", "version": _PROTOCOL_VERSION}:
                raise DeploymentInferenceRelayError("relay protocol handshake is invalid")
            self._handshake_complete.set()
            while True:
                frame = _recv_frame(self._connection)
                request_id = frame.get("request_id")
                if not isinstance(request_id, str) or not request_id:
                    raise DeploymentInferenceRelayError("relay response identifier is invalid")
                with self._requests_lock:
                    request = self._requests.get(request_id)
                if request is not None and not request.failed.is_set():
                    sequence = frame.get("sequence")
                    frame_type = frame.get("type")
                    if not isinstance(sequence, int) or sequence != request.next_sequence:
                        raise DeploymentInferenceRelayError("relay response sequence is invalid")
                    if request.state == "awaiting_start":
                        if frame_type == "response_start":
                            request.state = "streaming"
                        elif frame_type == "error":
                            request.state = "finished"
                        else:
                            raise DeploymentInferenceRelayError("relay response transition is invalid")
                    elif request.state == "streaming":
                        if frame_type == "response_chunk":
                            pass
                        elif frame_type in {"response_end", "error"}:
                            request.state = "finished"
                        else:
                            raise DeploymentInferenceRelayError("relay response transition is invalid")
                    else:
                        raise DeploymentInferenceRelayError("relay response transition is invalid")
                    request.next_sequence += 1
                    request.frames.put_nowait(frame)
        except (DeploymentInferenceRelayError, OSError) as exc:
            self._handshake_complete.set()
            self._fail_requests(
                exc
                if isinstance(exc, DeploymentInferenceRelayError)
                else DeploymentInferenceRelayError("relay peer closed")
            )

    def _acknowledge_response(self, frame: dict[str, Any]) -> None:
        self._send({
            "type": "response_consumed",
            "request_id": str(frame["request_id"]),
        })

    def _receive_for(
        self,
        request: _WorkerRequest,
        *,
        deadline: float,
    ) -> dict[str, Any]:
        if self._connection_error is not None:
            raise self._connection_error
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DeploymentInferenceRelayError("relay request deadline exceeded")
        try:
            frame = request.frames.get(timeout=remaining)
        except queue.Empty as exc:
            raise DeploymentInferenceRelayError("relay request deadline exceeded") from exc
        if isinstance(frame, DeploymentInferenceRelayError):
            raise frame
        return frame

    def _handle_http(self, handler: BaseHTTPRequestHandler) -> None:
        headers_sent = False
        admitted = self._request_slots.acquire(blocking=False)
        request_id = uuid.uuid4().hex
        worker_request = _WorkerRequest()
        if not admitted:
            handler.send_error(503, "deployment inference relay capacity exhausted")
            return
        with self._requests_lock:
            if self._connection_error is not None:
                self._request_slots.release()
                raise self._connection_error
            self._requests[request_id] = worker_request
        try:
            path = urlparse(handler.path).path
            length = int(handler.headers.get("Content-Length", "0"))
            if handler.command == "GET" and path != deployment_routes_path():
                raise DeploymentInferenceRelayError("relay request is not allowed")
            if length < 0 or length > _MAX_FRAME_BYTES:
                raise DeploymentInferenceRelayError("relay request is too large")
            raw_deadline = handler.headers.get(_RELAY_DEADLINE_HEADER)
            try:
                deadline_seconds = (
                    min(float(raw_deadline), _RELAY_MAX_DEADLINE_SECONDS)
                    if raw_deadline is not None
                    else _RELAY_MAX_DEADLINE_SECONDS
                )
            except (TypeError, ValueError) as exc:
                raise DeploymentInferenceRelayError("relay request deadline is invalid") from exc
            if deadline_seconds <= 0:
                raise DeploymentInferenceRelayError("relay request deadline is invalid")
            deadline = time.monotonic() + deadline_seconds
            self._send({
                "type": "request_start",
                "request_id": request_id,
                "method": handler.command,
                "path": path,
                "headers": dict(handler.headers.items()),
                "body": base64.b64encode(handler.rfile.read(length)).decode("ascii"),
            })
            response = self._receive_for(worker_request, deadline=deadline)
            self._acknowledge_response(response)
            if response.get("type") == "error":
                raise DeploymentInferenceRelayError("relay request was rejected")
            if response.get("type") != "response_start":
                raise DeploymentInferenceRelayError("relay response is invalid")
            status = int(response["status"])
            headers = response.get("headers")
            if not isinstance(headers, dict):
                raise DeploymentInferenceRelayError("relay response is invalid")
            headers_sent = True
            try:
                handler.send_response(status)
                for name, value in headers.items():
                    if str(name).lower() not in _HOP_BY_HOP_HEADERS:
                        handler.send_header(str(name), str(value))
                handler.send_header("Transfer-Encoding", "chunked")
                handler.end_headers()
                while True:
                    response = self._receive_for(
                        worker_request,
                        deadline=deadline,
                    )
                    response_type = response.get("type")
                    if response_type == "response_end":
                        handler.wfile.write(b"0\r\n\r\n")
                        handler.wfile.flush()
                        self._acknowledge_response(response)
                        break
                    if response_type == "error":
                        self._acknowledge_response(response)
                        raise DeploymentInferenceRelayError("relay response failed")
                    if response_type != "response_chunk":
                        self._acknowledge_response(response)
                        raise DeploymentInferenceRelayError("relay response is invalid")
                    body = base64.b64decode(str(response.get("body") or ""), validate=True)
                    if body:
                        handler.wfile.write(f"{len(body):X}\r\n".encode("ascii"))
                        handler.wfile.write(body)
                        handler.wfile.write(b"\r\n")
                        handler.wfile.flush()
                    self._acknowledge_response(response)
            except (BrokenPipeError, ConnectionError, OSError):
                handler.close_connection = True
                try:
                    self._send({"type": "cancel", "request_id": request_id})
                except (DeploymentInferenceRelayError, OSError):
                    pass
                raise
        except Exception as exc:
            logger.warning(
                "owner inference relay response failed phase=%s error_type=%s",
                "midstream" if headers_sent else "pre_header",
                type(exc).__name__,
            )
            if headers_sent:
                # HTTP status and headers are already on the wire. Sending a second
                # response would corrupt the provider stream; close it instead.
                handler.close_connection = True
                return
            try:
                handler.send_error(502, "deployment inference relay unavailable")
            except (BrokenPipeError, ConnectionError, OSError):
                handler.close_connection = True
        finally:
            with self._requests_lock:
                self._requests.pop(request_id, None)
            self._request_slots.release()
