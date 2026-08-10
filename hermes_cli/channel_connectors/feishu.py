"""Feishu transport for the canonical encrypted channel pipeline."""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, replace
from typing import Any, Mapping
from urllib.parse import quote

import httpx

from hermes_cli.channel_connectors.contracts import (
    DeliveryReceipt,
    NormalizedInboundEnvelope,
    OutboundDelivery,
)
from hermes_cli.channel_connectors.inbox import (
    CanonicalInbox as InboundQueueService,
    InboundCommitResult,
)
from hermes_cli.channel_dispatch import (
    ChannelDispatcher,
    advance_outbound,
    claim_outbound,
    fail_outbound,
    mark_outbound_ambiguous,
    recover_stale_outbound,
    release_outbound_claim,
    set_outbound_part_count,
)
from hermes_cli.channel_identity import ChannelIdentityStore, resolve_connector_account
from hermes_cli.channel_identity.models import ResolvedConnectorAccount

from .feishu_ws import FeishuWebSocketSession

PROVIDER = "feishu"
_FEISHU_API_BASE = "https://open.feishu.cn"
_LARK_API_BASE = "https://open.larksuite.com"
_MAX_TEXT_LENGTH = 8_000
_MARKDOWN_HINT_RE = re.compile(
    r"(^#{1,6}\s)|(^\s*[-*]\s)|(^\s*\d+\.\s)|(```)|(`[^`\n]+`)|"
    r"(\*\*[^*\n].+?\*\*)|(~~[^~\n].+?~~)|(\[[^\]]+\]\([^)]+\))|(^>\s)",
    re.MULTILINE,
)
_MARKDOWN_TABLE_RE = re.compile(r"^\|.*\|\n\|[-|: ]+\|", re.MULTILINE)
_RETRYABLE_PROVIDER_CODES = frozenset(
    {
        99991400,  # rate limited
        99991403,
        99991404,
        99991420,
        99991663,  # access token expired
        99991664,
    }
)


class FeishuTransportError(RuntimeError):
    """A sanitized Feishu delivery error with retry and acceptance certainty."""

    def __init__(
        self,
        code: str,
        *,
        retryable: bool,
        acceptance_uncertain: bool = False,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.acceptance_uncertain = acceptance_uncertain


@dataclass(frozen=True)
class FeishuOutboundClaim:
    delivery: OutboundDelivery
    account: ResolvedConnectorAccount
    parts: tuple[str, ...]

    @property
    def part(self) -> str:
        return self.parts[self.delivery.next_part_index]


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _first_text(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _load_content(raw_content: Any) -> Mapping[str, Any]:
    if isinstance(raw_content, Mapping):
        return raw_content
    if not isinstance(raw_content, str):
        return {}
    try:
        parsed = json.loads(raw_content)
    except json.JSONDecodeError:
        return {"text": raw_content}
    return parsed if isinstance(parsed, Mapping) else {}


def _walk_post_text(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            result.extend(_walk_post_text(item))
        return result
    if not isinstance(value, Mapping):
        return []
    tag = str(value.get("tag") or "").lower()
    if tag in {"img", "image"}:
        return ["[Image]"]
    if tag in {"file", "media", "audio", "video"}:
        name = _first_text(value.get("file_name"), value.get("title"))
        return [f"[Attachment: {name}]" if name else "[Attachment]"]
    if tag == "at":
        name = _first_text(value.get("user_name"), value.get("text"))
        return [f"@{name}" if name else ""]
    result: list[str] = []
    for key in ("title", "text", "content", "children", "elements"):
        result.extend(_walk_post_text(value.get(key)))
    return result


def _mention_identities(mention: Any) -> frozenset[str]:
    identity = _field(mention, "id") or _field(mention, "user_id")
    return frozenset(
        value.strip()
        for value in (
            str(_field(identity, "union_id") or ""),
            str(_field(identity, "open_id") or ""),
            str(_field(identity, "user_id") or ""),
            identity if isinstance(identity, str) else "",
        )
        if value.strip()
    )


def _verified_mentions(
    message: Any,
) -> tuple[tuple[frozenset[str], str], ...]:
    raw = _field(message, "mentions", ())
    if not isinstance(raw, (list, tuple)):
        return ()
    mentions: list[tuple[frozenset[str], str]] = []
    for mention in raw:
        identities = _mention_identities(mention)
        key = _first_text(_field(mention, "key"))
        if identities:
            mentions.append((identities, key))
    return tuple(mentions)


def _strip_current_bot_mention(
    payload: str,
    mentions: tuple[tuple[frozenset[str], str], ...],
    bot_identities: frozenset[str],
) -> str:
    text = payload
    for identities, key in mentions:
        if not identities.isdisjoint(bot_identities) and key:
            text = text.replace(key, "")
    return text.strip()


def _normalize_text(message_type: str, raw_content: Any) -> tuple[str, str | None]:
    content = _load_content(raw_content)
    if message_type == "text":
        return str(content.get("text") or "").strip(), None
    if message_type == "post":
        parts = _walk_post_text(content)
        return "\n".join(dict.fromkeys(part for part in parts if part)).strip(), None
    if message_type in {"interactive", "card", "merge_forward", "share_chat"}:
        parts = _walk_post_text(content)
        text = "\n".join(dict.fromkeys(part for part in parts if part)).strip()
        return text, None if text else "unsupported_message_type"
    return "", "unsupported_message_type"


def _timestamp_seconds(raw: Any) -> float | None:
    if raw in (None, ""):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value / 1000 if value > 10_000_000_000 else value


def normalize_verified_event(data: Any) -> NormalizedInboundEnvelope | None:
    """Normalize one event after Feishu's SDK has verified and decoded it."""
    event = _field(data, "event")
    message = _field(event, "message")
    sender = _field(event, "sender")
    sender_id = _field(sender, "sender_id")
    if message is None or sender_id is None:
        return None

    message_id = str(_field(message, "message_id") or "").strip()
    chat_id = str(_field(message, "chat_id") or "").strip()
    union_id = str(_field(sender_id, "union_id") or "").strip()
    open_id = str(_field(sender_id, "open_id") or "").strip()
    user_id = str(_field(sender_id, "user_id") or "").strip()
    actor_id = union_id or open_id or user_id
    message_type = str(
        _field(message, "message_type") or _field(message, "msg_type") or "text"
    ).strip().lower()
    payload, rejection_reason = _normalize_text(
        message_type,
        _field(message, "content", ""),
    )
    if not payload and rejection_reason is None:
        rejection_reason = "empty_message"

    chat_type = str(_field(message, "chat_type") or "p2p").strip().lower()
    root_id = str(_field(message, "root_id") or "").strip()
    thread_id = (
        str(_field(message, "thread_id") or root_id).strip() or None
        if chat_type != "p2p"
        else None
    )
    reply_to = _first_text(_field(message, "parent_id")) or None
    metadata = {
        "message_type": message_type,
        "sender_type": str(_field(sender, "sender_type") or "").strip() or None,
        "open_id": open_id or None,
        "user_id": user_id or None,
        "union_id": union_id or None,
    }
    return NormalizedInboundEnvelope(
        provider_message_id=message_id,
        conversation_id=chat_id,
        actor_id=actor_id,
        actor_display_name=_first_text(
            _field(sender, "name"),
            _field(sender, "sender_name"),
        )
        or None,
        payload_kind="text",
        payload=payload,
        conversation_kind="direct" if chat_type == "p2p" else "group",
        thread_id=thread_id,
        parent_conversation_id=chat_id if thread_id else None,
        reply_to_message_id=reply_to,
        occurred_at=_timestamp_seconds(
            _field(message, "create_time") or _field(message, "update_time")
        ),
        context_token=message_id or None,
        rejection_reason=rejection_reason,
        metadata=metadata,
    )


def enqueue_verified_event(
    store: ChannelIdentityStore,
    *,
    account_id: str,
    data: Any,
) -> InboundCommitResult | None:
    """Admit and commit one verified Feishu event for an exact managed account."""
    envelope = normalize_verified_event(data)
    if envelope is None:
        return None
    event = _field(data, "event")
    message = _field(event, "message")
    metadata = envelope.metadata or {}
    sender_type = str(metadata.get("sender_type") or "").strip().lower()
    if sender_type != "user":
        envelope = replace(envelope, rejection_reason="sender_not_human")

    with store.read() as conn:
        account = conn.execute(
            """
            SELECT p.revision, a.provider_account_id
            FROM connector_accounts a
            JOIN managed_feishu_accounts m ON m.account_id=a.account_id
                                              AND m.lifecycle_status='active'
            JOIN feishu_employee_profiles p ON p.account_id=a.account_id
                                             AND p.lifecycle_status='active'
            WHERE a.account_id=? AND a.provider='feishu' AND a.status='active'
            """,
            (account_id,),
        ).fetchone()
    if account is None:
        envelope = replace(envelope, rejection_reason="managed_account_unavailable")
    else:
        resolved = resolve_connector_account(
            store,
            provider=PROVIDER,
            account_id=account_id,
            require_managed_feishu=True,
        )
        bot_identities = frozenset(
            value.strip()
            for value in (
                str(resolved.provider_account_id or ""),
                str(resolved.credentials.get("bot_open_id") or ""),
                str(resolved.credentials.get("bot_union_id") or ""),
                str(resolved.credentials.get("bot_user_id") or ""),
            )
            if value.strip()
        )
        mentions = _verified_mentions(message)
        exact_mention = any(
            not identities.isdisjoint(bot_identities)
            for identities, _ in mentions
        )
        reply_receipt = None
        reply_to = str(envelope.reply_to_message_id or "").strip()
        if reply_to:
            receipt_hash = store.crypto.lookup_hash(
                f"provider-message:{PROVIDER}:{account_id}",
                reply_to,
            )
            peer_hash = store.crypto.lookup_hash(
                f"conversation:{PROVIDER}", envelope.conversation_id
            )
            with store.read() as conn:
                reply_receipt = conn.execute(
                    """
                    SELECT o.dispatch_scope FROM outbound_receipts r
                    JOIN channel_bindings b ON b.binding_id=r.binding_id
                    JOIN outbound_messages o ON o.outbound_id=r.outbound_id
                    WHERE r.account_id=? AND r.provider_message_lookup_hash=?
                      AND b.account_id=? AND b.peer_lookup_hash=?
                      AND b.status='active'
                    """,
                    (account_id, receipt_hash, account_id, peer_hash),
                ).fetchone()
        exact_reply = reply_receipt is not None
        if envelope.conversation_kind == "group":
            if not exact_mention and not exact_reply:
                envelope = replace(
                    envelope,
                    rejection_reason="group_not_addressed_to_bot",
                )
            else:
                envelope = replace(
                    envelope,
                    payload=_strip_current_bot_mention(
                        envelope.payload,
                        mentions,
                        bot_identities,
                    ),
                    group_admission_token=store.crypto.lookup_hash(
                        f"group-admission:{PROVIDER}:{account_id}",
                        f"{envelope.provider_message_id}:{envelope.actor_id}",
                    ),
                )
        scope_seed = str(envelope.thread_id or "").strip()
        if envelope.conversation_kind == "group" and exact_reply:
            dispatch_scope = str(reply_receipt["dispatch_scope"] or "")
        else:
            dispatch_scope = (
                store.crypto.lookup_hash(
                    f"dispatch-scope:{PROVIDER}:{account_id}",
                    scope_seed,
                )
                if scope_seed
                else ""
            )
        profile_revision = int(account["revision"])
        peer_hash = store.crypto.lookup_hash(
            f"conversation:{PROVIDER}", envelope.conversation_id
        )
        with store.read() as conn:
            session = conn.execute(
                """
                SELECT s.profile_revision FROM channel_bindings b
                JOIN channel_sessions s ON s.binding_id=b.binding_id
                                       AND s.dispatch_scope=?
                WHERE b.account_id=? AND b.peer_lookup_hash=?
                  AND b.status='active'
                """,
                (dispatch_scope, account_id, peer_hash),
            ).fetchone()
        if session is not None and session["profile_revision"] is not None:
            profile_revision = int(session["profile_revision"])
        envelope = replace(
            envelope,
            dispatch_scope=dispatch_scope,
            profile_revision=profile_revision,
        )
    if envelope.rejection_reason is None:
        from hermes_cli.channel_identity import ensure_managed_feishu_conversation_binding

        ensure_managed_feishu_conversation_binding(
            store,
            account_id=account_id,
            conversation_id=envelope.conversation_id,
            actor_id=envelope.actor_id,
            conversation_kind=envelope.conversation_kind,
        )
    inbox = InboundQueueService(store, provider=PROVIDER)
    with store.write() as conn:
        return inbox.commit(
            conn,
            account_id=account_id,
            envelope=envelope,
            result=True,
        )


def _split_text(payload: str, limit: int = _MAX_TEXT_LENGTH) -> tuple[str, ...]:
    text = payload
    try:
        envelope = json.loads(payload)
    except (TypeError, json.JSONDecodeError):
        envelope = None
    if isinstance(envelope, Mapping) and envelope.get("v") == 1:
        candidate = envelope.get("text")
        if not isinstance(candidate, str):
            raise ValueError("Feishu outbound envelope is invalid")
        text = candidate
        attachments = envelope.get("attachments", envelope.get("media", []))
        if attachments not in (None, []):
            raise ValueError("Feishu outbound media is not supported")
    text = text.strip()
    if not text:
        raise ValueError("Feishu send requires text")
    parts: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            parts.append(remaining)
            break
        split_at = remaining.rfind("\n", 0, limit)
        if split_at < limit // 2:
            split_at = remaining.rfind(" ", 0, limit)
        if split_at < 1:
            split_at = limit
        parts.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip()
    return tuple(parts)


def _markdown_post_payload(content: str) -> str:
    return json.dumps(
        {"zh_cn": {"content": [[{"tag": "md", "text": content}]]}},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _outbound_payload(content: str) -> tuple[str, str]:
    if _MARKDOWN_TABLE_RE.search(content):
        return "text", json.dumps({"text": content}, ensure_ascii=False)
    if _MARKDOWN_HINT_RE.search(content):
        return "post", _markdown_post_payload(content)
    return "text", json.dumps({"text": content}, ensure_ascii=False)


def claim_feishu_outbound(
    store: ChannelIdentityStore,
    *,
    holder: str,
    account_id: str | None = None,
) -> FeishuOutboundClaim | None:
    delivery = claim_outbound(
        store,
        provider=PROVIDER,
        holder=holder,
        account_id=account_id,
    )
    if delivery is None:
        return None
    try:
        account = resolve_connector_account(
            store,
            provider=PROVIDER,
            account_id=delivery.account_id,
            credential_version=delivery.credential_version,
            require_managed_feishu=True,
        )
        _credentials(account)
        parts = _split_text(delivery.payload)
        set_outbound_part_count(store, delivery, holder=holder, part_count=len(parts))
    except Exception as exc:
        release_outbound_claim(
            store,
            outbound_id=delivery.outbound_id,
            holder=holder,
            error=f"claim_error:{type(exc).__name__}",
        )
        raise
    return FeishuOutboundClaim(delivery=delivery, account=account, parts=parts)


def _credentials(account: ResolvedConnectorAccount) -> tuple[str, str, str]:
    app_id = account.credentials.get("app_id")
    app_secret = account.credentials.get("app_secret")
    domain = str(account.credentials.get("domain") or "feishu").strip().lower()
    if not isinstance(app_id, str) or not app_id.strip():
        raise RuntimeError("Feishu app_id is unavailable")
    if not isinstance(app_secret, str) or not app_secret.strip():
        raise RuntimeError("Feishu app_secret is unavailable")
    if domain not in {"feishu", "lark"}:
        raise RuntimeError("Feishu domain is invalid")
    return app_id.strip(), app_secret.strip(), domain


class FeishuHTTPTransport:
    """Deliver canonical outbox payloads through Feishu's HTTP API."""

    provider = PROVIDER

    def __init__(self, client: httpx.AsyncClient | None = None, *, timeout: float = 30) -> None:
        self._client = client or httpx.AsyncClient(timeout=timeout, trust_env=False)
        self._owns_client = client is None
        self._tokens: dict[tuple[str, int], tuple[str, float]] = {}

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def verify_account(
        self,
        account: ResolvedConnectorAccount,
    ) -> dict[str, str]:
        """Validate credentials and resolve non-secret immutable bot identity."""
        app_id, app_secret, domain = _credentials(account)
        base_url = _LARK_API_BASE if domain == "lark" else _FEISHU_API_BASE
        token = await self._access_token(
            account,
            app_id=app_id,
            app_secret=app_secret,
            base_url=base_url,
        )
        result = await self._get_json(
            f"{base_url}/open-apis/bot/v3/info",
            headers={"Authorization": f"Bearer {token}"},
        )
        bot = result.get("bot")
        if not isinstance(bot, Mapping):
            data = result.get("data")
            bot = data.get("bot") if isinstance(data, Mapping) else None
        if not isinstance(bot, Mapping):
            raise FeishuTransportError("bot_identity_missing", retryable=False)
        open_id = str(bot.get("open_id") or "").strip()
        user_id = str(bot.get("user_id") or "").strip()
        union_id = str(bot.get("union_id") or "").strip()
        if not any((open_id, user_id, union_id)):
            raise FeishuTransportError("bot_identity_missing", retryable=False)
        return {
            "app_id": app_id,
            "domain": domain,
            "bot_open_id": open_id,
            "bot_user_id": user_id,
            "bot_union_id": union_id,
            "bot_name": str(bot.get("app_name") or bot.get("name") or "").strip(),
        }

    async def send(
        self,
        account: ResolvedConnectorAccount,
        delivery: OutboundDelivery,
    ) -> DeliveryReceipt:
        app_id, app_secret, domain = _credentials(account)
        base_url = _LARK_API_BASE if domain == "lark" else _FEISHU_API_BASE
        token = await self._access_token(
            account,
            app_id=app_id,
            app_secret=app_secret,
            base_url=base_url,
        )
        msg_type, content = _outbound_payload(delivery.payload)
        body: dict[str, Any] = {
            "msg_type": msg_type,
            "content": content,
            "uuid": delivery.client_message_id,
        }
        if delivery.context_token and delivery.next_part_index == 0:
            path = f"/open-apis/im/v1/messages/{quote(delivery.context_token, safe='')}/reply"
        else:
            path = "/open-apis/im/v1/messages"
            body["receive_id"] = delivery.conversation_id
        params = None if "reply" in path else {"receive_id_type": "chat_id"}
        result = await self._post_json(
            f"{base_url}{path}",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            json_body=body,
            acceptance_uncertain_on_transport=True,
        )
        data = result.get("data")
        message_id = str(data.get("message_id") or "").strip() if isinstance(data, Mapping) else ""
        return DeliveryReceipt(provider_message_id=message_id or None)

    async def _access_token(
        self,
        account: ResolvedConnectorAccount,
        *,
        app_id: str,
        app_secret: str,
        base_url: str,
    ) -> str:
        key = (account.account_id, account.credential_version)
        cached = self._tokens.get(key)
        now = time.time()
        if cached is not None and cached[1] > now + 60:
            return cached[0]
        result = await self._post_json(
            f"{base_url}/open-apis/auth/v3/tenant_access_token/internal",
            headers=None,
            params=None,
            json_body={"app_id": app_id, "app_secret": app_secret},
        )
        token = str(result.get("tenant_access_token") or "").strip()
        if not token:
            raise FeishuTransportError("token_missing", retryable=True)
        try:
            expires_in = max(60, int(result.get("expire") or 7200))
        except (TypeError, ValueError):
            expires_in = 7200
        self._tokens = {key: (token, now + expires_in)}
        return token

    async def _get_json(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
    ) -> Mapping[str, Any]:
        try:
            response = await self._client.get(url, headers=headers)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise FeishuTransportError(
                "network_error", retryable=True, acceptance_uncertain=True
            ) from exc
        except httpx.HTTPError as exc:
            raise FeishuTransportError(
                "http_error", retryable=True, acceptance_uncertain=True
            ) from exc
        return self._validated_response(response)

    async def _post_json(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None,
        params: Mapping[str, str] | None,
        json_body: Mapping[str, Any],
        acceptance_uncertain_on_transport: bool = False,
    ) -> Mapping[str, Any]:
        try:
            response = await self._client.post(
                url,
                headers=headers,
                params=params,
                json=json_body,
            )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise FeishuTransportError(
                "network_error",
                retryable=True,
                acceptance_uncertain=acceptance_uncertain_on_transport,
            ) from exc
        except httpx.HTTPError as exc:
            raise FeishuTransportError(
                "http_error",
                retryable=True,
                acceptance_uncertain=acceptance_uncertain_on_transport,
            ) from exc
        return self._validated_response(response)

    @staticmethod
    def _validated_response(response: httpx.Response) -> Mapping[str, Any]:
        retryable_http = response.status_code in {408, 409, 425, 429} or response.status_code >= 500
        if response.status_code < 200 or response.status_code >= 300:
            raise FeishuTransportError(
                f"feishu_http_{response.status_code}",
                retryable=retryable_http,
            )
        try:
            result = response.json()
        except ValueError as exc:
            raise FeishuTransportError("invalid_response", retryable=True) from exc
        if not isinstance(result, Mapping):
            raise FeishuTransportError("invalid_response", retryable=True)
        try:
            provider_code = int(result.get("code") or 0)
        except (TypeError, ValueError):
            raise FeishuTransportError("invalid_response", retryable=True)
        if provider_code != 0:
            raise FeishuTransportError(
                f"feishu_code_{provider_code}",
                retryable=provider_code in _RETRYABLE_PROVIDER_CODES,
            )
        return result


async def verify_feishu_credentials(
    credentials: Mapping[str, Any],
    *,
    transport: FeishuHTTPTransport | None = None,
) -> dict[str, str]:
    """Verify candidate credentials without persisting secrets or provider bodies."""
    app_id = str(credentials.get("app_id") or "").strip()
    account = ResolvedConnectorAccount(
        provider=PROVIDER,
        account_id="candidate",
        provider_account_id=app_id,
        credentials=dict(credentials),
        credential_version=1,
    )
    client = transport or FeishuHTTPTransport()
    try:
        return await client.verify_account(account)
    finally:
        if transport is None:
            await client.close()


class FeishuSender:
    def __init__(
        self,
        store: ChannelIdentityStore,
        transport: FeishuHTTPTransport,
        *,
        config: Mapping[str, Any],
    ) -> None:
        self.store = store
        self.transport = transport
        self.retry_seconds = float(config.get("outbound_retry_seconds", 2))
        self.retry_max = float(config.get("outbound_retry_max_seconds", 300))
        self.max_attempts = int(config.get("outbound_max_attempts", 8))

    async def send_claim(self, claim: FeishuOutboundClaim, *, holder: str) -> bool:
        receipt = None
        try:
            account = resolve_connector_account(
                self.store,
                provider=PROVIDER,
                account_id=claim.delivery.account_id,
                credential_version=claim.delivery.credential_version,
                require_managed_feishu=True,
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
                        claim.delivery.outbound_id,
                        holder,
                        claim.delivery.next_part_index,
                        PROVIDER,
                        claim.delivery.credential_version,
                    ),
                ).fetchone()
            if current is None:
                raise RuntimeError("outbound send claim is stale")
            part_delivery = replace(claim.delivery, payload=claim.part)
            receipt = await self.transport.send(account, part_delivery)
        except asyncio.CancelledError:
            release_outbound_claim(
                self.store,
                outbound_id=claim.delivery.outbound_id,
                holder=holder,
                error="connector_stopped",
            )
            raise
        except Exception as exc:
            retryable = isinstance(exc, FeishuTransportError) and exc.retryable
            collaboration_delivery = claim.delivery.source_kind == "collaboration"
            acceptance_uncertain = (
                isinstance(exc, FeishuTransportError)
                and exc.acceptance_uncertain
            )
            if retryable and collaboration_delivery and acceptance_uncertain:
                # Only transport loss without a provider response leaves the
                # side effect uncertain. Explicit provider/HTTP rejections prove
                # non-acceptance and retain the normal bounded retry behavior.
                mark_outbound_ambiguous(
                    self.store,
                    claim.delivery,
                    holder=holder,
                    error=f"provider_outcome_uncertain:{exc}",
                )
            elif retryable and claim.delivery.part_attempts < self.max_attempts:
                delay = min(
                    self.retry_max,
                    self.retry_seconds
                    * (2 ** max(0, claim.delivery.part_attempts - 1)),
                )
                release_outbound_claim(
                    self.store,
                    outbound_id=claim.delivery.outbound_id,
                    holder=holder,
                    error=str(exc),
                    next_attempt_at=time.time() + delay,
                )
            else:
                fail_outbound(
                    self.store,
                    claim.delivery,
                    holder=holder,
                    error=f"retry_exhausted:{exc}" if retryable else str(exc),
                )
            return False
        return advance_outbound(
            self.store,
            claim.delivery,
            holder=holder,
            part_count=len(claim.parts),
            provider_message_id=(
                receipt.provider_message_id if receipt is not None else None
            ),
        )


class FeishuConnector:
    """Own Feishu WebSocket receipt and canonical outbox delivery only."""

    provider = PROVIDER

    def __init__(
        self,
        store: ChannelIdentityStore,
        supervisor: Any,
        *,
        account_id: str,
        config: Mapping[str, Any],
        transport: FeishuHTTPTransport | None = None,
    ) -> None:
        self.store = store
        self.account_id = str(account_id or "").strip()
        if not self.account_id:
            raise ValueError("Feishu account_id is required")
        self.config = dict(config)
        self.holder = f"feishu-{id(self):x}"
        self.idle_seconds = min(float(config.get("outbound_retry_seconds", 2)), 1.0)
        self.claim_timeout = float(config.get("dispatch_claim_timeout_seconds", 1800))
        self.transport = transport or FeishuHTTPTransport(
            timeout=float(config.get("outbound_timeout_seconds", 30))
        )
        self.dispatcher = ChannelDispatcher(
            store,
            supervisor,
            provider=PROVIDER,
            account_id=self.account_id,
            turn_timeout=self.claim_timeout,
            dispatch_config=dict(config),
            media_config=dict(config),
        )
        self.sender = FeishuSender(store, self.transport, config=config)
        self.dispatch_concurrency = max(
            1, int(config.get("dispatch_concurrency", 4))
        )
        self._running = False
        self._dispatcher_task: asyncio.Task | None = None
        self._dispatch_tasks: set[asyncio.Task] = set()
        self._sender_task: asyncio.Task | None = None
        self._ws_session: FeishuWebSocketSession | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    async def start(self) -> None:
        account = resolve_connector_account(
            self.store,
            provider=PROVIDER,
            account_id=self.account_id,
            require_managed_feishu=True,
        )
        app_id, app_secret, domain = _credentials(account)
        cutoff = time.time() - self.claim_timeout
        with self.store.write() as conn:
            conn.execute(
                """
                UPDATE inbound_messages SET status='queued', claimed_by=NULL,
                    claimed_at=NULL, updated_at=?
                WHERE status='processing' AND claimed_at<? AND account_id=?
                """,
                (time.time(), cutoff, self.account_id),
            )
        recover_stale_outbound(
            self.store,
            provider=PROVIDER,
            claimed_before=cutoff,
            account_id=self.account_id,
        )
        self._loop = asyncio.get_running_loop()
        self._ws_session = FeishuWebSocketSession(
            self._build_ws_client(
                app_id=app_id,
                app_secret=app_secret,
                domain=domain,
                encrypt_key=str(account.credentials.get("encrypt_key") or ""),
                verification_token=str(
                    account.credentials.get("verification_token") or ""
                ),
            )
        )
        await self._ws_session.start()
        self._running = True
        self._dispatcher_task = asyncio.create_task(
            self._dispatch_loop(), name=f"feishu-dispatch-{self.account_id}"
        )
        self._sender_task = asyncio.create_task(
            self._sender_loop(), name=f"feishu-sender-{self.account_id}"
        )

    def _build_ws_client(
        self,
        *,
        app_id: str,
        app_secret: str,
        domain: str,
        encrypt_key: str,
        verification_token: str,
    ) -> Any:
        try:
            import lark_oapi as lark
            from lark_oapi.event.dispatcher_handler import EventDispatcherHandler
            from lark_oapi.ws import Client as FeishuWSClient
        except ImportError as exc:
            raise RuntimeError("lark-oapi is required for Feishu WebSocket receipt") from exc

        handler = (
            EventDispatcherHandler.builder(encrypt_key, verification_token)
            .register_p2_im_message_receive_v1(self._on_verified_event)
            .build()
        )
        kwargs: dict[str, Any] = {
            "app_id": app_id,
            "app_secret": app_secret,
            "log_level": lark.LogLevel.WARNING,
            "event_handler": handler,
        }
        try:
            from lark_oapi.core.const import FEISHU_DOMAIN, LARK_DOMAIN

            kwargs["domain"] = LARK_DOMAIN if domain == "lark" else FEISHU_DOMAIN
        except ImportError:
            pass
        return FeishuWSClient(**kwargs)

    def _on_verified_event(self, data: Any) -> None:
        loop = self._loop
        if loop is None or loop.is_closed() or not self._running:
            return
        loop.call_soon_threadsafe(
            asyncio.create_task,
            self._enqueue_verified_event(data),
        )

    async def _enqueue_verified_event(self, data: Any) -> None:
        enqueue_verified_event(
            self.store,
            account_id=self.account_id,
            data=data,
        )

    async def _dispatch_loop(self) -> None:
        while self._running:
            try:
                while len(self._dispatch_tasks) < self.dispatch_concurrency:
                    claim = self.dispatcher.claim_next(holder=self.holder)
                    if claim is None:
                        break
                    task = asyncio.create_task(
                        self._dispatch_one(claim),
                        name=f"feishu-turn-{claim['inbound_id']}",
                    )
                    self._dispatch_tasks.add(task)
                    task.add_done_callback(self._dispatch_tasks.discard)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(self.idle_seconds)

    async def _dispatch_one(self, claim: dict) -> None:
        try:
            await self.dispatcher.dispatch_claim(claim, holder=self.holder)
        except asyncio.CancelledError:
            self.dispatcher.release_claim(
                claim["inbound_id"],
                self.holder,
                reason="connector_stopped",
            )
            raise
        except Exception:
            return

    async def _sender_loop(self) -> None:
        while self._running:
            try:
                claim = claim_feishu_outbound(
                    self.store,
                    holder=self.holder,
                    account_id=self.account_id,
                )
                if claim is not None:
                    await self.sender.send_claim(claim, holder=self.holder)
                    continue
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(self.idle_seconds)

    async def close(self) -> None:
        self._running = False
        tasks = [
            task
            for task in (self._dispatcher_task, self._sender_task, *self._dispatch_tasks)
            if task is not None
        ]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._dispatcher_task = None
        self._sender_task = None
        self._dispatch_tasks.clear()
        if self._ws_session is not None:
            await self._ws_session.close()
            self._ws_session = None
        self._loop = None
        await self.transport.close()
