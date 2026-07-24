"""Tests for the exact-generation Owner Worker gateway client."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from hermes_cli.owner_worker.gateway_client import OwnerWorkerGatewayClient


class _Socket:
    def __init__(self, frames):
        self.frames = list(frames)
        self.sent = []
        self.close = AsyncMock()

    async def send(self, value):
        self.sent.append(value)

    async def recv(self):
        return self.frames.pop(0)


@pytest.mark.asyncio
async def test_client_uses_exact_socket_correlates_rpc_and_releases_once():
    owner = SimpleNamespace(owner_key="ok1_owner")
    handle = SimpleNamespace(
        owner_key="ok1_owner",
        worker_generation=7,
        worker_id="worker-7",
        lease_version=3,
        recovery_generation=1,
        socket_path="/tmp/exact-worker.sock",
    )
    use_lease = Mock()
    supervisor = SimpleNamespace(
        get_or_start=Mock(return_value=handle),
        acquire_use=Mock(return_value=use_lease),
        control_home="/tmp/control",
    )
    socket = _Socket(
        [
            "ack-frame",
            json.dumps({"method": "message.delta", "params": {"session_id": "s", "text": "x"}}),
            json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"session_id": "s"}}),
            json.dumps({
                "jsonrpc": "2.0",
                "method": "event",
                "params": {
                    "type": "message.complete",
                    "session_id": "other",
                    "payload": {"text": "wrong"},
                },
            }),
            json.dumps({
                "jsonrpc": "2.0",
                "method": "event",
                "params": {
                    "type": "message.complete",
                    "session_id": "s",
                    "payload": {"text": "done"},
                },
            }),
        ]
    )

    with patch(
        "hermes_cli.owner_worker.gateway_client.mint_owner_worker_bootstrap",
        return_value="bootstrap-claim",
    ), patch(
        "hermes_cli.owner_worker.gateway_client.owp1_data",
        side_effect=lambda _claims, **kwargs: kwargs["text"],
    ), patch(
        "hermes_cli.owner_worker.gateway_client.parse_owp1_data",
        side_effect=lambda raw, _claims, **_kwargs: ("text", raw),
    ), patch(
        "hermes_cli.owner_worker.gateway_client.owner_worker_capability_public_config",
        return_value={
            "HERMES_OWNER_WORKER_CAPABILITY_PUBLIC_KEY": "public",
            "HERMES_OWNER_WORKER_CAPABILITY_ISSUER": "issuer",
            "HERMES_OWNER_WORKER_CAPABILITY_RETAINED_PUBLIC_KEYS": "{}",
        },
    ), patch(
        "hermes_cli.owner_worker.gateway_client.parse_owner_worker_bootstrap",
        return_value="parsed-bootstrap",
    ), patch(
        "hermes_cli.owner_worker.gateway_client.owp1_hello",
        return_value="hello-frame",
    ), patch(
        "hermes_cli.owner_worker.gateway_client.validate_owp1_control",
    ), patch(
        "hermes_cli.owner_worker.gateway_client.connect_owner_worker_ws",
        new=AsyncMock(return_value=socket),
    ) as connect:
        client = OwnerWorkerGatewayClient(supervisor, owner)
        await client.connect()
        response = await client.call("session.create", {})
        event = await client.wait_for_event("message.complete", session_id="s")
        await client.close()
        await client.close()

    assert response == {"session_id": "s"}
    assert event["params"]["text"] == "done"
    assert connect.await_args.args[0] == "/tmp/exact-worker.sock"
    assert "internal_owner_bootstrap=bootstrap-claim" in connect.await_args.args[1]
    assert socket.sent[0] == "hello-frame"
    request = json.loads(socket.sent[1])
    assert request["method"] == "session.create"
    use_lease.release.assert_called_once()
    socket.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_handshake_releases_lease():
    owner = SimpleNamespace(owner_key="ok1_owner")
    handle = SimpleNamespace(
        owner_key="ok1_owner",
        worker_generation=1,
        worker_id="worker",
        lease_version=1,
        recovery_generation=0,
        socket_path="/tmp/worker.sock",
    )
    use_lease = Mock()
    supervisor = SimpleNamespace(
        get_or_start=Mock(return_value=handle),
        acquire_use=Mock(return_value=use_lease),
        control_home="/tmp/control",
    )
    socket = _Socket(["bad-ack"])
    with patch(
        "hermes_cli.owner_worker.gateway_client.mint_owner_worker_bootstrap",
        return_value="claim",
    ), patch(
        "hermes_cli.owner_worker.gateway_client.owner_worker_capability_public_config",
        return_value={
            "HERMES_OWNER_WORKER_CAPABILITY_PUBLIC_KEY": "public",
            "HERMES_OWNER_WORKER_CAPABILITY_ISSUER": "issuer",
            "HERMES_OWNER_WORKER_CAPABILITY_RETAINED_PUBLIC_KEYS": "{}",
        },
    ), patch(
        "hermes_cli.owner_worker.gateway_client.parse_owner_worker_bootstrap",
        return_value="bootstrap",
    ), patch(
        "hermes_cli.owner_worker.gateway_client.owp1_hello",
        return_value="hello",
    ), patch(
        "hermes_cli.owner_worker.gateway_client.validate_owp1_control",
        side_effect=RuntimeError("stale generation"),
    ), patch(
        "hermes_cli.owner_worker.gateway_client.connect_owner_worker_ws",
        new=AsyncMock(return_value=socket),
    ):
        with pytest.raises(RuntimeError, match="stale"):
            await OwnerWorkerGatewayClient(supervisor, owner).connect()

    use_lease.release.assert_called_once()
    socket.close.assert_awaited_once()
