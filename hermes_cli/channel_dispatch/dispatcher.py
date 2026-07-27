"""Trusted channel inbound dispatcher and transactional outbox writer."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
import uuid
from pathlib import Path

from gateway.platforms.base import BasePlatformAdapter
from gateway.weixin_ilink.media import (
    WeixinMediaError,
    WeixinMediaLimits,
    download_and_decrypt_media,
    sanitize_filename,
    stage_media_file,
)
from hermes_cli.channel_identity.owner_resolution import resolve_binding
from hermes_cli.channel_identity.store import ChannelIdentityStore
from hermes_cli.owner_worker.gateway_client import OwnerWorkerGatewayClient

from .session_router import open_binding_session


class MediaDispatchError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(code)


class ChannelDispatcher:
    def __init__(
        self,
        store: ChannelIdentityStore,
        supervisor,
        *,
        turn_timeout: float = 1800,
        media_session=None,
        voice_config: dict | None = None,
        session=None,
    ) -> None:
        self.store = store
        self.supervisor = supervisor
        self.turn_timeout = turn_timeout
        self.media_session = media_session if media_session is not None else session
        config = voice_config or {}
        self.voice_enabled = bool(config.get("voice_enabled", True))
        self.voice_limits = WeixinMediaLimits(
            max_download_bytes=int(config.get("voice_max_download_bytes", 6 * 1024 * 1024)),
            timeout_seconds=float(config.get("voice_download_timeout_seconds", 60)),
        )
        self.media_limits = WeixinMediaLimits(
            max_download_bytes=int(config.get("media_max_download_bytes", 32 * 1024 * 1024)),
            timeout_seconds=float(config.get("media_download_timeout_seconds", 120)),
        )
        self.voice_max_duration = float(config.get("voice_max_duration_seconds", 300))
        self.voice_stt_timeout = float(config.get("voice_stt_timeout_seconds", 600))
        self.media_max_retries = int(config.get("voice_max_retries", 3))
        self.media_retry_base = float(config.get("voice_retry_base_seconds", 5))
        self.media_retry_max = float(config.get("voice_retry_max_seconds", 120))
        self.voice_temp_ttl = int(config.get("voice_temp_ttl_seconds", 3600))

    def claim_next(self, *, holder: str) -> dict | None:
        now = time.time()
        with self.store.write() as conn:
            row = conn.execute(
                """
                SELECT i.* FROM inbound_messages i
                WHERE i.status='queued' AND i.binding_id IS NOT NULL
                  AND i.next_attempt_at<=?
                  AND NOT EXISTS (
                    SELECT 1 FROM inbound_messages earlier
                    WHERE earlier.binding_id=i.binding_id
                      AND earlier.created_at<i.created_at
                      AND earlier.status IN ('queued','processing','outbound_pending')
                  )
                  AND NOT EXISTS (
                    SELECT 1 FROM outbound_messages o
                    WHERE o.binding_id=i.binding_id AND o.status IN ('queued','sending')
                  )
                ORDER BY i.created_at LIMIT 1
                """,
                (now,),
            ).fetchone()
            if row is None:
                return None
            changed = conn.execute(
                """
                UPDATE inbound_messages SET status='processing', claimed_by=?, claimed_at=?, updated_at=?
                WHERE inbound_id=? AND status='queued'
                """,
                (holder, now, now, row["inbound_id"]),
            ).rowcount
            return dict(row) if changed == 1 else None

    async def dispatch_claim(self, claim: dict, *, holder: str) -> str:
        owner, channel = resolve_binding(self.store, binding_id=claim["binding_id"])
        text = self.store.crypto.decrypt_text(
            claim["payload_ciphertext"],
            table="inbound_messages",
            record_id=claim["provider_message_id"],
            field="payload",
            version=claim["payload_key_version"],
        )
        payload_kind = str(claim.get("payload_kind") or "text")
        attachments: list[dict] = []
        if payload_kind == "media":
            try:
                envelope = json.loads(text)
                if (
                    not isinstance(envelope, dict)
                    or envelope.get("v") != 1
                    or not isinstance(envelope.get("text"), str)
                    or not isinstance(envelope.get("attachments"), list)
                    or not 1 <= len(envelope["attachments"]) <= 8
                ):
                    raise ValueError
                attachments = envelope["attachments"]
                text = envelope["text"]
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                error = MediaDispatchError("media_descriptor_invalid", retryable=False)
                self.fail_media_claim(claim, holder, error.code, retryable=False)
                raise error from exc
        if payload_kind in {"media", "voice_media"} and self.media_session is None:
            error = MediaDispatchError("media_disabled", retryable=False)
            self.fail_media_claim(claim, holder, error.code, retryable=False)
            raise error
        if payload_kind == "voice_media" and not self.voice_enabled:
            error = MediaDispatchError("voice_disabled", retryable=False)
            self.fail_media_claim(claim, holder, error.code, retryable=False)
            raise error
        context_token = None
        if claim["context_ciphertext"] is not None:
            context_token = self.store.crypto.decrypt_text(
                claim["context_ciphertext"],
                table="inbound_messages",
                record_id=claim["provider_message_id"],
                field="context",
                version=claim["context_key_version"],
            )
        turn_key = f"weixin-ilink:{claim['inbound_id']}"
        try:
            async with OwnerWorkerGatewayClient(self.supervisor, owner) as client:
                live_session_id, _ = await open_binding_session(
                    client,
                    self.store,
                    binding_id=claim["binding_id"],
                )
                if payload_kind == "voice_media":
                    text = await self._transcribe_voice(
                        claim,
                        holder=holder,
                        client=client,
                        session_id=live_session_id,
                        descriptor_text=text,
                    )
                elif attachments:
                    text = await self._attach_media(
                        claim,
                        owner=owner,
                        client=client,
                        session_id=live_session_id,
                        text=text,
                        attachments=attachments,
                    )
                await client.call(
                    "prompt.submit",
                    {
                        "session_id": live_session_id,
                        "text": text,
                        "idempotency_key": turn_key,
                    },
                )
                event = await client.wait_for_event(
                    "message.complete",
                    session_id=live_session_id,
                    timeout=self.turn_timeout,
                )
        except MediaDispatchError as exc:
            self.fail_media_claim(claim, holder, exc.code, retryable=exc.retryable)
            raise
        payload = event.get("params") or {}
        status = str(payload.get("status") or "")
        response_text = str(payload.get("text") or "")
        if status != "complete" or not response_text:
            self.fail_claim(claim["inbound_id"], holder, f"agent_{status or 'invalid'}")
            raise RuntimeError("owner Agent turn did not complete")
        outbound_id = f"om_{uuid.uuid4().hex}"
        client_message_id = f"hermes-ilink-{uuid.uuid4().hex}"
        outbound_payload = self._outbound_payload(response_text)
        response_ciphertext, response_version = self.store.crypto.encrypt_text(
            outbound_payload,
            table="outbound_messages",
            record_id=outbound_id,
            field="payload",
        )
        context_ciphertext = context_version = None
        if context_token:
            context_ciphertext, context_version = self.store.crypto.encrypt_text(
                context_token,
                table="outbound_messages",
                record_id=outbound_id,
                field="context",
            )
        with self.store.write() as conn:
            valid = conn.execute(
                "SELECT status, claimed_by FROM inbound_messages WHERE inbound_id=?",
                (claim["inbound_id"],),
            ).fetchone()
            if valid is None or valid["status"] != "processing" or valid["claimed_by"] != holder:
                raise RuntimeError("inbound dispatch claim is stale")
            conn.execute(
                """
                INSERT INTO outbound_messages
                  (outbound_id, inbound_id, account_id, binding_id, client_message_id,
                   payload_ciphertext, payload_key_version, context_ciphertext,
                   context_key_version, status, next_attempt_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?)
                """,
                (
                    outbound_id,
                    claim["inbound_id"],
                    channel.account_id,
                    channel.binding_id,
                    client_message_id,
                    response_ciphertext,
                    response_version,
                    context_ciphertext,
                    context_version,
                    time.time(),
                    time.time(),
                    time.time(),
                ),
            )
            conn.execute(
                """
                UPDATE inbound_messages SET status='outbound_pending', payload_ciphertext=NULL,
                    payload_key_version=NULL, context_ciphertext=NULL, context_key_version=NULL,
                    claimed_by=NULL, claimed_at=NULL, updated_at=? WHERE inbound_id=?
                """,
                (time.time(), claim["inbound_id"]),
            )
        return outbound_id

    async def _attach_media(
        self,
        claim: dict,
        *,
        owner,
        client,
        session_id: str,
        text: str,
        attachments: list[dict],
    ) -> str:
        attachment_root = (
            owner.owner_home
            / "workspaces"
            / "default"
            / ".hermes"
            / "weixin-attachments"
            / claim["inbound_id"]
        )
        references: list[str] = []
        for index, descriptor in enumerate(attachments, start=1):
            if not isinstance(descriptor, dict) or descriptor.get("kind") not in {
                "image", "video", "file"
            }:
                raise MediaDispatchError("media_descriptor_invalid", retryable=False)
            name = sanitize_filename(descriptor.get("file_name"), default="document.bin")
            media_descriptor = {"v": 1, "media": descriptor.get("media")}
            try:
                data = await download_and_decrypt_media(
                    self.media_session,
                    descriptor=media_descriptor,
                    limits=self.media_limits,
                )
            except WeixinMediaError as exc:
                raise MediaDispatchError(exc.code, retryable=exc.retryable) from exc
            destination = stage_media_file(data, attachment_root / f"{index}-{name}")
            try:
                if descriptor["kind"] == "image":
                    result = await client.call(
                        "image.attach",
                        {"session_id": session_id, "path": str(destination)},
                    )
                    reference = str((result or {}).get("text") or f"[User attached image: {name}]")
                else:
                    result = await client.call(
                        "file.attach",
                        {"session_id": session_id, "path": str(destination), "name": name},
                    )
                    reference = str((result or {}).get("ref_text") or "")
            except Exception as exc:
                raise MediaDispatchError("owner_worker_unavailable", retryable=True) from exc
            if not reference:
                raise MediaDispatchError("media_attach_failed", retryable=True)
            references.append(reference)
        return "\n\n".join(part for part in [text.strip(), *references] if part)

    def _outbound_payload(self, response_text: str) -> str:
        media, cleaned = BasePlatformAdapter.extract_media(response_text)
        media = BasePlatformAdapter.filter_media_delivery_paths(media)
        _images, cleaned = BasePlatformAdapter.extract_images(cleaned)
        local_files, cleaned = BasePlatformAdapter.extract_local_files(cleaned)
        local_files = BasePlatformAdapter.filter_local_delivery_paths(local_files)
        paths: list[dict[str, object]] = []
        seen: set[str] = set()
        for path, is_voice in [*media, *((path, False) for path in local_files)]:
            if path in seen:
                continue
            seen.add(path)
            paths.append({"path": path, "voice": bool(is_voice)})
        if not paths:
            return response_text
        return json.dumps(
            {"v": 1, "text": cleaned, "media": paths},
            ensure_ascii=False,
            separators=(",", ":"),
        )

    async def _transcribe_voice(
        self,
        claim: dict,
        *,
        holder: str,
        client,
        session_id: str,
        descriptor_text: str,
    ) -> str:
        try:
            descriptor = json.loads(descriptor_text)
            if not isinstance(descriptor, dict):
                raise ValueError
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise MediaDispatchError("voice_media_invalid", retryable=False) from exc
        playtime = descriptor.get("playtime")
        if isinstance(playtime, int) and playtime > self.voice_max_duration * 1000:
            raise MediaDispatchError("voice_too_long", retryable=False)
        try:
            media = await download_and_decrypt_media(
                self.media_session,
                descriptor=descriptor,
                limits=self.voice_limits,
            )
        except WeixinMediaError as exc:
            raise MediaDispatchError(exc.code, retryable=exc.retryable) from exc
        request_key = f"weixin-ilink:{claim['inbound_id']}"
        finished = False
        try:
            await client.call(
                "channel.voice.begin",
                {
                    "session_id": session_id,
                    "request_key": request_key,
                    "size": len(media),
                    "sha256": hashlib.sha256(media).hexdigest(),
                    "temp_ttl_seconds": self.voice_temp_ttl,
                },
            )
            offset = 0
            for start in range(0, len(media), 256 * 1024):
                chunk = media[start:start + 256 * 1024]
                result = await client.call(
                    "channel.voice.chunk",
                    {
                        "session_id": session_id,
                        "request_key": request_key,
                        "offset": offset,
                        "data": base64.b64encode(chunk).decode("ascii"),
                    },
                )
                offset = int(result.get("offset", -1))
                if offset != start + len(chunk):
                    raise MediaDispatchError("voice_upload_failed", retryable=True)
            result = await asyncio.wait_for(
                client.call(
                    "channel.voice.finish",
                    {
                        "session_id": session_id,
                        "request_key": request_key,
                        "timeout_seconds": self.voice_stt_timeout,
                        "max_duration_seconds": self.voice_max_duration,
                    },
                ),
                timeout=self.voice_stt_timeout + 30,
            )
            finished = True
        except MediaDispatchError:
            raise
        except Exception as exc:
            raise MediaDispatchError("owner_worker_unavailable", retryable=True) from exc
        finally:
            if not finished:
                try:
                    await client.call(
                        "channel.voice.abort",
                        {"session_id": session_id, "request_key": request_key},
                    )
                except Exception:
                    pass
        if not result.get("success"):
            raise MediaDispatchError(
                str(result.get("code") or "stt_failed"),
                retryable=bool(result.get("retryable")),
            )
        transcript = str(result.get("transcript") or "").strip()
        if not transcript:
            raise MediaDispatchError("stt_empty", retryable=False)
        self._checkpoint_transcript(claim, holder, transcript)
        claim["payload_kind"] = "voice_transcript"
        return transcript

    def _checkpoint_transcript(self, claim: dict, holder: str, transcript: str) -> None:
        ciphertext, version = self.store.crypto.encrypt_text(
            transcript,
            table="inbound_messages",
            record_id=claim["provider_message_id"],
            field="payload",
        )
        with self.store.write() as conn:
            changed = conn.execute(
                """
                UPDATE inbound_messages
                SET payload_ciphertext=?, payload_key_version=?, payload_kind='voice_transcript',
                    last_error=NULL, updated_at=?
                WHERE inbound_id=? AND status='processing' AND claimed_by=?
                  AND payload_kind='voice_media'
                """,
                (ciphertext, version, time.time(), claim["inbound_id"], holder),
            ).rowcount
            if changed != 1:
                raise MediaDispatchError("media_claim_stale", retryable=True)

    def fail_media_claim(self, claim: dict, holder: str, reason: str, *, retryable: bool) -> None:
        attempts = int(claim.get("attempts") or 0) + 1
        terminal = not retryable or attempts > self.media_max_retries
        now = time.time()
        delay = min(self.media_retry_max, self.media_retry_base * (2 ** max(0, attempts - 1)))
        with self.store.write() as conn:
            changed = conn.execute(
                """
                UPDATE inbound_messages
                SET status=?, attempts=?, next_attempt_at=?, last_error=?, rejection_reason=?,
                    claimed_by=NULL, claimed_at=NULL,
                    payload_ciphertext=CASE WHEN ? THEN NULL ELSE payload_ciphertext END,
                    payload_key_version=CASE WHEN ? THEN NULL ELSE payload_key_version END,
                    context_ciphertext=CASE WHEN ? THEN NULL ELSE context_ciphertext END,
                    context_key_version=CASE WHEN ? THEN NULL ELSE context_key_version END,
                    updated_at=?
                WHERE inbound_id=? AND status='processing' AND claimed_by=?
                """,
                (
                    "failed" if terminal else "queued",
                    attempts,
                    now if terminal else now + delay,
                    reason,
                    reason if terminal else None,
                    terminal,
                    terminal,
                    terminal,
                    terminal,
                    now,
                    claim["inbound_id"],
                    holder,
                ),
            ).rowcount
        if changed != 1:
            raise MediaDispatchError("media_claim_stale", retryable=True)

    def fail_claim(self, inbound_id: str, holder: str, reason: str) -> None:
        with self.store.write() as conn:
            conn.execute(
                """
                UPDATE inbound_messages SET status='failed', rejection_reason=?,
                    claimed_by=NULL, claimed_at=NULL, updated_at=?
                WHERE inbound_id=? AND status='processing' AND claimed_by=?
                """,
                (reason, time.time(), inbound_id, holder),
            )
