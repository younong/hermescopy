"""Exact-generation JSON-RPC client for an Owner Worker's gateway."""

from __future__ import annotations

import asyncio
import json
import secrets
from pathlib import Path
from typing import Any

from hermes_cli.dashboard_auth.authority import OwnerWorkerAuthorityLease, WorkerLeaseState
from hermes_cli.owner_worker.tokens import (
    mint_owner_worker_bootstrap,
    owner_worker_capability_public_config,
    owp1_data,
    owp1_hello,
    parse_owp1_data,
    parse_owner_worker_bootstrap,
    validate_owp1_control,
)


async def connect_owner_worker_ws(
    socket_path: Path,
    uri: str,
    *,
    open_timeout: float = 10.0,
    max_queue: int = 64,
) -> Any:
    try:
        import websockets
    except Exception as exc:  # pragma: no cover - runtime dependency gate
        raise RuntimeError("websockets package is required for owner worker gateway") from exc
    unix_connect = getattr(websockets, "unix_connect", None)
    if unix_connect is None:
        raise RuntimeError("websockets.unix_connect is unavailable")
    kwargs = {"open_timeout": open_timeout, "max_queue": max_queue}
    try:
        return await unix_connect(uri=uri, path=str(socket_path), **kwargs)
    except TypeError:
        return await unix_connect(str(socket_path), uri=uri, **kwargs)


def authority_lease_for_handle(handle: Any) -> OwnerWorkerAuthorityLease:
    return OwnerWorkerAuthorityLease(
        owner_key=str(handle.owner_key),
        worker_generation=int(handle.worker_generation),
        worker_id=str(handle.worker_id),
        state=WorkerLeaseState.ACTIVE,
        lease_version=int(handle.lease_version),
        recovery_generation=int(handle.recovery_generation),
    )


class OwnerWorkerGatewayClient:
    """Owns one exact Worker use lease and OWP1-authenticated `/api/ws`."""

    def __init__(self, supervisor: Any, owner: Any) -> None:
        self.supervisor = supervisor
        self.owner = owner
        self.handle: Any | None = None
        self.lease: Any | None = None
        self.websocket: Any | None = None
        self._request_id = 0
        self._send_sequence = 1
        self._receive_sequence = 1
        self._bootstrap: Any | None = None
        self._closed = False
        self._pending_events: list[dict[str, Any]] = []

    async def __aenter__(self) -> "OwnerWorkerGatewayClient":
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def connect(self) -> None:
        if self.websocket is not None:
            return
        handle = await asyncio.to_thread(self.supervisor.get_or_start, self.owner)
        lease = self.supervisor.acquire_use(handle)
        authority_lease = authority_lease_for_handle(handle)
        connection_id = secrets.token_urlsafe(18)
        nonce = secrets.token_urlsafe(18)
        claims = mint_owner_worker_bootstrap(
            authority_lease,
            path="/api/ws",
            connection_id=connection_id,
            nonce=nonce,
            control_home=getattr(self.supervisor, "control_home", None),
        )
        verifier = owner_worker_capability_public_config(
            getattr(self.supervisor, "control_home", None)
        )
        bootstrap = parse_owner_worker_bootstrap(
            claims,
            expected_lease=authority_lease,
            path="/api/ws",
            public_key=verifier["HERMES_OWNER_WORKER_CAPABILITY_PUBLIC_KEY"],
            issuer_key_version=verifier["HERMES_OWNER_WORKER_CAPABILITY_ISSUER"],
            retained_public_keys=verifier["HERMES_OWNER_WORKER_CAPABILITY_RETAINED_PUBLIC_KEYS"],
        )
        uri = f"ws://owner-worker/api/ws?internal_owner_bootstrap={claims}"
        websocket = None
        try:
            websocket = await connect_owner_worker_ws(handle.socket_path, uri)
            await websocket.send(owp1_hello(bootstrap))
            ack = await asyncio.wait_for(websocket.recv(), timeout=10.0)
            validate_owp1_control(ack, bootstrap, message_type="ack")
        except BaseException:
            if websocket is not None:
                try:
                    await websocket.close(code=1011)
                except Exception:
                    pass
            lease.release()
            raise
        self.handle = handle
        self.lease = lease
        self.websocket = websocket
        self._bootstrap = bootstrap

    async def _send_text(self, value: str) -> None:
        if self.websocket is None or self._bootstrap is None:
            raise RuntimeError("owner worker gateway is not connected")
        await self.websocket.send(
            owp1_data(
                self._bootstrap,
                direction="control-to-worker",
                sequence=self._send_sequence,
                text=value,
            )
        )
        self._send_sequence += 1

    async def _recv_data(self) -> str:
        if self.websocket is None or self._bootstrap is None:
            raise RuntimeError("owner worker gateway is not connected")
        raw = await self.websocket.recv()
        kind, payload = parse_owp1_data(
            raw,
            self._bootstrap,
            direction="worker-to-control",
            expected_sequence=self._receive_sequence,
        )
        self._receive_sequence += 1
        if kind != "text" or not isinstance(payload, str):
            raise RuntimeError("owner worker gateway returned a non-text frame")
        return payload

    async def call(self, method: str, params: dict[str, Any]) -> Any:
        if self.websocket is None:
            raise RuntimeError("owner worker gateway is not connected")
        self._request_id += 1
        request_id = self._request_id
        await self._send_text(
            json.dumps(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
                separators=(",", ":"),
            )
        )
        while True:
            message = self._decode(await self._recv_data())
            if message.get("id") == request_id:
                if "error" in message:
                    error = message["error"]
                    raise RuntimeError(str(error.get("message") if isinstance(error, dict) else error))
                return message.get("result")
            if "method" in message:
                self._pending_events.append(message)

    async def wait_for_event(
        self,
        method: str,
        *,
        session_id: str,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        async def _wait() -> dict[str, Any]:
            while True:
                for index, event in enumerate(self._pending_events):
                    if self._matches_event(event, method, session_id):
                        return self._pending_events.pop(index)
                if self.websocket is None:
                    raise RuntimeError("owner worker gateway is not connected")
                message = self._decode(await self._recv_data())
                if self._matches_event(message, method, session_id):
                    return message
                if "method" in message:
                    self._pending_events.append(message)

        return await asyncio.wait_for(_wait(), timeout=timeout) if timeout else await _wait()

    @staticmethod
    def _matches_event(message: dict[str, Any], method: str, session_id: str) -> bool:
        if message.get("method") != method:
            return False
        params = message.get("params") or {}
        observed = str(params.get("session_id") or message.get("session_id") or "")
        return observed == session_id

    @staticmethod
    def _decode(raw: Any) -> dict[str, Any]:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        value = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(value, dict):
            raise RuntimeError("owner worker gateway returned a malformed frame")
        if value.get("method") == "event":
            envelope = value.get("params")
            if isinstance(envelope, dict) and isinstance(envelope.get("type"), str):
                payload = envelope.get("payload")
                params = dict(payload) if isinstance(payload, dict) else {}
                params["session_id"] = str(envelope.get("session_id") or "")
                return {"method": envelope["type"], "params": params}
        return value

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        websocket, lease = self.websocket, self.lease
        self.websocket = None
        self.lease = None
        self._bootstrap = None
        if websocket is not None:
            try:
                await websocket.close(code=1000)
            except Exception:
                pass
        if lease is not None:
            lease.release()
