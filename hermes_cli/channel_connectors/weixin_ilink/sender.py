"""Transactional iLink outbox sender."""

from __future__ import annotations

import time
from dataclasses import dataclass

from gateway.weixin_ilink import WeixinILinkClient
from hermes_cli.channel_identity.store import ChannelIdentityStore


@dataclass(frozen=True)
class OutboundClaim:
    outbound_id: str
    account_id: str
    binding_id: str
    client_message_id: str
    text: str
    context_token: str | None
    base_url: str
    bot_token: str
    peer_id: str


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
                attempts=attempts+1, updated_at=?
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
        text=text,
        context_token=context,
        base_url=row["base_url"],
        bot_token=bot_token,
        peer_id=peer_id,
    )


class OutboundSender:
    def __init__(self, store: ChannelIdentityStore, session, *, retry_seconds: float = 2.0) -> None:
        self.store = store
        self.session = session
        self.retry_seconds = retry_seconds

    async def send_claim(self, claim: OutboundClaim, *, holder: str) -> bool:
        try:
            response = await WeixinILinkClient(
                self.session,
                base_url=claim.base_url,
                token=claim.bot_token,
            ).send_message(
                to=claim.peer_id,
                text=claim.text,
                context_token=claim.context_token,
                client_id=claim.client_message_id,
            )
            if response.get("ret") not in {None, 0} or response.get("errcode") not in {None, 0}:
                raise RuntimeError("iLink send message was rejected")
        except Exception as exc:
            with self.store.write() as conn:
                conn.execute(
                    """
                    UPDATE outbound_messages SET status='queued', next_attempt_at=?,
                        claimed_by=NULL, claimed_at=NULL, last_error=?, updated_at=?
                    WHERE outbound_id=? AND status='sending' AND claimed_by=?
                    """,
                    (
                        time.time() + self.retry_seconds,
                        type(exc).__name__,
                        time.time(),
                        claim.outbound_id,
                        holder,
                    ),
                )
            return False
        with self.store.write() as conn:
            changed = conn.execute(
                """
                UPDATE outbound_messages SET status='delivered', payload_ciphertext=NULL,
                    payload_key_version=NULL, context_ciphertext=NULL, context_key_version=NULL,
                    claimed_by=NULL, claimed_at=NULL, last_error=NULL, updated_at=?
                WHERE outbound_id=? AND status='sending' AND claimed_by=?
                """,
                (time.time(), claim.outbound_id, holder),
            ).rowcount
            if changed == 1:
                conn.execute(
                    "UPDATE inbound_messages SET status='completed', updated_at=? WHERE inbound_id=(SELECT inbound_id FROM outbound_messages WHERE outbound_id=?)",
                    (time.time(), claim.outbound_id),
                )
        return changed == 1
