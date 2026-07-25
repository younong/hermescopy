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


def test_failed_outbound_unblocks_next_inbound_but_active_outbound_does_not(queued):
    store, registered = queued
    dispatcher = ChannelDispatcher(store, object())
    now = 1.0
    with store.write() as conn:
        first = conn.execute("SELECT inbound_id FROM inbound_messages").fetchone()["inbound_id"]
        conn.execute(
            "UPDATE inbound_messages SET status='outbound_pending', created_at=? WHERE inbound_id=?",
            (now, first),
        )
        conn.execute(
            """
            INSERT INTO outbound_messages
              (outbound_id, inbound_id, account_id, binding_id, client_message_id,
               status, next_attempt_at, created_at, updated_at)
            VALUES ('outbound-first', ?, ?, ?, 'client-first', 'queued', 0, ?, ?)
            """,
            (first, registered.account_id, registered.binding_id, now, now),
        )
        conn.execute(
            """
            INSERT INTO inbound_messages
              (inbound_id, account_id, binding_id, provider_message_id, status,
               created_at, updated_at)
            VALUES ('inbound-next', ?, ?, 'provider-next', 'queued', ?, ?)
            """,
            (registered.account_id, registered.binding_id, now + 1, now + 1),
        )

    assert dispatcher.claim_next(holder="dispatcher") is None
    with store.write() as conn:
        conn.execute("UPDATE outbound_messages SET status='failed'")
        conn.execute("UPDATE inbound_messages SET status='failed' WHERE inbound_id=?", (first,))

    claim = dispatcher.claim_next(holder="dispatcher")
    assert claim is not None
    assert claim["inbound_id"] == "inbound-next"


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
