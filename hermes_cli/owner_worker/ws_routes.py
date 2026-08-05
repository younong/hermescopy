"""Owner-worker-local WebSocket routes.

These handlers intentionally avoid importing ``hermes_cli.web_server`` so an
Owner Worker does not construct or accidentally depend on the Control Plane's
module-global FastAPI app/state.  The Control Plane authenticates external
browser WebSockets, then bridges them to these worker-local UDS routes with an
owner-bound ``internal_owner_token``.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path
from typing import Any
from urllib import parse as urllib_parse

from fastapi import WebSocket, WebSocketDisconnect
from hermes_cli.dashboard_auth.audit import (
    AuthorityAuditEvent,
    AuthorityAuditReason,
    audit_authority,
    new_authority_correlation_id,
)
from hermes_cli.latency_trace import clean_latency_trace_id, log_latency_stage
from hermes_cli.dashboard_auth.authority import (
    AuthorityStore,
    AuthorityUnavailable,
    AuthorizationRejected,
    WorkerLeaseState,
)
from hermes_cli.owner_worker.tokens import (
    OwnerWorkerCapabilityInvalid,
    admit_owner_worker_bootstrap,
    owp1_ack,
    owp1_data,
    parse_owp1_data,
    validate_owp1_control,
)

_log = logging.getLogger(__name__)

_VALID_CHANNEL_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


class OwnerWorkerLiveState:
    """App-local authenticated event and Gateway state."""

    def __init__(self) -> None:
        self.event_channels: dict[str, set[WebSocket]] = {}
        self.event_lock = asyncio.Lock()
        # Bound by create_app after the exact worker lease is available. The
        # Gateway route rejects rather than falling back to standalone globals
        # when this worker-local binding is absent.
        self.gateway_runtime: Any | None = None


def _live_state(app: Any) -> OwnerWorkerLiveState:
    try:
        state = app.state.owner_worker_live_state
    except AttributeError:
        state = OwnerWorkerLiveState()
        app.state.owner_worker_live_state = state
    return state


def _owner_key(app: Any) -> str:
    return str(getattr(app.state, "owner_worker_owner_key", "") or "").strip()


def _control_home(app: Any) -> str | Path | None:
    return getattr(app.state, "owner_worker_control_home", None)


def _ws_close_reason(text: str) -> str:
    encoded = text.encode("utf-8", "replace")
    if len(encoded) <= 123:
        return text
    return encoded[:120].decode("utf-8", "ignore") + "..."


class _Owp1Peer:
    """Expose framed UDS peer data as existing FastAPI WebSocket operations."""

    def __init__(self, ws: WebSocket, claims: Any) -> None:
        self._ws = ws
        self._claims = claims
        self._in_sequence = 1
        self._out_sequence = 1

    @property
    def claims(self) -> Any:
        """Immutable bootstrap claims trusted for this exact UDS connection."""
        return self._claims

    def __getattr__(self, name: str) -> Any:
        return getattr(self._ws, name)

    async def accept(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs

    async def receive(self) -> dict[str, Any]:
        message = await self._ws.receive()
        if message.get("type") == "websocket.disconnect":
            return message
        framed = message.get("text")
        if framed is None:
            raise WebSocketDisconnect(code=4401)
        kind, payload = parse_owp1_data(
            framed,
            self._claims,
            direction="control-to-worker",
            expected_sequence=self._in_sequence,
        )
        self._in_sequence += 1
        return {"type": "websocket.receive", kind: payload}

    async def receive_text(self) -> str:
        message = await self.receive()
        if message.get("type") == "websocket.disconnect":
            raise WebSocketDisconnect(code=1000)
        if not isinstance(message.get("text"), str):
            raise WebSocketDisconnect(code=4401)
        return message["text"]

    async def send_text(self, data: str) -> None:
        await self._ws.send_text(owp1_data(
            self._claims,
            direction="worker-to-control",
            sequence=self._out_sequence,
            text=str(data),
        ))
        self._out_sequence += 1

    async def send_bytes(self, data: bytes) -> None:
        await self._ws.send_text(owp1_data(
            self._claims,
            direction="worker-to-control",
            sequence=self._out_sequence,
            data=bytes(data),
        ))
        self._out_sequence += 1


def _audit_bootstrap(reason: AuthorityAuditReason, lease: Any | None) -> None:
    if lease is None:
        return
    try:
        audit_authority(
            (
                AuthorityAuditEvent.CAPABILITY_ADMITTED
                if reason is AuthorityAuditReason.ADMITTED
                else AuthorityAuditEvent.CAPABILITY_REJECTED
            ),
            correlation_id=new_authority_correlation_id(),
            reason=reason,
            audience_class="none",
            worker_generation=int(lease.worker_generation),
            recovery_generation=int(lease.recovery_generation),
        )
    except Exception:
        return


def _active_bootstrap_lease(app: Any, configured_lease: Any) -> Any:
    """Resolve the exact active durable fence for this Worker process.

    A Worker is spawned with a ``STARTING`` lease in its environment, then the
    Control Plane promotes that same fence after its health probe.  Lifecycle
    state is therefore deliberately re-read here, while every identity field
    remains anchored to the process-start configuration.
    """
    store = AuthorityStore(_control_home(app))
    current = store.read_owner_worker_lease(configured_lease.owner_key)
    if current is None or (
        current.owner_key != configured_lease.owner_key
        or current.worker_generation != configured_lease.worker_generation
        or current.worker_id != configured_lease.worker_id
        or current.lease_version != configured_lease.lease_version
        or current.recovery_generation != configured_lease.recovery_generation
    ):
        raise OwnerWorkerCapabilityInvalid("bootstrap_worker_identity_mismatch")
    # This checks both the exact durable record and the current recovery
    # generation.  The consume transaction in admit_owner_worker_bootstrap()
    # remains the final race-closing check.
    return store.assert_worker_lease(
        current, states=frozenset({WorkerLeaseState.ACTIVE})
    )


async def _admit_bootstrap_or_close(ws: WebSocket) -> _Owp1Peer | None:
    """Consume one bootstrap and complete `owp1` hello/ack before route work."""
    latency_started_at = time.monotonic()
    latency_trace_id = clean_latency_trace_id(ws.query_params.get("ws_trace", ""))
    log_latency_stage(
        _log,
        trace_id=latency_trace_id,
        surface="owner-worker-ws",
        stage="bootstrap.received",
    )
    token = ws.query_params.get("internal_owner_bootstrap", "")
    configured_lease = getattr(ws.app.state, "owner_worker_lease", None)
    verifier = getattr(ws.app.state, "owner_worker_capability_verifier", {})
    if not token or configured_lease is None:
        _audit_bootstrap(AuthorityAuditReason.BOOTSTRAP_REJECTED, configured_lease)
        await ws.close(code=4401, reason=_ws_close_reason("auth: internal_owner_invalid"))
        return None
    lease = configured_lease
    try:
        stage_started_at = time.monotonic()
        lease = _active_bootstrap_lease(ws.app, configured_lease)
        claims = admit_owner_worker_bootstrap(
            token,
            expected_lease=lease,
            path=ws.url.path,
            authority_store=AuthorityStore(_control_home(ws.app)),
            public_key=verifier.get("HERMES_OWNER_WORKER_CAPABILITY_PUBLIC_KEY"),
            issuer_key_version=verifier.get("HERMES_OWNER_WORKER_CAPABILITY_ISSUER"),
            retained_public_keys=verifier.get("HERMES_OWNER_WORKER_CAPABILITY_RETAINED_PUBLIC_KEYS"),
        )
        log_latency_stage(
            _log,
            trace_id=latency_trace_id,
            surface="owner-worker-ws",
            stage="bootstrap.validated",
            started_at=stage_started_at,
        )
        await ws.accept()
        hello = await asyncio.wait_for(ws.receive_text(), timeout=5)
        validate_owp1_control(hello, claims, message_type="hello")
        await ws.send_text(owp1_ack(claims))
        log_latency_stage(
            _log,
            trace_id=latency_trace_id,
            surface="owner-worker-ws",
            stage="owp1.ack_sent",
            started_at=latency_started_at,
        )
        _audit_bootstrap(AuthorityAuditReason.ADMITTED, lease)
        return _Owp1Peer(ws, claims)
    except (
        AuthorityUnavailable,
        AuthorizationRejected,
        OwnerWorkerCapabilityInvalid,
        RuntimeError,
        TimeoutError,
    ):
        _audit_bootstrap(AuthorityAuditReason.BOOTSTRAP_REJECTED, lease)
        await ws.close(code=4401, reason=_ws_close_reason("auth: internal_owner_invalid"))
        return None


def _get_event_state(app: Any) -> tuple[dict[str, set[WebSocket]], asyncio.Lock]:
    state = _live_state(app)
    return state.event_channels, state.event_lock


def _channel_or_none(ws: WebSocket) -> str | None:
    channel = ws.query_params.get("channel", "")
    return channel if _VALID_CHANNEL_RE.match(channel) else None


async def _broadcast_event(app: Any, channel: str, payload: str) -> None:
    event_channels, event_lock = _get_event_state(app)
    async with event_lock:
        subs = list(event_channels.get(channel, ()))
    for sub in subs:
        try:
            await sub.send_text(payload)
        except Exception:
            pass


async def gateway_ws(ws: WebSocket) -> None:
    """Attach one exact authenticated Control Plane bridge."""
    peer = await _admit_bootstrap_or_close(ws)
    if peer is None:
        return
    runtime = _live_state(ws.app).gateway_runtime
    if runtime is None:
        await peer.close(code=1011, reason=_ws_close_reason("owner gateway runtime unavailable"))
        return
    from tui_gateway.ws import handle_ws

    await handle_ws(peer, runtime=runtime)


async def pub_ws(ws: WebSocket) -> None:
    peer = await _admit_bootstrap_or_close(ws)
    if peer is None:
        return
    channel = _channel_or_none(ws)
    if not channel:
        await peer.close(code=4400)
        return
    try:
        while True:
            await _broadcast_event(ws.app, channel, await peer.receive_text())
    except WebSocketDisconnect:
        pass


async def events_ws(ws: WebSocket) -> None:
    peer = await _admit_bootstrap_or_close(ws)
    if peer is None:
        return
    channel = _channel_or_none(ws)
    if not channel:
        await peer.close(code=4400)
        return
    event_channels, event_lock = _get_event_state(ws.app)
    async with event_lock:
        event_channels.setdefault(channel, set()).add(peer)
    try:
        while True:
            await peer.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        async with event_lock:
            subs = event_channels.get(channel)
            if subs is not None:
                subs.discard(peer)
                if not subs:
                    event_channels.pop(channel, None)


def register_owner_worker_ws_routes(app: Any) -> None:
    """Register owner-worker-local WebSocket routes on *app*."""
    app.add_api_websocket_route("/api/ws", gateway_ws)
    app.add_api_websocket_route("/api/pub", pub_ws)
    app.add_api_websocket_route("/api/events", events_ws)
