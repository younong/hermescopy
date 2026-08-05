"""Transactional iLink outbox sender."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path

from gateway.platforms.response_media import validate_media_delivery_path
from gateway.weixin_ilink import (
    ILinkTransportError,
    WeixinILinkClient,
    WeixinMediaError,
    upload_media_item,
)
from gateway.weixin_ilink.text import format_weixin_text, split_weixin_text
from hermes_cli.channel_connectors.contracts import OutboundDelivery
from hermes_cli.channel_dispatch.outbox import (
    advance_outbound,
    claim_outbound as claim_canonical_outbound,
    fail_outbound,
    release_outbound_claim,
    set_outbound_part_count,
)
from hermes_cli.channel_identity.owner_resolution import resolve_connector_account
from hermes_cli.channel_identity.store import ChannelIdentityStore


@dataclass(frozen=True)
class OutboundPart:
    kind: str
    value: str
    force_file: bool = False


@dataclass(frozen=True)
class OutboundClaim:
    outbound_id: str
    account_id: str
    binding_id: str
    client_message_id: str
    parts: tuple[OutboundPart, ...]
    next_part_index: int
    part_attempts: int
    context_token: str | None
    base_url: str
    bot_token: str
    peer_id: str
    workspace_root: Path
    delivery: OutboundDelivery

    @property
    def part(self) -> OutboundPart:
        return self.parts[self.next_part_index]

    @property
    def part_client_id(self) -> str:
        if len(self.parts) == 1:
            return self.client_message_id
        digest = hashlib.sha256(
            f"{self.client_message_id}:{self.next_part_index}".encode("utf-8")
        ).hexdigest()[:32]
        prefix = self.client_message_id.rsplit("-", 1)[0]
        return f"{prefix}-{digest}"


def _outbound_parts(
    payload: str,
    *,
    workspace_root: Path,
) -> tuple[OutboundPart, ...]:
    text = payload
    media: list[dict] = []
    try:
        envelope = json.loads(payload)
    except (TypeError, json.JSONDecodeError):
        envelope = None
    if isinstance(envelope, dict) and envelope.get("v") == 1:
        if not isinstance(envelope.get("text"), str):
            raise RuntimeError("outbound envelope is invalid")
        text = envelope["text"]
        if not isinstance(envelope.get("metadata", {}), dict):
            raise RuntimeError("outbound envelope is invalid")
        media = envelope.get("attachments", envelope.get("media", []))
        if not isinstance(media, list) or len(media) > 16:
            raise RuntimeError("outbound media payload is invalid")

    parts = [OutboundPart("text", chunk) for chunk in split_weixin_text(format_weixin_text(text))]
    seen: set[str] = set()
    for entry in media:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("path"), str)
            or not entry["path"]
            or not isinstance(entry.get("voice", False), bool)
        ):
            raise RuntimeError("outbound media payload is invalid")
        path = entry["path"]
        validated = validate_media_delivery_path(
            path,
            allowed_roots=(workspace_root,),
        )
        if validated is None or validated != path:
            raise RuntimeError("outbound media path is invalid")
        if path in seen:
            continue
        seen.add(path)
        parts.append(OutboundPart("media", path, force_file=bool(entry.get("voice"))))
    if not parts:
        raise RuntimeError("outbound payload is empty")
    return tuple(parts)


def claim_outbound(store: ChannelIdentityStore, *, holder: str) -> OutboundClaim | None:
    delivery = claim_canonical_outbound(
        store,
        provider="weixin_ilink",
        holder=holder,
    )
    if delivery is None:
        return None
    try:
        with store.read() as conn:
            owner = conn.execute(
                """
                SELECT o.owner_key
                FROM channel_bindings b
                JOIN external_identities e
                  ON e.external_identity_id=b.external_identity_id
                JOIN owner_bindings o ON o.canonical_user_id=e.canonical_user_id
                WHERE b.binding_id=? AND b.account_id=?
                """,
                (delivery.binding_id, delivery.account_id),
            ).fetchone()
        if owner is None:
            raise RuntimeError("iLink channel Owner is unavailable")
        workspace_root = (
            store.global_home
            / "users"
            / owner["owner_key"]
            / "workspaces"
            / "default"
        )
        parts = _outbound_parts(
            delivery.payload,
            workspace_root=workspace_root,
        )
        account = resolve_connector_account(
            store,
            provider=delivery.provider,
            account_id=delivery.account_id,
            credential_version=delivery.credential_version,
        )
        base_url = account.credentials.get("base_url")
        bot_token = account.credentials.get("bot_token")
        if not isinstance(base_url, str) or not isinstance(bot_token, str):
            raise RuntimeError("iLink account credentials are unavailable")
        set_outbound_part_count(
            store,
            delivery,
            holder=holder,
            part_count=len(parts),
        )
    except Exception as exc:
        release_outbound_claim(
            store,
            outbound_id=delivery.outbound_id,
            holder=holder,
            error=f"claim_error:{type(exc).__name__}",
        )
        raise
    return OutboundClaim(
        outbound_id=delivery.outbound_id,
        account_id=delivery.account_id,
        binding_id=delivery.binding_id,
        client_message_id=delivery.client_message_id,
        parts=parts,
        next_part_index=delivery.next_part_index,
        part_attempts=delivery.part_attempts,
        context_token=delivery.context_token,
        base_url=base_url,
        bot_token=bot_token,
        peer_id=delivery.conversation_id,
        workspace_root=workspace_root,
        delivery=delivery,
    )


def _contained_media_path(path: str, workspace_root: Path) -> str:
    root = workspace_root.resolve(strict=True)
    if root.is_symlink() or not root.is_dir():
        raise RuntimeError("outbound workspace is unavailable")
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve(strict=True)
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise RuntimeError("outbound media path is outside Owner workspace")
    for parent in (candidate, *candidate.parents):
        if parent == root.parent:
            break
        if parent.is_symlink():
            raise RuntimeError("outbound media path must not contain symlinks")
        if parent == root:
            break
    repeated = candidate.resolve(strict=True)
    if repeated != resolved or not repeated.is_relative_to(root):
        raise RuntimeError("outbound media path changed during validation")
    return str(repeated)


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
            resolve_connector_account(
                self.store,
                provider=claim.delivery.provider,
                account_id=claim.account_id,
                credential_version=claim.delivery.credential_version,
            )
            with self.store.read() as conn:
                current = conn.execute(
                    """
                    SELECT 1 FROM outbound_messages o
                    JOIN connector_accounts a ON a.account_id=o.account_id
                    JOIN channel_bindings b ON b.binding_id=o.binding_id
                    WHERE o.outbound_id=? AND o.status='sending' AND o.claimed_by=?
                      AND o.next_chunk_index=? AND a.provider=? AND a.status='active'
                      AND a.credential_version=? AND b.status='active'
                    """,
                    (
                        claim.outbound_id,
                        holder,
                        claim.next_part_index,
                        claim.delivery.provider,
                        claim.delivery.credential_version,
                    ),
                ).fetchone()
            if current is None:
                raise RuntimeError("outbound send claim is stale")
            client = WeixinILinkClient(
                self.session,
                base_url=claim.base_url,
                token=claim.bot_token,
            )
            if claim.part.kind == "text":
                await client.send_message(
                    to=claim.peer_id,
                    text=claim.part.value,
                    context_token=claim.context_token,
                    client_id=claim.part_client_id,
                )
            else:
                path = _contained_media_path(claim.part.value, claim.workspace_root)
                item = await upload_media_item(
                    self.session,
                    client,
                    to_user_id=claim.peer_id,
                    path=path,
                    force_file=claim.part.force_file,
                )
                await client.send_item(
                    to=claim.peer_id,
                    item=item,
                    context_token=claim.context_token,
                    client_id=claim.part_client_id,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error, transient = _classify_error(exc)
            if transient and claim.part_attempts < self.max_attempts:
                self._retry(claim, holder=holder, error=error)
            else:
                if transient:
                    error = f"retry_exhausted:{error}"
                self._fail(claim, holder=holder, error=error)
            return False
        return self._advance(claim, holder=holder)

    def _retry(self, claim: OutboundClaim, *, holder: str, error: str) -> None:
        exponent = max(0, claim.part_attempts - 1)
        delay = min(self.retry_max_seconds, self.retry_seconds * (2**exponent))
        release_outbound_claim(
            self.store,
            outbound_id=claim.outbound_id,
            holder=holder,
            error=error,
            next_attempt_at=time.time() + delay,
        )

    def _fail(self, claim: OutboundClaim, *, holder: str, error: str) -> None:
        fail_outbound(
            self.store,
            claim.delivery,
            holder=holder,
            error=error,
        )

    def _advance(self, claim: OutboundClaim, *, holder: str) -> bool:
        return advance_outbound(
            self.store,
            claim.delivery,
            holder=holder,
            part_count=len(claim.parts),
            next_attempt_at=time.time() + self.chunk_delay_seconds,
        )


def _classify_error(exc: Exception) -> tuple[str, bool]:
    if isinstance(exc, ILinkTransportError):
        parts = [exc.reason]
        if exc.http_status is not None:
            parts.append(f"http={exc.http_status}")
        if exc.provider_code is not None:
            parts.append(f"provider={exc.provider_code}")
        return ":".join(parts), exc.transient
    if isinstance(exc, WeixinMediaError):
        return exc.code, exc.retryable
    if isinstance(exc, TimeoutError):
        return "network_error", True
    if isinstance(exc, OSError):
        return "filesystem_error", False
    return f"internal_error:{type(exc).__name__}", False
