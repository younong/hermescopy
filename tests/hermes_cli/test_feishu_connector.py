"""Focused proofs for Feishu's canonical encrypted connector transport."""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from hermes_cli.channel_connectors.feishu import (
    FeishuHTTPTransport,
    FeishuSender,
    FeishuTransportError,
    claim_feishu_outbound,
    enqueue_verified_event,
    normalize_verified_event,
)
from hermes_cli.channel_dispatch import ChannelOutbox
from hermes_cli.channel_identity import (
    ChannelCrypto,
    ChannelIdentityStore,
    Keyring,
    register_connector_binding_for_owner,
    resolve_connector_account,
)
from hermes_cli.dashboard_auth.base import Session
from hermes_cli.dashboard_auth.owner_context import owner_context_from_session


def _owner(user_id: str):
    return owner_context_from_session(
        Session(
            user_id=user_id,
            email=f"{user_id}@example.com",
            display_name=user_id,
            org_id="org-a",
            provider="stub",
            expires_at=9_999_999_999,
            access_token="access",
            refresh_token="refresh",
        )
    )


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


def _register(store, *, owner_id="owner-a", actor_id="on_actor", chat_id="oc_chat"):
    return register_connector_binding_for_owner(
        store,
        owner=_owner(owner_id),
        provider="feishu",
        provider_account_id="cli_app",
        external_subject=actor_id,
        conversation_id=chat_id,
        credentials={
            "app_id": "cli_app",
            "app_secret": "app-secret",
            "domain": "feishu",
        },
    )


def _event(
    *,
    actor_id="on_actor",
    chat_id="oc_chat",
    message_id="om_message",
    content='{"text":"  hello Feishu  "}',
    chat_type="p2p",
):
    return SimpleNamespace(
        event=SimpleNamespace(
            sender=SimpleNamespace(
                sender_type="user",
                name="Alice",
                sender_id=SimpleNamespace(
                    union_id=actor_id,
                    open_id="ou_actor",
                    user_id="u_actor",
                ),
            ),
            message=SimpleNamespace(
                message_id=message_id,
                chat_id=chat_id,
                chat_type=chat_type,
                message_type="text",
                content=content,
                thread_id="omt_thread",
                root_id="om_root",
                parent_id="om_parent",
                create_time="1710000000123",
            ),
        )
    )


def _outbound_row(store):
    with store.read() as conn:
        return conn.execute("SELECT * FROM outbound_messages").fetchone()


def test_normalize_verified_event_preserves_verified_identity_and_conversation():
    envelope = normalize_verified_event(_event(chat_type="group"))

    assert envelope is not None
    assert envelope.provider_message_id == "om_message"
    assert envelope.conversation_id == "oc_chat"
    assert envelope.actor_id == "on_actor"
    assert envelope.actor_display_name == "Alice"
    assert envelope.payload_kind == "text"
    assert envelope.payload == "hello Feishu"
    assert envelope.conversation_kind == "group"
    assert envelope.thread_id == "omt_thread"
    assert envelope.parent_conversation_id == "oc_chat"
    assert envelope.reply_to_message_id == "om_parent"
    assert envelope.context_token == "om_message"
    assert envelope.occurred_at == pytest.approx(1_710_000_000.123)
    assert envelope.metadata == {
        "message_type": "text",
        "sender_type": "user",
        "open_id": "ou_actor",
        "user_id": "u_actor",
        "union_id": "on_actor",
    }


def test_verified_event_enqueues_through_encrypted_canonical_inbox(store):
    registered = _register(store)

    outcome = enqueue_verified_event(
        store,
        account_id=registered.account_id,
        data=_event(),
    )

    assert outcome is not None
    assert outcome.status == "queued"
    assert outcome.duplicate is False
    with store.read() as conn:
        row = conn.execute("SELECT * FROM inbound_messages").fetchone()
    assert row["binding_id"] == registered.binding_id
    assert row["payload_ciphertext"] != b"hello Feishu"
    assert store.crypto.decrypt_text(
        row["payload_ciphertext"],
        table="inbound_messages",
        record_id="om_message",
        field="payload",
        version=row["payload_key_version"],
    ) == "hello Feishu"


def test_payload_cannot_select_another_owner(store):
    first = _register(store, owner_id="owner-a")
    second = _register(
        store,
        owner_id="owner-b",
        actor_id="on_other",
        chat_id="oc_other",
    )
    assert first.account_id == second.account_id

    outcome = enqueue_verified_event(
        store,
        account_id=first.account_id,
        data=_event(content='{"text":"owner=owner-b"}'),
    )

    assert outcome is not None and outcome.status == "queued"
    with store.read() as conn:
        row = conn.execute(
            "SELECT binding_id FROM inbound_messages WHERE provider_message_id=?",
            ("om_message",),
        ).fetchone()
    assert row["binding_id"] == first.binding_id
    assert row["binding_id"] != second.binding_id


@pytest.mark.asyncio
async def test_http_transport_uses_account_credentials_and_canonical_delivery():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/tenant_access_token/internal"):
            assert json.loads(request.content) == {
                "app_id": "cli_app",
                "app_secret": "app-secret",
            }
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": "tenant-token", "expire": 7200},
            )
        assert request.headers["Authorization"] == "Bearer tenant-token"
        assert request.url.params.get("receive_id_type") == "chat_id"
        assert json.loads(request.content) == {
            "msg_type": "text",
            "content": json.dumps({"text": "hello"}, ensure_ascii=False),
            "uuid": "client-message",
            "receive_id": "oc_chat",
        }
        return httpx.Response(200, json={"code": 0, "data": {"message_id": "om_sent"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = FeishuHTTPTransport(client)
        account = SimpleNamespace(
            account_id="ca_one",
            credential_version=4,
            credentials={
                "app_id": "cli_app",
                "app_secret": "app-secret",
                "domain": "feishu",
            },
        )
        delivery = SimpleNamespace(
            payload="hello",
            client_message_id="client-message",
            conversation_id="oc_chat",
            context_token=None,
            next_part_index=0,
        )
        receipt = await transport.send(account, delivery)

    assert receipt.provider_message_id == "om_sent"
    assert [request.url.path for request in requests] == [
        "/open-apis/auth/v3/tenant_access_token/internal",
        "/open-apis/im/v1/messages",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "code", "retryable"),
    [
        (httpx.Response(429), "feishu_http_429", True),
        (httpx.Response(400), "feishu_http_400", False),
        (httpx.Response(200, json={"code": 99991400}), "feishu_code_99991400", True),
        (httpx.Response(200, json={"code": 230001}), "feishu_code_230001", False),
    ],
)
async def test_http_transport_classifies_retryable_failures(response, code, retryable):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: response)
    ) as client:
        transport = FeishuHTTPTransport(client)
        with pytest.raises(FeishuTransportError) as caught:
            await transport._post_json(
                "https://open.feishu.cn/example",
                headers=None,
                params=None,
                json_body={},
            )
    assert str(caught.value) == code
    assert caught.value.retryable is retryable


@pytest.mark.asyncio
async def test_sender_requeues_retryable_delivery_then_delivers(store):
    registered = _register(store)
    ChannelOutbox(store).enqueue_cron_result(
        owner_key=_owner("owner-a").owner_key,
        binding_id=registered.binding_id,
        fire_id="fire-a",
        payload="reply",
    )
    claim = claim_feishu_outbound(store, holder="sender")
    assert claim is not None

    class Transport:
        def __init__(self):
            self.calls = 0

        async def send(self, account, delivery):
            self.calls += 1
            if self.calls == 1:
                raise FeishuTransportError("network_error", retryable=True)

    sender = FeishuSender(
        store,
        Transport(),
        config={
            "outbound_retry_seconds": 0,
            "outbound_retry_max_seconds": 0,
            "outbound_max_attempts": 2,
        },
    )
    assert await sender.send_claim(claim, holder="sender") is False
    assert _outbound_row(store)["status"] == "queued"
    retry = claim_feishu_outbound(store, holder="sender")
    assert retry is not None
    assert await sender.send_claim(retry, holder="sender") is True
    assert _outbound_row(store)["status"] == "delivered"


def test_claim_uses_encrypted_account_credentials_not_payload(store):
    registered = _register(store)
    ChannelOutbox(store).enqueue_cron_result(
        owner_key=_owner("owner-a").owner_key,
        binding_id=registered.binding_id,
        fire_id="fire-credentials",
        payload='{"owner":"owner-b","app_secret":"attacker"}',
    )

    claim = claim_feishu_outbound(store, holder="sender")

    assert claim is not None
    assert claim.account == resolve_connector_account(
        store,
        provider="feishu",
        account_id=registered.account_id,
        credential_version=claim.delivery.credential_version,
    )
    assert claim.account.credentials["app_secret"] == "app-secret"
    assert claim.parts == ('{"owner":"owner-b","app_secret":"attacker"}',)
