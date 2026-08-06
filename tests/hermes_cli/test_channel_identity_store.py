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
    EmployeeProfileRevisionConflict,
    FeishuCredentialRevisionConflict,
    Keyring,
    claim_existing_feishu_account_for_owner,
    employee_profile_fingerprint,
    ensure_owner_binding,
    register_connector_binding_for_owner,
    register_managed_feishu_account_for_owner,
    register_weixin_identity,
    register_weixin_identity_for_owner,
    resolve_binding,
    resolve_connector_account,
    resolve_employee_profile,
    resolve_managed_feishu_account,
    resolve_managed_feishu_credentials,
    rollover_managed_feishu_sessions,
    rotate_managed_feishu_credentials,
    set_managed_feishu_account_status,
    update_employee_profile,
)
from hermes_cli.channel_identity.credentials import decrypt_account_credentials
from hermes_cli.channel_identity.store import ACCOUNT_CREDENTIAL_AAD_TABLE
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
    return ChannelIdentityStore(crypto, tmp_path / "control-plane", global_home=tmp_path)


def test_crypto_from_env_requires_separate_versioned_keys(monkeypatch):
    monkeypatch.setenv("HERMES_ILINK_LOOKUP_KEYS_JSON", json.dumps(_keys(1)))
    monkeypatch.setenv("HERMES_ILINK_ENCRYPTION_KEYS_JSON", json.dumps(_keys(2)))

    crypto = ChannelCrypto.from_env(lookup_version=1, encryption_version=1)

    assert crypto.lookup.key(1) != crypto.encryption.key(1)


def test_crypto_aad_rejects_cross_field_ciphertext(crypto):
    ciphertext, version = crypto.encrypt_text(
        "secret-value",
        table=ACCOUNT_CREDENTIAL_AAD_TABLE,
        record_id="account-1",
        field="bot_token",
    )

    assert crypto.decrypt_text(
        ciphertext,
        table=ACCOUNT_CREDENTIAL_AAD_TABLE,
        record_id="account-1",
        field="bot_token",
        version=version,
    ) == "secret-value"
    with pytest.raises(RuntimeError, match="failed authentication"):
        crypto.decrypt_text(
            ciphertext,
            table=ACCOUNT_CREDENTIAL_AAD_TABLE,
            record_id="account-1",
            field="cursor",
            version=version,
        )


def test_store_uses_explicit_control_plane_home_and_private_permissions(store, tmp_path):
    assert store.path == tmp_path / "control-plane" / "channel_identities.sqlite3"
    if os.name != "nt":
        assert stat_mode(store.path.parent) == 0o700
        assert stat_mode(store.path) == 0o600


def test_store_does_not_follow_ambient_hermes_home(tmp_path, crypto, monkeypatch):
    ambient = tmp_path / "ambient"
    explicit = tmp_path / "control"
    monkeypatch.setenv("HERMES_HOME", str(ambient))

    store = ChannelIdentityStore(crypto, explicit, global_home=tmp_path)

    assert store.path == explicit / "channel_identities.sqlite3"
    assert not ambient.exists()


def test_store_rejects_symlink_database(tmp_path, crypto):
    parent = tmp_path / "control-plane"
    parent.mkdir()
    target = tmp_path / "actual.sqlite3"
    target.write_text("", encoding="utf-8")
    (parent / "channel_identities.sqlite3").symlink_to(target)

    with pytest.raises(RuntimeError, match="regular file"):
        ChannelIdentityStore(crypto, parent, global_home=tmp_path)


def _downgrade_account_table_to_v7(
    conn: sqlite3.Connection,
    crypto: ChannelCrypto,
) -> None:
    rows = conn.execute("SELECT * FROM connector_accounts").fetchall()
    conn.execute("DROP TRIGGER IF EXISTS connector_accounts_ownership_immutable")
    conn.execute("DROP INDEX IF EXISTS idx_connector_accounts_lookup_hash")
    for column, declaration in (
        ("bot_id_lookup_hash", "TEXT NOT NULL DEFAULT ''"),
        ("bot_id_ciphertext", "BLOB NOT NULL DEFAULT X''"),
        ("bot_id_key_version", "INTEGER NOT NULL DEFAULT 0"),
        ("bot_token_ciphertext", "BLOB NOT NULL DEFAULT X''"),
        ("bot_token_key_version", "INTEGER NOT NULL DEFAULT 0"),
        ("base_url", "TEXT NOT NULL DEFAULT ''"),
    ):
        conn.execute(f"ALTER TABLE connector_accounts ADD COLUMN {column} {declaration}")
    for row in rows:
        credentials = decrypt_account_credentials(
            _StoreCrypto(crypto),
            account_id=row["account_id"],
            ciphertext=row["credentials_ciphertext"],
            key_version=row["credentials_key_version"],
        )
        bot_id_ciphertext, bot_id_version = crypto.encrypt_text(
            credentials["bot_id"],
            table=ACCOUNT_CREDENTIAL_AAD_TABLE,
            record_id=row["account_id"],
            field="bot_id",
        )
        bot_token_ciphertext, bot_token_version = crypto.encrypt_text(
            credentials["bot_token"],
            table=ACCOUNT_CREDENTIAL_AAD_TABLE,
            record_id=row["account_id"],
            field="bot_token",
        )
        conn.execute(
            """
            UPDATE connector_accounts
            SET bot_id_lookup_hash=?, bot_id_ciphertext=?, bot_id_key_version=?,
                bot_token_ciphertext=?, bot_token_key_version=?, base_url=?
            WHERE account_id=?
            """,
            (
                row["account_lookup_hash"], bot_id_ciphertext, bot_id_version,
                bot_token_ciphertext, bot_token_version, credentials["base_url"],
                row["account_id"],
            ),
        )
    conn.execute("ALTER TABLE connector_accounts DROP COLUMN account_lookup_hash")
    conn.execute("ALTER TABLE connector_accounts DROP COLUMN credentials_ciphertext")
    conn.execute("ALTER TABLE connector_accounts DROP COLUMN credentials_key_version")
    conn.execute("ALTER TABLE connector_accounts RENAME TO ilink_accounts")


class _StoreCrypto:
    def __init__(self, crypto: ChannelCrypto) -> None:
        self.crypto = crypto


def _rebuild_v4_account_table_with_inline_unique(path) -> None:
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            CREATE TABLE ilink_accounts_v4 (
                account_id TEXT PRIMARY KEY,
                external_identity_id TEXT NOT NULL
                    REFERENCES external_identities(external_identity_id),
                bot_id_lookup_hash TEXT NOT NULL UNIQUE,
                bot_id_ciphertext BLOB NOT NULL,
                bot_id_key_version INTEGER NOT NULL,
                bot_token_ciphertext BLOB NOT NULL,
                bot_token_key_version INTEGER NOT NULL,
                base_url TEXT NOT NULL,
                credential_version INTEGER NOT NULL,
                status TEXT NOT NULL,
                cursor_ciphertext BLOB,
                cursor_key_version INTEGER,
                poll_holder TEXT,
                poll_generation INTEGER NOT NULL DEFAULT 0,
                poll_health TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO ilink_accounts_v4
            SELECT account_id, external_identity_id, bot_id_lookup_hash,
                   bot_id_ciphertext, bot_id_key_version,
                   bot_token_ciphertext, bot_token_key_version, base_url,
                   credential_version, status, cursor_ciphertext,
                   cursor_key_version, poll_holder, poll_generation,
                   poll_health, created_at, updated_at
            FROM ilink_accounts
            """
        )
        conn.execute("DROP TABLE ilink_accounts")
        conn.execute("ALTER TABLE ilink_accounts_v4 RENAME TO ilink_accounts")
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def _downgrade_inbound_messages_to_v3(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TRIGGER IF EXISTS inbound_messages_binding_consistent_insert")
    conn.execute("DROP TRIGGER IF EXISTS inbound_messages_ownership_immutable")
    conn.execute("DROP TRIGGER IF EXISTS outbound_messages_consistent_insert")
    conn.execute("DROP INDEX IF EXISTS idx_inbound_due")
    conn.execute("DROP INDEX IF EXISTS idx_inbound_binding_sequence")
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
    first = ChannelIdentityStore(crypto, path.parent, global_home=tmp_path)
    with first.write() as conn:
        _downgrade_account_table_to_v7(conn, crypto)
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

    migrated = ChannelIdentityStore(crypto, path.parent, global_home=tmp_path)

    with migrated.read() as conn:
        assert conn.execute(
            "SELECT value FROM channel_identity_meta WHERE key='schema_version'"
        ).fetchone()["value"] == "12"
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
    first = ChannelIdentityStore(crypto, path.parent, global_home=tmp_path)
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
        credentials_ciphertext, credentials_version = crypto.encrypt_text(
            '{"base_url":"https://ilink.example","bot_id":"bot","bot_token":"token"}',
            table=ACCOUNT_CREDENTIAL_AAD_TABLE,
            record_id="account",
            field="credentials",
        )
        conn.execute(
            """
            INSERT INTO connector_accounts
              (account_id, provider, provider_account_id,
               account_lookup_hash, credentials_ciphertext, credentials_key_version,
               credential_version, status, created_at, updated_at)
            VALUES ('account', 'weixin_ilink', 'bot', 'bot', ?, ?,
                    1, 'active', 1, 1)
            """,
            (credentials_ciphertext, credentials_version),
        )
        conn.execute(
            """
            INSERT INTO inbound_messages
              (inbound_id, account_id, provider_message_id, status,
               created_at, updated_at)
            VALUES ('im_existing', 'account', 'msg', 'queued', 1, 1)
            """
        )
        _downgrade_account_table_to_v7(conn, crypto)
        _downgrade_inbound_messages_to_v3(conn)
        conn.execute("UPDATE channel_identity_meta SET value='3' WHERE key='schema_version'")

    migrated = ChannelIdentityStore(crypto, path.parent, global_home=tmp_path)

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
    assert version == "12"


def test_store_migrates_v4_ilink_account_to_provider_neutral_identity(tmp_path, crypto):
    control_home = tmp_path / "control-plane"
    first = ChannelIdentityStore(crypto, control_home, global_home=tmp_path)
    registered = register_weixin_identity(
        first,
        subject="subject-a",
        bot_id="bot-a",
        bot_token="token-a",
        base_url="https://ilink.example",
        peer_id="subject-a",
    )
    with first.write() as conn:
        conn.execute("DROP INDEX IF EXISTS idx_channel_accounts_provider_account")
        conn.execute("DROP TRIGGER IF EXISTS outbound_messages_consistent_insert")
        conn.execute("DROP TRIGGER IF EXISTS channel_bindings_identity_consistent_insert")
        conn.execute("DROP TRIGGER IF EXISTS inbound_messages_binding_consistent_insert")
        conn.execute("DROP TRIGGER IF EXISTS inbound_messages_assign_sequence")
        conn.execute("DROP TRIGGER IF EXISTS managed_feishu_accounts_provider_insert")
        conn.execute("DROP TABLE binding_sequences")
        _downgrade_account_table_to_v7(conn, crypto)
        conn.execute("UPDATE channel_identity_meta SET value='4'")
        conn.execute("ALTER TABLE ilink_accounts DROP COLUMN provider")
        conn.execute("ALTER TABLE ilink_accounts DROP COLUMN provider_account_id")
        conn.execute(
            "ALTER TABLE ilink_accounts ADD COLUMN external_identity_id TEXT"
        )
        conn.execute(
            "UPDATE ilink_accounts SET external_identity_id=? WHERE account_id=?",
            (registered.external_identity_id, registered.account_id),
        )

    _rebuild_v4_account_table_with_inline_unique(first.path)
    migrated = ChannelIdentityStore(crypto, control_home, global_home=tmp_path)

    with migrated.read() as conn:
        row = conn.execute(
            "SELECT provider, provider_account_id, account_lookup_hash "
            "FROM connector_accounts WHERE account_id=?",
            (registered.account_id,),
        ).fetchone()
        version = conn.execute(
            "SELECT value FROM channel_identity_meta WHERE key='schema_version'"
        ).fetchone()["value"]
        binding_sequence_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='binding_sequences'"
        ).fetchone()
        account_foreign_keys = {
            table: {
                foreign_key["table"]
                for foreign_key in conn.execute(f"PRAGMA foreign_key_list({table})")
                if foreign_key["from"] == "account_id"
            }
            for table in (
                "channel_bindings",
                "context_tokens",
                "inbound_messages",
                "outbound_messages",
            )
        }
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert row["provider"] == "weixin_ilink"
    assert row["provider_account_id"] == row["account_lookup_hash"]
    assert binding_sequence_table is not None
    assert account_foreign_keys == {
        "channel_bindings": {"connector_accounts"},
        "context_tokens": {"connector_accounts"},
        "inbound_messages": {"connector_accounts"},
        "outbound_messages": {"connector_accounts"},
    }
    assert version == "12"


def test_store_migrates_v7_account_table_without_reencrypting_credentials(tmp_path, crypto):
    control_home = tmp_path / "control-plane"
    first = ChannelIdentityStore(crypto, control_home, global_home=tmp_path)
    registered = register_weixin_identity(
        first,
        subject="subject-a",
        bot_id="bot-a",
        bot_token="token-a",
        base_url="https://ilink.example",
        peer_id="subject-a",
    )
    with first.write() as conn:
        _downgrade_account_table_to_v7(conn, crypto)
        conn.execute("UPDATE channel_identity_meta SET value='7' WHERE key='schema_version'")

    migrated = ChannelIdentityStore(crypto, control_home, global_home=tmp_path)

    with migrated.read() as conn:
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        version = conn.execute(
            "SELECT value FROM channel_identity_meta WHERE key='schema_version'"
        ).fetchone()["value"]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    owner, resolved = resolve_binding(migrated, binding_id=registered.binding_id)
    assert "connector_accounts" in tables
    assert "ilink_accounts" not in tables
    assert version == "12"
    assert owner.owner_key == registered.owner_key
    assert resolve_connector_account(
        migrated,
        provider="weixin_ilink",
        account_id=resolved.account_id,
        credential_version=resolved.credential_version,
    ).credentials["bot_id"] == "bot-a"
    assert resolve_connector_account(
        migrated,
        provider="weixin_ilink",
        account_id=resolved.account_id,
        credential_version=resolved.credential_version,
    ).credentials["bot_token"] == "token-a"


def test_store_rolls_back_v7_account_rename_when_validation_fails(
    tmp_path, crypto, monkeypatch
):
    control_home = tmp_path / "control-plane"
    first = ChannelIdentityStore(crypto, control_home, global_home=tmp_path)
    register_weixin_identity(
        first,
        subject="subject-a",
        bot_id="bot-a",
        bot_token="token-a",
        base_url="https://ilink.example",
        peer_id="subject-a",
    )
    with first.write() as conn:
        _downgrade_account_table_to_v7(conn, crypto)
        conn.execute("UPDATE channel_identity_meta SET value='7' WHERE key='schema_version'")

    original = ChannelIdentityStore._validate_schema

    def fail_validation(conn):
        original(conn)
        raise RuntimeError("forced validation failure")

    fail_validation = staticmethod(fail_validation)

    monkeypatch.setattr(ChannelIdentityStore, "_validate_schema", fail_validation)
    with pytest.raises(RuntimeError, match="forced validation failure"):
        ChannelIdentityStore(crypto, control_home, global_home=tmp_path)

    conn = sqlite3.connect(first.path)
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        version = conn.execute(
            "SELECT value FROM channel_identity_meta WHERE key='schema_version'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert "ilink_accounts" in tables
    assert "connector_accounts" not in tables
    assert version == "7"


def test_store_migrates_v8_credentials_to_provider_neutral_envelope(tmp_path, crypto):
    control_home = tmp_path / "control-plane"
    first = ChannelIdentityStore(crypto, control_home, global_home=tmp_path)
    registered = register_weixin_identity(
        first,
        subject="subject-a",
        bot_id="bot-a",
        bot_token="token-a",
        base_url="https://ilink.example",
        peer_id="subject-a",
    )
    with first.write() as conn:
        _downgrade_account_table_to_v7(conn, crypto)
        conn.execute("ALTER TABLE ilink_accounts RENAME TO connector_accounts")
        conn.execute("UPDATE channel_identity_meta SET value='8' WHERE key='schema_version'")

    migrated = ChannelIdentityStore(crypto, control_home, global_home=tmp_path)

    with migrated.read() as conn:
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(connector_accounts)")
        }
        version = conn.execute(
            "SELECT value FROM channel_identity_meta WHERE key='schema_version'"
        ).fetchone()["value"]
    _, resolved = resolve_binding(migrated, binding_id=registered.binding_id)
    account = resolve_connector_account(
        migrated,
        provider="weixin_ilink",
        account_id=resolved.account_id,
        credential_version=resolved.credential_version,
    )
    assert {
        "account_lookup_hash",
        "credentials_ciphertext",
        "credentials_key_version",
    } <= columns
    assert not {
        "bot_id_ciphertext",
        "bot_token_ciphertext",
        "base_url",
    } & columns
    assert version == "12"
    assert resolve_connector_account(
        migrated,
        provider="weixin_ilink",
        account_id=resolved.account_id,
        credential_version=resolved.credential_version,
    ).credentials["bot_id"] == "bot-a"
    assert account.credentials["bot_token"] == "token-a"
    assert account.credentials["base_url"] == "https://ilink.example"


def test_store_migrates_v9_shared_account_schema_without_reencrypting(tmp_path, crypto):
    control_home = tmp_path / "control-plane"
    first = ChannelIdentityStore(crypto, control_home, global_home=tmp_path)
    registered = register_weixin_identity(
        first,
        subject="subject-a",
        bot_id="bot-a",
        bot_token="token-a",
        base_url="https://ilink.example",
        peer_id="subject-a",
    )
    with first.write() as conn:
        credentials = conn.execute(
            "SELECT credentials_ciphertext, credentials_key_version "
            "FROM connector_accounts WHERE account_id=?",
            (registered.account_id,),
        ).fetchone()
        conn.execute("DROP TRIGGER IF EXISTS connector_accounts_ownership_immutable")
        conn.execute(
            "ALTER TABLE connector_accounts ADD COLUMN external_identity_id TEXT"
        )
        conn.execute(
            "UPDATE connector_accounts SET external_identity_id=? WHERE account_id=?",
            (registered.external_identity_id, registered.account_id),
        )
        conn.execute(
            "UPDATE channel_identity_meta SET value='9' WHERE key='schema_version'"
        )

    migrated = ChannelIdentityStore(crypto, control_home, global_home=tmp_path)

    with migrated.read() as conn:
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(connector_accounts)")
        }
        row = conn.execute(
            "SELECT credentials_ciphertext, credentials_key_version "
            "FROM connector_accounts WHERE account_id=?",
            (registered.account_id,),
        ).fetchone()
        version = conn.execute(
            "SELECT value FROM channel_identity_meta WHERE key='schema_version'"
        ).fetchone()["value"]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert "external_identity_id" not in columns
    assert row["credentials_ciphertext"] == credentials["credentials_ciphertext"]
    assert row["credentials_key_version"] == credentials["credentials_key_version"]
    assert version == "12"


def test_store_rolls_back_v9_shared_account_migration_on_validation_failure(
    tmp_path, crypto, monkeypatch
):
    control_home = tmp_path / "control-plane"
    first = ChannelIdentityStore(crypto, control_home, global_home=tmp_path)
    register_weixin_identity(
        first,
        subject="subject-a",
        bot_id="bot-a",
        bot_token="token-a",
        base_url="https://ilink.example",
        peer_id="subject-a",
    )
    with first.write() as conn:
        conn.execute("DROP TRIGGER IF EXISTS connector_accounts_ownership_immutable")
        conn.execute(
            "ALTER TABLE connector_accounts ADD COLUMN external_identity_id TEXT"
        )
        conn.execute(
            "UPDATE channel_identity_meta SET value='9' WHERE key='schema_version'"
        )

    original = ChannelIdentityStore._validate_schema

    def fail_validation(conn):
        original(conn)
        raise RuntimeError("forced v10 validation failure")

    monkeypatch.setattr(
        ChannelIdentityStore,
        "_validate_schema",
        staticmethod(fail_validation),
    )
    with pytest.raises(RuntimeError, match="forced v10 validation failure"):
        ChannelIdentityStore(crypto, control_home, global_home=tmp_path)

    conn = sqlite3.connect(first.path)
    try:
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(connector_accounts)")
        }
        version = conn.execute(
            "SELECT value FROM channel_identity_meta WHERE key='schema_version'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert "external_identity_id" in columns
    assert version == "9"


def test_store_rolls_back_v8_credential_migration_on_authentication_failure(
    tmp_path, crypto
):
    control_home = tmp_path / "control-plane"
    first = ChannelIdentityStore(crypto, control_home, global_home=tmp_path)
    register_weixin_identity(
        first,
        subject="subject-a",
        bot_id="bot-a",
        bot_token="token-a",
        base_url="https://ilink.example",
        peer_id="subject-a",
    )
    with first.write() as conn:
        _downgrade_account_table_to_v7(conn, crypto)
        conn.execute("ALTER TABLE ilink_accounts RENAME TO connector_accounts")
        conn.execute(
            "UPDATE connector_accounts SET bot_token_ciphertext=X'00'"
        )
        conn.execute("UPDATE channel_identity_meta SET value='8' WHERE key='schema_version'")

    with pytest.raises(RuntimeError, match="malformed|failed authentication"):
        ChannelIdentityStore(crypto, control_home, global_home=tmp_path)

    conn = sqlite3.connect(first.path)
    try:
        version = conn.execute(
            "SELECT value FROM channel_identity_meta WHERE key='schema_version'"
        ).fetchone()[0]
        columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(connector_accounts)")
        }
    finally:
        conn.close()
    assert version == "8"
    assert "bot_token_ciphertext" in columns
    assert "credentials_ciphertext" not in columns


def test_store_migrates_v2_outbound_with_fresh_chunk_attempts(tmp_path, crypto):
    path = tmp_path / "control-plane" / "channel_identities.sqlite3"
    first = ChannelIdentityStore(crypto, path.parent, global_home=tmp_path)
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
        _downgrade_account_table_to_v7(conn, crypto)
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

    migrated = ChannelIdentityStore(crypto, path.parent, global_home=tmp_path)

    with migrated.read() as conn:
        row = conn.execute(
            """
            SELECT attempts, chunk_count, next_chunk_index, chunk_attempts,
                   failed_chunk_index, provider, source_kind, source_id,
                   binding_sequence FROM outbound_messages
            WHERE outbound_id='outbound-existing'
            """
        ).fetchone()
    assert tuple(row) == (
        20700,
        None,
        0,
        0,
        None,
        "weixin_ilink",
        "inbound",
        "inbound:inbound-existing",
        1,
    )


def test_store_rejects_unknown_newer_schema(tmp_path, crypto):
    path = tmp_path / "control-plane" / "channel_identities.sqlite3"
    first = ChannelIdentityStore(crypto, path.parent, global_home=tmp_path)
    with first.write() as conn:
        conn.execute(
            "UPDATE channel_identity_meta SET value='999' WHERE key='schema_version'"
        )

    with pytest.raises(RuntimeError, match="newer"):
        ChannelIdentityStore(crypto, path.parent, global_home=tmp_path)


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
    account = resolve_connector_account(
        store,
        provider="weixin_ilink",
        account_id=resolved.account_id,
        credential_version=resolved.credential_version,
    )
    assert owner == dashboard_owner
    assert account.credentials["bot_token"] == "token-two"
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
    account = resolve_connector_account(
        store,
        provider="weixin_ilink",
        account_id=resolved.account_id,
        credential_version=resolved.credential_version,
    )
    assert owner.owner_key == first_owner.owner_key
    assert account.credentials["bot_token"] == "token-one"
    assert account.credentials["base_url"] == "https://ilink.example"
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
    account = resolve_connector_account(
        store,
        provider="weixin_ilink",
        account_id=resolved.account_id,
        credential_version=resolved.credential_version,
    )
    assert owner.owner_key == first.owner_key
    assert account.credentials["bot_token"] == "token-two"
    assert resolved.credential_version == 2


def test_generic_shared_account_binds_distinct_owners_without_rotating_identity(store):
    first_owner = _owner(user_id="owner-a")
    second_owner = _owner(user_id="owner-b")

    first = register_connector_binding_for_owner(
        store,
        owner=first_owner,
        provider="fake_provider",
        provider_account_id="shared-bot",
        external_subject="subject-a",
        conversation_id="conversation-a",
        credentials={"token": "token-one"},
    )
    second = register_connector_binding_for_owner(
        store,
        owner=second_owner,
        provider="fake_provider",
        provider_account_id="shared-bot",
        external_subject="subject-b",
        conversation_id="conversation-b",
        credentials={"token": "token-two"},
    )

    assert first.account_id == second.account_id
    assert first.external_identity_id != second.external_identity_id
    assert first.binding_id != second.binding_id
    first_resolved_owner, first_channel = resolve_binding(
        store, binding_id=first.binding_id
    )
    second_resolved_owner, second_channel = resolve_binding(
        store, binding_id=second.binding_id
    )
    account = resolve_connector_account(
        store,
        provider="fake_provider",
        account_id=first.account_id,
        credential_version=second_channel.credential_version,
    )
    assert first_resolved_owner == first_owner
    assert second_resolved_owner == second_owner
    assert first_channel.conversation_id == "conversation-a"
    assert second_channel.conversation_id == "conversation-b"
    assert account.credentials == {"token": "token-two"}


def test_generic_registration_rejects_cross_owner_subject_rebinding(store):
    first_owner = _owner(user_id="owner-a")
    second_owner = _owner(user_id="owner-b")
    register_connector_binding_for_owner(
        store,
        owner=first_owner,
        provider="fake_provider",
        provider_account_id="shared-bot",
        external_subject="subject-a",
        conversation_id="conversation-a",
        credentials={"token": "token-one"},
    )

    with pytest.raises(ChannelIdentityOwnershipConflict):
        register_connector_binding_for_owner(
            store,
            owner=second_owner,
            provider="fake_provider",
            provider_account_id="shared-bot",
            external_subject="subject-a",
            conversation_id="conversation-b",
            credentials={"token": "attacker-token"},
        )

    with pytest.raises(RuntimeError, match="unavailable"):
        resolve_connector_account(
            store,
            provider="fake_provider",
            account_id="missing",
        )


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


def test_initialization_rolls_back_schema_and_version_when_key_validation_fails(
    tmp_path, crypto, monkeypatch
):
    control_home = tmp_path / "control-plane"
    original = ChannelIdentityStore._validate_referenced_key_versions

    def fail_validation(self, conn):
        raise RuntimeError("missing key")

    monkeypatch.setattr(
        ChannelIdentityStore,
        "_validate_referenced_key_versions",
        fail_validation,
    )
    with pytest.raises(RuntimeError, match="missing key"):
        ChannelIdentityStore(crypto, control_home, global_home=tmp_path)

    path = control_home / "channel_identities.sqlite3"
    conn = sqlite3.connect(path)
    try:
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    finally:
        conn.close()
    assert tables == []

    monkeypatch.setattr(
        ChannelIdentityStore,
        "_validate_referenced_key_versions",
        original,
    )
    store = ChannelIdentityStore(crypto, control_home, global_home=tmp_path)
    with store.read() as conn:
        assert conn.execute(
            "SELECT value FROM channel_identity_meta WHERE key='schema_version'"
        ).fetchone()["value"] == "12"


def test_store_allows_shared_account_across_owner_bound_identities(store):
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
    with store.read() as conn:
        peer = conn.execute(
            "SELECT peer_lookup_hash, peer_ciphertext, peer_key_version "
            "FROM channel_bindings WHERE binding_id=?",
            (first.binding_id,),
        ).fetchone()
    with store.write() as conn:
        conn.execute(
            "INSERT INTO channel_bindings VALUES (?, ?, ?, ?, ?, ?, 'active', 1, 1)",
            (
                "cb_shared",
                first.external_identity_id,
                second.account_id,
                "shared-conversation",
                peer["peer_ciphertext"],
                peer["peer_key_version"],
            ),
        )
    with store.read() as conn:
        row = conn.execute(
            "SELECT account_id, external_identity_id FROM channel_bindings "
            "WHERE binding_id='cb_shared'"
        ).fetchone()
    assert tuple(row) == (second.account_id, first.external_identity_id)

    with pytest.raises(sqlite3.IntegrityError, match="identity mismatch"):
        with store.write() as conn:
            conn.execute(
                "INSERT INTO channel_bindings VALUES (?, ?, ?, ?, ?, ?, 'active', 1, 1)",
                (
                    "cb_provider_mismatch",
                    first.external_identity_id,
                    "other-provider-account",
                    "other-conversation",
                    peer["peer_ciphertext"],
                    peer["peer_key_version"],
                ),
            )


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


def test_v10_migration_adds_empty_feishu_management_tables_without_guessing_owner(
    tmp_path, crypto
):
    control_home = tmp_path / "control-plane"
    first = ChannelIdentityStore(crypto, control_home, global_home=tmp_path)
    registered = register_connector_binding_for_owner(
        first,
        owner=_owner(user_id="owner-a"),
        provider="feishu",
        provider_account_id="cli-app",
        external_subject="actor-a",
        conversation_id="chat-a",
        credentials={"app_id": "cli-app", "app_secret": "secret"},
    )
    with first.write() as conn:
        conn.execute("DROP TABLE feishu_employee_profiles")
        conn.execute("DROP TABLE managed_feishu_accounts")
        conn.execute(
            "UPDATE channel_identity_meta SET value='10' WHERE key='schema_version'"
        )

    migrated = ChannelIdentityStore(crypto, control_home, global_home=tmp_path)

    with migrated.read() as conn:
        version = conn.execute(
            "SELECT value FROM channel_identity_meta WHERE key='schema_version'"
        ).fetchone()["value"]
        managed = conn.execute(
            "SELECT * FROM managed_feishu_accounts WHERE account_id=?",
            (registered.account_id,),
        ).fetchone()
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert version == "12"
    assert managed is None
    with pytest.raises(RuntimeError, match="unavailable"):
        resolve_connector_account(
            migrated,
            provider="feishu",
            account_id=registered.account_id,
            require_managed_feishu=True,
        )


def test_managed_feishu_registration_is_atomic_when_profile_encryption_fails(
    store, monkeypatch
):
    owner = _owner(user_id="owner-a")
    original = store.crypto.encrypt_text

    def fail_profile(value, *, table, record_id, field):
        if table == "feishu_employee_profiles":
            raise RuntimeError("profile encryption failed")
        return original(
            value,
            table=table,
            record_id=record_id,
            field=field,
        )

    monkeypatch.setattr(store.crypto, "encrypt_text", fail_profile)
    with pytest.raises(RuntimeError, match="profile encryption failed"):
        register_managed_feishu_account_for_owner(
            store,
            owner=owner,
            provider_account_id="cli-app",
            external_subject="actor-a",
            conversation_id="chat-a",
            credentials={"app_id": "cli-app", "app_secret": "secret"},
            employee_profile={"name": "Researcher"},
        )

    with store.read() as conn:
        assert conn.execute(
            "SELECT 1 FROM connector_accounts WHERE provider='feishu'"
        ).fetchone() is None
        assert conn.execute("SELECT 1 FROM managed_feishu_accounts").fetchone() is None
        assert conn.execute("SELECT 1 FROM channel_bindings").fetchone() is None


def test_managed_feishu_account_can_start_without_a_fake_conversation_binding(store):
    owner = _owner(user_id="owner-a")
    registered = register_managed_feishu_account_for_owner(
        store,
        owner=owner,
        provider_account_id="cli-app",
        external_subject="bot-a",
        conversation_id=None,
        credentials={"app_id": "cli-app", "app_secret": "secret"},
        employee_profile={"name": "Researcher"},
    )

    assert registered.binding_id == ""
    with store.read() as conn:
        assert conn.execute(
            "SELECT 1 FROM channel_bindings WHERE account_id=?",
            (registered.account_id,),
        ).fetchone() is None
    assert resolve_employee_profile(
        store, owner=owner, account_id=registered.account_id
    ).profile == {"name": "Researcher"}


def test_managed_feishu_account_has_immutable_owner_and_one_current_profile(store):
    owner = _owner(user_id="owner-a")
    registered = register_managed_feishu_account_for_owner(
        store,
        owner=owner,
        provider_account_id="cli-app",
        external_subject="actor-a",
        conversation_id="chat-a",
        credentials={"app_id": "cli-app", "app_secret": "secret"},
        employee_profile={"name": "Researcher", "tools": ["web", "file"]},
    )

    managed = resolve_managed_feishu_account(
        store, owner=owner, account_id=registered.account_id
    )
    profile = resolve_employee_profile(
        store, owner=owner, account_id=registered.account_id
    )

    assert managed.canonical_user_id == registered.canonical_user_id
    assert managed.profile_revision == 1
    assert managed.profile_fingerprint == profile.fingerprint
    assert profile.profile == {"name": "Researcher", "tools": ["web", "file"]}
    resolve_connector_account(
        store,
        provider="feishu",
        account_id=registered.account_id,
        require_managed_feishu=True,
    )
    second_owner_id = ensure_owner_binding(store, _owner(user_id="owner-b"))
    with pytest.raises(sqlite3.IntegrityError, match="Owner is immutable"):
        with store.write() as conn:
            conn.execute(
                "UPDATE managed_feishu_accounts SET canonical_user_id=? "
                "WHERE account_id=?",
                (second_owner_id, registered.account_id),
            )


def test_employee_profiles_are_encrypted_canonical_and_revision_fenced(store):
    owner = _owner(user_id="owner-a")
    registered = register_managed_feishu_account_for_owner(
        store,
        owner=owner,
        provider_account_id="cli-app",
        external_subject="actor-a",
        conversation_id="chat-a",
        credentials={"app_id": "cli-app", "app_secret": "secret"},
        employee_profile={"name": "Analyst", "policy": {"model": "fast"}},
    )
    expected_fingerprint = employee_profile_fingerprint(
        {"policy": {"model": "fast"}, "name": "Analyst"}
    )
    with store.read() as conn:
        stored = conn.execute(
            "SELECT profile_ciphertext, profile_fingerprint "
            "FROM feishu_employee_profiles WHERE account_id=? AND revision=1",
            (registered.account_id,),
        ).fetchone()
    assert stored["profile_fingerprint"] == expected_fingerprint
    assert b"Analyst" not in stored["profile_ciphertext"]

    updated = update_employee_profile(
        store,
        owner=owner,
        account_id=registered.account_id,
        profile={"name": "Analyst", "policy": {"model": "strong"}},
        expected_revision=1,
    )
    assert updated.revision == 2
    with pytest.raises(EmployeeProfileRevisionConflict, match="changed from 1 to 2"):
        update_employee_profile(
            store,
            owner=owner,
            account_id=registered.account_id,
            profile={"name": "stale"},
            expected_revision=1,
        )
    assert resolve_employee_profile(
        store,
        owner=owner,
        account_id=registered.account_id,
        revision=1,
    ).lifecycle_status == "superseded"
    with store.read() as conn:
        current_count = conn.execute(
            "SELECT COUNT(*) AS count FROM feishu_employee_profiles "
            "WHERE account_id=? AND lifecycle_status='active'",
            (registered.account_id,),
        ).fetchone()["count"]
    assert current_count == 1


def test_managed_feishu_credentials_rotate_and_sessions_roll_over_by_account(store):
    owner = _owner(user_id="owner-a")
    first = register_managed_feishu_account_for_owner(
        store,
        owner=owner,
        provider_account_id="first-app",
        external_subject="actor-a",
        conversation_id="chat-a",
        credentials={"app_id": "first-app", "app_secret": "old"},
        employee_profile={"name": "Analyst"},
    )
    second = register_managed_feishu_account_for_owner(
        store,
        owner=owner,
        provider_account_id="second-app",
        external_subject="actor-b",
        conversation_id="chat-b",
        credentials={"app_id": "second-app", "app_secret": "other"},
        employee_profile={"name": "Researcher"},
    )
    with store.write() as conn:
        conn.execute(
            "INSERT INTO channel_sessions VALUES (?, '', ?, 'stored-a', 1, 1, 1)",
            (first.binding_id, owner.owner_key),
        )
        conn.execute(
            "INSERT INTO channel_sessions VALUES (?, '', ?, 'stored-b', 1, 1, 1)",
            (second.binding_id, owner.owner_key),
        )

    updated = rotate_managed_feishu_credentials(
        store,
        owner=owner,
        account_id=first.account_id,
        credentials={"app_id": "first-app", "app_secret": "new"},
        expected_credential_version=1,
    )
    assert updated.credential_version == 2
    credentials, version = resolve_managed_feishu_credentials(
        store, owner=owner, account_id=first.account_id
    )
    assert credentials["app_secret"] == "new"
    assert version == 2
    with pytest.raises(FeishuCredentialRevisionConflict):
        rotate_managed_feishu_credentials(
            store,
            owner=owner,
            account_id=first.account_id,
            credentials={"app_id": "first-app", "app_secret": "stale"},
            expected_credential_version=1,
        )

    with store.write() as conn:
        conn.execute(
            """
            INSERT INTO inbound_messages(
                inbound_id, account_id, binding_id, provider_message_id,
                payload_ciphertext, payload_key_version, status,
                payload_kind, binding_sequence, dispatch_scope,
                profile_revision, attempts, next_attempt_at,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 1, 'queued', 'text', 2, '', 1, 0, 0, 1, 1)
            """,
            ("queued-rollover", first.account_id, first.binding_id, "queued-message", b"payload"),
        )
    with pytest.raises(RuntimeError, match="active conversations"):
        rollover_managed_feishu_sessions(
            store, owner=owner, account_id=first.account_id
        )
    with store.write() as conn:
        conn.execute("DELETE FROM inbound_messages WHERE inbound_id='queued-rollover'")

    assert rollover_managed_feishu_sessions(
        store, owner=owner, account_id=first.account_id
    ) == 1
    with store.read() as conn:
        rows = conn.execute(
            "SELECT stored_session_id FROM channel_sessions"
        ).fetchall()
    assert [row["stored_session_id"] for row in rows] == ["stored-b"]


def test_managed_feishu_owner_fences_registration_profile_and_lifecycle(store):
    first_owner = _owner(user_id="owner-a")
    second_owner = _owner(user_id="owner-b")
    registered = register_managed_feishu_account_for_owner(
        store,
        owner=first_owner,
        provider_account_id="cli-app",
        external_subject="actor-a",
        conversation_id="chat-a",
        credentials={"app_id": "cli-app", "app_secret": "secret"},
        employee_profile={"name": "Analyst"},
    )

    with pytest.raises(ChannelIdentityOwnershipConflict, match="another Owner"):
        register_connector_binding_for_owner(
            store,
            owner=second_owner,
            provider="feishu",
            provider_account_id="cli-app",
            external_subject="actor-b",
            conversation_id="chat-b",
            credentials={"app_id": "cli-app", "app_secret": "attacker"},
        )
    with pytest.raises(RuntimeError, match="unavailable"):
        resolve_employee_profile(
            store, owner=second_owner, account_id=registered.account_id
        )

    suspended = set_managed_feishu_account_status(
        store,
        owner=first_owner,
        account_id=registered.account_id,
        status="suspended",
    )
    assert suspended.lifecycle_status == "suspended"
    with pytest.raises(RuntimeError, match="unavailable"):
        resolve_connector_account(
            store,
            provider="feishu",
            account_id=registered.account_id,
            require_managed_feishu=True,
        )


def test_legacy_feishu_claim_rejects_ambiguous_existing_account(store):
    first_owner = _owner(user_id="owner-a")
    second_owner = _owner(user_id="owner-b")
    first = register_connector_binding_for_owner(
        store,
        owner=first_owner,
        provider="feishu",
        provider_account_id="shared-app",
        external_subject="actor-a",
        conversation_id="chat-a",
        credentials={"app_id": "shared-app", "app_secret": "secret"},
    )
    register_connector_binding_for_owner(
        store,
        owner=second_owner,
        provider="feishu",
        provider_account_id="shared-app",
        external_subject="actor-b",
        conversation_id="chat-b",
        credentials={"app_id": "shared-app", "app_secret": "secret"},
    )

    with pytest.raises(RuntimeError, match="ambiguous"):
        claim_existing_feishu_account_for_owner(
            store,
            owner=first_owner,
            account_id=first.account_id,
            employee_profile={"name": "Analyst"},
        )
    with store.read() as conn:
        assert conn.execute(
            "SELECT 1 FROM managed_feishu_accounts WHERE account_id=?",
            (first.account_id,),
        ).fetchone() is None


def test_generic_weixin_and_webhook_registration_remain_unmanaged(store):
    owner = _owner(user_id="owner-a")
    webhook = register_connector_binding_for_owner(
        store,
        owner=owner,
        provider="webhook",
        provider_account_id="route",
        external_subject="route",
        conversation_id="route",
        credentials={"token": "secret"},
    )
    weixin = register_weixin_identity(
        store,
        subject="subject-a",
        bot_id="bot-a",
        bot_token="token-a",
        base_url="https://ilink.example",
        peer_id="subject-a",
    )
    assert resolve_connector_account(
        store, provider="webhook", account_id=webhook.account_id
    ).credentials == {"token": "secret"}
    assert resolve_connector_account(
        store, provider="weixin_ilink", account_id=weixin.account_id
    ).credentials["bot_id"] == "bot-a"
    with store.read() as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS count FROM managed_feishu_accounts"
        ).fetchone()["count"]
    assert count == 0


def stat_mode(path) -> int:
    return path.stat().st_mode & 0o777
