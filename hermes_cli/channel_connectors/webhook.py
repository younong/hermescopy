"""Authenticated generic Webhook ingress, dispatch, and callback delivery."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import socket
import time
import uuid
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web
from aiohttp.abc import AbstractResolver, ResolveResult

from hermes_cli.channel_connectors.contracts import (
    NormalizedInboundEnvelope,
    OutboundDelivery,
)
from hermes_cli.channel_connectors.inbox import CanonicalInbox
from hermes_cli.channel_dispatch import (
    ChannelDispatcher,
    advance_outbound,
    claim_outbound,
    fail_outbound,
    recover_stale_outbound,
    release_outbound_claim,
    set_outbound_part_count,
)
from hermes_cli.channel_identity import ChannelIdentityStore, resolve_connector_account

PROVIDER = "webhook"
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8644
DEFAULT_WEBHOOK_PATH = "/webhooks"
MAX_WEBHOOK_BYTES = 1024 * 1024

_ROUTE_TOKEN_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)


@dataclass(frozen=True)
class ResolvedWebhookRoute:
    account_id: str
    binding_id: str
    actor_id: str
    conversation_id: str
    hmac_secret: str
    prompt_template: str
    allowed_events: tuple[str, ...]


@dataclass(frozen=True)
class WebhookOutboundClaim:
    delivery: OutboundDelivery
    response_url: str
    response_hmac_secret: str


class WebhookSendError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(code)


class _ExactAddressResolver(AbstractResolver):
    """Pin one validated public DNS resolution for one callback request."""

    def __init__(self) -> None:
        self._records: dict[tuple[str, int], list[ResolveResult]] = {}

    async def pin(self, host: str, port: int) -> None:
        loop = asyncio.get_running_loop()
        try:
            records = await loop.getaddrinfo(
                host,
                port,
                type=socket.SOCK_STREAM,
                flags=socket.AI_ADDRCONFIG,
            )
        except OSError as exc:
            raise WebhookSendError("callback_dns_error", retryable=True) from exc
        if not records:
            raise WebhookSendError("callback_dns_error", retryable=True)
        resolved: list[ResolveResult] = []
        for family, _type, protocol, _canonname, sockaddr in records:
            try:
                address = ipaddress.ip_address(sockaddr[0])
            except ValueError as exc:
                raise WebhookSendError(
                    "callback_address_unsafe", retryable=False
                ) from exc
            if not address.is_global:
                raise WebhookSendError("callback_address_unsafe", retryable=False)
            resolved.append(
                ResolveResult(
                    hostname=host,
                    host=str(address),
                    port=port,
                    family=family,
                    proto=protocol,
                    flags=socket.AI_NUMERICHOST | socket.AI_NUMERICSERV,
                )
            )
        self._records[(host, port)] = resolved

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: socket.AddressFamily = socket.AF_INET,
    ) -> list[ResolveResult]:
        del family
        records = self._records.get((host, port))
        if records is None:
            raise OSError("callback host was not prevalidated")
        return records

    async def close(self) -> None:
        self._records.clear()


def webhook_signed_bytes(
    *,
    path: str,
    delivery_id: str,
    timestamp: str,
    event_type: str,
    body: bytes,
) -> bytes:
    """Build the exact versioned byte sequence authenticated by ingress HMAC."""
    return b"\n".join(
        (
            b"v1",
            path.encode("utf-8"),
            delivery_id.encode("utf-8"),
            timestamp.encode("ascii"),
            event_type.encode("utf-8"),
            body,
        )
    )


def render_webhook_prompt(
    template: str,
    payload: Mapping[str, Any],
    *,
    event_type: str,
) -> str:
    """Render account-owned dot-path placeholders without evaluating code."""
    if not template:
        rendered = json.dumps(payload, indent=2, sort_keys=True)[:4000]
        return f"Webhook event '{event_type}':\n\n```json\n{rendered}\n```"

    output: list[str] = []
    cursor = 0
    while cursor < len(template):
        start = template.find("{", cursor)
        if start < 0:
            output.append(template[cursor:])
            break
        end = template.find("}", start + 1)
        if end < 0:
            output.append(template[cursor:])
            break
        output.append(template[cursor:start])
        key = template[start + 1 : end]
        if not key or any(
            not (part.isalnum() or part in "_.") for part in key
        ):
            output.append(template[start : end + 1])
            cursor = end + 1
            continue
        if key == "__raw__":
            value: Any = payload
        else:
            value = payload
            for part in key.split("."):
                if not isinstance(value, Mapping) or part not in value:
                    value = None
                    break
                value = value[part]
        if value is None:
            output.append(template[start : end + 1])
        elif isinstance(value, (dict, list)):
            output.append(json.dumps(value, sort_keys=True)[:2000])
        else:
            output.append(str(value))
        cursor = end + 1
    return "".join(output)


def _validate_route_token(token: str) -> bool:
    return 24 <= len(token) <= 128 and all(char in _ROUTE_TOKEN_CHARS for char in token)


def _validate_callback_url(value: object) -> str:
    url = str(value or "").strip()
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise RuntimeError("callback_url_invalid") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or port not in {None, 443}
    ):
        raise RuntimeError("callback_url_invalid")
    try:
        ipaddress.ip_address(parsed.hostname)
    except ValueError:
        return url
    raise RuntimeError("callback_url_invalid")


class WebhookIngress:
    """Authenticate exact account routes and enqueue normalized deliveries."""

    def __init__(self, store: ChannelIdentityStore, *, config: Mapping[str, Any]) -> None:
        self._store = store
        self._inbox = CanonicalInbox(store, provider=PROVIDER)
        self._host = str(config.get("host") or DEFAULT_HOST)
        self._port = int(config.get("port", DEFAULT_PORT))
        self._webhook_path = self._normalize_path(
            config.get("webhook_path", DEFAULT_WEBHOOK_PATH)
        ).rstrip("/")
        self._health_path = self._normalize_path(config.get("health_path", "/health"))
        self._max_body_bytes = min(
            MAX_WEBHOOK_BYTES,
            max(1, int(config.get("max_body_bytes", MAX_WEBHOOK_BYTES))),
        )
        self._signature_tolerance = max(
            1, int(config.get("signature_tolerance_seconds", 300))
        )
        self._rate_limit = max(1, int(config.get("rate_limit", 30)))
        self._rate_windows: dict[str, deque[float]] = {}
        self._runner: web.AppRunner | None = None
        self._running = False
        self._accepted_count = 0
        self._duplicate_count = 0
        self._rejected_count = 0

    @staticmethod
    def _normalize_path(path: Any) -> str:
        raw = str(path or "").strip() or "/"
        return raw if raw.startswith("/") else f"/{raw}"

    @property
    def is_connected(self) -> bool:
        return self._running

    async def start(self) -> None:
        if not self._has_active_account():
            raise RuntimeError("webhook account unavailable")
        app = web.Application(client_max_size=self._max_body_bytes)
        app.router.add_get(self._health_path, self._handle_health)
        app.router.add_post(
            f"{self._webhook_path}/{{account_token}}", self._handle_webhook
        )
        self._runner = web.AppRunner(app)
        try:
            await self._runner.setup()
            await web.TCPSite(self._runner, self._host, self._port).start()
        except BaseException:
            await self.close()
            raise
        self._running = True

    async def close(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        self._running = False

    async def _handle_health(self, request: web.Request) -> web.Response:
        del request
        return web.json_response(
            {
                "status": "ok",
                "provider": PROVIDER,
                "accepted": self._accepted_count,
                "duplicates": self._duplicate_count,
                "rejected": self._rejected_count,
            }
        )

    async def _handle_webhook(self, request: web.Request) -> web.Response:
        token = str(request.match_info.get("account_token") or "").strip()
        if not _validate_route_token(token):
            self._rejected_count += 1
            return web.Response(status=401)
        route = self._resolve_route(token)
        if route is None:
            self._rejected_count += 1
            return web.Response(status=401)
        if (request.content_length or 0) > self._max_body_bytes:
            return web.Response(status=413)
        try:
            raw_body = await request.read()
        except Exception:
            return web.Response(status=400)
        if len(raw_body) > self._max_body_bytes:
            return web.Response(status=413)

        delivery_id = str(request.headers.get("X-Hermes-Webhook-Id") or "").strip()
        timestamp = str(
            request.headers.get("X-Hermes-Webhook-Timestamp") or ""
        ).strip()
        event_type = str(
            request.headers.get("X-Hermes-Webhook-Event") or ""
        ).strip()
        signature = str(
            request.headers.get("X-Hermes-Webhook-Signature") or ""
        ).strip()
        if not self._verify_signature(
            request_path=request.path,
            delivery_id=delivery_id,
            timestamp=timestamp,
            event_type=event_type,
            body=raw_body,
            signature=signature,
            secret=route.hmac_secret,
        ):
            self._rejected_count += 1
            return web.Response(status=401)
        if not self._within_rate_limit(route.account_id):
            return web.Response(status=429)
        if route.allowed_events and event_type not in route.allowed_events:
            return web.json_response({"status": "ignored"}, status=202)
        try:
            payload = json.loads(raw_body)
        except (TypeError, json.JSONDecodeError, UnicodeDecodeError):
            return web.Response(status=400)
        if not isinstance(payload, dict):
            return web.Response(status=400)

        envelope = NormalizedInboundEnvelope(
            provider_message_id=delivery_id,
            conversation_id=route.conversation_id,
            actor_id=route.actor_id,
            payload_kind="text",
            payload=render_webhook_prompt(
                route.prompt_template, payload, event_type=event_type
            ),
            conversation_kind="webhook",
            occurred_at=float(timestamp),
            metadata={"event_type": event_type},
        )
        with self._store.write() as conn:
            outcome = self._inbox.commit(
                conn,
                account_id=route.account_id,
                envelope=envelope,
                result=True,
            )
        if outcome.duplicate:
            self._duplicate_count += 1
            return web.json_response({"status": "duplicate"}, status=200)
        if outcome.status != "queued":
            self._rejected_count += 1
            return web.Response(status=400)
        self._accepted_count += 1
        return web.json_response({"status": "accepted"}, status=202)

    def _has_active_account(self) -> bool:
        with self._store.read() as conn:
            row = conn.execute(
                "SELECT 1 FROM connector_accounts "
                "WHERE provider=? AND status='active' LIMIT 1",
                (PROVIDER,),
            ).fetchone()
        return row is not None

    def _resolve_route(self, token: str) -> ResolvedWebhookRoute | None:
        with self._store.read() as conn:
            rows = conn.execute(
                """
                SELECT a.account_id, a.credential_version,
                       b.binding_id, b.peer_ciphertext, b.peer_key_version,
                       e.external_identity_id, e.subject_ciphertext,
                       e.subject_key_version
                FROM connector_accounts a
                JOIN channel_bindings b ON b.account_id=a.account_id
                                       AND b.status='active'
                JOIN external_identities e ON e.external_identity_id=b.external_identity_id
                                          AND e.provider=a.provider
                                          AND e.status='active'
                JOIN canonical_users u ON u.canonical_user_id=e.canonical_user_id
                                      AND u.status='active'
                JOIN owner_bindings o ON o.canonical_user_id=u.canonical_user_id
                WHERE a.provider=? AND a.provider_account_id=? AND a.status='active'
                LIMIT 2
                """,
                (PROVIDER, token),
            ).fetchall()
        if len(rows) != 1:
            return None
        row = rows[0]
        try:
            account = resolve_connector_account(
                self._store,
                provider=PROVIDER,
                account_id=str(row["account_id"]),
                credential_version=int(row["credential_version"]),
            )
            secret = account.credentials.get("hmac_secret")
            if not isinstance(secret, str) or len(secret.encode("utf-8")) < 32:
                return None
            raw_events = account.credentials.get("allowed_events") or []
            if not isinstance(raw_events, list) or any(
                not isinstance(event, str) or not event.strip()
                for event in raw_events
            ):
                return None
            template = account.credentials.get("prompt_template", "")
            if not isinstance(template, str):
                return None
            actor_id = self._store.crypto.decrypt_text(
                row["subject_ciphertext"],
                table="external_identities",
                record_id=row["external_identity_id"],
                field="subject",
                version=row["subject_key_version"],
            )
            conversation_id = self._store.crypto.decrypt_text(
                row["peer_ciphertext"],
                table="channel_bindings",
                record_id=row["binding_id"],
                field="peer",
                version=row["peer_key_version"],
            )
        except Exception:
            return None
        return ResolvedWebhookRoute(
            account_id=str(row["account_id"]),
            binding_id=str(row["binding_id"]),
            actor_id=actor_id,
            conversation_id=conversation_id,
            hmac_secret=secret,
            prompt_template=template,
            allowed_events=tuple(event.strip() for event in raw_events),
        )

    def _verify_signature(
        self,
        *,
        request_path: str,
        delivery_id: str,
        timestamp: str,
        event_type: str,
        body: bytes,
        signature: str,
        secret: str,
    ) -> bool:
        if (
            not delivery_id
            or len(delivery_id) > 256
            or not event_type
            or len(event_type) > 128
            or not signature.startswith("v1=")
            or len(signature) != 67
        ):
            return False
        try:
            parsed_timestamp = int(timestamp)
            bytes.fromhex(signature[3:])
        except (TypeError, ValueError):
            return False
        if abs(int(time.time()) - parsed_timestamp) > self._signature_tolerance:
            return False
        expected = hmac.new(
            secret.encode("utf-8"),
            webhook_signed_bytes(
                path=request_path,
                delivery_id=delivery_id,
                timestamp=timestamp,
                event_type=event_type,
                body=body,
            ),
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected.lower(), signature[3:].lower())

    def _within_rate_limit(self, account_id: str) -> bool:
        now = time.time()
        window = self._rate_windows.setdefault(account_id, deque())
        cutoff = now - 60
        while window and window[0] < cutoff:
            window.popleft()
        if len(window) >= self._rate_limit:
            return False
        window.append(now)
        return True


def claim_webhook_outbound(
    store: ChannelIdentityStore,
    *,
    holder: str,
) -> WebhookOutboundClaim | None:
    delivery = claim_outbound(store, provider=PROVIDER, holder=holder)
    if delivery is None:
        return None
    try:
        account = resolve_connector_account(
            store,
            provider=PROVIDER,
            account_id=delivery.account_id,
            credential_version=delivery.credential_version,
        )
        response_url = _validate_callback_url(account.credentials.get("response_url"))
        response_secret = account.credentials.get("response_hmac_secret")
        if (
            not isinstance(response_secret, str)
            or len(response_secret.encode("utf-8")) < 32
        ):
            raise RuntimeError("callback_credential_unavailable")
        set_outbound_part_count(store, delivery, holder=holder, part_count=1)
    except Exception as exc:
        release_outbound_claim(
            store,
            outbound_id=delivery.outbound_id,
            holder=holder,
            error=f"claim_error:{type(exc).__name__}",
        )
        raise
    return WebhookOutboundClaim(
        delivery=delivery,
        response_url=response_url,
        response_hmac_secret=response_secret,
    )


class WebhookSender:
    def __init__(self, store: ChannelIdentityStore, *, config: Mapping[str, Any]) -> None:
        self.store = store
        self.retry_seconds = float(config.get("outbound_retry_seconds", 2))
        self.retry_max = float(config.get("outbound_retry_max_seconds", 300))
        self.max_attempts = int(config.get("outbound_max_attempts", 8))
        self.timeout = float(config.get("outbound_timeout_seconds", 30))

    async def send_claim(self, claim: WebhookOutboundClaim, *, holder: str) -> bool:
        try:
            resolve_connector_account(
                self.store,
                provider=PROVIDER,
                account_id=claim.delivery.account_id,
                credential_version=claim.delivery.credential_version,
            )
            with self.store.read() as conn:
                current = conn.execute(
                    """
                    SELECT 1 FROM outbound_messages o
                    JOIN connector_accounts a ON a.account_id=o.account_id
                    JOIN channel_bindings b ON b.binding_id=o.binding_id
                    WHERE o.outbound_id=? AND o.status='sending' AND o.claimed_by=?
                      AND o.next_chunk_index=0 AND a.provider=? AND a.status='active'
                      AND a.credential_version=? AND b.status='active'
                    """,
                    (
                        claim.delivery.outbound_id,
                        holder,
                        PROVIDER,
                        claim.delivery.credential_version,
                    ),
                ).fetchone()
            if current is None:
                raise RuntimeError("outbound send claim is stale")
            await self._send(claim)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            retryable = isinstance(exc, WebhookSendError) and exc.retryable
            if retryable and claim.delivery.part_attempts < self.max_attempts:
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
            part_count=1,
        )

    async def _send(self, claim: WebhookOutboundClaim) -> None:
        parsed = urlsplit(claim.response_url)
        resolver = _ExactAddressResolver()
        await resolver.pin(parsed.hostname or "", 443)
        body = json.dumps(
            {
                "id": claim.delivery.client_message_id,
                "conversation_id": claim.delivery.conversation_id,
                "text": claim.delivery.payload,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        timestamp = str(int(time.time()))
        signature = hmac.new(
            claim.response_hmac_secret.encode("utf-8"),
            b"v1\n" + timestamp.encode("ascii") + b"\n" + body,
            hashlib.sha256,
        ).hexdigest()
        try:
            timeout = aiohttp.ClientTimeout(total=self.timeout)
            connector = aiohttp.TCPConnector(
                resolver=resolver,
                use_dns_cache=False,
            )
            async with aiohttp.ClientSession(
                timeout=timeout,
                trust_env=False,
                connector=connector,
            ) as client:
                async with client.post(
                    claim.response_url,
                    data=body,
                    allow_redirects=False,
                    headers={
                        "Content-Type": "application/json",
                        "X-Hermes-Webhook-Id": claim.delivery.client_message_id,
                        "X-Hermes-Webhook-Timestamp": timestamp,
                        "X-Hermes-Webhook-Signature": f"v1={signature}",
                    },
                ) as response:
                    status = response.status
        except WebhookSendError:
            raise
        except Exception as exc:
            raise WebhookSendError("callback_network_error", retryable=True) from exc
        finally:
            await resolver.close()
        if status < 200 or status >= 300:
            retryable = status == 429 or status >= 500
            raise WebhookSendError(
                f"callback_http_{status}", retryable=retryable
            )


class WebhookService:
    """Own the Webhook listener and canonical Owner-Worker queue loops."""

    provider = PROVIDER

    def __init__(
        self,
        store: ChannelIdentityStore,
        supervisor: Any,
        *,
        config: Mapping[str, Any],
    ) -> None:
        self.store = store
        self.holder = f"webhook-{uuid.uuid4().hex}"
        self.claim_timeout = float(
            config.get("dispatch_claim_timeout_seconds", 1800)
        )
        self.idle_seconds = min(
            float(config.get("dispatch_retry_seconds", 2)), 1.0
        )
        self.ingress = WebhookIngress(store, config=config)
        self.dispatcher = ChannelDispatcher(
            store,
            supervisor,
            provider=PROVIDER,
            turn_timeout=self.claim_timeout,
            dispatch_config=dict(config),
            media_config=dict(config),
        )
        self.sender = WebhookSender(store, config=config)
        self.dispatch_concurrency = max(
            1, int(config.get("dispatch_concurrency", 4))
        )
        self._running = False
        self._tasks: set[asyncio.Task] = set()
        self._dispatch_tasks: set[asyncio.Task] = set()

    async def start(self) -> None:
        cutoff = time.time() - self.claim_timeout
        with self.store.write() as conn:
            conn.execute(
                """
                UPDATE inbound_messages SET status='queued', claimed_by=NULL,
                    claimed_at=NULL, updated_at=?
                WHERE status='processing' AND claimed_at<? AND account_id IN (
                    SELECT account_id FROM connector_accounts WHERE provider=?
                )
                """,
                (time.time(), cutoff, PROVIDER),
            )
        recover_stale_outbound(
            self.store,
            provider=PROVIDER,
            claimed_before=cutoff,
        )
        await self.ingress.start()
        self._running = True
        self._start_task(self._dispatch_loop(), "webhook-dispatch")
        self._start_task(self._sender_loop(), "webhook-sender")

    async def _dispatch_loop(self) -> None:
        while self._running:
            while len(self._dispatch_tasks) < self.dispatch_concurrency:
                claim = self.dispatcher.claim_next(holder=self.holder)
                if claim is None:
                    break
                task = asyncio.create_task(
                    self._dispatch_one(claim),
                    name=f"webhook-turn-{claim['inbound_id']}",
                )
                self._dispatch_tasks.add(task)
                task.add_done_callback(self._dispatch_tasks.discard)
            await asyncio.sleep(self.idle_seconds)

    async def _dispatch_one(self, claim: dict) -> None:
        try:
            await self.dispatcher.dispatch_claim(claim, holder=self.holder)
        except asyncio.CancelledError:
            raise
        except Exception:
            return

    async def _sender_loop(self) -> None:
        while self._running:
            try:
                claim = claim_webhook_outbound(self.store, holder=self.holder)
                if claim is not None:
                    await self.sender.send_claim(claim, holder=self.holder)
                    continue
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(self.idle_seconds)

    def _start_task(self, coroutine: Any, name: str) -> None:
        task = asyncio.create_task(coroutine, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def close(self) -> None:
        self._running = False
        await self.ingress.close()
        tasks = [*self._tasks, *self._dispatch_tasks]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._dispatch_tasks.clear()
