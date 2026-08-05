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
) -> OutboundDelivery | None:
    """Claim and decrypt the next ordered delivery for one exact provider."""
    exact_provider = str(provider or "").strip()
    exact_holder = str(holder or "").strip()
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
              AND o.status='queued' AND o.next_attempt_at<=?
              AND NOT EXISTS (
                SELECT 1 FROM inbound_messages earlier
                WHERE earlier.binding_id=o.binding_id
                  AND earlier.binding_sequence<o.binding_sequence
                  AND earlier.status IN ('queued','processing')
              )
              AND NOT EXISTS (
                SELECT 1 FROM outbound_messages earlier_out
                WHERE earlier_out.binding_id=o.binding_id
                  AND earlier_out.binding_sequence<COALESCE(o.binding_sequence, i.binding_sequence)
                  AND earlier_out.status IN ('queued','sending')
              )
            ORDER BY COALESCE(o.binding_sequence, i.binding_sequence), o.created_at
            LIMIT 1
            """,
            (exact_provider, exact_provider, exact_provider, now),
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


def fail_outbound(
    store: ChannelIdentityStore,
    delivery: OutboundDelivery,
    *,
    holder: str,
    error: str,
) -> bool:
    now = time.time()
    with store.write() as conn:
        changed = conn.execute(
            """
            UPDATE outbound_messages
            SET status='failed', claimed_by=NULL, claimed_at=NULL, last_error=?,
                failed_chunk_index=?, payload_ciphertext=NULL,
                payload_key_version=NULL, context_ciphertext=NULL,
                context_key_version=NULL, updated_at=?
            WHERE outbound_id=? AND (provider=? OR provider IS NULL) AND status='sending' AND claimed_by=?
              AND next_chunk_index=?
            """,
            (
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
                UPDATE inbound_messages SET status='failed',
                    rejection_reason='outbound_failed', updated_at=?
                WHERE inbound_id=(
                    SELECT inbound_id FROM outbound_messages WHERE outbound_id=?
                )
                """,
                (now, delivery.outbound_id),
            )
    return changed == 1


def advance_outbound(
    store: ChannelIdentityStore,
    delivery: OutboundDelivery,
    *,
    holder: str,
    part_count: int,
    next_attempt_at: float | None = None,
) -> bool:
    next_index = delivery.next_part_index + 1
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
            return changed == 1
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
    return changed == 1


def recover_stale_outbound(
    store: ChannelIdentityStore,
    *,
    provider: str,
    claimed_before: float,
) -> int:
    now = time.time()
    with store.write() as conn:
        return conn.execute(
            """
            UPDATE outbound_messages
            SET status='queued', claimed_by=NULL, claimed_at=NULL,
                next_attempt_at=?, updated_at=?
            WHERE status='sending' AND claimed_at<?
              AND account_id IN (
                SELECT account_id FROM connector_accounts WHERE provider=?
              )
            """,
            (now, now, claimed_before, provider),
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
                INSERT INTO binding_sequences(binding_id, last_sequence)
                VALUES (?, 1)
                ON CONFLICT(binding_id) DO UPDATE SET last_sequence=last_sequence + 1
                """,
                (exact_binding,),
            )
            sequence = int(
                conn.execute(
                    "SELECT last_sequence FROM binding_sequences WHERE binding_id=?",
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
                   source_kind, source_id, binding_sequence, client_message_id,
                   payload_ciphertext, payload_key_version, context_ciphertext,
                   context_key_version, status, next_attempt_at, created_at, updated_at)
                VALUES (?, NULL, ?, ?, ?, 'cron', ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?)
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
