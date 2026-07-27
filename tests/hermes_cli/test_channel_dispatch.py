"""Tests for trusted channel dispatch and transactional outbox."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from gateway.weixin_ilink.media import WeixinMediaError
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


def _replace_with_voice(store, *, transcript: str | None = None):
    with store.write() as conn:
        row = conn.execute("SELECT provider_message_id FROM inbound_messages").fetchone()
        descriptor = {
            "v": 1,
            "media": {"full_url": "https://novac2c.cdn.weixin.qq.com/voice"},
            "playtime": 1000,
        }
        value = transcript if transcript is not None else json.dumps(descriptor)
        ciphertext, version = store.crypto.encrypt_text(
            value,
            table="inbound_messages",
            record_id=row["provider_message_id"],
            field="payload",
        )
        conn.execute(
            "UPDATE inbound_messages SET payload_ciphertext=?, payload_key_version=?, payload_kind=?",
            (ciphertext, version, "voice_transcript" if transcript is not None else "voice_media"),
        )


def _client_context(client):
    class _Context:
        async def __aenter__(self):
            return client

        async def __aexit__(self, *args):
            return None

    return _Context()


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
async def test_voice_media_transcribes_checkpoints_and_submits_raw_transcript(queued):
    store, registered = queued
    _replace_with_voice(store)
    dispatcher = ChannelDispatcher(store, object(), media_session=object())
    claim = dispatcher.claim_next(holder="dispatcher")
    from hermes_cli.channel_identity.owner_resolution import resolve_binding
    owner, _ = resolve_binding(store, binding_id=registered.binding_id)
    client = AsyncMock()
    client.owner = owner
    client.handle = type("Handle", (), {"worker_generation": 1})()
    client.call.side_effect = [
        {"session_id": "live-1", "stored_session_id": "stored-1"},
        {"accepted": True, "offset": 0, "chunk_bytes": 262144},
        {"accepted": True, "offset": len(b"#!SILK_V3 test")},
        {
            "success": True,
            "transcript": "转录内容",
            "provider": "local",
            "code": "",
            "retryable": False,
        },
        {"status": "streaming"},
    ]
    client.wait_for_event.return_value = {
        "params": {"session_id": "live-1", "status": "complete", "text": "answer"},
    }

    with patch(
        "hermes_cli.channel_dispatch.dispatcher.OwnerWorkerGatewayClient",
        return_value=_client_context(client),
    ), patch(
        "hermes_cli.channel_dispatch.dispatcher.download_and_decrypt_media",
        new=AsyncMock(return_value=b"#!SILK_V3 test"),
    ):
        await dispatcher.dispatch_claim(claim, holder="dispatcher")

    methods = [entry.args[0] for entry in client.call.await_args_list]
    assert methods == [
        "session.create",
        "channel.voice.begin",
        "channel.voice.chunk",
        "channel.voice.finish",
        "prompt.submit",
    ]
    assert client.call.await_args_list[-1].args[1]["text"] == "转录内容"


@pytest.mark.asyncio
async def test_voice_transient_failure_requeues_with_backoff_and_checkpoint_skips_stt(queued):
    store, registered = queued
    _replace_with_voice(store)
    dispatcher = ChannelDispatcher(
        store,
        object(),
        media_session=object(),
        voice_config={"voice_retry_base_seconds": 5, "voice_max_retries": 2},
    )
    claim = dispatcher.claim_next(holder="dispatcher")
    from hermes_cli.channel_identity.owner_resolution import resolve_binding
    owner, _ = resolve_binding(store, binding_id=registered.binding_id)
    client = AsyncMock()
    client.owner = owner
    client.handle = type("Handle", (), {"worker_generation": 1})()
    client.call.return_value = {"session_id": "live-1", "stored_session_id": "stored-1"}

    with patch(
        "hermes_cli.channel_dispatch.dispatcher.OwnerWorkerGatewayClient",
        return_value=_client_context(client),
    ), patch(
        "hermes_cli.channel_dispatch.dispatcher.download_and_decrypt_media",
        new=AsyncMock(side_effect=WeixinMediaError("media_download_timeout", retryable=True)),
    ):
        with pytest.raises(RuntimeError, match="media_download_timeout"):
            await dispatcher.dispatch_claim(claim, holder="dispatcher")

    with store.read() as conn:
        row = conn.execute("SELECT status, attempts, next_attempt_at, last_error FROM inbound_messages").fetchone()
    assert row["status"] == "queued"
    assert row["attempts"] == 1
    assert row["next_attempt_at"] > 0
    assert row["last_error"] == "media_download_timeout"
    assert dispatcher.claim_next(holder="dispatcher-2") is None


@pytest.mark.asyncio
async def test_voice_checkpoint_restart_skips_media_download(queued):
    store, registered = queued
    _replace_with_voice(store, transcript="已保存转录")
    with store.write() as conn:
        conn.execute(
            "UPDATE inbound_messages SET status='queued', claimed_by=NULL, claimed_at=NULL"
        )

    dispatcher = ChannelDispatcher(store, object())
    claim = dispatcher.claim_next(holder="dispatcher")
    assert claim["payload_kind"] == "voice_transcript"
    owner = __import__(
        "hermes_cli.channel_identity.owner_resolution", fromlist=["resolve_binding"]
    ).resolve_binding(store, binding_id=registered.binding_id)[0]
    client = AsyncMock()
    client.owner = owner
    client.handle = type("Handle", (), {"worker_generation": 1})()
    client.call.side_effect = [
        {"session_id": "live-1", "stored_session_id": "stored-1"},
        {"status": "streaming"},
    ]
    client.wait_for_event.return_value = {
        "params": {"session_id": "live-1", "status": "complete", "text": "answer"},
    }
    with patch(
        "hermes_cli.channel_dispatch.dispatcher.OwnerWorkerGatewayClient",
        return_value=_client_context(client),
    ), patch(
        "hermes_cli.channel_dispatch.dispatcher.download_and_decrypt_media",
        new=AsyncMock(),
    ) as download:
        await dispatcher.dispatch_claim(claim, holder="dispatcher")
    download.assert_not_awaited()
    assert client.call.await_args_list[-1].args[1]["text"] == "已保存转录"


@pytest.mark.asyncio
async def test_voice_terminal_failure_clears_sensitive_payload_and_unblocks_fifo(queued):
    store, registered = queued
    _replace_with_voice(store)
    with store.write() as conn:
        first = conn.execute("SELECT * FROM inbound_messages").fetchone()
        text_ciphertext, text_version = store.crypto.encrypt_text(
            "later",
            table="inbound_messages",
            record_id="msg-2",
            field="payload",
        )
        conn.execute(
            """
            INSERT INTO inbound_messages
              (inbound_id, account_id, binding_id, provider_message_id,
               payload_ciphertext, payload_key_version, payload_kind, status,
               next_attempt_at, created_at, updated_at)
            VALUES ('im-second', ?, ?, 'msg-2', ?, ?, 'text', 'queued', 0, ?, ?)
            """,
            (
                first["account_id"], first["binding_id"], text_ciphertext, text_version,
                first["created_at"] + 1, first["updated_at"] + 1,
            ),
        )
    dispatcher = ChannelDispatcher(store, object(), media_session=object())
    claim = dispatcher.claim_next(holder="dispatcher")
    owner = __import__(
        "hermes_cli.channel_identity.owner_resolution", fromlist=["resolve_binding"]
    ).resolve_binding(store, binding_id=registered.binding_id)[0]
    client = AsyncMock()
    client.owner = owner
    client.handle = type("Handle", (), {"worker_generation": 1})()
    client.call.return_value = {"session_id": "live-1", "stored_session_id": "stored-1"}
    with patch(
        "hermes_cli.channel_dispatch.dispatcher.OwnerWorkerGatewayClient",
        return_value=_client_context(client),
    ), patch(
        "hermes_cli.channel_dispatch.dispatcher.download_and_decrypt_media",
        new=AsyncMock(side_effect=WeixinMediaError("unsafe_media_url")),
    ):
        with pytest.raises(RuntimeError, match="unsafe_media_url"):
            await dispatcher.dispatch_claim(claim, holder="dispatcher")
    with store.read() as conn:
        failed = conn.execute(
            "SELECT * FROM inbound_messages WHERE provider_message_id='msg-1'"
        ).fetchone()
    assert failed["status"] == "failed"
    assert failed["payload_ciphertext"] is None
    assert failed["context_ciphertext"] is None
    assert dispatcher.claim_next(holder="other")["provider_message_id"] == "msg-2"


@pytest.mark.asyncio
async def test_dispatch_downloads_and_attaches_file_before_prompt(queued, tmp_path):
    store, registered = queued
    with store.write() as conn:
        conn.execute("DELETE FROM inbound_messages")
    lease = acquire_poll_lease(store, account_id=registered.account_id, holder="file-poller")
    commit_update_batch(
        store,
        lease,
        messages=(
            {
                "message_id": "msg-file",
                "from_user_id": "peer-a",
                "item_list": [
                    {
                        "type": 4,
                        "file_item": {
                            "file_name": "report.txt",
                            "len": "6",
                            "media": {
                                "full_url": "https://novac2c.cdn.weixin.qq.com/report",
                                "aes_key": base64.b64encode(b"a" * 16).decode(),
                            },
                        },
                    }
                ],
            },
        ),
        cursor="cursor-file",
    )
    session = object()
    dispatcher = ChannelDispatcher(store, object(), session=session)
    claim = dispatcher.claim_next(holder="dispatcher")
    assert claim is not None

    client = AsyncMock()
    client.owner = None
    client.handle = type("Handle", (), {"worker_generation": 1})()
    client.call.side_effect = [
        {"session_id": "live-1", "stored_session_id": "stored-1"},
        {"ref_text": "@file:.hermes/weixin-attachments/msg-file/report.txt"},
        {"status": "streaming"},
    ]
    client.wait_for_event.return_value = {
        "method": "message.complete",
        "params": {"session_id": "live-1", "status": "complete", "text": "answer"},
    }

    from hermes_cli.channel_identity.owner_resolution import resolve_binding
    owner, _ = resolve_binding(store, binding_id=registered.binding_id)

    class _Context:
        async def __aenter__(self):
            client.owner = owner
            return client

        async def __aexit__(self, *args):
            return None

    owner.host_owner_home.mkdir(parents=True, exist_ok=True)
    staged = owner.owner_home / "workspaces" / "default" / ".hermes" / "weixin-attachments"

    async def fake_download(observed_session, *, descriptor, limits):
        assert observed_session is session
        assert descriptor == {
            "v": 1,
            "media": {
                "full_url": "https://novac2c.cdn.weixin.qq.com/report",
                "aes_key": base64.b64encode(b"a" * 16).decode(),
            },
        }
        assert limits.max_download_bytes == 32 * 1024 * 1024
        return b"report"

    with (
        patch(
            "hermes_cli.channel_dispatch.dispatcher.OwnerWorkerGatewayClient",
            return_value=_Context(),
        ),
        patch(
            "hermes_cli.channel_dispatch.dispatcher.download_and_decrypt_media",
            side_effect=fake_download,
        ),
    ):
        await dispatcher.dispatch_claim(claim, holder="dispatcher")

    attach = client.call.await_args_list[1]
    assert attach.args[0] == "file.attach"
    assert Path(attach.args[1]["path"]).is_relative_to(staged)
    prompt = client.call.await_args_list[2]
    assert prompt.args[0] == "prompt.submit"
    assert prompt.args[1]["text"] == "@file:.hermes/weixin-attachments/msg-file/report.txt"


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
