"""Fenced iLink account polling and atomic inbound queue commits."""

from __future__ import annotations

import json
import time
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
from hermes_cli.channel_connectors.contracts import (
    InboundBatch,
    NormalizedInboundEnvelope,
)
from hermes_cli.channel_connectors.polling import (
    PollLease,
    StalePollLeaseError,
    acquire_poll_lease as acquire_connector_poll_lease,
    commit_inbound_batch,
    load_poll_account as load_connector_poll_account,
)
from hermes_cli.channel_identity.store import ChannelIdentityStore

_MAX_DESCRIPTOR_BYTES = 64 * 1024
_MAX_MEDIA_FIELD_CHARS = 8 * 1024
_MAX_ATTACHMENTS = 8
_MAX_DECLARED_MEDIA_BYTES = 32 * 1024 * 1024
_PROVIDER = "weixin_ilink"


@dataclass(frozen=True)
class InboundPayload:
    kind: str
    value: str


def acquire_poll_lease(
    store: ChannelIdentityStore,
    *,
    account_id: str,
    holder: str,
) -> PollLease:
    try:
        return acquire_connector_poll_lease(
            store,
            provider=_PROVIDER,
            account_id=account_id,
            holder=holder,
        )
    except RuntimeError as exc:
        if str(exc) == "active connector account not found":
            raise RuntimeError("active iLink account not found") from exc
        raise


def load_poll_account(store: ChannelIdentityStore, lease: PollLease) -> tuple[str, str, str]:
    account = load_connector_poll_account(store, lease)
    base_url = account.credentials.get("base_url")
    token = account.credentials.get("bot_token")
    if not isinstance(base_url, str) or not isinstance(token, str):
        raise RuntimeError("iLink account credentials are unavailable")
    return base_url, token, account.cursor


def commit_update_batch(
    store: ChannelIdentityStore,
    lease: PollLease,
    *,
    messages: tuple[Mapping[str, Any], ...],
    cursor: str,
) -> int:
    """Atomically validate, enqueue, update context, and advance the cursor."""
    envelopes = tuple(_normalize_message(message) for message in messages)
    try:
        return commit_inbound_batch(
            store,
            lease,
            batch=InboundBatch(cursor=cursor, messages=envelopes),
        )
    except StalePollLeaseError as exc:
        raise StalePollLeaseError("iLink poll lease became stale") from exc


def _normalize_message(message: Mapping[str, Any]) -> NormalizedInboundEnvelope:
    payload = _extract_payload(message.get("item_list"))
    reason = None
    if message.get("room_id") or message.get("chat_room_id"):
        reason = "group_not_supported"
    elif payload is None:
        reason = "unsupported_message_type"
    elif payload.kind == "media_invalid":
        reason = "media_descriptor_invalid"
    elif payload.kind == "voice_invalid":
        reason = "voice_media_invalid"
    actor_id = str(message.get("from_user_id") or "")
    return NormalizedInboundEnvelope(
        provider_message_id=str(message.get("message_id") or ""),
        conversation_id=actor_id,
        actor_id=actor_id,
        payload_kind=payload.kind if reason is None and payload is not None else "text",
        payload=payload.value if reason is None and payload is not None else "",
        context_token=str(message.get("context_token") or "") or None,
        rejection_reason=reason,
    )


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
