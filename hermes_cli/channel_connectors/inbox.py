"""Shared transactional commit service for normalized channel inbound data."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

from hermes_cli.channel_identity.store import ChannelIdentityStore

from .contracts import NormalizedInboundEnvelope


@dataclass(frozen=True)
class InboundCommitResult:
    inserted: bool
    status: str
    rejection_reason: str | None
    duplicate: bool = False


class CanonicalInbox:
    def __init__(self, store: ChannelIdentityStore, *, provider: str) -> None:
        self.store = store
        self.provider = str(provider or "").strip()
        if not self.provider:
            raise ValueError("provider is required")

    def commit(
        self,
        conn,
        *,
        account_id: str,
        envelope: NormalizedInboundEnvelope,
        result: bool = False,
    ) -> int | InboundCommitResult:
        """Validate a normalized envelope and atomically enqueue or reject it."""
        now = time.time()
        provider_message_id = envelope.provider_message_id.strip()
        conversation_id = envelope.conversation_id.strip()
        peer_hash = (
            self.store.crypto.lookup_hash(
                f"conversation:{self.provider}", conversation_id
            )
            if conversation_id
            else ""
        )
        binding = (
            conn.execute(
                """
                SELECT b.binding_id, e.subject_lookup_hash,
                       CASE WHEN m.account_id IS NULL THEN 0 ELSE 1 END AS managed_feishu
                FROM channel_bindings b
                JOIN connector_accounts a ON a.account_id=b.account_id
                JOIN external_identities e ON e.external_identity_id=b.external_identity_id
                                          AND e.provider=a.provider
                LEFT JOIN managed_feishu_accounts m ON m.account_id=a.account_id
                                                  AND m.lifecycle_status='active'
                WHERE b.account_id=? AND b.peer_lookup_hash=? AND a.provider=?
                  AND b.status='active' AND a.status='active' AND e.status='active'
                """,
                (account_id, peer_hash, self.provider),
            ).fetchone()
            if peer_hash
            else None
        )
        status = "queued"
        if not provider_message_id:
            provider_message_id = f"rejected-{uuid.uuid4().hex}"
            reason = "missing_provider_message_id"
        elif binding is None:
            reason = "unknown_peer"
        elif not envelope.actor_id.strip():
            reason = "missing_actor_id"
        elif envelope.conversation_kind == "group":
            expected_admission = self.store.crypto.lookup_hash(
                f"group-admission:{self.provider}:{account_id}",
                f"{provider_message_id}:{envelope.actor_id.strip()}",
            )
            if not binding["managed_feishu"]:
                reason = "group_binding_unmanaged"
            elif envelope.rejection_reason is not None:
                reason = envelope.rejection_reason
            elif envelope.group_admission_token != expected_admission:
                reason = "group_admission_unverified"
            else:
                reason = None
        elif binding["subject_lookup_hash"] != self.store.crypto.lookup_hash(
            f"external-subject:{self.provider}", envelope.actor_id.strip()
        ):
            reason = "identity_mismatch"
        else:
            reason = envelope.rejection_reason
        if reason is not None:
            status = "rejected"

        payload_ciphertext = payload_version = None
        if status == "queued":
            payload_ciphertext, payload_version = self.store.crypto.encrypt_text(
                envelope.payload,
                table="inbound_messages",
                record_id=provider_message_id,
                field="payload",
            )

        context_ciphertext = context_version = None
        context = str(envelope.context_token or "").strip()
        token_ciphertext = token_version = None
        if status == "queued" and context:
            context_ciphertext, context_version = self.store.crypto.encrypt_text(
                context,
                table="inbound_messages",
                record_id=provider_message_id,
                field="context",
            )
            token_ciphertext, token_version = self.store.crypto.encrypt_text(
                context,
                table="context_tokens",
                record_id=f"{account_id}:{peer_hash}",
                field="token",
            )

        binding_id = binding["binding_id"] if binding else None
        try:
            conn.execute(
                """
                INSERT INTO inbound_messages
                  (inbound_id, account_id, binding_id, provider_message_id,
                   payload_ciphertext, payload_key_version, context_ciphertext,
                   context_key_version, status, rejection_reason, payload_kind,
                   dispatch_scope, profile_revision, next_attempt_at, created_at,
                   updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"im_{uuid.uuid4().hex}",
                    account_id,
                    binding_id,
                    provider_message_id,
                    payload_ciphertext,
                    payload_version,
                    context_ciphertext,
                    context_version,
                    status,
                    reason,
                    envelope.payload_kind if status == "queued" else "text",
                    str(envelope.dispatch_scope or "") if status == "queued" else "",
                    envelope.profile_revision if status == "queued" else None,
                    now,
                    now,
                    now,
                ),
            )
        except Exception as exc:
            import sqlite3

            if isinstance(exc, sqlite3.IntegrityError) and "UNIQUE constraint" in str(exc):
                if result:
                    return InboundCommitResult(
                        inserted=False,
                        status=status,
                        rejection_reason=reason,
                        duplicate=True,
                    )
                return 0
            raise
        if token_ciphertext is not None:
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
        if result:
            return InboundCommitResult(
                inserted=True,
                status=status,
                rejection_reason=reason,
            )
        return 1
