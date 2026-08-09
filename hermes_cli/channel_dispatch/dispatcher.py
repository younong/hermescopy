"""Trusted provider-neutral dispatch and transactional outbox writing."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path

from hermes_cli.channel_connectors.contracts import (
    MediaMaterializationRequest,
    MediaMaterializer,
    OutboundEncoder,
)
from hermes_cli.channel_identity.owner_resolution import resolve_binding
from hermes_cli.channel_identity.store import ChannelIdentityStore
from hermes_cli.owner_worker.gateway_client import OwnerWorkerGatewayClient
from hermes_cli.owner_worker.tokens import CONNECTION_PURPOSE_RETAINED_CHANNEL

from .session_router import open_binding_session


def _identity_outbound(text: str) -> str:
    return text


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
        provider: str,
        account_id: str | None = None,
        turn_timeout: float = 1800,
        media_materializer: MediaMaterializer | None = None,
        outbound_encoder: OutboundEncoder | None = None,
        media_config: dict | None = None,
        dispatch_config: dict | None = None,
    ) -> None:
        self.store = store
        self.supervisor = supervisor
        self.provider = str(provider or "").strip()
        if not self.provider:
            raise ValueError("provider is required")
        self.account_id = str(account_id or "").strip() or None
        expected_global_home = store.global_home
        supervisor_global_home = getattr(supervisor, "global_home", expected_global_home)
        if Path(supervisor_global_home).resolve() != expected_global_home:
            raise RuntimeError("channel store and Owner Worker supervisor global homes differ")
        self.turn_timeout = turn_timeout
        self.media_materializer = media_materializer
        self.outbound_encoder = outbound_encoder or _identity_outbound
        config = media_config or {}
        self.media_max_retries = int(config.get("media_max_retries", config.get("voice_max_retries", 3)))
        self.media_retry_base = float(
            config.get("media_retry_base_seconds", config.get("voice_retry_base_seconds", 5))
        )
        self.media_retry_max = float(
            config.get("media_retry_max_seconds", config.get("voice_retry_max_seconds", 120))
        )
        dispatch = dispatch_config or config
        self.dispatch_max_retries = int(dispatch.get("dispatch_max_retries", 8))
        self.dispatch_retry_base = float(dispatch.get("dispatch_retry_base_seconds", 2))
        self.dispatch_retry_max = float(dispatch.get("dispatch_retry_max_seconds", 120))

    def claim_next(self, *, holder: str) -> dict | None:
        now = time.time()
        with self.store.write() as conn:
            row = conn.execute(
                """
                SELECT i.* FROM inbound_messages i
                JOIN channel_bindings b ON b.binding_id=i.binding_id
                                       AND b.account_id=i.account_id
                JOIN connector_accounts a ON a.account_id=i.account_id
                JOIN external_identities e ON e.external_identity_id=b.external_identity_id
                                          AND e.provider=a.provider
                WHERE i.status='queued' AND i.binding_id IS NOT NULL
                  AND i.binding_sequence IS NOT NULL AND i.next_attempt_at<=?
                  AND a.provider=? AND (? IS NULL OR i.account_id=?)
                  AND b.status='active' AND a.status='active' AND e.status='active'
                  AND NOT EXISTS (
                    SELECT 1 FROM inbound_messages earlier
                    WHERE earlier.binding_id=i.binding_id
                      AND earlier.dispatch_scope=i.dispatch_scope
                      AND earlier.binding_sequence<i.binding_sequence
                      AND earlier.status IN ('queued','processing','outbound_pending')
                  )
                  AND NOT EXISTS (
                    SELECT 1 FROM outbound_messages o
                    WHERE o.binding_id=i.binding_id
                      AND o.dispatch_scope=i.dispatch_scope
                      AND o.status IN ('queued','sending')
                  )
                ORDER BY i.created_at, i.binding_sequence LIMIT 1
                """,
                (now, self.provider, self.account_id, self.account_id),
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
        if channel.provider != self.provider or (
            self.account_id is not None and channel.account_id != self.account_id
        ):
            self.fail_claim(
                claim["inbound_id"], holder, "provider_mismatch", retryable=False
            )
            raise RuntimeError("channel claim provider mismatch")
        try:
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
                envelope = json.loads(text)
                if (
                    not isinstance(envelope, dict)
                    or envelope.get("v") != 1
                    or not isinstance(envelope.get("text"), str)
                    or not isinstance(envelope.get("attachments"), list)
                    or not 1 <= len(envelope["attachments"]) <= 8
                ):
                    raise ValueError("media descriptor invalid")
                attachments = envelope["attachments"]
                text = envelope["text"]
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            self.fail_claim(
                claim["inbound_id"], holder, "payload_invalid", retryable=False
            )
            raise RuntimeError("channel payload is invalid") from exc
        except Exception:
            self.fail_claim(
                claim["inbound_id"], holder, "payload_unavailable", retryable=True
            )
            raise
        if payload_kind in {"media", "voice_media"} and self.media_materializer is None:
            error = MediaDispatchError("media_disabled", retryable=False)
            self.fail_media_claim(claim, holder, error.code, retryable=False)
            raise error

        context_token = None
        if claim["context_ciphertext"] is not None:
            try:
                context_token = self.store.crypto.decrypt_text(
                    claim["context_ciphertext"],
                    table="inbound_messages",
                    record_id=claim["provider_message_id"],
                    field="context",
                    version=claim["context_key_version"],
                )
            except Exception:
                self.fail_claim(
                    claim["inbound_id"], holder, "context_unavailable", retryable=True
                )
                raise
        provider_slug = channel.provider.replace("_", "-")
        turn_key = f"channel:{provider_slug}:{claim['inbound_id']}"
        try:
            async with OwnerWorkerGatewayClient(
                self.supervisor,
                owner,
                connection_purpose=CONNECTION_PURPOSE_RETAINED_CHANNEL,
            ) as client:
                live_session_id, _ = await open_binding_session(
                    client,
                    self.store,
                    binding_id=claim["binding_id"],
                    dispatch_scope=str(claim.get("dispatch_scope") or ""),
                    profile_revision=claim.get("profile_revision"),
                    conversation_kind=claim.get("conversation_kind"),
                    conversation_id=channel.conversation_id,
                    thread_id=str(claim.get("thread_id") or ""),
                    source=provider_slug,
                    title=f"{channel.provider} channel",
                )
                if payload_kind in {"media", "voice_media"}:
                    assert self.media_materializer is not None
                    text = await self.media_materializer(
                        MediaMaterializationRequest(
                            claim=claim,
                            owner=owner,
                            client=client,
                            session_id=live_session_id,
                            payload_kind=payload_kind,
                            text=text,
                            attachments=attachments,
                        )
                    )
                    if payload_kind == "voice_media":
                        self._checkpoint_transcript(claim, holder, text)
                        claim["payload_kind"] = "voice_transcript"
                try:
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
                except asyncio.CancelledError:
                    try:
                        await asyncio.shield(
                            client.call(
                                "session.interrupt",
                                {"session_id": live_session_id},
                            )
                        )
                    except Exception:
                        pass
                    raise
        except MediaDispatchError as exc:
            self.fail_media_claim(claim, holder, exc.code, retryable=exc.retryable)
            raise
        except Exception:
            self.fail_claim(
                claim["inbound_id"], holder, "owner_worker_unavailable", retryable=True
            )
            raise
        payload = event.get("params") or {}
        status = str(payload.get("status") or "")
        response_text = str(payload.get("text") or "")
        if status != "complete" or not response_text:
            self.fail_claim(
                claim["inbound_id"],
                holder,
                f"agent_{status or 'invalid'}",
                retryable=status in {"error", "cancelled", "interrupted", "timeout", ""},
            )
            raise RuntimeError("owner Agent turn did not complete")
        outbound_id = f"om_{uuid.uuid4().hex}"
        client_message_id = f"hermes-{provider_slug}-{uuid.uuid4().hex}"
        outbound_payload = self.outbound_encoder(response_text)
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
        now = time.time()
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
                  (outbound_id, inbound_id, account_id, binding_id, provider,
                   source_kind, source_id, binding_sequence, dispatch_scope,
                   client_message_id, payload_ciphertext, payload_key_version,
                   context_ciphertext, context_key_version, status,
                   next_attempt_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'inbound', ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?)
                """,
                (
                    outbound_id,
                    claim["inbound_id"],
                    channel.account_id,
                    channel.binding_id,
                    channel.provider,
                    f"inbound:{claim['inbound_id']}",
                    claim["binding_sequence"],
                    str(claim.get("dispatch_scope") or ""),
                    client_message_id,
                    response_ciphertext,
                    response_version,
                    context_ciphertext,
                    context_version,
                    now,
                    now,
                    now,
                ),
            )
            conn.execute(
                """
                UPDATE inbound_messages SET status='outbound_pending', payload_ciphertext=NULL,
                    payload_key_version=NULL, context_ciphertext=NULL, context_key_version=NULL,
                    claimed_by=NULL, claimed_at=NULL, updated_at=? WHERE inbound_id=?
                """,
                (now, claim["inbound_id"]),
            )
        return outbound_id

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

    def release_claim(self, inbound_id: str, holder: str, *, reason: str) -> bool:
        """Return one exact in-flight claim to its account queue unchanged."""
        now = time.time()
        with self.store.write() as conn:
            changed = conn.execute(
                """
                UPDATE inbound_messages
                SET status='queued', next_attempt_at=?, last_error=?,
                    claimed_by=NULL, claimed_at=NULL, updated_at=?
                WHERE inbound_id=? AND status='processing' AND claimed_by=?
                  AND (? IS NULL OR account_id=?)
                """,
                (
                    now,
                    reason,
                    now,
                    inbound_id,
                    holder,
                    self.account_id,
                    self.account_id,
                ),
            ).rowcount
        return changed == 1

    def fail_claim(
        self,
        inbound_id: str,
        holder: str,
        reason: str,
        *,
        retryable: bool = True,
    ) -> None:
        now = time.time()
        with self.store.write() as conn:
            row = conn.execute(
                "SELECT attempts FROM inbound_messages "
                "WHERE inbound_id=? AND status='processing' AND claimed_by=?",
                (inbound_id, holder),
            ).fetchone()
            if row is None:
                return
            attempts = int(row["attempts"] or 0) + 1
            terminal = not retryable or attempts >= self.dispatch_max_retries
            delay = min(
                self.dispatch_retry_max,
                self.dispatch_retry_base * (2 ** max(0, attempts - 1)),
            )
            conn.execute(
                """
                UPDATE inbound_messages SET status=?, attempts=?, next_attempt_at=?,
                    rejection_reason=?, last_error=?, claimed_by=NULL, claimed_at=NULL,
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
                    reason if terminal else None,
                    reason,
                    terminal,
                    terminal,
                    terminal,
                    terminal,
                    now,
                    inbound_id,
                    holder,
                ),
            )
