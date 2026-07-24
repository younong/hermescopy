"""Tests for trusted channel dispatch and transactional outbox."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from hermes_cli.channel_connectors.weixin_ilink.poller import acquire_poll_lease, commit_update_batch
from hermes_cli.channel_dispatch.dispatcher import ChannelDispatcher
from hermes_cli.channel_identity import (
    ChannelCrypto,
    ChannelIdentityStore,
    Keyring,
    register_weixin_identity,
)


@pytest.fixture
def queued(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_OWNER_SECRET", "owner-secret")
    store = ChannelIdentityStore(
        ChannelCrypto(
            lookup=Keyring(keys={1: b"l" * 32}, active_version=1),
            encryption=Keyring(keys={1: b"e" * 32}, active_version=1),
        )
    )
    registered = register_weixin_identity(
        store,
        subject="peer-a",
        bot_id="bot-a",
        bot_token="token-a",
        base_url="https://ilink.example",
        peer_id="peer-a",
    )
    lease = acquire_poll_lease(store, account_id=registered.account_id, holder="poller")
    commit_update_batch(
        store,
        lease,
        messages=(
            {
                "message_id": "msg-1",
                "from_user_id": "peer-a",
                "context_token": "context-a",
                "item_list": [{"type": 1, "text_item": {"text": "hello"}}],
            },
        ),
        cursor="cursor",
    )
    return store, registered


@pytest.mark.asyncio
async def test_dispatch_creates_session_submits_idempotent_turn_and_writes_outbox(queued):
    store, registered = queued
    dispatcher = ChannelDispatcher(store, object())
    claim = dispatcher.claim_next(holder="dispatcher")
    assert claim is not None

    client = AsyncMock()
    client.owner = None
    client.handle = type("Handle", (), {"worker_generation": 1})()
    client.call.side_effect = [
        {"session_id": "live-1", "stored_session_id": "stored-1"},
        {"status": "streaming"},
    ]
    client.wait_for_event.return_value = {
        "method": "message.complete",
        "params": {"session_id": "live-1", "status": "complete", "text": "answer"},
    }

    class _Context:
        async def __aenter__(self):
            client.owner = owner
            return client

        async def __aexit__(self, *args):
            return None

    from hermes_cli.channel_identity.owner_resolution import resolve_binding
    owner, _ = resolve_binding(store, binding_id=registered.binding_id)
    with patch(
        "hermes_cli.channel_dispatch.dispatcher.OwnerWorkerGatewayClient",
        return_value=_Context(),
    ):
        outbound_id = await dispatcher.dispatch_claim(claim, holder="dispatcher")

    prompt = client.call.await_args_list[1]
    assert prompt.args[0] == "prompt.submit"
    assert prompt.args[1]["text"] == "hello"
    assert prompt.args[1]["idempotency_key"].startswith("weixin-ilink:im_")
    with store.read() as conn:
        inbound = conn.execute("SELECT status, payload_ciphertext FROM inbound_messages").fetchone()
        outbound = conn.execute(
            "SELECT outbound_id, status, client_message_id FROM outbound_messages"
        ).fetchone()
    assert inbound["status"] == "outbound_pending"
    assert inbound["payload_ciphertext"] is None
    assert outbound["outbound_id"] == outbound_id
    assert outbound["status"] == "queued"
    assert outbound["client_message_id"].startswith("hermes-ilink-")


@pytest.mark.asyncio
async def test_failed_agent_turn_does_not_create_outbox(queued):
    store, registered = queued
    dispatcher = ChannelDispatcher(store, object())
    claim = dispatcher.claim_next(holder="dispatcher")
    from hermes_cli.channel_identity.owner_resolution import resolve_binding
    owner, _ = resolve_binding(store, binding_id=registered.binding_id)
    client = AsyncMock()
    client.owner = owner
    client.handle = type("Handle", (), {"worker_generation": 1})()
    client.call.side_effect = [
        {"session_id": "live-1", "stored_session_id": "stored-1"},
        {"status": "streaming"},
    ]
    client.wait_for_event.return_value = {
        "method": "message.complete",
        "params": {"session_id": "live-1", "status": "error", "text": "failed"},
    }

    class _Context:
        async def __aenter__(self):
            return client

        async def __aexit__(self, *args):
            return None

    with patch(
        "hermes_cli.channel_dispatch.dispatcher.OwnerWorkerGatewayClient",
        return_value=_Context(),
    ):
        with pytest.raises(RuntimeError, match="did not complete"):
            await dispatcher.dispatch_claim(claim, holder="dispatcher")

    with store.read() as conn:
        inbound = conn.execute("SELECT status FROM inbound_messages").fetchone()
        count = conn.execute("SELECT COUNT(*) AS count FROM outbound_messages").fetchone()["count"]
    assert inbound["status"] == "failed"
    assert count == 0
