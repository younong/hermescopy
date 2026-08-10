"""Canonical encrypted channel outbox enqueue operations."""

from __future__ import annotations

import hashlib
import hmac
import time

from hermes_cli.channel_connectors.contracts import OutboundDelivery
from hermes_cli.channel_identity.store import ChannelIdentityStore


def claim_outbound(
    store: ChannelIdentityStore,
    *,
    provider: str,
    holder: str,
    account_id: str | None = None,
) -> OutboundDelivery | None:
    """Claim and decrypt the next ordered delivery for one exact provider."""
    exact_provider = str(provider or "").strip()
    exact_holder = str(holder or "").strip()
    exact_account = str(account_id or "").strip() or None
    if not exact_provider or not exact_holder:
        raise ValueError("provider and holder are required")
    now = time.time()
    with store.write() as conn:
        row = conn.execute(
            """
            SELECT o.*, a.credential_version,
                   b.peer_ciphertext, b.peer_key_version
            FROM outbound_messages o
            LEFT JOIN inbound_messages i ON i.inbound_id=o.inbound_id
                                        AND i.account_id=o.account_id
                                        AND i.binding_id=o.binding_id
            JOIN connector_accounts a ON a.account_id=o.account_id
                                     AND a.provider=? AND a.status='active'
            JOIN channel_bindings b ON b.binding_id=o.binding_id
                                   AND b.account_id=o.account_id
                                   AND b.status='active'
            JOIN external_identities e ON e.external_identity_id=b.external_identity_id
                                      AND e.provider=a.provider AND e.status='active'
            WHERE (o.provider=? OR (o.provider IS NULL AND a.provider=?))
              AND (? IS NULL OR o.account_id=?)
              AND o.status='queued' AND o.next_attempt_at<=?
              AND NOT EXISTS (
                SELECT 1 FROM inbound_messages earlier
                WHERE earlier.binding_id=o.binding_id
                  AND earlier.dispatch_scope=o.dispatch_scope
                  AND earlier.binding_sequence<o.binding_sequence
                  AND earlier.status IN ('queued','processing')
              )
              AND NOT EXISTS (
                SELECT 1 FROM outbound_messages earlier_out
                WHERE earlier_out.binding_id=o.binding_id
                  AND earlier_out.dispatch_scope=o.dispatch_scope
                  AND earlier_out.binding_sequence<COALESCE(o.binding_sequence, i.binding_sequence)
                  AND earlier_out.status IN ('queued','sending')
              )
            ORDER BY COALESCE(o.binding_sequence, i.binding_sequence), o.created_at
            LIMIT 1
            """,
            (
                exact_provider,
                exact_provider,
                exact_provider,
                exact_account,
                exact_account,
                now,
            ),
        ).fetchone()
        if row is None:
            return None
        changed = conn.execute(
            """
            UPDATE outbound_messages
            SET status='sending', claimed_by=?, claimed_at=?, attempts=attempts+1,
                chunk_attempts=chunk_attempts+1, updated_at=?
            WHERE outbound_id=? AND status='queued'
            """,
            (exact_holder, now, now, row["outbound_id"]),
        ).rowcount
        if changed != 1:
            return None
    try:
        payload = store.crypto.decrypt_text(
            row["payload_ciphertext"],
            table="outbound_messages",
            record_id=row["outbound_id"],
            field="payload",
            version=row["payload_key_version"],
        )
        conversation_id = store.crypto.decrypt_text(
            row["peer_ciphertext"],
            table="channel_bindings",
            record_id=row["binding_id"],
            field="peer",
            version=row["peer_key_version"],
        )
        context = None
        if row["context_ciphertext"] is not None:
            context = store.crypto.decrypt_text(
                row["context_ciphertext"],
                table="outbound_messages",
                record_id=row["outbound_id"],
                field="context",
                version=row["context_key_version"],
            )
    except Exception as exc:
        release_outbound_claim(
            store,
            outbound_id=row["outbound_id"],
            holder=exact_holder,
            error=f"claim_error:{type(exc).__name__}",
        )
        raise
    return OutboundDelivery(
        provider=exact_provider,
        account_id=row["account_id"],
        binding_id=row["binding_id"],
        conversation_id=conversation_id,
        outbound_id=row["outbound_id"],
        client_message_id=row["client_message_id"],
        payload=payload,
        credential_version=int(row["credential_version"]),
        next_part_index=int(row["next_chunk_index"]),
        part_attempts=int(row["chunk_attempts"]) + 1,
        context_token=context,
        source_kind=str(row["source_kind"] or "") or None,
    )


def set_outbound_part_count(
    store: ChannelIdentityStore,
    delivery: OutboundDelivery,
    *,
    holder: str,
    part_count: int,
) -> None:
    if part_count < 1 or delivery.next_part_index >= part_count:
        raise RuntimeError("outbound part progress is invalid")
    with store.write() as conn:
        changed = conn.execute(
            """
            UPDATE outbound_messages SET chunk_count=?, updated_at=?
            WHERE outbound_id=? AND (provider=? OR provider IS NULL)
              AND status='sending' AND claimed_by=? AND next_chunk_index=?
              AND (chunk_count IS NULL OR chunk_count=?)
            """,
            (
                part_count,
                time.time(),
                delivery.outbound_id,
                delivery.provider,
                holder,
                delivery.next_part_index,
                part_count,
            ),
        ).rowcount
    if changed != 1:
        raise RuntimeError("outbound send claim is stale")


def release_outbound_claim(
    store: ChannelIdentityStore,
    *,
    outbound_id: str,
    holder: str,
    error: str,
    next_attempt_at: float | None = None,
) -> bool:
    now = time.time()
    due = now if next_attempt_at is None else next_attempt_at
    with store.write() as conn:
        changed = conn.execute(
            """
            UPDATE outbound_messages
            SET status='queued', claimed_by=NULL, claimed_at=NULL,
                next_attempt_at=?, last_error=?, updated_at=?
            WHERE outbound_id=? AND status='sending' AND claimed_by=?
            """,
            (due, error, now, outbound_id, holder),
        ).rowcount
    return changed == 1


def _finish_outbound_failure(
    store: ChannelIdentityStore,
    delivery: OutboundDelivery,
    *,
    holder: str,
    error: str,
    status: str,
) -> bool:
    if status not in {"failed", "ambiguous"}:
        raise ValueError("outbound failure status is invalid")
    now = time.time()
    with store.write() as conn:
        changed = conn.execute(
            """
            UPDATE outbound_messages
            SET status=?, claimed_by=NULL, claimed_at=NULL, last_error=?,
                failed_chunk_index=?, payload_ciphertext=NULL,
                payload_key_version=NULL, context_ciphertext=NULL,
                context_key_version=NULL, updated_at=?
            WHERE outbound_id=? AND (provider=? OR provider IS NULL) AND status='sending' AND claimed_by=?
              AND next_chunk_index=?
            """,
            (
                status,
                error,
                delivery.next_part_index,
                now,
                delivery.outbound_id,
                delivery.provider,
                holder,
                delivery.next_part_index,
            ),
        ).rowcount
        if changed == 1:
            conn.execute(
                """
                UPDATE inbound_messages SET status='failed', rejection_reason=?, updated_at=?
                WHERE inbound_id=(
                    SELECT inbound_id FROM outbound_messages WHERE outbound_id=?
                )
                """,
                (
                    "outbound_ambiguous" if status == "ambiguous" else "outbound_failed",
                    now,
                    delivery.outbound_id,
                ),
            )
    return changed == 1


def fail_outbound(
    store: ChannelIdentityStore,
    delivery: OutboundDelivery,
    *,
    holder: str,
    error: str,
) -> bool:
    return _finish_outbound_failure(
        store, delivery, holder=holder, error=error, status="failed"
    )


def mark_outbound_ambiguous(
    store: ChannelIdentityStore,
    delivery: OutboundDelivery,
    *,
    holder: str,
    error: str,
) -> bool:
    """Terminally stop replay when a provider side effect may have occurred."""
    return _finish_outbound_failure(
        store, delivery, holder=holder, error=error, status="ambiguous"
    )


def advance_outbound(
    store: ChannelIdentityStore,
    delivery: OutboundDelivery,
    *,
    holder: str,
    part_count: int,
    provider_message_id: str | None = None,
    next_attempt_at: float | None = None,
) -> bool:
    next_index = delivery.next_part_index + 1
    exact_receipt = str(provider_message_id or "").strip()
    now = time.time()
    with store.write() as conn:
        if next_index < part_count:
            changed = conn.execute(
                """
                UPDATE outbound_messages
                SET status='queued', next_chunk_index=?, chunk_attempts=0,
                    next_attempt_at=?, claimed_by=NULL, claimed_at=NULL,
                    last_error=NULL, failed_chunk_index=NULL, updated_at=?
                WHERE outbound_id=? AND (provider=? OR provider IS NULL) AND status='sending'
                  AND claimed_by=? AND next_chunk_index=? AND chunk_count=?
                """,
                (
                    next_index,
                    now if next_attempt_at is None else next_attempt_at,
                    now,
                    delivery.outbound_id,
                    delivery.provider,
                    holder,
                    delivery.next_part_index,
                    part_count,
                ),
            ).rowcount
        else:
            changed = conn.execute(
                """
                UPDATE outbound_messages
                SET status='delivered', next_chunk_index=?, payload_ciphertext=NULL,
                    payload_key_version=NULL, context_ciphertext=NULL,
                    context_key_version=NULL, claimed_by=NULL, claimed_at=NULL,
                    chunk_attempts=0, last_error=NULL, failed_chunk_index=NULL,
                    updated_at=?
                WHERE outbound_id=? AND (provider=? OR provider IS NULL) AND status='sending'
                  AND claimed_by=? AND next_chunk_index=? AND chunk_count=?
                """,
                (
                    next_index,
                    now,
                    delivery.outbound_id,
                    delivery.provider,
                    holder,
                    delivery.next_part_index,
                    part_count,
                ),
            ).rowcount
            if changed == 1:
                conn.execute(
                    """
                    UPDATE inbound_messages SET status='completed', updated_at=?
                    WHERE inbound_id=(
                        SELECT inbound_id FROM outbound_messages WHERE outbound_id=?
                    )
                    """,
                    (now, delivery.outbound_id),
                )
        if changed != 1:
            return False
        if exact_receipt:
            receipt_hash = store.crypto.lookup_hash(
                f"provider-message:{delivery.provider}:{delivery.account_id}",
                exact_receipt,
            )
            conn.execute(
                """
                INSERT INTO outbound_receipts
                  (account_id, binding_id, outbound_id, part_index,
                   provider_message_lookup_hash, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    delivery.account_id,
                    delivery.binding_id,
                    delivery.outbound_id,
                    delivery.next_part_index,
                    receipt_hash,
                    now,
                ),
            )
    return True


def recover_stale_outbound(
    store: ChannelIdentityStore,
    *,
    provider: str,
    claimed_before: float,
    account_id: str | None = None,
) -> int:
    exact_account = str(account_id or "").strip() or None
    now = time.time()
    with store.write() as conn:
        return conn.execute(
            """
            UPDATE outbound_messages
            SET status='queued', claimed_by=NULL, claimed_at=NULL,
                next_attempt_at=?, updated_at=?
            WHERE status='sending' AND claimed_at<?
              AND (? IS NULL OR account_id=?)
              AND account_id IN (
                SELECT account_id FROM connector_accounts WHERE provider=?
              )
            """,
            (
                now,
                now,
                claimed_before,
                exact_account,
                exact_account,
                provider,
            ),
        ).rowcount


class ChannelOutbox:
    """Enqueue trusted non-inbound deliveries into the shared encrypted outbox."""

    def __init__(self, store: ChannelIdentityStore) -> None:
        self.store = store

    def enqueue_cron_result(
        self,
        *,
        owner_key: str,
        binding_id: str,
        fire_id: str,
        payload: str,
    ) -> str:
        exact_owner = str(owner_key or "").strip()
        exact_binding = str(binding_id or "").strip()
        stable_fire_id = str(fire_id or "").strip()
        text = str(payload or "")
        if not exact_owner or not exact_binding or not stable_fire_id:
            raise ValueError("owner_key, binding_id, and fire_id are required")
        if not text.strip():
            raise ValueError("cron delivery payload is empty")
        if len(text.encode("utf-8")) > 256_000:
            raise ValueError("cron delivery payload exceeds 256000 bytes")

        source_id = f"cron:{stable_fire_id}"
        digest = hashlib.sha256(source_id.encode("utf-8")).hexdigest()
        outbound_id = f"om_cron_{digest[:32]}"
        client_message_id = f"hermes-cron-{digest}"
        ciphertext, key_version = self.store.crypto.encrypt_text(
            text,
            table="outbound_messages",
            record_id=outbound_id,
            field="payload",
        )
        now = time.time()
        with self.store.write() as conn:
            binding = conn.execute(
                """
                SELECT b.binding_id, b.account_id, b.peer_lookup_hash,
                       a.provider, o.owner_key, ct.token_ciphertext,
                       ct.token_key_version
                FROM channel_bindings b
                JOIN connector_accounts a ON a.account_id=b.account_id
                JOIN external_identities e ON e.external_identity_id=b.external_identity_id
                                          AND e.provider=a.provider
                JOIN canonical_users u ON u.canonical_user_id=e.canonical_user_id
                JOIN owner_bindings o ON o.canonical_user_id=u.canonical_user_id
                LEFT JOIN context_tokens ct ON ct.account_id=b.account_id
                                           AND ct.peer_lookup_hash=b.peer_lookup_hash
                WHERE b.binding_id=? AND b.status='active' AND a.status='active'
                  AND e.status='active' AND u.status='active'
                """,
                (exact_binding,),
            ).fetchone()
            if binding is None:
                raise RuntimeError("channel binding is unavailable")
            if not hmac.compare_digest(str(binding["owner_key"]), exact_owner):
                raise RuntimeError("channel binding belongs to another Owner")
            existing = conn.execute(
                "SELECT outbound_id, binding_id FROM outbound_messages WHERE source_id=?",
                (source_id,),
            ).fetchone()
            if existing is not None:
                if not hmac.compare_digest(str(existing["binding_id"]), exact_binding):
                    raise RuntimeError("cron fire is already bound to another channel")
                return str(existing["outbound_id"])
            conn.execute(
                """
                INSERT INTO binding_sequences(binding_id, dispatch_scope, last_sequence)
                VALUES (?, '', 1)
                ON CONFLICT(binding_id, dispatch_scope)
                DO UPDATE SET last_sequence=last_sequence + 1
                """,
                (exact_binding,),
            )
            sequence = int(
                conn.execute(
                    "SELECT last_sequence FROM binding_sequences "
                    "WHERE binding_id=? AND dispatch_scope=''",
                    (exact_binding,),
                ).fetchone()[0]
            )
            context_ciphertext = context_version = None
            if binding["token_ciphertext"] is not None:
                context = self.store.crypto.decrypt_text(
                    binding["token_ciphertext"],
                    table="context_tokens",
                    record_id=f"{binding['account_id']}:{binding['peer_lookup_hash']}",
                    field="token",
                    version=binding["token_key_version"],
                )
                context_ciphertext, context_version = self.store.crypto.encrypt_text(
                    context,
                    table="outbound_messages",
                    record_id=outbound_id,
                    field="context",
                )
            conn.execute(
                """
                INSERT INTO outbound_messages
                  (outbound_id, inbound_id, account_id, binding_id, provider,
                   source_kind, source_id, binding_sequence, dispatch_scope,
                   client_message_id, payload_ciphertext, payload_key_version,
                   context_ciphertext, context_key_version, status,
                   next_attempt_at, created_at, updated_at)
                VALUES (?, NULL, ?, ?, ?, 'cron', ?, ?, '', ?, ?, ?, ?, ?, 'queued', ?, ?, ?)
                """,
                (
                    outbound_id,
                    binding["account_id"],
                    exact_binding,
                    binding["provider"],
                    source_id,
                    sequence,
                    client_message_id,
                    ciphertext,
                    key_version,
                    context_ciphertext,
                    context_version,
                    now,
                    now,
                    now,
                ),
            )
        return outbound_id

    def collaboration_delivery_status(
        self, outbound_id: str
    ) -> dict[str, str | None] | None:
        """Read the terminal or in-flight state of one collaboration outbox row."""
        exact_outbound = str(outbound_id or "").strip()
        if not exact_outbound:
            raise ValueError("collaboration outbound ID is required")
        with self.store.read() as conn:
            row = conn.execute(
                "SELECT status, last_error FROM outbound_messages "
                "WHERE outbound_id=? AND source_kind='collaboration'",
                (exact_outbound,),
            ).fetchone()
        if row is None:
            return None
        return {"status": str(row["status"]), "error": row["last_error"]}

    def enqueue_collaboration_origin(
        self,
        *,
        owner_key: str,
        account_id: str,
        binding_id: str,
        conversation_id: str,
        thread_id: str,
        delivery_key: str,
        payload: str,
    ) -> str:
        """Idempotently encrypt one exact Feishu direct origin notification."""
        exact_owner = str(owner_key or "").strip()
        exact_account = str(account_id or "").strip()
        exact_binding = str(binding_id or "").strip()
        exact_conversation = str(conversation_id or "").strip()
        exact_thread = str(thread_id or "")
        stable_key = str(delivery_key or "").strip()
        text = str(payload or "")
        if not all((exact_owner, exact_account, exact_binding, exact_conversation, stable_key)):
            raise ValueError("collaboration delivery identity is incomplete")
        if exact_thread:
            raise RuntimeError("Feishu direct collaboration thread must be empty")
        if not text.strip() or len(text.encode("utf-8")) > 256_000:
            raise ValueError("collaboration delivery payload is invalid")
        if not stable_key.startswith("collaboration:"):
            raise ValueError("collaboration delivery key is invalid")
        source_id = stable_key
        digest = hashlib.sha256(source_id.encode("utf-8")).hexdigest()
        outbound_id = f"om_collab_{digest[:32]}"
        client_message_id = f"hermes-collaboration-{digest}"
        ciphertext, key_version = self.store.crypto.encrypt_text(
            text,
            table="outbound_messages",
            record_id=outbound_id,
            field="payload",
        )
        expected_peer = self.store.crypto.lookup_hash(
            "conversation:feishu", exact_conversation
        )
        now = time.time()
        with self.store.write() as conn:
            binding = conn.execute(
                "SELECT b.account_id, b.peer_lookup_hash, o.owner_key, "
                "ct.token_ciphertext, ct.token_key_version "
                "FROM channel_bindings b "
                "JOIN connector_accounts a ON a.account_id=b.account_id "
                "JOIN external_identities e ON e.external_identity_id=b.external_identity_id "
                "JOIN canonical_users u ON u.canonical_user_id=e.canonical_user_id "
                "JOIN owner_bindings o ON o.canonical_user_id=u.canonical_user_id "
                "JOIN managed_feishu_accounts m ON m.account_id=a.account_id "
                "LEFT JOIN context_tokens ct ON ct.account_id=b.account_id "
                "AND ct.peer_lookup_hash=b.peer_lookup_hash "
                "WHERE b.binding_id=? AND b.account_id=? AND b.peer_lookup_hash=? "
                "AND b.status='active' AND a.provider='feishu' AND a.status='active' "
                "AND e.status='active' AND u.status='active' AND m.lifecycle_status='active'",
                (exact_binding, exact_account, expected_peer),
            ).fetchone()
            if binding is None:
                raise RuntimeError("Feishu collaboration binding is unavailable")
            if not hmac.compare_digest(str(binding["owner_key"]), exact_owner):
                raise RuntimeError("Feishu collaboration binding belongs to another Owner")
            existing = conn.execute(
                "SELECT outbound_id, account_id, binding_id FROM outbound_messages "
                "WHERE source_id=?",
                (source_id,),
            ).fetchone()
            if existing is not None:
                if (
                    not hmac.compare_digest(str(existing["account_id"]), exact_account)
                    or not hmac.compare_digest(str(existing["binding_id"]), exact_binding)
                ):
                    raise RuntimeError("collaboration delivery key is bound elsewhere")
                return str(existing["outbound_id"])
            conn.execute(
                "INSERT INTO binding_sequences(binding_id, dispatch_scope, last_sequence) "
                "VALUES (?, '', 1) ON CONFLICT(binding_id, dispatch_scope) "
                "DO UPDATE SET last_sequence=last_sequence + 1",
                (exact_binding,),
            )
            sequence = int(conn.execute(
                "SELECT last_sequence FROM binding_sequences "
                "WHERE binding_id=? AND dispatch_scope=''",
                (exact_binding,),
            ).fetchone()[0])
            context_ciphertext = context_version = None
            if binding["token_ciphertext"] is not None:
                context = self.store.crypto.decrypt_text(
                    binding["token_ciphertext"],
                    table="context_tokens",
                    record_id=f"{exact_account}:{binding['peer_lookup_hash']}",
                    field="token",
                    version=binding["token_key_version"],
                )
                context_ciphertext, context_version = self.store.crypto.encrypt_text(
                    context,
                    table="outbound_messages",
                    record_id=outbound_id,
                    field="context",
                )
            conn.execute(
                "INSERT INTO outbound_messages "
                "(outbound_id, inbound_id, account_id, binding_id, provider, source_kind, "
                "source_id, binding_sequence, dispatch_scope, client_message_id, "
                "payload_ciphertext, payload_key_version, context_ciphertext, "
                "context_key_version, status, next_attempt_at, created_at, updated_at) "
                "VALUES (?, NULL, ?, ?, 'feishu', 'collaboration', ?, ?, '', ?, ?, ?, ?, ?, "
                "'queued', ?, ?, ?)",
                (
                    outbound_id, exact_account, exact_binding, source_id, sequence,
                    client_message_id, ciphertext, key_version, context_ciphertext,
                    context_version, now, now, now,
                ),
            )
        return outbound_id
