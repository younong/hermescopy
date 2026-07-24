"""Tests for encrypted channel identity storage and immutable registration."""

from __future__ import annotations

import base64
import json
import os
import sqlite3

import pytest

from hermes_cli.channel_identity import (
    ChannelCrypto,
    ChannelIdentityStore,
    Keyring,
    register_weixin_identity,
    resolve_binding,
)


def _keys(byte: int) -> dict[str, str]:
    return {"1": base64.b64encode(bytes([byte]) * 32).decode("ascii")}


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


def test_store_rejects_unknown_newer_schema(tmp_path, crypto):
    path = tmp_path / "control-plane" / "channel_identities.sqlite3"
    first = ChannelIdentityStore(crypto, path=path)
    with first.write() as conn:
        conn.execute(
            "UPDATE channel_identity_meta SET value='999' WHERE key='schema_version'"
        )

    with pytest.raises(RuntimeError, match="newer"):
        ChannelIdentityStore(crypto, path=path)


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
