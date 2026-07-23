"""Lease-bound Control Plane resource broker for authenticated executors.

The Control Plane retains all cgroup filesystem authority.  An Owner Worker gets
only one inherited socket endpoint bound to its exact durable generation lease;
requests contain bounded executor identity fields and opaque reservation tokens,
never cgroup paths or raw owner identifiers.
"""
from __future__ import annotations

import json
import os
import secrets
import socket
import struct
import threading
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence

from hermes_cli.dashboard_auth.authority import (
    AuthorityStore,
    AuthorizationRejected,
    OwnerWorkerAuthorityLease,
    WorkerLeaseState,
)
from hermes_cli.owner_worker.cgroup_v2 import CgroupResourceEvents, CgroupScopeLease
from hermes_cli.owner_worker.executor_identity import ExecutorIdentity, ExecutorIdentityInvalid

_MAX_FRAME_BYTES = 64 * 1024
_MAX_PIDS = 64
_MAX_TEXT = 512


class ResourceBrokerError(RuntimeError):
    """The private resource authority rejected or could not serve a request."""


class ExecutorResourceScope(Protocol):
    def attach_pids(self, pids: Sequence[int]) -> None: ...
    def verify_pids(self, pids: Sequence[int]) -> bool: ...
    def read_events(self) -> CgroupResourceEvents: ...
    def release(self) -> None: ...


class ExecutorResourceController(Protocol):
    def reserve_executor(self, identity: ExecutorIdentity, invocation_id: str) -> ExecutorResourceScope: ...
    def shutdown_generation(self) -> None: ...
    def close(self) -> None: ...


class ResourceManager(Protocol):
    def admit_executor(self, identity: ExecutorIdentity, invocation_id: str) -> CgroupScopeLease: ...


def _send_frame(connection: socket.socket, value: Mapping[str, Any]) -> None:
    encoded = json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
    if not encoded or len(encoded) > _MAX_FRAME_BYTES:
        raise ResourceBrokerError("resource broker frame is invalid")
    connection.sendall(struct.pack("!I", len(encoded)) + encoded)


def _recv_exact(connection: socket.socket, count: int) -> bytes:
    chunks: list[bytes] = []
    remaining = count
    while remaining:
        chunk = connection.recv(remaining)
        if not chunk:
            raise ResourceBrokerError("resource broker peer closed")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _recv_frame(connection: socket.socket) -> dict[str, Any]:
    size = struct.unpack("!I", _recv_exact(connection, 4))[0]
    if not size or size > _MAX_FRAME_BYTES:
        raise ResourceBrokerError("resource broker frame is invalid")
    try:
        value = json.loads(_recv_exact(connection, size))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ResourceBrokerError("resource broker frame is malformed") from exc
    if not isinstance(value, dict):
        raise ResourceBrokerError("resource broker frame is malformed")
    return value


def _bounded_text(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise ResourceBrokerError(f"resource {field_name} is invalid")
    normalized = value.strip()
    if not normalized or len(normalized) > _MAX_TEXT or "\x00" in normalized:
        raise ResourceBrokerError(f"resource {field_name} is invalid")
    return normalized


def _pids(value: object) -> tuple[int, ...]:
    if not isinstance(value, list) or not value or len(value) > _MAX_PIDS:
        raise ResourceBrokerError("resource process list is invalid")
    result: list[int] = []
    for pid in value:
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            raise ResourceBrokerError("resource process list is invalid")
        result.append(pid)
    if len(result) != len(set(result)):
        raise ResourceBrokerError("resource process list is invalid")
    return tuple(result)


def _events_payload(events: CgroupResourceEvents) -> dict[str, Any]:
    return {
        "populated": events.populated,
        "frozen": events.frozen,
        "cpu": dict(events.cpu),
        "memory": dict(events.memory),
        "pids": dict(events.pids),
    }


def _events_from_payload(value: object) -> CgroupResourceEvents:
    if not isinstance(value, dict) or set(value) != {"populated", "frozen", "cpu", "memory", "pids"}:
        raise ResourceBrokerError("resource event response is invalid")
    if not isinstance(value["populated"], bool) or not isinstance(value["frozen"], bool):
        raise ResourceBrokerError("resource event response is invalid")
    mappings: list[dict[str, int]] = []
    for name in ("cpu", "memory", "pids"):
        source = value[name]
        if not isinstance(source, dict) or any(
            not isinstance(key, str) or not key or isinstance(item, bool) or not isinstance(item, int) or item < 0
            for key, item in source.items()
        ):
            raise ResourceBrokerError("resource event response is invalid")
        mappings.append(dict(source))
    from types import MappingProxyType

    return CgroupResourceEvents(
        populated=value["populated"], frozen=value["frozen"],
        cpu=MappingProxyType(mappings[0]), memory=MappingProxyType(mappings[1]),
        pids=MappingProxyType(mappings[2]),
    )


@dataclass
class _Reservation:
    identity: ExecutorIdentity
    invocation_id: str
    scope: CgroupScopeLease


@dataclass
class _BrokerPeer:
    lease: OwnerWorkerAuthorityLease
    connection: socket.socket
    thread: threading.Thread
    reservations: dict[str, _Reservation] = field(default_factory=dict)
    operation_lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    closed: bool = False


class DeploymentResourceBroker:
    """Control-plane-only cgroup authority fenced to exact worker leases."""

    def __init__(self, *, manager: ResourceManager, authority_store: AuthorityStore) -> None:
        if not callable(getattr(manager, "admit_executor", None)):
            raise ResourceBrokerError("resource manager is invalid")
        self._manager = manager
        self._authority_store = authority_store
        self._peers: dict[tuple[str, int, str, int, int], _BrokerPeer] = {}
        self._lock = threading.RLock()
        self._closed = False

    @staticmethod
    def _key(lease: OwnerWorkerAuthorityLease) -> tuple[str, int, str, int, int]:
        return (
            lease.owner_key, lease.worker_generation, lease.worker_id,
            lease.lease_version, lease.recovery_generation,
        )

    @staticmethod
    def _generation_key(lease: OwnerWorkerAuthorityLease) -> tuple[str, int, str, int]:
        return lease.owner_key, lease.worker_generation, lease.worker_id, lease.recovery_generation

    def register(self, lease: OwnerWorkerAuthorityLease) -> int:
        self._authority_store.assert_worker_lease(lease, states=frozenset({WorkerLeaseState.STARTING}))
        parent, child = socket.socketpair()
        parent.set_inheritable(False)
        child.set_inheritable(False)
        key = self._key(lease)
        thread = threading.Thread(
            target=self._serve_peer, args=(key,), daemon=True,
            name=f"resource-broker-{lease.worker_generation}",
        )
        peer = _BrokerPeer(lease, parent, thread)
        with self._lock:
            if self._closed or key in self._peers:
                parent.close()
                child.close()
                raise ResourceBrokerError("resource broker registration is unavailable")
            self._peers[key] = peer
        thread.start()
        return child.detach()

    def activate(self, lease: OwnerWorkerAuthorityLease) -> None:
        generation_key = self._generation_key(lease)
        with self._lock:
            current_key = next(
                (key for key, peer in self._peers.items() if self._generation_key(peer.lease) == generation_key),
                None,
            )
            if current_key is None:
                raise ResourceBrokerError("resource broker peer is unavailable")
            peer = self._peers.pop(current_key)
            with peer.operation_lock:
                if peer.closed:
                    raise ResourceBrokerError("resource broker peer is unavailable")
                peer.lease = lease
            self._peers[self._key(lease)] = peer

    def revoke(self, lease: OwnerWorkerAuthorityLease) -> None:
        generation_key = self._generation_key(lease)
        with self._lock:
            key = next(
                (candidate for candidate, peer in self._peers.items() if self._generation_key(peer.lease) == generation_key),
                None,
            )
            peer = self._peers.pop(key, None) if key is not None else None
        if peer is not None:
            self._close_peer(peer)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            peers = tuple(self._peers.values())
            self._peers.clear()
        first_error: Exception | None = None
        for peer in peers:
            try:
                self._close_peer(peer)
            except Exception as exc:
                first_error = first_error or exc
        if first_error is not None:
            raise ResourceBrokerError("resource broker cleanup failed") from first_error

    def _serve_peer(self, key: tuple[str, int, str, int, int]) -> None:
        with self._lock:
            peer = self._peers.get(key)
        if peer is None:
            return
        try:
            while True:
                request = _recv_frame(peer.connection)
                try:
                    with peer.operation_lock:
                        if peer.closed:
                            raise ResourceBrokerError("resource broker peer is unavailable")
                        response = self._handle_request(peer, request)
                except Exception:
                    response = {"ok": False, "error": "resource request rejected"}
                _send_frame(peer.connection, response)
        except (OSError, ResourceBrokerError):
            pass
        finally:
            with self._lock:
                for candidate, item in tuple(self._peers.items()):
                    if item is peer:
                        self._peers.pop(candidate, None)
                        break
            try:
                self._close_peer(peer)
            except Exception:
                pass

    def _require_active(self, peer: _BrokerPeer) -> None:
        try:
            self._authority_store.assert_worker_lease(
                peer.lease, states=frozenset({WorkerLeaseState.ACTIVE})
            )
        except AuthorizationRejected as exc:
            raise ResourceBrokerError("resource worker lease is not active") from exc

    @staticmethod
    def _identity(lease: OwnerWorkerAuthorityLease, value: object) -> ExecutorIdentity:
        if not isinstance(value, dict) or set(value) != {
            "workspace_prefix", "task_id", "session_id", "executor_id", "executor_generation",
        }:
            raise ResourceBrokerError("resource executor identity is invalid")
        try:
            identity = ExecutorIdentity(
                owner_key=lease.owner_key,
                workspace_prefix=_bounded_text(value["workspace_prefix"], "workspace identity"),
                worker_id=lease.worker_id,
                worker_generation=lease.worker_generation,
                lease_version=lease.lease_version,
                recovery_generation=lease.recovery_generation,
                task_id=_bounded_text(value["task_id"], "task identity"),
                session_id=_bounded_text(value["session_id"], "session identity"),
                executor_id=_bounded_text(value["executor_id"], "executor identity"),
                executor_generation=value["executor_generation"],
            )
        except (ExecutorIdentityInvalid, TypeError, ValueError) as exc:
            raise ResourceBrokerError("resource executor identity is invalid") from exc
        return identity

    @staticmethod
    def _reservation(peer: _BrokerPeer, request: Mapping[str, Any]) -> _Reservation:
        token = _bounded_text(request.get("reservation"), "reservation")
        reservation = peer.reservations.get(token)
        if reservation is None:
            raise ResourceBrokerError("resource reservation is unavailable")
        return reservation

    def _handle_request(self, peer: _BrokerPeer, request: dict[str, Any]) -> dict[str, Any]:
        self._require_active(peer)
        operation = request.get("operation")
        if operation == "reserve_executor":
            if set(request) != {"operation", "identity", "invocation"}:
                raise ResourceBrokerError("resource reservation request is invalid")
            identity = self._identity(peer.lease, request["identity"])
            invocation = _bounded_text(request["invocation"], "invocation identity")
            scope = self._manager.admit_executor(identity, invocation)
            token = secrets.token_hex(24)
            peer.reservations[token] = _Reservation(identity, invocation, scope)
            return {"ok": True, "reservation": token}
        if operation in {"attach_pids", "verify_pids"}:
            if set(request) != {"operation", "reservation", "pids"}:
                raise ResourceBrokerError("resource process request is invalid")
            reservation = self._reservation(peer, request)
            pids = _pids(request["pids"])
            if operation == "attach_pids":
                for pid in pids:
                    reservation.scope.attach(pid)
                return {"ok": True}
            return {"ok": True, "verified": all(reservation.scope.verify_membership(pid) for pid in pids)}
        if operation == "read_events":
            if set(request) != {"operation", "reservation"}:
                raise ResourceBrokerError("resource event request is invalid")
            reservation = self._reservation(peer, request)
            return {"ok": True, "events": _events_payload(reservation.scope.read_events())}
        if operation == "release":
            if set(request) != {"operation", "reservation"}:
                raise ResourceBrokerError("resource release request is invalid")
            token = _bounded_text(request["reservation"], "reservation")
            reservation = self._reservation(peer, request)
            reservation.scope.cleanup()
            peer.reservations.pop(token, None)
            return {"ok": True}
        if operation == "shutdown_generation":
            if set(request) != {"operation"}:
                raise ResourceBrokerError("resource shutdown request is invalid")
            self._cleanup_reservations(peer)
            return {"ok": True}
        raise ResourceBrokerError("resource operation is not allowed")

    @staticmethod
    def _cleanup_reservations(peer: _BrokerPeer) -> None:
        first_error: Exception | None = None
        for token, reservation in tuple(peer.reservations.items()):
            try:
                reservation.scope.cleanup()
            except Exception as exc:
                first_error = first_error or exc
            else:
                peer.reservations.pop(token, None)
        if first_error is not None:
            raise first_error

    def _close_peer(self, peer: _BrokerPeer) -> None:
        with peer.operation_lock:
            if peer.closed:
                return
            peer.closed = True
            try:
                peer.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            peer.connection.close()
            self._cleanup_reservations(peer)


def _deidentified_identity(identity: ExecutorIdentity) -> dict[str, Any]:
    if not isinstance(identity, ExecutorIdentity):
        raise ResourceBrokerError("resource executor identity is invalid")
    return {
        "workspace_prefix": identity.workspace_prefix,
        "task_id": identity.task_id,
        "session_id": identity.session_id,
        "executor_id": identity.executor_id,
        "executor_generation": identity.executor_generation,
    }


class OwnerResourceBrokerClient:
    """Owner-worker controller backed only by one inherited private endpoint."""

    def __init__(self, inherited_fd: int) -> None:
        if isinstance(inherited_fd, bool) or not isinstance(inherited_fd, int) or inherited_fd < 0:
            raise ResourceBrokerError("resource broker descriptor is invalid")
        try:
            self._connection = socket.socket(fileno=inherited_fd)
            if self._connection.family != socket.AF_UNIX:
                raise ResourceBrokerError("resource broker descriptor is invalid")
            self._connection.set_inheritable(False)
        except Exception:
            try:
                os.close(inherited_fd)
            except OSError:
                pass
            raise
        self._lock = threading.Lock()
        self._closed = False

    def _request(self, request: Mapping[str, Any]) -> dict[str, Any]:
        if self._closed:
            raise ResourceBrokerError("resource broker is unavailable")
        try:
            with self._lock:
                _send_frame(self._connection, request)
                response = _recv_frame(self._connection)
        except OSError as exc:
            raise ResourceBrokerError("resource broker is unavailable") from exc
        if response.get("ok") is not True:
            raise ResourceBrokerError("resource request was rejected")
        return response

    def reserve_executor(self, identity: ExecutorIdentity, invocation_id: str) -> "OwnerResourceScopeClient":
        response = self._request({
            "operation": "reserve_executor",
            "identity": _deidentified_identity(identity),
            "invocation": _bounded_text(invocation_id, "invocation identity"),
        })
        token = _bounded_text(response.get("reservation"), "reservation")
        return OwnerResourceScopeClient(self, token)

    def shutdown_generation(self) -> None:
        try:
            self._request({"operation": "shutdown_generation"})
        finally:
            self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._connection.close()


class OwnerResourceScopeClient:
    def __init__(self, controller: OwnerResourceBrokerClient, token: str) -> None:
        self._controller = controller
        self._token = token
        self._released = False

    def _active_token(self) -> str:
        if self._released:
            raise ResourceBrokerError("resource reservation is released")
        return self._token

    def attach_pids(self, pids: Sequence[int]) -> None:
        values = _pids(list(pids))
        self._controller._request({
            "operation": "attach_pids", "reservation": self._active_token(), "pids": list(values),
        })

    def verify_pids(self, pids: Sequence[int]) -> bool:
        values = _pids(list(pids))
        response = self._controller._request({
            "operation": "verify_pids", "reservation": self._active_token(), "pids": list(values),
        })
        if not isinstance(response.get("verified"), bool):
            raise ResourceBrokerError("resource verification response is invalid")
        return response["verified"]

    def read_events(self) -> CgroupResourceEvents:
        response = self._controller._request({
            "operation": "read_events", "reservation": self._active_token(),
        })
        return _events_from_payload(response.get("events"))

    def release(self) -> None:
        if self._released:
            return
        self._controller._request({"operation": "release", "reservation": self._token})
        self._released = True
