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


def test_store_migrates_v1_attempts_to_owner_target_schema(tmp_path, crypto, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_OWNER_SECRET", "owner-secret")
    path = tmp_path / "control-plane" / "channel_identities.sqlite3"
    first = ChannelIdentityStore(crypto, path=path)
    with first.write() as conn:
        conn.execute("UPDATE channel_identity_meta SET value='1' WHERE key='schema_version'")
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
        ).fetchone()["value"] == "2"
        row = conn.execute(
            "SELECT target_canonical_user_id FROM enrollment_attempts WHERE attempt_id='enr_existing'"
        ).fetchone()
    assert row["target_canonical_user_id"] is None


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
