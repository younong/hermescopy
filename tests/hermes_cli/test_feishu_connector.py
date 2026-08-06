"""Focused proofs for Feishu's canonical encrypted connector transport."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from hermes_cli.channel_connectors.contracts import NormalizedInboundEnvelope
from hermes_cli.channel_connectors.feishu import (
    FeishuConnector,
    FeishuHTTPTransport,
    FeishuSender,
    FeishuTransportError,
    claim_feishu_outbound,
    enqueue_verified_event,
    normalize_verified_event,
)
from hermes_cli.channel_connectors.feishu_ws import FeishuWebSocketSession
from hermes_cli.channel_dispatch import ChannelOutbox
from hermes_cli.channel_dispatch.dispatcher import ChannelDispatcher
from hermes_cli.channel_dispatch.outbox import recover_stale_outbound
from hermes_cli.channel_identity import (
    ChannelCrypto,
    ChannelIdentityStore,
    Keyring,
    register_managed_feishu_account_for_owner,
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


def _register(
    store,
    *,
    owner_id="owner-a",
    actor_id="on_actor",
    chat_id="oc_chat",
    provider_account_id="cli_app",
):
    return register_managed_feishu_account_for_owner(
        store,
        owner=_owner(owner_id),
        provider_account_id=provider_account_id,
        external_subject=actor_id,
        conversation_id=chat_id,
        credentials={
            "app_id": provider_account_id,
            "app_secret": "app-secret",
            "domain": "feishu",
            "bot_open_id": provider_account_id,
        },
        employee_profile={
            "schema_version": 1,
            "name": f"employee-{owner_id}",
            "model_registration_id": "registration-a",
            "system_prompt": "You are a focused Feishu employee.",
            "toolsets": [],
            "skills": [],
            "mcp_servers": [],
            "workspace_relative_path": "employees/researcher",
            "knowledge_relative_paths": [],
            "max_iterations": 20,
        },
    )


def _event(
    *,
    actor_id="on_actor",
    chat_id="oc_chat",
    message_id="om_message",
    content='{"text":"  hello Feishu  "}',
    chat_type="p2p",
    sender_type="user",
    mentions=(),
    thread_id="omt_thread",
    root_id="om_root",
    parent_id="om_parent",
):
    return SimpleNamespace(
        event=SimpleNamespace(
            sender=SimpleNamespace(
                sender_type=sender_type,
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
                mentions=mentions,
                thread_id=thread_id,
                root_id=root_id,
                parent_id=parent_id,
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


def test_direct_messages_ignore_thread_metadata_and_root_is_not_direct_reply():
    direct = normalize_verified_event(_event(chat_type="p2p"))
    group_root_only = normalize_verified_event(
        _event(chat_type="group", parent_id="")
    )

    assert direct is not None and direct.thread_id is None
    assert direct.parent_conversation_id is None
    assert direct.reply_to_message_id == "om_parent"
    assert group_root_only is not None
    assert group_root_only.thread_id == "omt_thread"
    assert group_root_only.reply_to_message_id is None


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


def test_first_valid_direct_message_binds_human_not_api_verified_bot(store):
    registered = register_managed_feishu_account_for_owner(
        store,
        owner=_owner("owner-a"),
        provider_account_id="cli_app",
        external_subject="ou_verified_bot",
        conversation_id=None,
        credentials={
            "app_id": "cli_app",
            "app_secret": "app-secret",
            "domain": "feishu",
            "bot_open_id": "ou_verified_bot",
        },
        employee_profile={
            "schema_version": 1,
            "model_registration_id": "registration-a",
            "system_prompt": "You are a focused Feishu employee.",
            "toolsets": [],
            "skills": [],
            "mcp_servers": [],
            "workspace_relative_path": "employees/researcher",
            "knowledge_relative_paths": [],
            "max_iterations": 20,
        },
    )

    accepted = enqueue_verified_event(
        store,
        account_id=registered.account_id,
        data=_event(actor_id="on_human", chat_id="oc_real_direct"),
    )

    assert accepted is not None and accepted.status == "queued"
    with store.read() as conn:
        binding = conn.execute(
            """
            SELECT e.subject_lookup_hash FROM channel_bindings b
            JOIN external_identities e ON e.external_identity_id=b.external_identity_id
            WHERE b.account_id=?
            """,
            (registered.account_id,),
        ).fetchone()
    assert binding["subject_lookup_hash"] == store.crypto.lookup_hash(
        "external-subject:feishu", "on_human"
    )


def test_first_valid_group_message_creates_real_account_owned_binding(store):
    registered = register_managed_feishu_account_for_owner(
        store,
        owner=_owner("owner-a"),
        provider_account_id="cli_app",
        external_subject="cli_app",
        conversation_id=None,
        credentials={
            "app_id": "cli_app",
            "app_secret": "app-secret",
            "domain": "feishu",
            "bot_open_id": "cli_app",
        },
        employee_profile={
            "schema_version": 1,
            "model_registration_id": "registration-a",
            "system_prompt": "You are a focused Feishu employee.",
            "toolsets": [],
            "skills": [],
            "mcp_servers": [],
            "workspace_relative_path": "employees/researcher",
            "knowledge_relative_paths": [],
            "max_iterations": 20,
        },
    )
    mention = SimpleNamespace(key="@_user_1", id=SimpleNamespace(open_id="cli_app"))

    accepted = enqueue_verified_event(
        store,
        account_id=registered.account_id,
        data=_event(chat_type="group", mentions=(mention,)),
    )

    assert accepted is not None and accepted.status == "queued"
    with store.read() as conn:
        binding = conn.execute(
            "SELECT binding_id, peer_lookup_hash FROM channel_bindings WHERE account_id=?",
            (registered.account_id,),
        ).fetchone()
        inbound = conn.execute(
            "SELECT binding_id FROM inbound_messages WHERE provider_message_id='om_message'"
        ).fetchone()
    assert binding["peer_lookup_hash"] == store.crypto.lookup_hash(
        "conversation:feishu", "oc_chat"
    )
    assert inbound["binding_id"] == binding["binding_id"]


def test_group_admission_requires_human_exact_mention_and_strips_only_current_bot(store):
    registered = _register(store)
    current = SimpleNamespace(
        key="@_user_1",
        id=SimpleNamespace(union_id="wrong-union", open_id="cli_app"),
    )
    other = SimpleNamespace(
        key="@_user_2",
        id=SimpleNamespace(open_id="another-human"),
    )

    accepted = enqueue_verified_event(
        store,
        account_id=registered.account_id,
        data=_event(
            chat_type="group",
            content='{"text":"@_user_1 ask @_user_2"}',
            mentions=(current, other),
        ),
    )
    rejected = enqueue_verified_event(
        store,
        account_id=registered.account_id,
        data=_event(
            message_id="om_display_only",
            chat_type="group",
            content='{"text":"cli_app ask"}',
            mentions=(),
        ),
    )
    bot = enqueue_verified_event(
        store,
        account_id=registered.account_id,
        data=_event(message_id="om_bot", sender_type="bot"),
    )

    assert accepted is not None and accepted.status == "queued"
    assert rejected is not None and rejected.rejection_reason == "group_not_addressed_to_bot"
    assert bot is not None and bot.rejection_reason == "sender_not_human"
    with store.read() as conn:
        row = conn.execute(
            "SELECT * FROM inbound_messages WHERE provider_message_id='om_message'"
        ).fetchone()
    assert store.crypto.decrypt_text(
        row["payload_ciphertext"],
        table="inbound_messages",
        record_id="om_message",
        field="payload",
        version=row["payload_key_version"],
    ) == "ask @_user_2"
    assert row["dispatch_scope"] == store.crypto.lookup_hash(
        f"dispatch-scope:feishu:{registered.account_id}",
        "omt_thread",
    )
    assert row["dispatch_scope"] != "omt_thread"
    assert row["profile_revision"] == 1


def test_group_envelope_without_verified_admission_token_cannot_bypass_identity(store):
    registered = _register(store)
    envelope = NormalizedInboundEnvelope(
        provider_message_id="om_unverified_group",
        conversation_id="oc_chat",
        actor_id="on_second_human",
        payload_kind="text",
        payload="hello",
        conversation_kind="group",
        profile_revision=1,
    )

    from hermes_cli.channel_connectors.inbox import CanonicalInbox

    with store.write() as conn:
        result = CanonicalInbox(store, provider="feishu").commit(
            conn,
            account_id=registered.account_id,
            envelope=envelope,
            result=True,
        )

    assert result.rejection_reason == "group_admission_unverified"
    with store.read() as conn:
        row = conn.execute(
            "SELECT * FROM inbound_messages WHERE provider_message_id='om_unverified_group'"
        ).fetchone()
    assert row["status"] == "rejected"
    assert row["payload_ciphertext"] is None


def test_group_allows_multiple_humans_but_existing_direct_identity_remains_exact(store):
    registered = _register(store)
    mention = SimpleNamespace(
        key="@_user_1",
        id=SimpleNamespace(open_id="cli_app"),
    )

    group = enqueue_verified_event(
        store,
        account_id=registered.account_id,
        data=_event(
            actor_id="on_second_human",
            message_id="om_group_second_human",
            chat_type="group",
            mentions=(mention,),
            content='{"text":"@_user_1 hello"}',
        ),
    )
    direct = enqueue_verified_event(
        store,
        account_id=registered.account_id,
        data=_event(
            actor_id="on_second_human",
            message_id="om_direct_second_human",
        ),
    )

    assert group is not None and group.status == "queued"
    assert direct is not None and direct.rejection_reason == "identity_mismatch"


def test_exact_reply_receipt_is_account_chat_and_binding_scoped(store):
    first = _register(store, owner_id="owner-a", chat_id="oc_chat")
    second = _register(
        store,
        owner_id="owner-b",
        actor_id="on_actor_b",
        chat_id="oc_other",
        provider_account_id="cli_app_b",
    )
    ChannelOutbox(store).enqueue_cron_result(
        owner_key=_owner("owner-a").owner_key,
        binding_id=first.binding_id,
        fire_id="receipt-source",
        payload="reply source",
    )
    claim = claim_feishu_outbound(
        store,
        holder="sender-a",
        account_id=first.account_id,
    )
    assert claim is not None
    from hermes_cli.channel_dispatch.outbox import advance_outbound, set_outbound_part_count

    set_outbound_part_count(
        store,
        claim.delivery,
        holder="sender-a",
        part_count=1,
    )
    assert advance_outbound(
        store,
        claim.delivery,
        holder="sender-a",
        part_count=1,
        provider_message_id="om_receipt",
    )

    accepted = enqueue_verified_event(
        store,
        account_id=first.account_id,
        data=_event(
            actor_id="on_second_human",
            message_id="om_reply",
            chat_type="group",
            content='{"text":"reply"}',
            mentions=(),
            thread_id="omt_untrusted_reply_thread",
            root_id="om_untrusted_reply_root",
            parent_id="om_receipt",
        ),
    )
    wrong_chat = enqueue_verified_event(
        store,
        account_id=first.account_id,
        data=_event(
            actor_id="on_second_human",
            chat_id="oc_other",
            message_id="om_wrong_chat",
            chat_type="group",
            content='{"text":"reply"}',
            mentions=(),
            thread_id="",
            root_id="",
            parent_id="om_receipt",
        ),
    )
    wrong_account = enqueue_verified_event(
        store,
        account_id=second.account_id,
        data=_event(
            actor_id="on_actor_b",
            chat_id="oc_other",
            message_id="om_wrong_account",
            chat_type="group",
            content='{"text":"reply"}',
            mentions=(),
            thread_id="",
            root_id="",
            parent_id="om_receipt",
        ),
    )
    root_ancestry_only = enqueue_verified_event(
        store,
        account_id=first.account_id,
        data=_event(
            actor_id="on_second_human",
            message_id="om_root_ancestry",
            chat_type="group",
            content='{"text":"reply to another human"}',
            mentions=(),
            thread_id="omt_receipt_thread",
            root_id="om_receipt",
            parent_id="",
        ),
    )

    assert accepted is not None and accepted.status == "queued"
    assert wrong_chat is not None and wrong_chat.rejection_reason == "unknown_peer"
    assert wrong_account is not None and wrong_account.rejection_reason == "group_not_addressed_to_bot"
    assert root_ancestry_only is not None
    assert root_ancestry_only.rejection_reason == "group_not_addressed_to_bot"
    with store.read() as conn:
        receipt = conn.execute("SELECT * FROM outbound_receipts").fetchone()
        inbound = conn.execute(
            "SELECT dispatch_scope FROM inbound_messages WHERE provider_message_id='om_reply'"
        ).fetchone()
    assert receipt["account_id"] == first.account_id
    assert receipt["binding_id"] == first.binding_id
    assert receipt["part_index"] == 0
    assert receipt["provider_message_lookup_hash"] != "om_receipt"
    assert inbound["dispatch_scope"] == ""


def test_feishu_claim_and_stale_recovery_are_exact_account_scoped(store):
    first = _register(store, owner_id="owner-a", chat_id="oc_chat")
    second = _register(
        store,
        owner_id="owner-b",
        actor_id="on_actor_b",
        chat_id="oc_other",
        provider_account_id="cli_app_b",
    )
    for registered, owner_id in ((first, "owner-a"), (second, "owner-b")):
        enqueue_verified_event(
            store,
            account_id=registered.account_id,
            data=_event(
                actor_id="on_actor" if owner_id == "owner-a" else "on_actor_b",
                chat_id="oc_chat" if owner_id == "owner-a" else "oc_other",
                message_id=f"om_{owner_id}",
            ),
        )

    dispatcher = ChannelDispatcher(
        store,
        object(),
        provider="feishu",
        account_id=first.account_id,
    )
    inbound = dispatcher.claim_next(holder="dispatcher-a")
    assert inbound is not None and inbound["account_id"] == first.account_id
    with store.write() as conn:
        conn.execute(
            "UPDATE inbound_messages SET status='completed' WHERE inbound_id=?",
            (inbound["inbound_id"],),
        )

    for registered, owner_id in ((first, "owner-a"), (second, "owner-b")):
        ChannelOutbox(store).enqueue_cron_result(
            owner_key=_owner(owner_id).owner_key,
            binding_id=registered.binding_id,
            fire_id=f"fire-{owner_id}",
            payload="reply",
        )
    outbound = claim_feishu_outbound(
        store,
        holder="sender-a",
        account_id=first.account_id,
    )
    assert outbound is not None and outbound.delivery.account_id == first.account_id

    with store.write() as conn:
        conn.execute(
            "UPDATE outbound_messages SET status='sending', claimed_by='dead', claimed_at=0"
        )
    assert recover_stale_outbound(
        store,
        provider="feishu",
        claimed_before=1,
        account_id=first.account_id,
    ) == 1
    with store.read() as conn:
        statuses = {
            row["account_id"]: row["status"]
            for row in conn.execute(
                "SELECT account_id, status FROM outbound_messages"
            ).fetchall()
        }
    assert statuses[first.account_id] == "queued"
    assert statuses[second.account_id] == "sending"


def test_payload_cannot_select_another_owner(store):
    first = _register(store, owner_id="owner-a")

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
    ("domain", "host"),
    [("feishu", "open.feishu.cn"), ("lark", "open.larksuite.com")],
)
async def test_http_transport_verifies_bot_identity_without_exposing_secrets(domain, host):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/tenant_access_token/internal"):
            assert json.loads(request.content)["app_secret"] == "private-secret"
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": "tenant-token", "expire": 7200},
            )
        assert request.method == "GET"
        assert request.headers["Authorization"] == "Bearer tenant-token"
        return httpx.Response(
            200,
            json={
                "code": 0,
                "bot": {
                    "open_id": "ou_bot",
                    "user_id": "bot-user",
                    "union_id": "on_bot",
                    "app_name": "Research Bot",
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        identity = await FeishuHTTPTransport(client).verify_account(
            SimpleNamespace(
                account_id="candidate",
                credential_version=1,
                provider_account_id="cli_app",
                credentials={
                    "app_id": "cli_app",
                    "app_secret": "private-secret",
                    "domain": domain,
                },
            )
        )

    assert identity == {
        "app_id": "cli_app",
        "domain": domain,
        "bot_open_id": "ou_bot",
        "bot_user_id": "bot-user",
        "bot_union_id": "on_bot",
        "bot_name": "Research Bot",
    }
    assert all(request.url.host == host for request in requests)
    assert "private-secret" not in json.dumps(identity)
    assert "tenant-token" not in json.dumps(identity)


@pytest.mark.asyncio
async def test_http_transport_rejects_missing_bot_identity():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": "tenant-token", "expire": 7200},
            )
        return httpx.Response(200, json={"code": 0, "data": {"bot": {"app_name": "No ID"}}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(FeishuTransportError, match="bot_identity_missing") as caught:
            await FeishuHTTPTransport(client).verify_account(
                SimpleNamespace(
                    account_id="candidate",
                    credential_version=1,
                    provider_account_id="cli_app",
                    credentials={
                        "app_id": "cli_app",
                        "app_secret": "private-secret",
                        "domain": "feishu",
                    },
                )
            )
    assert caught.value.retryable is False


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


@pytest.mark.asyncio
async def test_connector_dispatches_inbound_through_exact_owner_worker(store):
    registered = _register(store)
    enqueue_verified_event(
        store,
        account_id=registered.account_id,
        data=_event(),
    )
    supervisor = SimpleNamespace(global_home=store.global_home)
    transport = AsyncMock()
    connector = FeishuConnector(
        store,
        supervisor,
        account_id=registered.account_id,
        config={"dispatch_retry_seconds": 0.001},
        transport=transport,
    )
    connector._running = True

    client = AsyncMock()
    client.owner = None
    client.handle = SimpleNamespace(worker_generation=1)
    client.call.side_effect = [
        {"session_id": "live-1", "stored_session_id": "stored-1"},
        {"status": "streaming"},
        {"session_id": "live-2", "stored_session_id": "stored-1"},
        {"status": "streaming"},
    ]
    client.wait_for_event.return_value = {
        "method": "message.complete",
        "params": {"session_id": "live-1", "status": "complete", "text": "answer"},
    }

    from hermes_cli.channel_identity.owner_resolution import resolve_binding

    owner, _channel = resolve_binding(store, binding_id=registered.binding_id)

    class _Context:
        async def __aenter__(self):
            client.owner = owner
            return client

        async def __aexit__(self, *_args):
            return None

    with patch(
        "hermes_cli.channel_dispatch.dispatcher.OwnerWorkerGatewayClient",
        return_value=_Context(),
    ):
        task = asyncio.create_task(connector._dispatch_loop())
        for _ in range(100):
            with store.read() as conn:
                status = conn.execute(
                    "SELECT status FROM inbound_messages"
                ).fetchone()["status"]
            if status == "outbound_pending":
                break
            await asyncio.sleep(0.001)
        connector._running = False
        await task

    create_method, create_params = client.call.await_args_list[0].args
    assert create_method == "session.create"
    assert create_params["source"] == "feishu"
    assert create_params["title"] == "feishu channel"
    assert create_params["close_on_disconnect"] is False
    assert create_params["employee_policy"]["account_id"] == registered.account_id
    assert create_params["employee_policy"]["profile_revision"] == 1
    assert create_params["employee_policy"]["source_policy"]["system_prompt"] == (
        "You are a focused Feishu employee."
    )
    assert client.call.await_args_list[1].args[0] == "prompt.submit"
    assert client.call.await_args_list[1].args[1]["text"] == "hello Feishu"
    with store.read() as conn:
        inbound = conn.execute("SELECT status FROM inbound_messages").fetchone()
        outbound = conn.execute(
            "SELECT status, payload_ciphertext FROM outbound_messages"
        ).fetchone()
    assert inbound["status"] == "outbound_pending"
    assert outbound["status"] == "queued"
    assert outbound["payload_ciphertext"] is not None

    with store.write() as conn:
        conn.execute("UPDATE outbound_messages SET status='delivered'")
        conn.execute("UPDATE inbound_messages SET status='completed'")
    enqueue_verified_event(
        store,
        account_id=registered.account_id,
        data=_event(message_id="om_second", content='{"text":"again"}'),
    )
    claim = connector.dispatcher.claim_next(holder=connector.holder)
    assert claim is not None
    with patch(
        "hermes_cli.channel_dispatch.dispatcher.OwnerWorkerGatewayClient",
        return_value=_Context(),
    ):
        await connector.dispatcher.dispatch_claim(claim, holder=connector.holder)

    assert client.call.await_args_list[2].args == (
        "session.resume",
        {"session_id": "stored-1", "source": "feishu"},
    )
    assert client.call.await_args_list[3].args[0] == "prompt.submit"
    assert client.call.await_args_list[3].args[1]["text"] == "again"
    await connector.close()


@pytest.mark.asyncio
async def test_websocket_sessions_start_concurrently_and_stop_independently():
    connected: list[str] = []
    disconnected: list[str] = []
    ping_stopped: dict[str, asyncio.Event] = {
        "first": asyncio.Event(),
        "second": asyncio.Event(),
    }

    class Client:
        _auto_reconnect = True

        def __init__(self, name):
            self.name = name

        async def _connect(self):
            connected.append(self.name)

        async def _reconnect(self):
            raise AssertionError("unexpected reconnect")

        async def _ping_loop(self):
            try:
                await asyncio.Future()
            finally:
                ping_stopped[self.name].set()

        async def _disconnect(self):
            disconnected.append(self.name)

    first = FeishuWebSocketSession(Client("first"))
    second = FeishuWebSocketSession(Client("second"))
    await asyncio.gather(first.start(), second.start())

    assert connected == ["first", "second"]
    await first.close()
    assert ping_stopped["first"].is_set()
    assert not ping_stopped["second"].is_set()
    assert disconnected == ["first"]

    await second.close()
    assert ping_stopped["second"].is_set()
    assert disconnected == ["first", "second"]


@pytest.mark.asyncio
async def test_connector_recovers_stale_inbound_claim_and_cancels_dispatch(store):
    registered = _register(store)
    enqueue_verified_event(
        store,
        account_id=registered.account_id,
        data=_event(),
    )
    with store.write() as conn:
        conn.execute(
            "UPDATE inbound_messages SET status='processing', claimed_by='dead', claimed_at=0"
        )

    class Transport:
        async def close(self):
            return None

    connector = FeishuConnector(
        store,
        SimpleNamespace(global_home=store.global_home),
        account_id=registered.account_id,
        config={"dispatch_claim_timeout_seconds": 1},
        transport=Transport(),
    )
    class WSClient:
        _auto_reconnect = True

        async def _connect(self):
            return None

        async def _reconnect(self):
            return None

        async def _ping_loop(self):
            await asyncio.Future()

        async def _disconnect(self):
            return None

    connector._build_ws_client = lambda **_kwargs: WSClient()
    await connector.start()

    with store.read() as conn:
        row = conn.execute("SELECT status, claimed_by FROM inbound_messages").fetchone()
    assert row["status"] in {"queued", "processing", "outbound_pending"}
    assert row["claimed_by"] != "dead"
    assert connector._dispatcher_task is not None

    await connector.close()

    assert connector._dispatcher_task is None
    assert connector._dispatch_tasks == set()


@pytest.mark.asyncio
async def test_connector_cancellation_releases_exact_inbound_and_outbound_claims(store):
    registered = _register(store)
    enqueue_verified_event(
        store,
        account_id=registered.account_id,
        data=_event(),
    )
    connector = FeishuConnector(
        store,
        SimpleNamespace(global_home=store.global_home),
        account_id=registered.account_id,
        config={},
        transport=AsyncMock(),
    )
    inbound = connector.dispatcher.claim_next(holder=connector.holder)
    assert inbound is not None
    connector.dispatcher.dispatch_claim = AsyncMock(side_effect=asyncio.CancelledError)

    with pytest.raises(asyncio.CancelledError):
        await connector._dispatch_one(inbound)

    with store.read() as conn:
        released = conn.execute(
            "SELECT status, claimed_by, attempts FROM inbound_messages WHERE inbound_id=?",
            (inbound["inbound_id"],),
        ).fetchone()
    assert tuple(released) == ("queued", None, 0)

    with store.write() as conn:
        conn.execute(
            "UPDATE inbound_messages SET status='completed' WHERE inbound_id=?",
            (inbound["inbound_id"],),
        )
    ChannelOutbox(store).enqueue_cron_result(
        owner_key=_owner("owner-a").owner_key,
        binding_id=registered.binding_id,
        fire_id="cancel-send",
        payload="reply",
    )
    outbound = claim_feishu_outbound(
        store,
        holder=connector.holder,
        account_id=registered.account_id,
    )
    assert outbound is not None
    connector.transport.send = AsyncMock(side_effect=asyncio.CancelledError)

    with pytest.raises(asyncio.CancelledError):
        await connector.sender.send_claim(outbound, holder=connector.holder)

    with store.read() as conn:
        released = conn.execute(
            "SELECT status, claimed_by FROM outbound_messages WHERE outbound_id=?",
            (outbound.delivery.outbound_id,),
        ).fetchone()
    assert tuple(released) == ("queued", None)


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
