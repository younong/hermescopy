"""Lease-bound deployment media relay with no worker-visible credentials.

Generalizes the retired image-only relay: one socketpair per worker lease,
one framed JSON protocol, and operations (``image_generate``,
``video_generate``, ``tts_synthesize``, ``transcribe``, ``embed``) routed by
``(kind, provider, model)`` against the deployment media policy. Audio bytes
and embedding vectors travel inside the same 96MB-fenced frames. See
``docs/model-plane.md``.
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

from agent.image_gen_provider import canonical_aspect_ratio
from hermes_cli.dashboard_auth.authority import (
    AuthorityStore, AuthorizationRejected, OwnerWorkerAuthorityLease, WorkerLeaseState,
)
from hermes_cli.deployment_media import (
    AUDIO_MIME_TYPES,
    IMAGE_MIME_TYPES,
    MAX_EMBEDDING_DIMENSIONS,
    MAX_TRANSCRIPT_CHARS,
    TTS_OUTPUT_MIME_TYPES,
    VIDEO_MIME_TYPES,
    DeploymentMediaDescriptor,
    DeploymentMediaPolicy,
    DeploymentMediaPolicyInvalid,
    DeploymentMediaSelectionRejected,
    OPERATION_KINDS,
)

_MAX_FRAME_BYTES = 96 * 1024 * 1024
_ALLOWED_MIME_TYPES = IMAGE_MIME_TYPES | VIDEO_MIME_TYPES
_SAFE_METADATA_KEYS = frozenset({
    "aspect_ratio_native", "effective_aspect_ratio", "effective_resolution",
    "output_format", "quality", "requested_aspect_ratio", "requested_resolution",
    "resolution_mode", "revised_prompt", "size", "upstream_model",
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
        kind = OPERATION_KINDS.get(operation if isinstance(operation, str) else "", "")
        if not isinstance(prompt, str) or len(prompt) > 32_768 or "\x00" in prompt:
            raise DeploymentMediaRelayError("deployment media prompt is invalid")
        if operation != "transcribe" and not prompt.strip():
            raise DeploymentMediaRelayError("deployment media prompt is invalid")
        if not isinstance(provider, str) or not isinstance(model, str):
            raise DeploymentMediaRelayError("deployment media selection is invalid")
        route = self._policy.route_for(kind, provider, model)
        if route is None:
            raise DeploymentMediaRelayError("deployment media selection is invalid")
        descriptor = route.descriptor
        if not isinstance(aspect_ratio, str) or len(aspect_ratio) > 64:
            raise DeploymentMediaRelayError("deployment media selection is invalid")
        if descriptor.kind == "image":
            aspect_ratio = canonical_aspect_ratio(aspect_ratio)
            if aspect_ratio is None:
                raise DeploymentMediaRelayError("deployment media selection is invalid")
        if kind == "voice":
            # tts_synthesize carries no input; transcribe carries exactly one
            # audio sample as its single reference.
            expected = 0 if operation == "tts_synthesize" else 1
            allowed_mime_types = AUDIO_MIME_TYPES
            reference_cap = 1
        elif kind == "vector":
            expected = 0
            allowed_mime_types = frozenset()
            reference_cap = 0
        else:
            expected = None
            allowed_mime_types = _ALLOWED_MIME_TYPES
            reference_cap = descriptor.max_reference_images
        if not isinstance(raw_references, list) or len(raw_references) > reference_cap:
            raise DeploymentMediaRelayError("deployment media references are invalid")
        if expected is not None and len(raw_references) != expected:
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
            if mime_type not in allowed_mime_types:
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
            prompt=prompt.strip() if operation != "transcribe" else prompt,
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
        elif "audio_bytes" in result:
            response["audio"] = base64.b64encode(result["audio_bytes"]).decode("ascii")
            response["mime_type"] = result["mime_type"]
        elif "text" in result:
            response["text"] = result["text"]
        elif "embedding" in result:
            response["embedding"] = result["embedding"]
            response["dimensions"] = result["dimensions"]
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
        kind = OPERATION_KINDS.get(operation, "")
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
        if kind == "vector":
            embedding = response.get("embedding")
            if (
                not isinstance(embedding, list)
                or not embedding
                or len(embedding) > MAX_EMBEDDING_DIMENSIONS
                or any(
                    isinstance(value, bool) or not isinstance(value, (int, float))
                    for value in embedding
                )
            ):
                raise DeploymentMediaRelayError("deployment media response is invalid")
            dimensions = response.get("dimensions")
            if dimensions != len(embedding):
                raise DeploymentMediaRelayError("deployment media response is invalid")
            return {**response, "embedding": [float(value) for value in embedding]}
        if "text" in response:
            text = response["text"]
            if not isinstance(text, str) or len(text) > MAX_TRANSCRIPT_CHARS:
                raise DeploymentMediaRelayError("deployment media response is invalid")
            return {**response, "text": text}
        field = {"image": "image", "voice": "audio"}.get(kind, "video")
        try:
            data = base64.b64decode(response[field], validate=True)
        except (KeyError, TypeError, ValueError) as exc:
            raise DeploymentMediaRelayError("deployment media response is invalid") from exc
        allowed = (
            IMAGE_MIME_TYPES if kind == "image"
            else TTS_OUTPUT_MIME_TYPES if kind == "voice"
            else VIDEO_MIME_TYPES
        )
        if not data or len(data) > route.max_output_bytes or response.get("mime_type") not in allowed:
            raise DeploymentMediaRelayError("deployment media response is invalid")
        key = {"image": "image_bytes", "voice": "audio_bytes"}.get(kind, "video_bytes")
        return {**response, key: data}

    def close(self) -> None:
        try:
            self._connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._connection.close()
