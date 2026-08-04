"""Focused proofs for authenticated canonical Webhook ingress and callbacks."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import socket
import time

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from hermes_cli.channel_connectors.webhook import (
    WebhookIngress,
    WebhookSender,
    _ExactAddressResolver,
    _validate_callback_url,
    claim_webhook_outbound,
    webhook_signed_bytes,
)
from hermes_cli.channel_dispatch import ChannelOutbox
from hermes_cli.channel_identity import (
    ChannelCrypto,
    ChannelIdentityStore,
    Keyring,
    register_connector_binding_for_owner,
)
from hermes_cli.dashboard_auth.base import Session
from hermes_cli.dashboard_auth.owner_context import owner_context_from_session

TOKEN = "route_token_0123456789abcdef0123456789"
SECRET = "ingress-secret-0123456789abcdef0123456789"
RESPONSE_SECRET = "response-secret-0123456789abcdef012345"


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_OWNER_SECRET", "owner-secret")
    crypto = ChannelCrypto(
        lookup=Keyring(keys={1: b"l" * 32}, active_version=1),
        encryption=Keyring(keys={1: b"e" * 32}, active_version=1),
    )
    return ChannelIdentityStore(
        crypto,
        tmp_path / "control-plane",
        global_home=tmp_path,
    )


def _owner():
    return owner_context_from_session(
        Session(
            user_id="owner-a",
            email="owner@example.com",
            display_name="Owner",
            org_id="org-a",
            provider="stub",
            expires_at=9_999_999_999,
            access_token="access",
            refresh_token="refresh",
        )
    )


def _register(
    store,
    *,
    allowed_events=None,
    response_url="https://callback.example/result",
):
    return register_connector_binding_for_owner(
        store,
        owner=_owner(),
        provider="webhook",
        provider_account_id=TOKEN,
        external_subject="verified-hook-actor",
        conversation_id="hook-conversation",
        credentials={
            "hmac_secret": SECRET,
            "prompt_template": "Deploy {deployment.name}: {status}",
            "allowed_events": allowed_events or ["deploy.completed"],
            "response_url": response_url,
            "response_hmac_secret": RESPONSE_SECRET,
        },
    )


def _ingress(store, **overrides):
    config = {
        "host": "127.0.0.1",
        "port": 0,
        "rate_limit": 3,
        "signature_tolerance_seconds": 300,
    }
    config.update(overrides)
    return WebhookIngress(store, config=config)


def _signed_headers(
    body: bytes,
    *,
    event="deploy.completed",
    delivery="delivery-1",
    timestamp=None,
):
    timestamp = str(int(time.time()) if timestamp is None else timestamp)
    path = f"/webhooks/{TOKEN}"
    digest = hmac.new(
        SECRET.encode(),
        webhook_signed_bytes(
            path=path,
            delivery_id=delivery,
            timestamp=timestamp,
            event_type=event,
            body=body,
        ),
        hashlib.sha256,
    ).hexdigest()
    return {
        "Content-Type": "application/json",
        "X-Hermes-Webhook-Id": delivery,
        "X-Hermes-Webhook-Timestamp": timestamp,
        "X-Hermes-Webhook-Event": event,
        "X-Hermes-Webhook-Signature": f"v1={digest}",
    }


async def _client(ingress):
    app = web.Application()
    app.router.add_get("/health", ingress._handle_health)
    app.router.add_post("/webhooks/{account_token}", ingress._handle_webhook)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


class TestWebhookCanonicalIngress:
    @pytest.mark.anyio
    async def test_signed_delivery_enters_encrypted_inbox(self, store):
        registered = _register(store)
        body = json.dumps(
            {"deployment": {"name": "production"}, "status": "ready"}
        ).encode()
        client = await _client(_ingress(store))
        try:
            response = await client.post(
                f"/webhooks/{TOKEN}", data=body, headers=_signed_headers(body)
            )
        finally:
            await client.close()

        assert response.status == 202
        with store.read() as conn:
            row = conn.execute("SELECT * FROM inbound_messages").fetchone()
        assert row["account_id"] == registered.account_id
        assert row["binding_id"] == registered.binding_id
        assert row["provider_message_id"] == "delivery-1"
        assert row["status"] == "queued"
        prompt = store.crypto.decrypt_text(
            row["payload_ciphertext"],
            table="inbound_messages",
            record_id="delivery-1",
            field="payload",
            version=row["payload_key_version"],
        )
        assert prompt == "Deploy production: ready"

    @pytest.mark.anyio
    async def test_payload_owner_fields_cannot_select_owner(self, store):
        registered = _register(store)
        body = json.dumps(
            {
                "deployment": {"name": "production"},
                "status": "ready",
                "owner": "attacker",
                "profile": "legacy",
                "conversation_id": "another-channel",
            }
        ).encode()
        client = await _client(_ingress(store))
        try:
            response = await client.post(
                f"/webhooks/{TOKEN}", data=body, headers=_signed_headers(body)
            )
        finally:
            await client.close()
        assert response.status == 202
        with store.read() as conn:
            row = conn.execute("SELECT binding_id FROM inbound_messages").fetchone()
        assert row["binding_id"] == registered.binding_id

    @pytest.mark.anyio
    async def test_unknown_route_and_bad_signature_share_auth_failure(self, store):
        _register(store)
        body = b'{"deployment":{"name":"prod"},"status":"ready"}'
        client = await _client(_ingress(store))
        try:
            unknown = await client.post(
                "/webhooks/unknown_route_token_0123456789abcdef",
                data=body,
                headers=_signed_headers(body),
            )
            headers = _signed_headers(body)
            headers["X-Hermes-Webhook-Signature"] = "v1=" + "0" * 64
            invalid = await client.post(
                f"/webhooks/{TOKEN}", data=body, headers=headers
            )
        finally:
            await client.close()
        assert unknown.status == invalid.status == 401
        with store.read() as conn:
            assert conn.execute("SELECT COUNT(*) FROM inbound_messages").fetchone()[0] == 0

    @pytest.mark.anyio
    async def test_stale_signature_rejected(self, store):
        _register(store)
        body = b'{"deployment":{"name":"prod"},"status":"ready"}'
        client = await _client(_ingress(store))
        try:
            response = await client.post(
                f"/webhooks/{TOKEN}",
                data=body,
                headers=_signed_headers(body, timestamp=int(time.time()) - 301),
            )
        finally:
            await client.close()
        assert response.status == 401

    @pytest.mark.anyio
    async def test_authentication_precedes_rate_limiting(self, store):
        _register(store)
        body = b'{"deployment":{"name":"prod"},"status":"ready"}'
        client = await _client(_ingress(store, rate_limit=1))
        try:
            for _ in range(3):
                headers = _signed_headers(body)
                headers["X-Hermes-Webhook-Signature"] = "v1=" + "0" * 64
                assert (
                    await client.post(f"/webhooks/{TOKEN}", data=body, headers=headers)
                ).status == 401
            valid = await client.post(
                f"/webhooks/{TOKEN}", data=body, headers=_signed_headers(body)
            )
        finally:
            await client.close()
        assert valid.status == 202

    @pytest.mark.anyio
    async def test_event_filter_and_duplicate_are_durable(self, store):
        _register(store)
        body = b'{"deployment":{"name":"prod"},"status":"ready"}'
        first = await _client(_ingress(store))
        try:
            ignored = await first.post(
                f"/webhooks/{TOKEN}",
                data=body,
                headers=_signed_headers(body, event="deploy.started"),
            )
            ignored_payload = await ignored.json()
            accepted = await first.post(
                f"/webhooks/{TOKEN}", data=body, headers=_signed_headers(body)
            )
        finally:
            await first.close()
        assert ignored.status == 202
        assert ignored_payload == {"status": "ignored"}
        assert accepted.status == 202

        second = await _client(_ingress(store))
        try:
            duplicate = await second.post(
                f"/webhooks/{TOKEN}", data=body, headers=_signed_headers(body)
            )
            duplicate_payload = await duplicate.json()
        finally:
            await second.close()
        assert duplicate.status == 200
        assert duplicate_payload == {"status": "duplicate"}


class TestWebhookLifecycle:
    @pytest.mark.anyio
    async def test_listener_requires_active_account(self, store):
        with pytest.raises(RuntimeError, match="account unavailable"):
            await _ingress(store).start()

    @pytest.mark.anyio
    async def test_listener_starts_and_closes_with_active_account(self, store):
        _register(store)
        ingress = _ingress(store)
        try:
            await ingress.start()
            assert ingress.is_connected is True
        finally:
            await ingress.close()
        assert ingress.is_connected is False


class TestWebhookCallbackSender:
    def test_callback_url_requires_exact_https_origin(self):
        assert _validate_callback_url("https://callback.example/result") == (
            "https://callback.example/result"
        )
        for value in (
            "http://callback.example/result",
            "https://user:pass@callback.example/result",
            "https://127.0.0.1/result",
            "https://callback.example:8443/result",
            "https://callback.example/result#fragment",
        ):
            with pytest.raises(RuntimeError, match="callback_url_invalid"):
                _validate_callback_url(value)

    @pytest.mark.anyio
    async def test_callback_dns_rejects_any_non_public_address(self, monkeypatch):
        async def fake_getaddrinfo(*args, **kwargs):
            return [
                (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    socket.IPPROTO_TCP,
                    "",
                    ("8.8.8.8", 443),
                ),
                (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    socket.IPPROTO_TCP,
                    "",
                    ("127.0.0.1", 443),
                ),
            ]

        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo)
        resolver = _ExactAddressResolver()
        with pytest.raises(RuntimeError, match="callback_address_unsafe"):
            await resolver.pin("callback.example", 443)

    def test_claim_uses_exact_credential_version(self, store):
        registered = _register(store)
        outbound_id = ChannelOutbox(store).enqueue_cron_result(
            owner_key=registered.owner_key,
            binding_id=registered.binding_id,
            fire_id="webhook-callback",
            payload="callback response",
        )
        claim = claim_webhook_outbound(store, holder="sender-a")
        assert claim is not None
        assert claim.delivery.outbound_id == outbound_id
        assert claim.response_url == "https://callback.example/result"

    @pytest.mark.anyio
    async def test_sender_marks_successful_callback_delivered(self, store, monkeypatch):
        registered = _register(store)
        outbound_id = ChannelOutbox(store).enqueue_cron_result(
            owner_key=registered.owner_key,
            binding_id=registered.binding_id,
            fire_id="webhook-send",
            payload="callback response",
        )
        claim = claim_webhook_outbound(store, holder="sender-a")
        assert claim is not None
        sender = WebhookSender(store, config={})
        captured = {}

        async def fake_send(outbound_claim):
            captured["url"] = outbound_claim.response_url
            captured["payload"] = outbound_claim.delivery.payload

        monkeypatch.setattr(sender, "_send", fake_send)
        assert await sender.send_claim(claim, holder="sender-a") is True
        assert captured == {
            "url": "https://callback.example/result",
            "payload": "callback response",
        }
        with store.read() as conn:
            row = conn.execute(
                "SELECT * FROM outbound_messages WHERE outbound_id=?", (outbound_id,)
            ).fetchone()
        assert row["status"] == "delivered"
        assert row["payload_ciphertext"] is None
