"""Tests for central iLink poll fencing and durable queue semantics."""

from __future__ import annotations

import base64
import json
import sqlite3
import time
from unittest.mock import AsyncMock

import pytest

from gateway.weixin_ilink import ILinkTransportError
from hermes_cli.channel_connectors.weixin_ilink.poller import (
    acquire_poll_lease,
    commit_update_batch,
    load_poll_account,
)
from hermes_cli.channel_connectors.weixin_ilink.sender import OutboundSender, claim_outbound
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
    store = ChannelIdentityStore(crypto, tmp_path / "control-plane", global_home=tmp_path)
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


def _queue_outbound(store, registered, *, text: str, attempts: int = 0) -> str:
    lease = acquire_poll_lease(store, account_id=registered.account_id, holder="outbound-seed")
    commit_update_batch(
        store,
        lease,
        messages=(_text_message(f"msg-outbound-{time.time_ns()}"),),
        cursor="cursor",
    )
    with store.write() as conn:
        inbound_id = conn.execute(
            "SELECT inbound_id FROM inbound_messages ORDER BY created_at DESC LIMIT 1"
        ).fetchone()["inbound_id"]
        conn.execute(
            "UPDATE inbound_messages SET status='outbound_pending' WHERE inbound_id=?",
            (inbound_id,),
        )
        outbound_id = f"outbound-{time.time_ns()}"
        ciphertext, version = store.crypto.encrypt_text(
            text,
            table="outbound_messages",
            record_id=outbound_id,
            field="payload",
        )
        context, context_version = store.crypto.encrypt_text(
            "ctx",
            table="outbound_messages",
            record_id=outbound_id,
            field="context",
        )
        now = time.time()
        conn.execute(
            """
            INSERT INTO outbound_messages
              (outbound_id, inbound_id, account_id, binding_id, client_message_id,
               payload_ciphertext, payload_key_version, context_ciphertext,
               context_key_version, status, attempts, next_attempt_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, 0, ?, ?)
            """,
            (
                outbound_id,
                inbound_id,
                registered.account_id,
                registered.binding_id,
                f"client-{outbound_id}",
                ciphertext,
                version,
                context,
                context_version,
                attempts,
                now,
                now,
            ),
        )
    return outbound_id


def _voice_message(message_id: str, voice_item: dict):
    return {
        "message_id": message_id,
        "from_user_id": "peer-a",
        "context_token": "ctx",
        "item_list": [{"type": 3, "voice_item": voice_item}],
    }


def _decrypt_inbound_payload(identity_store, row) -> str:
    return identity_store.crypto.decrypt_text(
        row["payload_ciphertext"],
        table="inbound_messages",
        record_id=row["provider_message_id"],
        field="payload",
        version=row["payload_key_version"],
    )


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


@pytest.mark.asyncio
async def test_outbound_sender_delivers_long_reply_with_stable_chunk_progress(store, monkeypatch):
    identity_store, registered = store
    outbound_id = _queue_outbound(identity_store, registered, text="word " * 1200, attempts=20700)
    sent: list[tuple[str, str]] = []

    async def send_message(_client, *, text, client_id, **_kwargs):
        sent.append((text, client_id))
        return {"ret": 0}

    monkeypatch.setattr(
        "hermes_cli.channel_connectors.weixin_ilink.sender.WeixinILinkClient.send_message",
        send_message,
    )
    sender = OutboundSender(identity_store, object(), chunk_delay_seconds=0)

    first = claim_outbound(identity_store, holder="sender")
    assert first is not None
    assert first.outbound_id == outbound_id
    assert first.part_attempts == 1
    assert len(first.parts) > 1
    first_client_id = first.part_client_id
    assert first.part_attempts == 1
    assert await sender.send_claim(first, holder="sender") is True

    second = claim_outbound(identity_store, holder="sender")
    assert second is not None
    assert second.next_part_index == 1
    assert second.part_attempts == 1
    assert second.part_client_id != first_client_id
    while second is not None:
        assert await sender.send_claim(second, holder="sender") is True
        second = claim_outbound(identity_store, holder="sender")

    assert all(len(text) <= 2000 for text, _ in sent)
    with identity_store.read() as conn:
        outbound = conn.execute(
            "SELECT status, attempts, next_chunk_index, chunk_attempts FROM outbound_messages"
        ).fetchone()
        inbound = conn.execute("SELECT status FROM inbound_messages").fetchone()
    assert outbound["status"] == "delivered"
    assert outbound["attempts"] == 20700 + len(sent)
    assert outbound["next_chunk_index"] == len(sent)
    assert outbound["chunk_attempts"] == 0
    assert inbound["status"] == "completed"


@pytest.mark.asyncio
async def test_outbound_sender_retries_same_chunk_id_then_terminally_fails(store, monkeypatch):
    identity_store, registered = store
    _queue_outbound(identity_store, registered, text="reply")
    ids: list[str] = []
    failures = [
        ILinkTransportError("send message", "rate_limited", provider_code=-2, transient=True),
        ILinkTransportError("send message", "stale_session", provider_code=-14),
    ]

    async def send_message(_client, *, client_id, **_kwargs):
        ids.append(client_id)
        raise failures.pop(0)

    monkeypatch.setattr(
        "hermes_cli.channel_connectors.weixin_ilink.sender.WeixinILinkClient.send_message",
        send_message,
    )
    sender = OutboundSender(identity_store, object(), retry_seconds=0, max_attempts=3)

    first = claim_outbound(identity_store, holder="sender")
    assert first is not None
    assert await sender.send_claim(first, holder="sender") is False
    retry = claim_outbound(identity_store, holder="sender")
    assert retry is not None
    assert retry.part_attempts == 2
    assert retry.part_client_id == first.part_client_id
    assert await sender.send_claim(retry, holder="sender") is False

    assert ids == [first.part_client_id, first.part_client_id]
    assert claim_outbound(identity_store, holder="sender") is None
    with identity_store.read() as conn:
        outbound = conn.execute(
            "SELECT status, last_error, failed_chunk_index FROM outbound_messages"
        ).fetchone()
        inbound = conn.execute(
            "SELECT status, rejection_reason FROM inbound_messages"
        ).fetchone()
    assert tuple(outbound) == ("failed", "stale_session:provider=-14", 0)
    assert tuple(inbound) == ("failed", "outbound_failed")


@pytest.mark.asyncio
async def test_outbound_sender_delivers_text_then_media_parts(store, monkeypatch, tmp_path):
    identity_store, registered = store
    from hermes_cli.channel_identity.owner_resolution import resolve_binding

    owner, _ = resolve_binding(identity_store, binding_id=registered.binding_id)
    workspace = owner.owner_home / "workspaces" / "default"
    workspace.mkdir(parents=True)
    image = workspace / "reply.png"
    audio = workspace / "reply.mp3"
    image.write_bytes(b"png")
    audio.write_bytes(b"mp3")
    payload = json.dumps(
        {
            "v": 1,
            "text": "reply",
            "media": [
                {"path": str(image), "voice": False},
                {"path": str(audio), "voice": True},
            ],
        }
    )
    _queue_outbound(identity_store, registered, text=payload)
    sent: list[tuple] = []

    async def send_message(_client, *, text, client_id, **_kwargs):
        sent.append(("text", text, client_id))
        return {"ret": 0}

    async def upload_media(_session, _client, *, path, force_file, **_kwargs):
        sent.append(("upload", path, force_file))
        return {"type": 2 if path.endswith(".png") else 4}

    async def send_item(_client, *, item, client_id, **_kwargs):
        sent.append(("item", item["type"], client_id))
        return {"ret": 0}

    monkeypatch.setattr(
        "hermes_cli.channel_connectors.weixin_ilink.sender.WeixinILinkClient.send_message",
        send_message,
    )
    monkeypatch.setattr(
        "hermes_cli.channel_connectors.weixin_ilink.sender.upload_media_item",
        upload_media,
    )
    monkeypatch.setattr(
        "hermes_cli.channel_connectors.weixin_ilink.sender.WeixinILinkClient.send_item",
        send_item,
    )
    sender = OutboundSender(identity_store, object(), chunk_delay_seconds=0)

    claim = claim_outbound(identity_store, holder="sender")
    assert claim is not None
    assert [part.kind for part in claim.parts] == ["text", "media", "media"]
    client_ids = []
    while claim is not None:
        client_ids.append(claim.part_client_id)
        assert await sender.send_claim(claim, holder="sender") is True
        claim = claim_outbound(identity_store, holder="sender")

    assert len(set(client_ids)) == 3
    assert sent == [
        ("text", "reply", client_ids[0]),
        ("upload", str(image), False),
        ("item", 2, client_ids[1]),
        ("upload", str(audio), True),
        ("item", 4, client_ids[2]),
    ]


@pytest.mark.asyncio
async def test_outbound_sender_rejects_media_outside_exact_owner_workspace(
    store, monkeypatch, tmp_path
):
    identity_store, registered = store
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"png")
    payload = json.dumps(
        {"v": 1, "text": "", "attachments": [{"path": str(outside), "voice": False}]}
    )
    _queue_outbound(identity_store, registered, text=payload)
    upload = AsyncMock()
    monkeypatch.setattr(
        "hermes_cli.channel_connectors.weixin_ilink.sender.upload_media_item",
        upload,
    )
    sender = OutboundSender(identity_store, object())
    with pytest.raises(RuntimeError, match="outbound media path is invalid"):
        claim_outbound(identity_store, holder="sender")
    upload.assert_not_awaited()
    with identity_store.read() as conn:
        row = conn.execute("SELECT status, last_error FROM outbound_messages").fetchone()
    assert tuple(row) == ("queued", "claim_error:RuntimeError")


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["stale_context", "provider_rejected"])
async def test_outbound_sender_terminally_fails_ambiguous_provider_rejection(
    store, monkeypatch, reason
):
    identity_store, registered = store
    _queue_outbound(identity_store, registered, text="reply")

    async def send_message(_client, **_kwargs):
        raise ILinkTransportError("send message", reason, provider_code=-2)

    monkeypatch.setattr(
        "hermes_cli.channel_connectors.weixin_ilink.sender.WeixinILinkClient.send_message",
        send_message,
    )
    sender = OutboundSender(identity_store, object(), retry_seconds=0, max_attempts=3)

    claim = claim_outbound(identity_store, holder="sender")
    assert claim is not None
    assert claim.part_attempts == 1
    assert await sender.send_claim(claim, holder="sender") is False

    assert claim_outbound(identity_store, holder="sender") is None
    with identity_store.read() as conn:
        outbound = conn.execute(
            "SELECT status, attempts, chunk_attempts, last_error, failed_chunk_index "
            "FROM outbound_messages"
        ).fetchone()
        inbound = conn.execute(
            "SELECT status, rejection_reason FROM inbound_messages"
        ).fetchone()
    assert tuple(outbound) == (
        "failed",
        1,
        1,
        f"{reason}:provider=-2",
        0,
    )
    assert tuple(inbound) == ("failed", "outbound_failed")


def test_ilink_poll_and_sender_ignore_other_provider_accounts(store):
    identity_store, registered = store
    with identity_store.write() as conn:
        conn.execute(
            "UPDATE connector_accounts SET status='suspended' WHERE account_id=?",
            (registered.account_id,),
        )
        account = conn.execute(
            "SELECT * FROM connector_accounts WHERE account_id=?",
            (registered.account_id,),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO connector_accounts
              (account_id, provider, provider_account_id,
               account_lookup_hash, credentials_ciphertext, credentials_key_version,
               credential_version, status, created_at, updated_at)
            VALUES ('other-account', 'other_provider', 'other', 'other-bot', ?, ?,
                    1, 'active', 1, 1)
            """,
            (
                account["credentials_ciphertext"],
                account["credentials_key_version"],
            ),
        )
    with pytest.raises(RuntimeError, match="active iLink account not found"):
        acquire_poll_lease(identity_store, account_id="other-account", holder="poller")
    assert claim_outbound(identity_store, holder="sender") is None


def test_claim_outbound_releases_sending_row_on_decrypt_failure(store, monkeypatch):
    identity_store, registered = store
    outbound_id = _queue_outbound(identity_store, registered, text="reply")
    original = identity_store.crypto.decrypt_text

    def fail_payload(ciphertext, **kwargs):
        if kwargs["table"] == "outbound_messages" and kwargs["field"] == "payload":
            raise RuntimeError("decrypt failed")
        return original(ciphertext, **kwargs)

    monkeypatch.setattr(identity_store.crypto, "decrypt_text", fail_payload)
    with pytest.raises(RuntimeError, match="decrypt failed"):
        claim_outbound(identity_store, holder="sender")
    with identity_store.read() as conn:
        row = conn.execute(
            "SELECT status, claimed_by, last_error FROM outbound_messages WHERE outbound_id=?",
            (outbound_id,),
        ).fetchone()
    assert tuple(row) == ("queued", None, "claim_error:RuntimeError")


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


def test_provider_voice_transcript_queues_without_media_descriptor(store):
    identity_store, registered = store
    lease = acquire_poll_lease(identity_store, account_id=registered.account_id, holder="holder")

    commit_update_batch(
        identity_store,
        lease,
        messages=(_voice_message("msg-voice-text", {"text": "  你好 Hermes  "}),),
        cursor="cursor",
    )

    with identity_store.read() as conn:
        row = conn.execute("SELECT * FROM inbound_messages").fetchone()
    assert row["status"] == "queued"
    assert row["payload_kind"] == "voice_transcript"
    assert _decrypt_inbound_payload(identity_store, row) == "你好 Hermes"


def test_raw_voice_queues_minimal_encrypted_descriptor(store):
    identity_store, registered = store
    lease = acquire_poll_lease(identity_store, account_id=registered.account_id, holder="holder")

    commit_update_batch(
        identity_store,
        lease,
        messages=(
            _voice_message(
                "msg-voice-media",
                {
                    "media": {
                        "encrypt_query_param": "signed-query",
                        "aes_key": "encrypted-key",
                        "ignored": "must-not-persist",
                    },
                    "playtime": 1200,
                    "sample_rate": 24000,
                    "ignored": "must-not-persist",
                },
            ),
        ),
        cursor="cursor",
    )

    with identity_store.read() as conn:
        row = conn.execute("SELECT * FROM inbound_messages").fetchone()
        raw_database = identity_store.path.read_bytes()
    descriptor = json.loads(_decrypt_inbound_payload(identity_store, row))
    assert row["status"] == "queued"
    assert row["payload_kind"] == "voice_media"
    assert descriptor == {
        "v": 1,
        "media": {
            "encrypt_query_param": "signed-query",
            "aes_key": "encrypted-key",
        },
        "playtime": 1200,
        "sample_rate": 24000,
    }
    assert b"signed-query" not in raw_database
    assert b"encrypted-key" not in raw_database


def test_file_message_is_queued_with_encrypted_download_descriptor(store):
    identity_store, registered = store
    lease = acquire_poll_lease(identity_store, account_id=registered.account_id, holder="holder")
    aes_key = base64.b64encode(b"a" * 16).decode()

    inserted = commit_update_batch(
        identity_store,
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
                            "len": "12",
                            "media": {
                                "encrypt_query_param": "download-token",
                                "aes_key": aes_key,
                            },
                        },
                    }
                ],
            },
        ),
        cursor="cursor-file",
    )

    assert inserted == 1
    with identity_store.read() as conn:
        row = conn.execute("SELECT * FROM inbound_messages").fetchone()
    assert row["status"] == "queued"
    assert row["payload_kind"] == "media"
    assert json.loads(_decrypt_inbound_payload(identity_store, row)) == {
        "v": 1,
        "text": "",
        "attachments": [
            {
                "kind": "file",
                "file_name": "report.txt",
                "media": {
                    "encrypt_query_param": "download-token",
                    "aes_key": aes_key,
                },
                "size": 12,
            }
        ],
    }


@pytest.mark.parametrize(
    ("item", "kind", "file_name"),
    [
        (
            {
                "type": 2,
                "image_item": {
                    "file_name": "photo.png",
                    "media": {"encrypt_query_param": "image-token"},
                },
            },
            "image",
            "photo.png",
        ),
        (
            {
                "type": 5,
                "video_item": {
                    "file_name": "clip.mp4",
                    "media": {"encrypt_query_param": "video-token"},
                },
            },
            "video",
            "clip.mp4",
        ),
    ],
)
def test_known_visual_media_is_queued_with_text(store, item, kind, file_name):
    identity_store, registered = store
    lease = acquire_poll_lease(identity_store, account_id=registered.account_id, holder="holder")

    commit_update_batch(
        identity_store,
        lease,
        messages=(
            {
                "message_id": f"msg-{kind}",
                "from_user_id": "peer-a",
                "item_list": [
                    {"type": 1, "text_item": {"text": "caption"}},
                    item,
                ],
            },
        ),
        cursor=f"cursor-{kind}",
    )

    with identity_store.read() as conn:
        row = conn.execute("SELECT * FROM inbound_messages").fetchone()
    payload = json.loads(_decrypt_inbound_payload(identity_store, row))
    assert row["payload_kind"] == "media"
    assert payload["text"] == "caption"
    assert payload["attachments"] == [
        {
            "kind": kind,
            "file_name": file_name,
            "media": item[f"{kind}_item"]["media"],
        }
    ]


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
            "media_descriptor_invalid",
        ),
        (_voice_message("msg-invalid-voice", {"media": {}}), "voice_media_invalid"),
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
