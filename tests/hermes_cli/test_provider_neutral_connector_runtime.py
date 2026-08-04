"""Provider-neutral connector runtime proofs with a non-iLink provider."""

from __future__ import annotations

import pytest

from hermes_cli.channel_connectors import (
    InboundBatch,
    NormalizedInboundEnvelope,
    acquire_poll_lease,
    commit_inbound_batch,
    load_poll_account,
)
from hermes_cli.channel_dispatch import (
    ChannelDispatcher,
    advance_outbound,
    claim_outbound,
    recover_stale_outbound,
    set_outbound_part_count,
)
from hermes_cli.channel_identity import (
    ChannelCrypto,
    ChannelIdentityStore,
    Keyring,
    register_connector_binding_for_owner,
)
from hermes_cli.dashboard_auth.base import Session
from hermes_cli.dashboard_auth.owner_context import owner_context_from_session

_PROVIDER = "fake_provider"


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


def _register(store, *, owner_id: str, subject: str, conversation: str):
    return register_connector_binding_for_owner(
        store,
        owner=_owner(owner_id),
        provider=_PROVIDER,
        provider_account_id="shared-account",
        external_subject=subject,
        conversation_id=conversation,
        credentials={"token": "shared-secret"},
    )


def test_fake_provider_polling_supports_shared_account_and_owner_bindings(store):
    first = _register(
        store,
        owner_id="owner-a",
        subject="actor-a",
        conversation="conversation-a",
    )
    second = _register(
        store,
        owner_id="owner-b",
        subject="actor-b",
        conversation="conversation-b",
    )
    assert first.account_id == second.account_id

    lease = acquire_poll_lease(
        store,
        provider=_PROVIDER,
        account_id=first.account_id,
        holder="fake-poller",
    )
    account = load_poll_account(store, lease)
    assert account.credentials == {"token": "shared-secret"}
    assert account.cursor == ""

    inserted = commit_inbound_batch(
        store,
        lease,
        batch=InboundBatch(
            cursor="cursor-1",
            messages=(
                NormalizedInboundEnvelope(
                    provider_message_id="message-a",
                    conversation_id="conversation-a",
                    actor_id="actor-a",
                    payload_kind="text",
                    payload="hello a",
                ),
                NormalizedInboundEnvelope(
                    provider_message_id="message-b",
                    conversation_id="conversation-b",
                    actor_id="actor-b",
                    payload_kind="text",
                    payload="hello b",
                ),
            ),
        ),
    )
    assert inserted == 2
    assert load_poll_account(store, lease).cursor == "cursor-1"

    dispatcher = ChannelDispatcher(
        store,
        object(),
        provider=_PROVIDER,
    )
    first_claim = dispatcher.claim_next(holder="fake-dispatcher")
    assert first_claim is not None
    assert first_claim["binding_id"] in {first.binding_id, second.binding_id}
    with store.write() as conn:
        conn.execute(
            "UPDATE inbound_messages SET status='completed', claimed_by=NULL, "
            "claimed_at=NULL WHERE inbound_id=?",
            (first_claim["inbound_id"],),
        )
    second_claim = dispatcher.claim_next(holder="fake-dispatcher")
    assert second_claim is not None
    assert second_claim["binding_id"] != first_claim["binding_id"]


def test_identity_mismatch_is_rejected_before_owner_dispatch(store):
    registered = _register(
        store,
        owner_id="owner-a",
        subject="actor-a",
        conversation="conversation-a",
    )
    lease = acquire_poll_lease(
        store,
        provider=_PROVIDER,
        account_id=registered.account_id,
        holder="fake-poller",
    )
    assert commit_inbound_batch(
        store,
        lease,
        batch=InboundBatch(
            cursor="cursor-1",
            messages=(
                NormalizedInboundEnvelope(
                    provider_message_id="message-mismatch",
                    conversation_id="conversation-a",
                    actor_id="actor-b",
                    payload_kind="text",
                    payload="untrusted",
                ),
            ),
        ),
    ) == 1
    with store.read() as conn:
        inbound = conn.execute("SELECT * FROM inbound_messages").fetchone()
    assert inbound["binding_id"] == registered.binding_id
    assert inbound["status"] == "rejected"
    assert inbound["rejection_reason"] == "identity_mismatch"
    assert inbound["payload_ciphertext"] is None
    assert ChannelDispatcher(
        store,
        object(),
        provider=_PROVIDER,
    ).claim_next(holder="fake-dispatcher") is None


def test_duplicate_inbound_does_not_roll_back_context_token(store):
    registered = _register(
        store,
        owner_id="owner-a",
        subject="actor-a",
        conversation="conversation-a",
    )
    lease = acquire_poll_lease(
        store,
        provider=_PROVIDER,
        account_id=registered.account_id,
        holder="fake-poller",
    )
    first = NormalizedInboundEnvelope(
        provider_message_id="message-a",
        conversation_id="conversation-a",
        actor_id="actor-a",
        payload_kind="text",
        payload="hello",
        context_token="new-context",
    )
    assert commit_inbound_batch(
        store,
        lease,
        batch=InboundBatch(cursor="cursor-1", messages=(first,)),
    ) == 1
    replay = NormalizedInboundEnvelope(
        provider_message_id="message-a",
        conversation_id="conversation-a",
        actor_id="actor-a",
        payload_kind="text",
        payload="hello",
        context_token="old-context",
    )
    assert commit_inbound_batch(
        store,
        lease,
        batch=InboundBatch(cursor="cursor-2", messages=(replay,)),
    ) == 0
    with store.read() as conn:
        token = conn.execute("SELECT * FROM context_tokens").fetchone()
    assert store.crypto.decrypt_text(
        token["token_ciphertext"],
        table="context_tokens",
        record_id=f"{registered.account_id}:{token['peer_lookup_hash']}",
        field="token",
        version=token["token_key_version"],
    ) == "new-context"


def test_pending_binding_does_not_suspend_existing_shared_account(store):
    first = _register(
        store,
        owner_id="owner-a",
        subject="actor-a",
        conversation="conversation-a",
    )
    second = register_connector_binding_for_owner(
        store,
        owner=_owner("owner-b"),
        provider=_PROVIDER,
        provider_account_id="shared-account",
        external_subject="actor-b",
        conversation_id="conversation-b",
        credentials={"token": "rotated-secret"},
        activate=False,
    )
    assert second.account_id == first.account_id
    with store.read() as conn:
        account_status = conn.execute(
            "SELECT status FROM connector_accounts WHERE account_id=?",
            (first.account_id,),
        ).fetchone()["status"]
        binding_status = conn.execute(
            "SELECT status FROM channel_bindings WHERE binding_id=?",
            (second.binding_id,),
        ).fetchone()["status"]
    assert account_status == "active"
    assert binding_status == "pending"


def test_fake_provider_outbox_claim_is_provider_scoped_and_fenced(store):
    registered = _register(
        store,
        owner_id="owner-a",
        subject="actor-a",
        conversation="conversation-a",
    )
    lease = acquire_poll_lease(
        store,
        provider=_PROVIDER,
        account_id=registered.account_id,
        holder="fake-poller",
    )
    commit_inbound_batch(
        store,
        lease,
        batch=InboundBatch(
            cursor="cursor-1",
            messages=(
                NormalizedInboundEnvelope(
                    provider_message_id="message-a",
                    conversation_id="conversation-a",
                    actor_id="actor-a",
                    payload_kind="text",
                    payload="hello",
                ),
            ),
        ),
    )
    with store.read() as conn:
        inbound = conn.execute("SELECT * FROM inbound_messages").fetchone()
    outbound_id = "outbound-a"
    payload_ciphertext, payload_version = store.crypto.encrypt_text(
        "reply",
        table="outbound_messages",
        record_id=outbound_id,
        field="payload",
    )
    with store.write() as conn:
        conn.execute(
            "UPDATE inbound_messages SET status='outbound_pending' WHERE inbound_id=?",
            (inbound["inbound_id"],),
        )
        conn.execute(
            """
            INSERT INTO outbound_messages
              (outbound_id, inbound_id, account_id, binding_id, provider,
               source_kind, source_id, binding_sequence, client_message_id,
               payload_ciphertext, payload_key_version, status,
               next_attempt_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'inbound', ?, ?, ?, ?, ?, 'queued', 0, 1, 1)
            """,
            (
                outbound_id,
                inbound["inbound_id"],
                registered.account_id,
                registered.binding_id,
                _PROVIDER,
                f"inbound:{inbound['inbound_id']}",
                inbound["binding_sequence"],
                "client-a",
                payload_ciphertext,
                payload_version,
            ),
        )

    assert claim_outbound(store, provider="other_provider", holder="sender") is None
    delivery = claim_outbound(store, provider=_PROVIDER, holder="sender")
    assert delivery is not None
    assert delivery.conversation_id == "conversation-a"
    assert delivery.payload == "reply"
    set_outbound_part_count(store, delivery, holder="sender", part_count=1)
    assert advance_outbound(
        store,
        delivery,
        holder="sender",
        part_count=1,
    ) is True
    with store.read() as conn:
        outbound = conn.execute(
            "SELECT status, payload_ciphertext FROM outbound_messages"
        ).fetchone()
        inbound_status = conn.execute(
            "SELECT status FROM inbound_messages"
        ).fetchone()["status"]
    assert tuple(outbound) == ("delivered", None)
    assert inbound_status == "completed"
    assert recover_stale_outbound(
        store,
        provider="other_provider",
        claimed_before=10,
    ) == 0
