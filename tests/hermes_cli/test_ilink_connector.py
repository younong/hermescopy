"""Tests for central iLink poll fencing and durable queue semantics."""

from __future__ import annotations

import sqlite3
import time
from unittest.mock import AsyncMock

import pytest

from hermes_cli.channel_connectors.weixin_ilink.poller import (
    acquire_poll_lease,
    commit_update_batch,
    load_poll_account,
)
from hermes_cli.channel_connectors.weixin_ilink.service import WeixinILinkService
from hermes_cli.channel_identity import (
    ChannelCrypto,
    ChannelIdentityStore,
    Keyring,
    register_weixin_identity,
)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_OWNER_SECRET", "owner-secret")
    crypto = ChannelCrypto(
        lookup=Keyring(keys={1: b"l" * 32}, active_version=1),
        encryption=Keyring(keys={1: b"e" * 32}, active_version=1),
    )
    store = ChannelIdentityStore(crypto)
    registered = register_weixin_identity(
        store,
        subject="peer-a",
        bot_id="bot-a",
        bot_token="token-a",
        base_url="https://ilink.example",
        peer_id="peer-a",
    )
    return store, registered


def _text_message(message_id: str | None, *, sender: str = "peer-a", text: str = "hello", context="ctx"):
    return {
        "message_id": message_id,
        "from_user_id": sender,
        "context_token": context,
        "item_list": [{"type": 1, "text_item": {"text": text}}],
    }


@pytest.mark.asyncio
async def test_service_recovers_stale_claims_and_starts_bounded_loops(store, monkeypatch):
    identity_store, registered = store
    lease = acquire_poll_lease(identity_store, account_id=registered.account_id, holder="seed")
    commit_update_batch(
        identity_store,
        lease,
        messages=(_text_message("msg-recover"),),
        cursor="cursor",
    )
    stale = time.time() - 100
    with identity_store.write() as conn:
        inbound_id = conn.execute("SELECT inbound_id FROM inbound_messages").fetchone()["inbound_id"]
        conn.execute(
            "UPDATE inbound_messages SET status='processing', claimed_by='old', claimed_at=?",
            (stale,),
        )
        conn.execute(
            """
            INSERT INTO outbound_messages
              (outbound_id, inbound_id, account_id, binding_id, client_message_id,
               status, next_attempt_at, claimed_by, claimed_at, created_at, updated_at)
            VALUES ('outbound-stale', ?, ?, ?, 'client-stale',
                    'sending', ?, 'old', ?, ?, ?)
            """,
            (inbound_id, registered.account_id, registered.binding_id, stale, stale, stale, stale),
        )

    monkeypatch.setattr(
        "hermes_cli.channel_connectors.weixin_ilink.service.ChannelDispatcher.claim_next",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "hermes_cli.channel_connectors.weixin_ilink.service.claim_outbound",
        lambda *_args, **_kwargs: None,
    )
    service = WeixinILinkService(
        identity_store,
        object(),
        object(),
        config={
            "dispatch_claim_timeout_seconds": 1,
            "outbound_retry_seconds": 0.01,
            "provider_retry_seconds": 0.01,
            "dispatch_concurrency": 2,
        },
    )
    service.pollers.start = AsyncMock()
    service.pollers.stop = AsyncMock()

    await service.start()
    try:
        assert service._running is True
        assert len(service._tasks) == 3
        service.pollers.start.assert_awaited_once()
        with identity_store.read() as conn:
            inbound = conn.execute(
                "SELECT status, claimed_by, claimed_at FROM inbound_messages"
            ).fetchone()
            outbound = conn.execute(
                "SELECT status, claimed_by, claimed_at FROM outbound_messages"
            ).fetchone()
        assert tuple(inbound) == ("queued", None, None)
        assert tuple(outbound) == ("queued", None, None)
    finally:
        await service.stop()
    service.pollers.stop.assert_awaited_once()
    assert service._tasks == set()


def test_new_poll_generation_fences_old_poller(store):
    identity_store, registered = store
    old = acquire_poll_lease(identity_store, account_id=registered.account_id, holder="old")
    acquire_poll_lease(identity_store, account_id=registered.account_id, holder="new")

    with pytest.raises(RuntimeError, match="stale"):
        commit_update_batch(identity_store, old, messages=(), cursor="cursor-old")


def test_batch_atomically_advances_cursor_context_and_inbound(store):
    identity_store, registered = store
    lease = acquire_poll_lease(identity_store, account_id=registered.account_id, holder="holder")

    inserted = commit_update_batch(
        identity_store,
        lease,
        messages=(_text_message("msg-1"),),
        cursor="cursor-2",
    )

    assert inserted == 1
    _, _, cursor = load_poll_account(identity_store, lease)
    assert cursor == "cursor-2"
    with identity_store.read() as conn:
        inbound = conn.execute("SELECT status FROM inbound_messages").fetchone()
        token = conn.execute("SELECT COUNT(*) AS count FROM context_tokens").fetchone()
    assert inbound["status"] == "queued"
    assert token["count"] == 1


def test_provider_replay_is_idempotent_but_same_text_new_id_is_distinct(store):
    identity_store, registered = store
    lease = acquire_poll_lease(identity_store, account_id=registered.account_id, holder="holder")

    first = commit_update_batch(
        identity_store,
        lease,
        messages=(_text_message("msg-1"),),
        cursor="cursor-1",
    )
    replay = commit_update_batch(
        identity_store,
        lease,
        messages=(_text_message("msg-1"), _text_message("msg-2")),
        cursor="cursor-2",
    )

    assert first == 1
    assert replay == 1
    with identity_store.read() as conn:
        assert conn.execute("SELECT COUNT(*) AS count FROM inbound_messages").fetchone()["count"] == 2


@pytest.mark.parametrize(
    ("message", "reason"),
    [
        (_text_message(None), "missing_provider_message_id"),
        (_text_message("msg-unknown", sender="peer-attacker"), "unknown_peer"),
        ({**_text_message("msg-group"), "room_id": "room"}, "group_not_supported"),
        (
            {
                "message_id": "msg-media",
                "from_user_id": "peer-a",
                "item_list": [{"type": 2, "image_item": {}}],
            },
            "non_text_not_supported",
        ),
    ],
)
def test_unsupported_inbound_is_explicitly_rejected(store, message, reason):
    identity_store, registered = store
    lease = acquire_poll_lease(identity_store, account_id=registered.account_id, holder="holder")

    commit_update_batch(identity_store, lease, messages=(message,), cursor="cursor")

    with identity_store.read() as conn:
        row = conn.execute(
            "SELECT status, rejection_reason, payload_ciphertext FROM inbound_messages"
        ).fetchone()
    assert row["status"] == "rejected"
    assert row["rejection_reason"] == reason
    assert row["payload_ciphertext"] is None


def test_transaction_rolls_back_cursor_and_inbound_on_failure(store, monkeypatch):
    identity_store, registered = store
    lease = acquire_poll_lease(identity_store, account_id=registered.account_id, holder="holder")
    original = identity_store.crypto.encrypt_text

    def fail_context(value, **kwargs):
        if kwargs["field"] == "context":
            raise RuntimeError("crypto unavailable")
        return original(value, **kwargs)

    monkeypatch.setattr(identity_store.crypto, "encrypt_text", fail_context)

    with pytest.raises(RuntimeError, match="crypto unavailable"):
        commit_update_batch(
            identity_store,
            lease,
            messages=(_text_message("msg-1"),),
            cursor="cursor-new",
        )

    _, _, cursor = load_poll_account(identity_store, lease)
    assert cursor == ""
    with identity_store.read() as conn:
        assert conn.execute("SELECT COUNT(*) AS count FROM inbound_messages").fetchone()["count"] == 0
