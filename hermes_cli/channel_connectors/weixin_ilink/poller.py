"""Fenced iLink account polling and atomic inbound queue commits."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import Any, Mapping

from gateway.weixin_ilink import (
    ITEM_FILE,
    ITEM_IMAGE,
    ITEM_TEXT,
    ITEM_VIDEO,
    ITEM_VOICE,
    WeixinILinkClient,
    sanitize_filename,
)
from hermes_cli.channel_identity.store import ChannelIdentityStore

_MAX_DESCRIPTOR_BYTES = 64 * 1024
_MAX_MEDIA_FIELD_CHARS = 8 * 1024
_MAX_ATTACHMENTS = 8
_MAX_DECLARED_MEDIA_BYTES = 32 * 1024 * 1024


@dataclass(frozen=True)
class InboundPayload:
    kind: str
    value: str


class StalePollLeaseError(RuntimeError):
    """The account lease was replaced or its credentials changed."""


@dataclass(frozen=True)
class PollLease:
    account_id: str
    holder: str
    generation: int
    credential_version: int


def acquire_poll_lease(
    store: ChannelIdentityStore,
    *,
    account_id: str,
    holder: str,
) -> PollLease:
    with store.write() as conn:
        changed = conn.execute(
            """
            UPDATE ilink_accounts SET poll_generation=poll_generation+1, poll_holder=?,
                poll_health='starting', updated_at=?
            WHERE account_id=? AND status='active'
            """,
            (holder, time.time(), account_id),
        ).rowcount
        if changed != 1:
            raise RuntimeError("active iLink account not found")
        row = conn.execute(
            "SELECT poll_generation, credential_version FROM ilink_accounts WHERE account_id=?",
            (account_id,),
        ).fetchone()
    return PollLease(
        account_id=account_id,
        holder=holder,
        generation=row["poll_generation"],
        credential_version=row["credential_version"],
    )


def load_poll_account(store: ChannelIdentityStore, lease: PollLease) -> tuple[str, str, str]:
    with store.read() as conn:
        row = conn.execute(
            """
            SELECT base_url, bot_token_ciphertext, bot_token_key_version,
                   cursor_ciphertext, cursor_key_version
            FROM ilink_accounts
            WHERE account_id=? AND status='active' AND poll_holder=?
              AND poll_generation=? AND credential_version=?
            """,
            (
                lease.account_id,
                lease.holder,
                lease.generation,
                lease.credential_version,
            ),
        ).fetchone()
    if row is None:
        raise StalePollLeaseError("iLink poll lease is stale")
    token = store.crypto.decrypt_text(
        row["bot_token_ciphertext"],
        table="ilink_accounts",
        record_id=lease.account_id,
        field="bot_token",
        version=row["bot_token_key_version"],
    )
    cursor = ""
    if row["cursor_ciphertext"] is not None:
        cursor = store.crypto.decrypt_text(
            row["cursor_ciphertext"],
            table="ilink_accounts",
            record_id=lease.account_id,
            field="cursor",
            version=row["cursor_key_version"],
        )
    return row["base_url"], token, cursor


def commit_update_batch(
    store: ChannelIdentityStore,
    lease: PollLease,
    *,
    messages: tuple[Mapping[str, Any], ...],
    cursor: str,
) -> int:
    """Atomically validate, enqueue, update context, and advance the cursor."""
    now = time.time()
    inserted = 0
    cursor_ciphertext, cursor_version = store.crypto.encrypt_text(
        cursor,
        table="ilink_accounts",
        record_id=lease.account_id,
        field="cursor",
    )
    with store.write() as conn:
        account = conn.execute(
            """
            SELECT account_id FROM ilink_accounts
            WHERE account_id=? AND status='active' AND poll_holder=?
              AND poll_generation=? AND credential_version=?
            """,
            (
                lease.account_id,
                lease.holder,
                lease.generation,
                lease.credential_version,
            ),
        ).fetchone()
        if account is None:
            raise StalePollLeaseError("iLink poll lease is stale")
        for message in messages:
            inserted += _commit_message(store, conn, lease.account_id, message, now)
        changed = conn.execute(
            """
            UPDATE ilink_accounts SET cursor_ciphertext=?, cursor_key_version=?,
                poll_health='healthy', updated_at=?
            WHERE account_id=? AND status='active' AND poll_holder=?
              AND poll_generation=? AND credential_version=?
            """,
            (
                cursor_ciphertext,
                cursor_version,
                now,
                lease.account_id,
                lease.holder,
                lease.generation,
                lease.credential_version,
            ),
        ).rowcount
        if changed != 1:
            raise RuntimeError("iLink poll lease became stale")
    return inserted


def _commit_message(store, conn, account_id: str, message: Mapping[str, Any], now: float) -> int:
    provider_message_id = str(message.get("message_id") or "").strip()
    sender = str(message.get("from_user_id") or "").strip()
    peer_hash = store.crypto.lookup_hash("peer-id", sender) if sender else ""
    binding = conn.execute(
        """
        SELECT binding_id FROM channel_bindings
        WHERE account_id=? AND peer_lookup_hash=? AND status='active'
        """,
        (account_id, peer_hash),
    ).fetchone() if peer_hash else None
    status = "queued"
    reason = None
    payload = _extract_payload(message.get("item_list"))
    if not provider_message_id:
        provider_message_id = f"rejected-{uuid.uuid4().hex}"
        status, reason = "rejected", "missing_provider_message_id"
    elif binding is None:
        status, reason = "rejected", "unknown_peer"
    elif message.get("room_id") or message.get("chat_room_id"):
        status, reason = "rejected", "group_not_supported"
    elif payload is None:
        status, reason = "rejected", "unsupported_message_type"
    elif payload.kind == "media_invalid":
        status, reason = "rejected", "media_descriptor_invalid"
    elif payload.kind == "voice_invalid":
        status, reason = "rejected", "voice_media_invalid"
    payload_ciphertext = payload_version = None
    if status == "queued":
        assert payload is not None
        payload_ciphertext, payload_version = store.crypto.encrypt_text(
            payload.value,
            table="inbound_messages",
            record_id=provider_message_id,
            field="payload",
        )
    context = str(message.get("context_token") or "").strip()
    context_ciphertext = context_version = None
    if context and binding is not None:
        context_ciphertext, context_version = store.crypto.encrypt_text(
            context,
            table="inbound_messages",
            record_id=provider_message_id,
            field="context",
        )
        token_ciphertext, token_version = store.crypto.encrypt_text(
            context,
            table="context_tokens",
            record_id=f"{account_id}:{peer_hash}",
            field="token",
        )
        conn.execute(
            """
            INSERT INTO context_tokens VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(account_id, peer_lookup_hash) DO UPDATE SET
              token_ciphertext=excluded.token_ciphertext,
              token_key_version=excluded.token_key_version,
              updated_at=excluded.updated_at
            """,
            (account_id, peer_hash, token_ciphertext, token_version, now),
        )
    inbound_id = f"im_{uuid.uuid4().hex}"
    try:
        conn.execute(
            """
            INSERT INTO inbound_messages
              (inbound_id, account_id, binding_id, provider_message_id,
               payload_ciphertext, payload_key_version, context_ciphertext,
               context_key_version, status, rejection_reason, payload_kind,
               next_attempt_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                inbound_id,
                account_id,
                binding["binding_id"] if binding else None,
                provider_message_id,
                payload_ciphertext,
                payload_version,
                context_ciphertext,
                context_version,
                status,
                reason,
                payload.kind if status == "queued" and payload is not None else "text",
                now,
                now,
                now,
            ),
        )
    except Exception as exc:
        import sqlite3
        if isinstance(exc, sqlite3.IntegrityError) and "UNIQUE constraint" in str(exc):
            return 0
        raise
    return 1


def _media_descriptor(item: Mapping[str, Any], item_type: int) -> dict[str, Any] | None:
    item_fields = {
        ITEM_IMAGE: ("image", "image_item", "image.jpg"),
        ITEM_FILE: ("file", "file_item", "document.bin"),
        ITEM_VIDEO: ("video", "video_item", "video.mp4"),
    }
    kind, field, default_name = item_fields[item_type]
    detail = item.get(field)
    if not isinstance(detail, Mapping):
        return None
    media = detail.get("media")
    if not isinstance(media, Mapping):
        return None
    descriptor: dict[str, Any] = {
        "kind": kind,
        "file_name": sanitize_filename(detail.get("file_name") or default_name, default=default_name),
        "media": {},
    }
    for key in ("encrypt_query_param", "aes_key", "full_url"):
        value = media.get(key)
        if value is None:
            continue
        if not isinstance(value, str) or not value or len(value) > _MAX_MEDIA_FIELD_CHARS:
            return None
        descriptor["media"][key] = value
    if not (descriptor["media"].get("encrypt_query_param") or descriptor["media"].get("full_url")):
        return None
    size = detail.get("len", detail.get("file_size"))
    if size is not None and size != "":
        try:
            declared_size = int(size)
        except (TypeError, ValueError):
            return None
        if declared_size < 0 or declared_size > _MAX_DECLARED_MEDIA_BYTES:
            return None
        descriptor["size"] = declared_size
    return descriptor


def _voice_payload(item: Mapping[str, Any]) -> InboundPayload:
    voice = item.get("voice_item")
    if not isinstance(voice, Mapping):
        return InboundPayload("voice_invalid", "")
    transcript = voice.get("text")
    if isinstance(transcript, str) and transcript.strip():
        return InboundPayload("voice_transcript", transcript.strip())
    media = voice.get("media")
    if not isinstance(media, Mapping):
        return InboundPayload("voice_invalid", "")
    descriptor: dict[str, Any] = {"v": 1, "media": {}}
    for key in ("encrypt_query_param", "aes_key", "full_url"):
        value = media.get(key)
        if value is None:
            continue
        if not isinstance(value, str) or not value or len(value) > _MAX_MEDIA_FIELD_CHARS:
            return InboundPayload("voice_invalid", "")
        descriptor["media"][key] = value
    if not (descriptor["media"].get("encrypt_query_param") or descriptor["media"].get("full_url")):
        return InboundPayload("voice_invalid", "")
    for key in ("playtime", "sample_rate", "encode_type", "bits_per_sample"):
        value = voice.get(key)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return InboundPayload("voice_invalid", "")
        descriptor[key] = value
    serialized = json.dumps(descriptor, ensure_ascii=False, separators=(",", ":"))
    if len(serialized.encode("utf-8")) > _MAX_DESCRIPTOR_BYTES:
        return InboundPayload("voice_invalid", "")
    return InboundPayload("voice_media", serialized)


def _extract_payload(items: Any) -> InboundPayload | None:
    if not isinstance(items, list) or not items or len(items) > _MAX_ATTACHMENTS + 1:
        return None
    text = ""
    attachments: list[dict[str, Any]] = []
    voice_payload: InboundPayload | None = None
    for item in items:
        if not isinstance(item, Mapping):
            return InboundPayload("media_invalid", "")
        item_type = item.get("type")
        if item_type == ITEM_TEXT:
            value = (item.get("text_item") or {}).get("text")
            if not isinstance(value, str) or not value.strip() or text:
                return InboundPayload("media_invalid", "")
            text = value
        elif item_type == ITEM_VOICE:
            if len(items) != 1:
                return InboundPayload("voice_invalid", "")
            voice_payload = _voice_payload(item)
        elif item_type in {ITEM_IMAGE, ITEM_FILE, ITEM_VIDEO}:
            descriptor = _media_descriptor(item, item_type)
            if descriptor is None:
                return InboundPayload("media_invalid", "")
            attachments.append(descriptor)
        else:
            return None
    if voice_payload is not None:
        return voice_payload
    if attachments:
        serialized = json.dumps(
            {"v": 1, "text": text, "attachments": attachments},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if len(serialized.encode("utf-8")) > _MAX_DESCRIPTOR_BYTES:
            return InboundPayload("media_invalid", "")
        return InboundPayload("media", serialized)
    return InboundPayload("text", text) if text else None


class AccountPoller:
    def __init__(self, store: ChannelIdentityStore, session, lease: PollLease) -> None:
        self.store = store
        self.session = session
        self.lease = lease

    async def poll_once(self, *, timeout_ms: int) -> int:
        base_url, token, cursor = load_poll_account(self.store, self.lease)
        batch = await WeixinILinkClient(
            self.session,
            base_url=base_url,
            token=token,
        ).get_updates(cursor, timeout_ms=timeout_ms)
        return commit_update_batch(
            self.store,
            self.lease,
            messages=batch.messages,
            cursor=batch.cursor,
        )
