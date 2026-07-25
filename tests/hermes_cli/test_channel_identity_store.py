"""Tests for encrypted channel identity storage and immutable registration."""

from __future__ import annotations

import base64
import json
import os
import sqlite3

import pytest

from hermes_cli.channel_identity import (
    ChannelCrypto,
    ChannelIdentityOwnershipConflict,
    ChannelIdentityStore,
    Keyring,
    ensure_owner_binding,
    register_weixin_identity,
    register_weixin_identity_for_owner,
    resolve_binding,
)
from hermes_cli.dashboard_auth.base import Session
from hermes_cli.dashboard_auth.owner_context import owner_context_from_session


def _keys(byte: int) -> dict[str, str]:
    return {"1": base64.b64encode(bytes([byte]) * 32).decode("ascii")}


def _owner(*, user_id: str = "dashboard-user"):
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
def crypto() -> ChannelCrypto:
    return ChannelCrypto(
        lookup=Keyring(keys={1: b"l" * 32}, active_version=1),
        encryption=Keyring(keys={1: b"e" * 32}, active_version=1),
    )


@pytest.fixture
def store(tmp_path, crypto, monkeypatch) -> ChannelIdentityStore:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_OWNER_SECRET", "owner-secret")
    return ChannelIdentityStore(crypto)


def test_crypto_from_env_requires_separate_versioned_keys(monkeypatch):
    monkeypatch.setenv("HERMES_ILINK_LOOKUP_KEYS_JSON", json.dumps(_keys(1)))
    monkeypatch.setenv("HERMES_ILINK_ENCRYPTION_KEYS_JSON", json.dumps(_keys(2)))

    crypto = ChannelCrypto.from_env(lookup_version=1, encryption_version=1)

    assert crypto.lookup.key(1) != crypto.encryption.key(1)


def test_crypto_aad_rejects_cross_field_ciphertext(crypto):
    ciphertext, version = crypto.encrypt_text(
        "secret-value",
        table="ilink_accounts",
        record_id="account-1",
        field="bot_token",
    )

    assert crypto.decrypt_text(
        ciphertext,
        table="ilink_accounts",
        record_id="account-1",
        field="bot_token",
        version=version,
    ) == "secret-value"
    with pytest.raises(RuntimeError, match="failed authentication"):
        crypto.decrypt_text(
            ciphertext,
            table="ilink_accounts",
            record_id="account-1",
            field="cursor",
            version=version,
        )


def test_store_uses_profile_home_and_private_permissions(store, tmp_path):
    assert store.path == tmp_path / "control-plane" / "channel_identities.sqlite3"
    if os.name != "nt":
        assert stat_mode(store.path.parent) == 0o700
        assert stat_mode(store.path) == 0o600


def test_store_rejects_symlink_database(tmp_path, crypto):
    parent = tmp_path / "control-plane"
    parent.mkdir()
    target = tmp_path / "actual.sqlite3"
    target.write_text("", encoding="utf-8")
    (parent / "channel_identities.sqlite3").symlink_to(target)

    with pytest.raises(RuntimeError, match="regular file"):
        ChannelIdentityStore(crypto, path=parent / "channel_identities.sqlite3")


def _downgrade_inbound_messages_to_v3(conn: sqlite3.Connection) -> None:
    conn.execute("DROP INDEX idx_inbound_due")
    conn.execute("DROP INDEX idx_inbound_binding_status")
    conn.execute("ALTER TABLE inbound_messages RENAME TO inbound_messages_v3")
    conn.execute(
        """
        CREATE TABLE inbound_messages (
            inbound_id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL REFERENCES ilink_accounts(account_id),
            binding_id TEXT REFERENCES channel_bindings(binding_id),
            provider_message_id TEXT NOT NULL,
            payload_ciphertext BLOB,
            payload_key_version INTEGER,
            context_ciphertext BLOB,
            context_key_version INTEGER,
            status TEXT NOT NULL,
            claimed_by TEXT,
            claimed_at REAL,
            rejection_reason TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            UNIQUE(account_id, provider_message_id)
        )
        """
    )
    conn.execute(
        """
        INSERT INTO inbound_messages
        SELECT inbound_id, account_id, binding_id, provider_message_id,
               payload_ciphertext, payload_key_version, context_ciphertext,
               context_key_version, status, claimed_by, claimed_at,
               rejection_reason, created_at, updated_at
        FROM inbound_messages_v3
        """
    )
    conn.execute("DROP TABLE inbound_messages_v3")
    conn.execute(
        "CREATE INDEX idx_inbound_binding_status "
        "ON inbound_messages(binding_id, status, created_at)"
    )


def test_store_migrates_v1_attempts_to_owner_target_schema(tmp_path, crypto, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_OWNER_SECRET", "owner-secret")
    path = tmp_path / "control-plane" / "channel_identities.sqlite3"
    first = ChannelIdentityStore(crypto, path=path)
    with first.write() as conn:
        conn.execute("UPDATE channel_identity_meta SET value='1' WHERE key='schema_version'")
        _downgrade_inbound_messages_to_v3(conn)
        conn.execute(
            """
            INSERT INTO enrollment_attempts
              (attempt_id, status, scene, source_lookup_hash, device_lookup_hash,
               expires_at, next_poll_at, created_at, updated_at)
            VALUES ('enr_existing', 'waiting', 'join', 'source', 'device', 10, 0, 1, 1)
            """
        )
        conn.execute("ALTER TABLE enrollment_attempts RENAME TO enrollment_attempts_v2")
        conn.execute(
            """
            CREATE TABLE enrollment_attempts (
                attempt_id TEXT PRIMARY KEY, status TEXT NOT NULL, scene TEXT NOT NULL,
                source_lookup_hash TEXT NOT NULL, device_lookup_hash TEXT NOT NULL,
                qr_ciphertext BLOB, qr_key_version INTEGER, confirmed_ciphertext BLOB,
                confirmed_key_version INTEGER, expires_at REAL NOT NULL,
                next_poll_at REAL NOT NULL, consumed_at REAL, created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO enrollment_attempts
            SELECT attempt_id, status, scene, source_lookup_hash, device_lookup_hash,
                   qr_ciphertext, qr_key_version, confirmed_ciphertext,
                   confirmed_key_version, expires_at, next_poll_at, consumed_at,
                   created_at, updated_at
            FROM enrollment_attempts_v2
            """
        )
        conn.execute("DROP TABLE enrollment_attempts_v2")

    migrated = ChannelIdentityStore(crypto, path=path)

    with migrated.read() as conn:
        assert conn.execute(
            "SELECT value FROM channel_identity_meta WHERE key='schema_version'"
        ).fetchone()["value"] == "4"
        row = conn.execute(
            "SELECT target_canonical_user_id FROM enrollment_attempts WHERE attempt_id='enr_existing'"
        ).fetchone()
        inbound_columns = {
            column["name"]
            for column in conn.execute("PRAGMA table_info(inbound_messages)").fetchall()
        }
    assert row["target_canonical_user_id"] is None
    assert {"payload_kind", "attempts", "next_attempt_at", "last_error"} <= inbound_columns


def test_store_migrates_v3_inbound_rows_with_retry_defaults(tmp_path, crypto):
    path = tmp_path / "control-plane" / "channel_identities.sqlite3"
    first = ChannelIdentityStore(crypto, path=path)
    with first.write() as conn:
        conn.execute(
            "INSERT INTO canonical_users VALUES ('cu', 'active', 1, 1)"
        )
        conn.execute(
            """
            INSERT INTO external_identities
            VALUES ('ei', 'weixin-ilink', 'subject', X'00', 1, 'cu', 'active', 1, 1)
            """
        )
        conn.execute(
            """
            INSERT INTO ilink_accounts
              (account_id, external_identity_id, bot_id_lookup_hash,
               bot_id_ciphertext, bot_id_key_version, bot_token_ciphertext,
               bot_token_key_version, base_url, credential_version, status,
               created_at, updated_at)
            VALUES ('account', 'ei', 'bot', X'00', 1, X'00', 1,
                    'https://ilink.example', 1, 'active', 1, 1)
            """
        )
        conn.execute(
            """
            INSERT INTO inbound_messages
              (inbound_id, account_id, provider_message_id, status,
               created_at, updated_at)
            VALUES ('im_existing', 'account', 'msg', 'queued', 1, 1)
            """
        )
        _downgrade_inbound_messages_to_v3(conn)
        conn.execute("UPDATE channel_identity_meta SET value='3' WHERE key='schema_version'")

    migrated = ChannelIdentityStore(crypto, path=path)

    with migrated.read() as conn:
        row = conn.execute(
            """
            SELECT payload_kind, attempts, next_attempt_at, last_error
            FROM inbound_messages WHERE inbound_id='im_existing'
            """
        ).fetchone()
        version = conn.execute(
            "SELECT value FROM channel_identity_meta WHERE key='schema_version'"
        ).fetchone()["value"]
    assert tuple(row) == ("text", 0, 0, None)
    assert version == "4"


def test_store_migrates_v2_outbound_with_fresh_chunk_attempts(tmp_path, crypto):
    path = tmp_path / "control-plane" / "channel_identities.sqlite3"
    first = ChannelIdentityStore(crypto, path=path)
    registered = register_weixin_identity(
        first,
        subject="subject-a",
        bot_id="bot-a",
        bot_token="token-a",
        base_url="https://ilink.example",
        peer_id="subject-a",
    )
    with first.write() as conn:
        now = 1.0
        conn.execute(
            """
            INSERT INTO inbound_messages
              (inbound_id, account_id, binding_id, provider_message_id, status,
               created_at, updated_at)
            VALUES ('inbound-existing', ?, ?, 'provider-existing', 'outbound_pending', ?, ?)
            """,
            (registered.account_id, registered.binding_id, now, now),
        )
        conn.execute(
            """
            INSERT INTO outbound_messages
              (outbound_id, inbound_id, account_id, binding_id, client_message_id,
               status, attempts, next_attempt_at, created_at, updated_at)
            VALUES ('outbound-existing', 'inbound-existing', ?, ?, 'client-existing',
                    'queued', 20700, 0, ?, ?)
            """,
            (registered.account_id, registered.binding_id, now, now),
        )
        conn.execute("UPDATE channel_identity_meta SET value='2' WHERE key='schema_version'")
        conn.execute("ALTER TABLE outbound_messages RENAME TO outbound_messages_v3")
        conn.execute(
            """
            CREATE TABLE outbound_messages AS
            SELECT outbound_id, inbound_id, account_id, binding_id, client_message_id,
                   payload_ciphertext, payload_key_version, context_ciphertext,
                   context_key_version, status, attempts, next_attempt_at, claimed_by,
                   claimed_at, last_error, created_at, updated_at
            FROM outbound_messages_v3
            """
        )
        conn.execute("DROP TABLE outbound_messages_v3")

    migrated = ChannelIdentityStore(crypto, path=path)

    with migrated.read() as conn:
        row = conn.execute(
            """
            SELECT attempts, chunk_count, next_chunk_index, chunk_attempts,
                   failed_chunk_index FROM outbound_messages
            WHERE outbound_id='outbound-existing'
            """
        ).fetchone()
    assert tuple(row) == (20700, None, 0, 0, None)


def test_store_rejects_unknown_newer_schema(tmp_path, crypto):
    path = tmp_path / "control-plane" / "channel_identities.sqlite3"
    first = ChannelIdentityStore(crypto, path=path)
    with first.write() as conn:
        conn.execute(
            "UPDATE channel_identity_meta SET value='999' WHERE key='schema_version'"
        )

    with pytest.raises(RuntimeError, match="newer"):
        ChannelIdentityStore(crypto, path=path)


def test_dashboard_owner_binding_uses_random_registry_identity(store):
    dashboard_owner = _owner()

    canonical_user_id = ensure_owner_binding(store, dashboard_owner)
    again = ensure_owner_binding(store, dashboard_owner)

    assert canonical_user_id == again
    assert canonical_user_id.startswith("cu_")
    assert canonical_user_id != dashboard_owner.owner_user_id
    with store.read() as conn:
        row = conn.execute(
            "SELECT * FROM owner_bindings WHERE canonical_user_id=?",
            (canonical_user_id,),
        ).fetchone()
    assert row["auth_provider"] == dashboard_owner.auth_provider
    assert row["tenant_id"] == dashboard_owner.tenant_id
    assert row["owner_user_id"] == dashboard_owner.owner_user_id
    assert row["owner_key"] == dashboard_owner.owner_key


def test_owner_linked_registration_resolves_dashboard_owner_and_rotates_credentials(store):
    dashboard_owner = _owner()
    target = ensure_owner_binding(store, dashboard_owner)

    first = register_weixin_identity_for_owner(
        store,
        target_canonical_user_id=target,
        subject="subject-a",
        bot_id="bot-a",
        bot_token="token-one",
        base_url="https://ilink.example/",
        peer_id="subject-a",
    )
    second = register_weixin_identity_for_owner(
        store,
        target_canonical_user_id=target,
        subject="subject-a",
        bot_id="bot-a",
        bot_token="token-two",
        base_url="https://ilink.example/",
        peer_id="subject-a",
    )

    assert first.created is True
    assert second.created is False
    assert second.canonical_user_id == target
    assert second.owner_key == dashboard_owner.owner_key
    owner, resolved = resolve_binding(store, binding_id=first.binding_id)
    assert owner == dashboard_owner
    assert resolved.bot_token == "token-two"
    assert resolved.credential_version == 2


def test_owner_linked_registration_conflict_does_not_rotate_credentials(store):
    first_owner = _owner(user_id="owner-a")
    second_owner = _owner(user_id="owner-b")
    first_target = ensure_owner_binding(store, first_owner)
    second_target = ensure_owner_binding(store, second_owner)
    registered = register_weixin_identity_for_owner(
        store,
        target_canonical_user_id=first_target,
        subject="subject-a",
        bot_id="bot-a",
        bot_token="token-one",
        base_url="https://ilink.example/",
        peer_id="subject-a",
    )

    with pytest.raises(ChannelIdentityOwnershipConflict):
        register_weixin_identity_for_owner(
            store,
            target_canonical_user_id=second_target,
            subject="subject-a",
            bot_id="bot-a",
            bot_token="token-attacker",
            base_url="https://attacker.example/",
            peer_id="subject-a",
        )

    owner, resolved = resolve_binding(store, binding_id=registered.binding_id)
    assert owner.owner_key == first_owner.owner_key
    assert resolved.bot_token == "token-one"
    assert resolved.account_base_url == "https://ilink.example"
    assert resolved.credential_version == 1


def test_repeated_registration_restores_same_owner_and_rotates_credentials(store):
    first = register_weixin_identity(
        store,
        subject="subject-a",
        bot_id="bot-a",
        bot_token="token-one",
        base_url="https://ilink.example/",
        peer_id="subject-a",
    )
    second = register_weixin_identity(
        store,
        subject="subject-a",
        bot_id="bot-a",
        bot_token="token-two",
        base_url="https://ilink.example/",
        peer_id="subject-a",
    )

    assert first.created is True
    assert second.created is False
    assert second.canonical_user_id == first.canonical_user_id
    assert second.owner_key == first.owner_key
    owner, resolved = resolve_binding(store, binding_id=first.binding_id)
    assert owner.owner_key == first.owner_key
    assert resolved.bot_token == "token-two"
    assert resolved.credential_version == 2


def test_different_subjects_receive_distinct_owners(store):
    first = register_weixin_identity(
        store,
        subject="subject-a",
        bot_id="bot-a",
        bot_token="token-a",
        base_url="https://ilink.example",
        peer_id="subject-a",
    )
    second = register_weixin_identity(
        store,
        subject="subject-b",
        bot_id="bot-b",
        bot_token="token-b",
        base_url="https://ilink.example",
        peer_id="subject-b",
    )

    assert first.canonical_user_id != second.canonical_user_id
    assert first.owner_key != second.owner_key


def test_owner_binding_trigger_is_immutable(store):
    registered = register_weixin_identity(
        store,
        subject="subject-a",
        bot_id="bot-a",
        bot_token="token-a",
        base_url="https://ilink.example",
        peer_id="subject-a",
    )

    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with store.write() as conn:
            conn.execute(
                "UPDATE owner_bindings SET owner_key='ok1_attacker' WHERE canonical_user_id=?",
                (registered.canonical_user_id,),
            )


def test_resolution_fails_closed_for_suspended_user(store):
    registered = register_weixin_identity(
        store,
        subject="subject-a",
        bot_id="bot-a",
        bot_token="token-a",
        base_url="https://ilink.example",
        peer_id="subject-a",
    )
    with store.write() as conn:
        conn.execute(
            "UPDATE canonical_users SET status='suspended' WHERE canonical_user_id=?",
            (registered.canonical_user_id,),
        )

    with pytest.raises(RuntimeError, match="unavailable"):
        resolve_binding(store, binding_id=registered.binding_id)


def test_registration_rejects_conflicting_bot_for_existing_subject(store):
    register_weixin_identity(
        store,
        subject="subject-a",
        bot_id="bot-a",
        bot_token="token-a",
        base_url="https://ilink.example",
        peer_id="subject-a",
    )

    with pytest.raises(RuntimeError, match="conflicts"):
        register_weixin_identity(
            store,
            subject="subject-a",
            bot_id="bot-attacker",
            bot_token="token-b",
            base_url="https://ilink.example",
            peer_id="subject-a",
        )


def stat_mode(path) -> int:
    return path.stat().st_mode & 0o777
