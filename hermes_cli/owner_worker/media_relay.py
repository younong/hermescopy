"""Lease-bound deployment media relay with no worker-visible credentials.

Generalizes the retired image-only relay: one socketpair per worker lease,
one framed JSON protocol, and operations (``image_generate``,
``video_generate``) routed by ``(kind, provider, model)`` against the
deployment media policy. See ``docs/model-plane.md``.
"""
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
from hermes_cli.deployment_media import (
    IMAGE_MIME_TYPES,
    VIDEO_MIME_TYPES,
    DeploymentMediaDescriptor,
    DeploymentMediaPolicy,
    DeploymentMediaPolicyInvalid,
    DeploymentMediaSelectionRejected,
)

_MAX_FRAME_BYTES = 96 * 1024 * 1024
_ALLOWED_MIME_TYPES = IMAGE_MIME_TYPES | VIDEO_MIME_TYPES
_ALLOWED_ASPECT_RATIOS = frozenset({"landscape", "square", "portrait"})
_SAFE_METADATA_KEYS = frozenset({
    "aspect_ratio_native", "output_format", "quality", "revised_prompt", "size",
    "upstream_model",
})
_MAX_PARAM_KEY_LENGTH = 64
_MAX_PARAM_VALUE_LENGTH = 4096


class DeploymentMediaRelayError(RuntimeError):
    """The worker-to-control-plane media relay rejected a request."""


def _send_frame(connection: socket.socket, value: dict[str, Any]) -> None:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if not encoded or len(encoded) > _MAX_FRAME_BYTES:
        raise DeploymentMediaRelayError("deployment media relay frame is invalid")
    connection.sendall(struct.pack("!I", len(encoded)) + encoded)


def _recv_exact(connection: socket.socket, count: int) -> bytes:
    chunks: list[bytes] = []
    remaining = count
    while remaining:
        chunk = connection.recv(remaining)
        if not chunk:
            raise DeploymentMediaRelayError("deployment media relay peer closed")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _recv_frame(connection: socket.socket) -> dict[str, Any]:
    size = struct.unpack("!I", _recv_exact(connection, 4))[0]
    if not size or size > _MAX_FRAME_BYTES:
        raise DeploymentMediaRelayError("deployment media relay frame is invalid")
    try:
        value = json.loads(_recv_exact(connection, size))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DeploymentMediaRelayError("deployment media relay frame is malformed") from exc
    if not isinstance(value, dict):
        raise DeploymentMediaRelayError("deployment media relay frame is malformed")
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


def _safe_params(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or len(value) > 16:
        raise DeploymentMediaRelayError("deployment media params are invalid")
    safe: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key or len(key) > _MAX_PARAM_KEY_LENGTH:
            raise DeploymentMediaRelayError("deployment media params are invalid")
        if item is None or isinstance(item, bool):
            safe[key] = item
        elif isinstance(item, int):
            safe[key] = item
        elif isinstance(item, float):
            safe[key] = item
        elif isinstance(item, str) and len(item) <= _MAX_PARAM_VALUE_LENGTH and "\x00" not in item:
            safe[key] = item
        else:
            raise DeploymentMediaRelayError("deployment media params are invalid")
    return safe


@dataclass
class _RelayPeer:
    lease: OwnerWorkerAuthorityLease
    connection: socket.socket
    lock: threading.Lock
    thread: threading.Thread


class DeploymentMediaBroker:
    """Control-plane-only media broker fenced to exact durable worker leases."""

    def __init__(self, *, policy: DeploymentMediaPolicy, authority_store: AuthorityStore) -> None:
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
        thread = threading.Thread(target=self._serve_peer, args=(key,), daemon=True, name=f"media-relay-{lease.worker_generation}")
        peer = _RelayPeer(lease, parent, threading.Lock(), thread)
        with self._lock:
            if self._closed or key in self._peers:
                parent.close()
                child.close()
                raise DeploymentMediaRelayError("deployment media relay registration is unavailable")
            self._peers[key] = peer
        thread.start()
        return child.detach()

    def activate(self, lease: OwnerWorkerAuthorityLease) -> None:
        identity = (lease.owner_key, lease.worker_generation, lease.worker_id, lease.recovery_generation)
        with self._lock:
            current_key = next((key for key, peer in self._peers.items()
                if (peer.lease.owner_key, peer.lease.worker_generation, peer.lease.worker_id, peer.lease.recovery_generation) == identity), None)
            if current_key is None:
                raise DeploymentMediaRelayError("deployment media relay peer is unavailable")
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
                    except (DeploymentMediaRelayError, DeploymentMediaPolicyInvalid, DeploymentMediaSelectionRejected):
                        response = {"ok": False, "error": "deployment media request rejected"}
                    _send_frame(peer.connection, response)
        except (DeploymentMediaRelayError, OSError):
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
            raise DeploymentMediaRelayError("deployment media worker lease is not active") from exc
        if set(request) != {"operation", "policy_id", "provider", "model", "prompt", "aspect_ratio", "references", "params"}:
            raise DeploymentMediaRelayError("deployment media request is invalid")
        if request["policy_id"] != self._policy.descriptor().policy_id:
            raise DeploymentMediaRelayError("deployment media request policy is invalid")
        operation = request["operation"]
        provider = request["provider"]
        model = request["model"]
        prompt = request["prompt"]
        aspect_ratio = request["aspect_ratio"]
        raw_references = request["references"]
        params = _safe_params(request["params"])
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 32_768 or "\x00" in prompt:
            raise DeploymentMediaRelayError("deployment media prompt is invalid")
        if not isinstance(provider, str) or not isinstance(model, str):
            raise DeploymentMediaRelayError("deployment media selection is invalid")
        route = self._policy.route_for(
            "video" if operation == "video_generate" else "image" if operation == "image_generate" else "",
            provider,
            model,
        )
        if route is None:
            raise DeploymentMediaRelayError("deployment media selection is invalid")
        descriptor = route.descriptor
        if descriptor.kind == "image" and aspect_ratio not in _ALLOWED_ASPECT_RATIOS:
            raise DeploymentMediaRelayError("deployment media selection is invalid")
        if not isinstance(aspect_ratio, str) or len(aspect_ratio) > 64:
            raise DeploymentMediaRelayError("deployment media selection is invalid")
        if not isinstance(raw_references, list) or len(raw_references) > descriptor.max_reference_images:
            raise DeploymentMediaRelayError("deployment media references are invalid")
        references: list[dict[str, Any]] = []
        total = 0
        for item in raw_references:
            if not isinstance(item, dict) or set(item) != {"name", "mime_type", "data"}:
                raise DeploymentMediaRelayError("deployment media reference is invalid")
            name = item["name"]
            mime_type = item["mime_type"]
            if not isinstance(name, str) or not name or len(name) > 255 or any(ch in name for ch in "/\\\x00"):
                raise DeploymentMediaRelayError("deployment media reference name is invalid")
            if mime_type not in _ALLOWED_MIME_TYPES:
                raise DeploymentMediaRelayError("deployment media reference type is invalid")
            try:
                data = base64.b64decode(item["data"], validate=True)
            except (TypeError, ValueError) as exc:
                raise DeploymentMediaRelayError("deployment media reference data is invalid") from exc
            total += len(data)
            if not data or len(data) > descriptor.max_reference_bytes or total > descriptor.max_total_reference_bytes:
                raise DeploymentMediaRelayError("deployment media reference is too large")
            references.append({"name": name, "mime_type": mime_type, "data": data})
        result = self._policy.execute(
            operation,
            provider=descriptor.provider,
            model=model,
            prompt=prompt.strip(),
            aspect_ratio=aspect_ratio,
            references=tuple(references),
            params=params,
        )
        response: dict[str, Any] = {
            "ok": True,
            "provider": result["provider"],
            "model": result["model"],
            "aspect_ratio": result["aspect_ratio"],
            "modality": result["modality"],
            "metadata": _safe_metadata(result.get("metadata")),
        }
        if "image_bytes" in result:
            response["image"] = base64.b64encode(result["image_bytes"]).decode("ascii")
            response["mime_type"] = result["mime_type"]
        elif "video_bytes" in result:
            response["video"] = base64.b64encode(result["video_bytes"]).decode("ascii")
            response["mime_type"] = result["mime_type"]
        else:
            response["video_url"] = result["video_url"]
        return response


class OwnerMediaRelayClient:
    """Owner-worker client backed only by its inherited private descriptor."""

    def __init__(self, inherited_fd: int, descriptor: DeploymentMediaDescriptor) -> None:
        if inherited_fd < 0 or not isinstance(descriptor, DeploymentMediaDescriptor):
            raise DeploymentMediaRelayError("deployment media relay is invalid")
        self.descriptor = descriptor
        self._connection = socket.socket(fileno=inherited_fd)
        self._connection.set_inheritable(False)
        self._lock = threading.Lock()

    def execute(
        self,
        operation: str,
        *,
        provider: str,
        model: str,
        prompt: str,
        aspect_ratio: str = "",
        references: list[dict[str, Any]] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        kind = "video" if operation == "video_generate" else "image" if operation == "image_generate" else ""
        route = self.descriptor.route_for(kind, provider, model)
        if route is None:
            raise DeploymentMediaRelayError("deployment media selection is not allowed")
        encoded_references = [{
            "name": item["name"], "mime_type": item["mime_type"],
            "data": base64.b64encode(item["data"]).decode("ascii"),
        } for item in references or []]
        request = {
            "operation": operation, "policy_id": self.descriptor.policy_id,
            "provider": route.provider, "model": model,
            "prompt": prompt, "aspect_ratio": aspect_ratio,
            "references": encoded_references, "params": dict(params or {}),
        }
        try:
            with self._lock:
                _send_frame(self._connection, request)
                response = _recv_frame(self._connection)
        except OSError as exc:
            raise DeploymentMediaRelayError("deployment media relay is unavailable") from exc
        if response.get("ok") is not True:
            raise DeploymentMediaRelayError("deployment media request was rejected")
        return self._decode_response(route, response)

    def _decode_response(self, route: Any, response: dict[str, Any]) -> dict[str, Any]:
        kind = route.kind
        if "video_url" in response:
            url = response["video_url"]
            if not isinstance(url, str) or not url.startswith("https://"):
                raise DeploymentMediaRelayError("deployment media response is invalid")
            return {**response, "video_url": url}
        field = "image" if kind == "image" else "video"
        try:
            data = base64.b64decode(response[field], validate=True)
        except (KeyError, TypeError, ValueError) as exc:
            raise DeploymentMediaRelayError("deployment media response is invalid") from exc
        allowed = IMAGE_MIME_TYPES if kind == "image" else VIDEO_MIME_TYPES
        if not data or len(data) > route.max_output_bytes or response.get("mime_type") not in allowed:
            raise DeploymentMediaRelayError("deployment media response is invalid")
        key = "image_bytes" if kind == "image" else "video_bytes"
        return {**response, key: data}

    def close(self) -> None:
        try:
            self._connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._connection.close()
