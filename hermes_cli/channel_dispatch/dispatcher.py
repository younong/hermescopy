"""Trusted channel inbound dispatcher and transactional outbox writer."""

from __future__ import annotations

import time
import uuid

from hermes_cli.channel_identity.owner_resolution import resolve_binding
from hermes_cli.channel_identity.store import ChannelIdentityStore
from hermes_cli.owner_worker.gateway_client import OwnerWorkerGatewayClient

from .session_router import open_binding_session


class ChannelDispatcher:
    def __init__(self, store: ChannelIdentityStore, supervisor, *, turn_timeout: float = 1800) -> None:
        self.store = store
        self.supervisor = supervisor
        self.turn_timeout = turn_timeout

    def claim_next(self, *, holder: str) -> dict | None:
        now = time.time()
        with self.store.write() as conn:
            row = conn.execute(
                """
                SELECT i.* FROM inbound_messages i
                WHERE i.status='queued' AND i.binding_id IS NOT NULL
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
                """
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
        async with OwnerWorkerGatewayClient(self.supervisor, owner) as client:
            live_session_id, _ = await open_binding_session(
                client,
                self.store,
                binding_id=claim["binding_id"],
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
        payload = event.get("params") or {}
        status = str(payload.get("status") or "")
        response_text = str(payload.get("text") or "")
        if status != "complete" or not response_text:
            self.fail_claim(claim["inbound_id"], holder, f"agent_{status or 'invalid'}")
            raise RuntimeError("owner Agent turn did not complete")
        outbound_id = f"om_{uuid.uuid4().hex}"
        client_message_id = f"hermes-ilink-{uuid.uuid4().hex}"
        response_ciphertext, response_version = self.store.crypto.encrypt_text(
            response_text,
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
