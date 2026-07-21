"""Lease-bound deployment image relay with no worker-visible credentials."""
from __future__ import annotations

import base64
import json
import os
import socket
import struct
import threading
from dataclasses import dataclass
from typing import Any

from hermes_cli.dashboard_auth.authority import (
    AuthorityStore, AuthorizationRejected, OwnerWorkerAuthorityLease, WorkerLeaseState,
)
from hermes_cli.deployment_image import (
    DeploymentImageDescriptor, DeploymentImagePolicy, DeploymentImagePolicyInvalid,
    DeploymentImageSelectionRejected,
)

_MAX_FRAME_BYTES = 96 * 1024 * 1024
_ALLOWED_MIME_TYPES = frozenset({"image/png", "image/jpeg", "image/webp"})
_ALLOWED_ASPECT_RATIOS = frozenset({"landscape", "square", "portrait"})
_SAFE_METADATA_KEYS = frozenset({
    "aspect_ratio_native", "quality", "revised_prompt", "size", "upstream_model",
})


class DeploymentImageRelayError(RuntimeError):
    """The worker-to-control-plane image relay rejected a request."""


def _send_frame(connection: socket.socket, value: dict[str, Any]) -> None:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if not encoded or len(encoded) > _MAX_FRAME_BYTES:
        raise DeploymentImageRelayError("deployment image relay frame is invalid")
    connection.sendall(struct.pack("!I", len(encoded)) + encoded)


def _recv_exact(connection: socket.socket, count: int) -> bytes:
    chunks: list[bytes] = []
    remaining = count
    while remaining:
        chunk = connection.recv(remaining)
        if not chunk:
            raise DeploymentImageRelayError("deployment image relay peer closed")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _recv_frame(connection: socket.socket) -> dict[str, Any]:
    size = struct.unpack("!I", _recv_exact(connection, 4))[0]
    if not size or size > _MAX_FRAME_BYTES:
        raise DeploymentImageRelayError("deployment image relay frame is invalid")
    try:
        value = json.loads(_recv_exact(connection, size))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DeploymentImageRelayError("deployment image relay frame is malformed") from exc
    if not isinstance(value, dict):
        raise DeploymentImageRelayError("deployment image relay frame is malformed")
    return value


def _safe_metadata(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    safe: dict[str, Any] = {}
    for key, item in value.items():
        name = str(key)
        if name not in _SAFE_METADATA_KEYS:
            continue
        if item is None or isinstance(item, (bool, int, float)):
            safe[name] = item
        elif isinstance(item, str) and len(item) <= 4096:
            safe[name] = item
    return safe


@dataclass
class _RelayPeer:
    lease: OwnerWorkerAuthorityLease
    connection: socket.socket
    lock: threading.Lock
    thread: threading.Thread


class DeploymentImageBroker:
    """Control-plane-only APIYI broker fenced to exact durable worker leases."""

    def __init__(self, *, policy: DeploymentImagePolicy, authority_store: AuthorityStore) -> None:
        self._policy = policy
        self._authority_store = authority_store
        self._peers: dict[tuple[str, int, str, int, int], _RelayPeer] = {}
        self._lock = threading.RLock()
        self._closed = False

    @staticmethod
    def _key(lease: OwnerWorkerAuthorityLease) -> tuple[str, int, str, int, int]:
        return (lease.owner_key, lease.worker_generation, lease.worker_id, lease.lease_version, lease.recovery_generation)

    def register(self, lease: OwnerWorkerAuthorityLease) -> int:
        self._authority_store.assert_worker_lease(lease, states=frozenset({WorkerLeaseState.STARTING}))
        parent, child = socket.socketpair()
        parent.set_inheritable(False)
        child.set_inheritable(False)
        key = self._key(lease)
        thread = threading.Thread(target=self._serve_peer, args=(key,), daemon=True, name=f"image-relay-{lease.worker_generation}")
        peer = _RelayPeer(lease, parent, threading.Lock(), thread)
        with self._lock:
            if self._closed or key in self._peers:
                parent.close()
                child.close()
                raise DeploymentImageRelayError("deployment image relay registration is unavailable")
            self._peers[key] = peer
        thread.start()
        return child.detach()

    def activate(self, lease: OwnerWorkerAuthorityLease) -> None:
        identity = (lease.owner_key, lease.worker_generation, lease.worker_id, lease.recovery_generation)
        with self._lock:
            current_key = next((key for key, peer in self._peers.items()
                if (peer.lease.owner_key, peer.lease.worker_generation, peer.lease.worker_id, peer.lease.recovery_generation) == identity), None)
            if current_key is None:
                raise DeploymentImageRelayError("deployment image relay peer is unavailable")
            peer = self._peers.pop(current_key)
            peer.lease = lease
            self._peers[self._key(lease)] = peer

    def revoke(self, lease: OwnerWorkerAuthorityLease) -> None:
        with self._lock:
            peer = self._peers.pop(self._key(lease), None)
            if peer is None:
                identity = (lease.owner_key, lease.worker_generation, lease.worker_id, lease.recovery_generation)
                key = next((candidate for candidate, item in self._peers.items()
                    if (item.lease.owner_key, item.lease.worker_generation, item.lease.worker_id, item.lease.recovery_generation) == identity), None)
                peer = self._peers.pop(key, None) if key is not None else None
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
                        response = self._handle_request(peer.lease, request)
                    except (DeploymentImageRelayError, DeploymentImagePolicyInvalid, DeploymentImageSelectionRejected):
                        response = {"ok": False, "error": "deployment image request rejected"}
                    _send_frame(peer.connection, response)
        except (DeploymentImageRelayError, OSError):
            pass
        finally:
            with self._lock:
                for candidate, item in tuple(self._peers.items()):
                    if item is peer:
                        self._peers.pop(candidate, None)
                        break
            peer.connection.close()

    def _handle_request(self, lease: OwnerWorkerAuthorityLease, request: dict[str, Any]) -> dict[str, Any]:
        try:
            self._authority_store.assert_worker_lease(lease, states=frozenset({WorkerLeaseState.ACTIVE}))
        except AuthorizationRejected as exc:
            raise DeploymentImageRelayError("deployment image worker lease is not active") from exc
        if set(request) != {"operation", "policy_id", "prompt", "aspect_ratio", "model", "references"}:
            raise DeploymentImageRelayError("deployment image request is invalid")
        descriptor = self._policy.descriptor()
        if request["operation"] != "image_generate" or request["policy_id"] != descriptor.policy_id:
            raise DeploymentImageRelayError("deployment image request policy is invalid")
        prompt = request["prompt"]
        aspect_ratio = request["aspect_ratio"]
        model = request["model"]
        raw_references = request["references"]
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 32_768 or "\x00" in prompt:
            raise DeploymentImageRelayError("deployment image prompt is invalid")
        if aspect_ratio not in _ALLOWED_ASPECT_RATIOS or not isinstance(model, str) or not descriptor.allows_model(model):
            raise DeploymentImageRelayError("deployment image selection is invalid")
        if not isinstance(raw_references, list) or len(raw_references) > descriptor.max_reference_images:
            raise DeploymentImageRelayError("deployment image references are invalid")
        references: list[dict[str, Any]] = []
        total = 0
        for item in raw_references:
            if not isinstance(item, dict) or set(item) != {"name", "mime_type", "data"}:
                raise DeploymentImageRelayError("deployment image reference is invalid")
            name = item["name"]
            mime_type = item["mime_type"]
            if not isinstance(name, str) or not name or len(name) > 255 or any(ch in name for ch in "/\\\x00"):
                raise DeploymentImageRelayError("deployment image reference name is invalid")
            if mime_type not in _ALLOWED_MIME_TYPES:
                raise DeploymentImageRelayError("deployment image reference type is invalid")
            try:
                data = base64.b64decode(item["data"], validate=True)
            except (TypeError, ValueError) as exc:
                raise DeploymentImageRelayError("deployment image reference data is invalid") from exc
            total += len(data)
            if not data or len(data) > descriptor.max_reference_bytes or total > descriptor.max_total_reference_bytes:
                raise DeploymentImageRelayError("deployment image reference is too large")
            references.append({"name": name, "mime_type": mime_type, "data": data})
        result = self._policy.generate(prompt=prompt.strip(), aspect_ratio=aspect_ratio, model=model, references=references)
        return {
            "ok": True,
            "image": base64.b64encode(result["image_bytes"]).decode("ascii"),
            "mime_type": result["mime_type"],
            "provider": result["provider"],
            "model": result["model"],
            "aspect_ratio": result["aspect_ratio"],
            "modality": result["modality"],
            "metadata": _safe_metadata(result.get("metadata")),
        }


class OwnerImageRelayClient:
    """Owner-worker client backed only by its inherited private descriptor."""

    def __init__(self, inherited_fd: int, descriptor: DeploymentImageDescriptor) -> None:
        if inherited_fd < 0 or not isinstance(descriptor, DeploymentImageDescriptor):
            raise DeploymentImageRelayError("deployment image relay is invalid")
        self.descriptor = descriptor
        self._connection = socket.socket(fileno=inherited_fd)
        self._connection.set_inheritable(False)
        self._lock = threading.Lock()

    def generate(self, *, prompt: str, aspect_ratio: str, model: str | None, references: list[dict[str, Any]]) -> dict[str, Any]:
        selected = str(model or self.descriptor.model).strip()
        encoded_references = [{
            "name": item["name"], "mime_type": item["mime_type"],
            "data": base64.b64encode(item["data"]).decode("ascii"),
        } for item in references]
        request = {
            "operation": "image_generate", "policy_id": self.descriptor.policy_id,
            "prompt": prompt, "aspect_ratio": aspect_ratio, "model": selected,
            "references": encoded_references,
        }
        try:
            with self._lock:
                _send_frame(self._connection, request)
                response = _recv_frame(self._connection)
        except OSError as exc:
            raise DeploymentImageRelayError("deployment image relay is unavailable") from exc
        if response.get("ok") is not True:
            raise DeploymentImageRelayError("deployment image request was rejected")
        try:
            image = base64.b64decode(response["image"], validate=True)
        except (KeyError, TypeError, ValueError) as exc:
            raise DeploymentImageRelayError("deployment image response is invalid") from exc
        if not image or len(image) > self.descriptor.max_output_bytes or response.get("mime_type") not in _ALLOWED_MIME_TYPES:
            raise DeploymentImageRelayError("deployment image response is invalid")
        return {**response, "image_bytes": image}

    def close(self) -> None:
        try:
            self._connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._connection.close()
