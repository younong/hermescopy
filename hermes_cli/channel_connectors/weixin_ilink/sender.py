"""Transactional iLink outbox sender."""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass

from gateway.weixin_ilink import ILinkTransportError, WeixinILinkClient
from gateway.weixin_ilink.text import format_weixin_text, split_weixin_text
from hermes_cli.channel_identity.store import ChannelIdentityStore


@dataclass(frozen=True)
class OutboundClaim:
    outbound_id: str
    account_id: str
    binding_id: str
    client_message_id: str
    chunks: tuple[str, ...]
    next_chunk_index: int
    chunk_attempts: int
    context_token: str | None
    base_url: str
    bot_token: str
    peer_id: str

    @property
    def chunk(self) -> str:
        return self.chunks[self.next_chunk_index]

    @property
    def chunk_client_id(self) -> str:
        if len(self.chunks) == 1:
            return self.client_message_id
        digest = hashlib.sha256(
            f"{self.client_message_id}:{self.next_chunk_index}".encode("utf-8")
        ).hexdigest()[:32]
        return f"hermes-ilink-{digest}"


def claim_outbound(store: ChannelIdentityStore, *, holder: str) -> OutboundClaim | None:
    now = time.time()
    with store.write() as conn:
        row = conn.execute(
            """
            SELECT o.*, a.base_url, a.bot_token_ciphertext, a.bot_token_key_version,
                   b.peer_ciphertext, b.peer_key_version
            FROM outbound_messages o
            JOIN ilink_accounts a ON a.account_id=o.account_id AND a.status='active'
            JOIN channel_bindings b ON b.binding_id=o.binding_id AND b.status='active'
            WHERE o.status='queued' AND o.next_attempt_at<=?
            ORDER BY o.created_at LIMIT 1
            """,
            (now,),
        ).fetchone()
        if row is None:
            return None
        changed = conn.execute(
            """
            UPDATE outbound_messages SET status='sending', claimed_by=?, claimed_at=?,
                attempts=attempts+1, chunk_attempts=chunk_attempts+1, updated_at=?
            WHERE outbound_id=? AND status='queued'
            """,
            (holder, now, now, row["outbound_id"]),
        ).rowcount
        if changed != 1:
            return None
    text = store.crypto.decrypt_text(
        row["payload_ciphertext"],
        table="outbound_messages",
        record_id=row["outbound_id"],
        field="payload",
        version=row["payload_key_version"],
    )
    chunks = tuple(split_weixin_text(format_weixin_text(text)))
    if not chunks:
        raise RuntimeError("outbound payload is empty")
    next_chunk_index = int(row["next_chunk_index"])
    if next_chunk_index < 0 or next_chunk_index >= len(chunks):
        raise RuntimeError("outbound chunk progress is invalid")
    if row["chunk_count"] is not None and int(row["chunk_count"]) != len(chunks):
        raise RuntimeError("outbound chunk count changed")
    with store.write() as conn:
        changed = conn.execute(
            """
            UPDATE outbound_messages SET chunk_count=?, updated_at=?
            WHERE outbound_id=? AND status='sending' AND claimed_by=?
              AND (chunk_count IS NULL OR chunk_count=?)
            """,
            (len(chunks), time.time(), row["outbound_id"], holder, len(chunks)),
        ).rowcount
        if changed != 1:
            raise RuntimeError("outbound send claim is stale")
    context = None
    if row["context_ciphertext"] is not None:
        context = store.crypto.decrypt_text(
            row["context_ciphertext"],
            table="outbound_messages",
            record_id=row["outbound_id"],
            field="context",
            version=row["context_key_version"],
        )
    bot_token = store.crypto.decrypt_text(
        row["bot_token_ciphertext"],
        table="ilink_accounts",
        record_id=row["account_id"],
        field="bot_token",
        version=row["bot_token_key_version"],
    )
    peer_id = store.crypto.decrypt_text(
        row["peer_ciphertext"],
        table="channel_bindings",
        record_id=row["binding_id"],
        field="peer",
        version=row["peer_key_version"],
    )
    return OutboundClaim(
        outbound_id=row["outbound_id"],
        account_id=row["account_id"],
        binding_id=row["binding_id"],
        client_message_id=row["client_message_id"],
        chunks=chunks,
        next_chunk_index=next_chunk_index,
        chunk_attempts=int(row["chunk_attempts"]) + 1,
        context_token=context,
        base_url=row["base_url"],
        bot_token=bot_token,
        peer_id=peer_id,
    )


class OutboundSender:
    def __init__(
        self,
        store: ChannelIdentityStore,
        session,
        *,
        retry_seconds: float = 2.0,
        retry_max_seconds: float = 300.0,
        max_attempts: int = 8,
        chunk_delay_seconds: float = 0.2,
    ) -> None:
        self.store = store
        self.session = session
        self.retry_seconds = max(0.0, retry_seconds)
        self.retry_max_seconds = max(self.retry_seconds, retry_max_seconds)
        self.max_attempts = max(1, max_attempts)
        self.chunk_delay_seconds = max(0.0, chunk_delay_seconds)

    async def send_claim(self, claim: OutboundClaim, *, holder: str) -> bool:
        try:
            await WeixinILinkClient(
                self.session,
                base_url=claim.base_url,
                token=claim.bot_token,
            ).send_message(
                to=claim.peer_id,
                text=claim.chunk,
                context_token=claim.context_token,
                client_id=claim.chunk_client_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error, transient = _classify_error(exc)
            if transient and claim.chunk_attempts < self.max_attempts:
                self._retry(claim, holder=holder, error=error)
            else:
                if transient:
                    error = f"retry_exhausted:{error}"
                self._fail(claim, holder=holder, error=error)
            return False
        return self._advance(claim, holder=holder)

    def _retry(self, claim: OutboundClaim, *, holder: str, error: str) -> None:
        exponent = max(0, claim.chunk_attempts - 1)
        delay = min(self.retry_max_seconds, self.retry_seconds * (2**exponent))
        now = time.time()
        with self.store.write() as conn:
            conn.execute(
                """
                UPDATE outbound_messages SET status='queued', next_attempt_at=?,
                    claimed_by=NULL, claimed_at=NULL, last_error=?, updated_at=?
                WHERE outbound_id=? AND status='sending' AND claimed_by=?
                """,
                (now + delay, error, now, claim.outbound_id, holder),
            )

    def _fail(self, claim: OutboundClaim, *, holder: str, error: str) -> None:
        now = time.time()
        with self.store.write() as conn:
            changed = conn.execute(
                """
                UPDATE outbound_messages SET status='failed', claimed_by=NULL, claimed_at=NULL,
                    last_error=?, failed_chunk_index=?, updated_at=?
                WHERE outbound_id=? AND status='sending' AND claimed_by=?
                """,
                (error, claim.next_chunk_index, now, claim.outbound_id, holder),
            ).rowcount
            if changed == 1:
                conn.execute(
                    """
                    UPDATE inbound_messages SET status='failed', rejection_reason='outbound_failed',
                        updated_at=? WHERE inbound_id=(
                            SELECT inbound_id FROM outbound_messages WHERE outbound_id=?
                        )
                    """,
                    (now, claim.outbound_id),
                )

    def _advance(self, claim: OutboundClaim, *, holder: str) -> bool:
        now = time.time()
        next_index = claim.next_chunk_index + 1
        with self.store.write() as conn:
            if next_index < len(claim.chunks):
                changed = conn.execute(
                    """
                    UPDATE outbound_messages SET status='queued', next_chunk_index=?,
                        chunk_attempts=0, next_attempt_at=?, claimed_by=NULL, claimed_at=NULL,
                        last_error=NULL, failed_chunk_index=NULL, updated_at=?
                    WHERE outbound_id=? AND status='sending' AND claimed_by=?
                      AND next_chunk_index=?
                    """,
                    (
                        next_index,
                        now + self.chunk_delay_seconds,
                        now,
                        claim.outbound_id,
                        holder,
                        claim.next_chunk_index,
                    ),
                ).rowcount
                return changed == 1
            changed = conn.execute(
                """
                UPDATE outbound_messages SET status='delivered', next_chunk_index=?,
                    payload_ciphertext=NULL, payload_key_version=NULL,
                    context_ciphertext=NULL, context_key_version=NULL,
                    claimed_by=NULL, claimed_at=NULL, chunk_attempts=0,
                    last_error=NULL, failed_chunk_index=NULL, updated_at=?
                WHERE outbound_id=? AND status='sending' AND claimed_by=?
                  AND next_chunk_index=?
                """,
                (next_index, now, claim.outbound_id, holder, claim.next_chunk_index),
            ).rowcount
            if changed == 1:
                conn.execute(
                    """
                    UPDATE inbound_messages SET status='completed', updated_at=?
                    WHERE inbound_id=(
                        SELECT inbound_id FROM outbound_messages WHERE outbound_id=?
                    )
                    """,
                    (now, claim.outbound_id),
                )
        return changed == 1


def _classify_error(exc: Exception) -> tuple[str, bool]:
    if isinstance(exc, ILinkTransportError):
        parts = [exc.reason]
        if exc.http_status is not None:
            parts.append(f"http={exc.http_status}")
        if exc.provider_code is not None:
            parts.append(f"provider={exc.provider_code}")
        return ":".join(parts), exc.transient
    if isinstance(exc, (TimeoutError, OSError)):
        return "network_error", True
    return f"internal_error:{type(exc).__name__}", False
