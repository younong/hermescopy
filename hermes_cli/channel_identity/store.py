"""Durable SQLite store for external channel identities and queues."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .crypto import ChannelCrypto

SCHEMA_VERSION = 17
ACCOUNT_CREDENTIAL_AAD_TABLE = "ilink_accounts"
EMPLOYEE_PROFILE_AAD_TABLE = "employee_profiles"
_DB_FILENAME = "channel_identities.sqlite3"


def _builtin_assistant_employee_id(canonical_user_id: str) -> str:
    digest = hashlib.sha256(
        f"builtin-assistant:{canonical_user_id}".encode("utf-8")
    ).hexdigest()
    return f"emp_builtin_{digest}"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS channel_identity_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS enrollment_attempts (
    attempt_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    scene TEXT NOT NULL,
    source_lookup_hash TEXT NOT NULL,
    device_lookup_hash TEXT NOT NULL,
    qr_ciphertext BLOB,
    qr_key_version INTEGER,
    confirmed_ciphertext BLOB,
    confirmed_key_version INTEGER,
    target_canonical_user_id TEXT REFERENCES canonical_users(canonical_user_id),
    expires_at REAL NOT NULL,
    next_poll_at REAL NOT NULL,
    consumed_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_enrollment_attempt_status ON enrollment_attempts(status, next_poll_at);
CREATE TABLE IF NOT EXISTS enrollment_rate_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_lookup_hash TEXT NOT NULL,
    device_lookup_hash TEXT NOT NULL,
    occurred_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_enrollment_rate_time ON enrollment_rate_events(occurred_at);
CREATE TABLE IF NOT EXISTS canonical_users (
    canonical_user_id TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK(status IN ('pending', 'active', 'suspended')),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS owner_bindings (
    canonical_user_id TEXT PRIMARY KEY REFERENCES canonical_users(canonical_user_id),
    auth_provider TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    owner_user_id TEXT NOT NULL,
    owner_key TEXT NOT NULL UNIQUE,
    created_at REAL NOT NULL
);
CREATE TRIGGER IF NOT EXISTS owner_bindings_immutable
BEFORE UPDATE ON owner_bindings
BEGIN
    SELECT RAISE(ABORT, 'owner binding is immutable');
END;
CREATE TABLE IF NOT EXISTS external_identities (
    external_identity_id TEXT PRIMARY KEY,
    provider TEXT NOT NULL CHECK(length(trim(provider)) > 0),
    subject_lookup_hash TEXT NOT NULL,
    subject_ciphertext BLOB NOT NULL,
    subject_key_version INTEGER NOT NULL,
    canonical_user_id TEXT NOT NULL REFERENCES canonical_users(canonical_user_id),
    status TEXT NOT NULL CHECK(status IN ('active', 'suspended', 'revoked')),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(provider, subject_lookup_hash)
);
CREATE TRIGGER IF NOT EXISTS external_identities_ownership_immutable
BEFORE UPDATE OF provider, subject_lookup_hash, canonical_user_id ON external_identities
BEGIN
    SELECT RAISE(ABORT, 'external identity ownership is immutable');
END;
CREATE TABLE IF NOT EXISTS connector_accounts (
    account_id TEXT PRIMARY KEY,
    provider TEXT NOT NULL CHECK(length(trim(provider)) > 0),
    provider_account_id TEXT NOT NULL CHECK(length(trim(provider_account_id)) > 0),
    account_lookup_hash TEXT NOT NULL,
    credentials_ciphertext BLOB NOT NULL,
    credentials_key_version INTEGER NOT NULL,
    credential_version INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'active', 'suspended', 'revoked')),
    cursor_ciphertext BLOB,
    cursor_key_version INTEGER,
    poll_holder TEXT,
    poll_generation INTEGER NOT NULL DEFAULT 0,
    poll_health TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_connector_accounts_lookup_hash
ON connector_accounts(account_lookup_hash);
CREATE UNIQUE INDEX IF NOT EXISTS idx_channel_accounts_provider_account
ON connector_accounts(provider, provider_account_id);
CREATE TRIGGER IF NOT EXISTS connector_accounts_ownership_immutable
BEFORE UPDATE OF provider, provider_account_id, account_lookup_hash
ON connector_accounts
BEGIN
    SELECT RAISE(ABORT, 'channel account ownership is immutable');
END;
CREATE TABLE IF NOT EXISTS employees (
    employee_id TEXT PRIMARY KEY CHECK(employee_id GLOB 'emp_*'),
    canonical_user_id TEXT NOT NULL REFERENCES canonical_users(canonical_user_id),
    employee_kind TEXT NOT NULL DEFAULT 'managed'
        CHECK(employee_kind IN ('managed', 'builtin_assistant')),
    lifecycle_status TEXT NOT NULL
        CHECK(lifecycle_status IN ('active', 'suspended', 'revoked')),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_employees_owner
ON employees(canonical_user_id, lifecycle_status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_employees_builtin_assistant_owner
ON employees(canonical_user_id) WHERE employee_kind='builtin_assistant';
CREATE TRIGGER IF NOT EXISTS employees_owner_immutable
BEFORE UPDATE OF employee_id, canonical_user_id ON employees
BEGIN
    SELECT RAISE(ABORT, 'employee Owner is immutable');
END;
CREATE TRIGGER IF NOT EXISTS builtin_assistant_delete_protected
BEFORE DELETE ON employees
WHEN OLD.employee_kind='builtin_assistant'
BEGIN
    SELECT RAISE(ABORT, 'builtin employee is protected');
END;
CREATE TRIGGER IF NOT EXISTS builtin_assistant_identity_protected
BEFORE UPDATE OF employee_id, canonical_user_id, employee_kind, lifecycle_status
ON employees
WHEN OLD.employee_kind='builtin_assistant' OR NEW.employee_kind='builtin_assistant'
BEGIN
    SELECT RAISE(ABORT, 'builtin employee is protected');
END;
CREATE TABLE IF NOT EXISTS employee_collaboration_policies (
    employee_id TEXT PRIMARY KEY REFERENCES employees(employee_id),
    may_participate INTEGER NOT NULL DEFAULT 1
        CHECK(may_participate IN (0, 1)),
    may_create_groups INTEGER NOT NULL DEFAULT 0
        CHECK(may_create_groups IN (0, 1)),
    invite_quota INTEGER DEFAULT 5
        CHECK(invite_quota IS NULL OR invite_quota >= 0),
    updated_by_owner_key TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TRIGGER IF NOT EXISTS employee_collaboration_policy_identity_immutable
BEFORE UPDATE OF employee_id, created_at ON employee_collaboration_policies
BEGIN
    SELECT RAISE(ABORT, 'employee collaboration policy identity is immutable');
END;
CREATE TABLE IF NOT EXISTS builtin_assistant_policy (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    model_registration_id TEXT NOT NULL CHECK(length(trim(model_registration_id)) > 0),
    reasoning_effort TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision >= 1),
    updated_by_account_id TEXT NOT NULL CHECK(length(trim(updated_by_account_id)) > 0),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS employee_profiles (
    employee_id TEXT NOT NULL REFERENCES employees(employee_id),
    revision INTEGER NOT NULL CHECK(revision >= 1),
    profile_ciphertext BLOB NOT NULL,
    profile_key_version INTEGER NOT NULL,
    profile_fingerprint TEXT NOT NULL,
    lifecycle_status TEXT NOT NULL
        CHECK(lifecycle_status IN ('active', 'superseded', 'revoked')),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY(employee_id, revision)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_employee_current_profile
ON employee_profiles(employee_id) WHERE lifecycle_status='active';
CREATE TRIGGER IF NOT EXISTS employee_profiles_identity_immutable
BEFORE UPDATE OF employee_id, revision, profile_ciphertext, profile_key_version,
                 profile_fingerprint, created_at
ON employee_profiles
BEGIN
    SELECT RAISE(ABORT, 'employee profile revision is immutable');
END;
CREATE TABLE IF NOT EXISTS employee_channel_bindings (
    binding_id TEXT PRIMARY KEY CHECK(binding_id GLOB 'ecb_*'),
    employee_id TEXT NOT NULL REFERENCES employees(employee_id),
    provider TEXT NOT NULL CHECK(length(trim(provider)) > 0),
    connector_account_id TEXT NOT NULL REFERENCES connector_accounts(account_id),
    lifecycle_status TEXT NOT NULL
        CHECK(lifecycle_status IN ('active', 'suspended', 'revoked')),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_employee_current_feishu_binding
ON employee_channel_bindings(employee_id)
WHERE provider='feishu' AND lifecycle_status<>'revoked';
CREATE UNIQUE INDEX IF NOT EXISTS idx_employee_current_feishu_account
ON employee_channel_bindings(connector_account_id)
WHERE provider='feishu' AND lifecycle_status<>'revoked';
CREATE TRIGGER IF NOT EXISTS employee_channel_bindings_identity_immutable
BEFORE UPDATE OF binding_id, employee_id, provider, connector_account_id, created_at
ON employee_channel_bindings
BEGIN
    SELECT RAISE(ABORT, 'employee channel binding identity is immutable');
END;
CREATE TRIGGER IF NOT EXISTS employee_channel_bindings_provider_insert
BEFORE INSERT ON employee_channel_bindings
WHEN NOT EXISTS (
    SELECT 1 FROM connector_accounts a
    WHERE a.account_id=NEW.connector_account_id AND a.provider=NEW.provider
)
BEGIN
    SELECT RAISE(ABORT, 'employee channel binding provider mismatch');
END;
CREATE TABLE IF NOT EXISTS channel_bindings (
    binding_id TEXT PRIMARY KEY,
    external_identity_id TEXT NOT NULL REFERENCES external_identities(external_identity_id),
    account_id TEXT NOT NULL REFERENCES connector_accounts(account_id),
    peer_lookup_hash TEXT NOT NULL,
    peer_ciphertext BLOB NOT NULL,
    peer_key_version INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'active', 'suspended', 'revoked')),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(account_id, peer_lookup_hash)
);
CREATE TRIGGER IF NOT EXISTS channel_bindings_ownership_immutable
BEFORE UPDATE OF external_identity_id, account_id, peer_lookup_hash ON channel_bindings
BEGIN
    SELECT RAISE(ABORT, 'channel binding ownership is immutable');
END;
CREATE TRIGGER IF NOT EXISTS channel_bindings_identity_consistent_insert
BEFORE INSERT ON channel_bindings
WHEN NOT EXISTS (
    SELECT 1 FROM connector_accounts a
    JOIN external_identities e ON e.external_identity_id=NEW.external_identity_id
    WHERE a.account_id=NEW.account_id
      AND a.provider=e.provider
)
BEGIN
    SELECT RAISE(ABORT, 'channel binding identity mismatch');
END;
CREATE TABLE IF NOT EXISTS context_tokens (
    account_id TEXT NOT NULL REFERENCES connector_accounts(account_id),
    peer_lookup_hash TEXT NOT NULL,
    token_ciphertext BLOB NOT NULL,
    token_key_version INTEGER NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY(account_id, peer_lookup_hash)
);
CREATE TABLE IF NOT EXISTS channel_sessions (
    binding_id TEXT NOT NULL REFERENCES channel_bindings(binding_id),
    dispatch_scope TEXT NOT NULL DEFAULT '',
    owner_key TEXT NOT NULL,
    stored_session_id TEXT NOT NULL,
    worker_generation INTEGER,
    profile_revision INTEGER,
    updated_at REAL NOT NULL,
    PRIMARY KEY(binding_id, dispatch_scope)
);
CREATE TABLE IF NOT EXISTS binding_sequences (
    binding_id TEXT NOT NULL REFERENCES channel_bindings(binding_id),
    dispatch_scope TEXT NOT NULL DEFAULT '',
    last_sequence INTEGER NOT NULL DEFAULT 0 CHECK(last_sequence >= 0),
    PRIMARY KEY(binding_id, dispatch_scope)
);
CREATE TABLE IF NOT EXISTS inbound_messages (
    inbound_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES connector_accounts(account_id),
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
    payload_kind TEXT NOT NULL DEFAULT 'text',
    binding_sequence INTEGER,
    dispatch_scope TEXT NOT NULL DEFAULT '',
    profile_revision INTEGER,
    conversation_kind TEXT
        CHECK(conversation_kind IS NULL OR conversation_kind IN ('direct', 'group')),
    thread_id TEXT NOT NULL DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(account_id, provider_message_id)
);
CREATE INDEX IF NOT EXISTS idx_inbound_binding_status ON inbound_messages(binding_id, status, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_inbound_binding_sequence
ON inbound_messages(binding_id, dispatch_scope, binding_sequence)
WHERE binding_id IS NOT NULL;
CREATE TRIGGER IF NOT EXISTS inbound_messages_binding_consistent_insert
BEFORE INSERT ON inbound_messages
WHEN NEW.binding_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM channel_bindings b
    JOIN connector_accounts a ON a.account_id=b.account_id
    JOIN external_identities e ON e.external_identity_id=b.external_identity_id
    WHERE b.binding_id=NEW.binding_id AND b.account_id=NEW.account_id
      AND a.provider=e.provider
)
BEGIN
    SELECT RAISE(ABORT, 'inbound account binding mismatch');
END;
CREATE TRIGGER IF NOT EXISTS inbound_messages_assign_sequence
AFTER INSERT ON inbound_messages
WHEN NEW.binding_id IS NOT NULL AND NEW.binding_sequence IS NULL
BEGIN
    INSERT INTO binding_sequences(binding_id, dispatch_scope, last_sequence)
    VALUES (NEW.binding_id, NEW.dispatch_scope, 1)
    ON CONFLICT(binding_id, dispatch_scope)
    DO UPDATE SET last_sequence=last_sequence + 1;
    UPDATE inbound_messages
    SET binding_sequence=(
        SELECT last_sequence FROM binding_sequences
        WHERE binding_id=NEW.binding_id AND dispatch_scope=NEW.dispatch_scope
    )
    WHERE inbound_id=NEW.inbound_id;
END;
CREATE TRIGGER IF NOT EXISTS inbound_messages_ownership_immutable
BEFORE UPDATE OF account_id, binding_id, dispatch_scope, profile_revision ON inbound_messages
BEGIN
    SELECT RAISE(ABORT, 'inbound ownership is immutable');
END;
CREATE TRIGGER IF NOT EXISTS inbound_messages_sequence_immutable
BEFORE UPDATE OF binding_sequence ON inbound_messages
WHEN OLD.binding_sequence IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'inbound sequence is immutable');
END;
CREATE TABLE IF NOT EXISTS outbound_messages (
    outbound_id TEXT PRIMARY KEY,
    inbound_id TEXT UNIQUE REFERENCES inbound_messages(inbound_id),
    account_id TEXT NOT NULL REFERENCES connector_accounts(account_id),
    binding_id TEXT NOT NULL REFERENCES channel_bindings(binding_id),
    provider TEXT CHECK(provider IS NULL OR length(trim(provider)) > 0),
    source_kind TEXT CHECK(source_kind IS NULL OR source_kind IN ('inbound', 'cron', 'collaboration')),
    source_id TEXT UNIQUE,
    binding_sequence INTEGER,
    dispatch_scope TEXT NOT NULL DEFAULT '',
    client_message_id TEXT NOT NULL UNIQUE,
    payload_ciphertext BLOB,
    payload_key_version INTEGER,
    context_ciphertext BLOB,
    context_key_version INTEGER,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    chunk_count INTEGER,
    next_chunk_index INTEGER NOT NULL DEFAULT 0,
    chunk_attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL,
    claimed_by TEXT,
    claimed_at REAL,
    last_error TEXT,
    failed_chunk_index INTEGER,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_outbound_status_time ON outbound_messages(status, next_attempt_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_outbound_binding_sequence
ON outbound_messages(binding_id, dispatch_scope, binding_sequence)
WHERE binding_sequence IS NOT NULL;
CREATE TABLE IF NOT EXISTS outbound_receipts (
    account_id TEXT NOT NULL REFERENCES connector_accounts(account_id),
    binding_id TEXT NOT NULL REFERENCES channel_bindings(binding_id),
    outbound_id TEXT NOT NULL REFERENCES outbound_messages(outbound_id),
    part_index INTEGER NOT NULL CHECK(part_index >= 0),
    provider_message_lookup_hash TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY(outbound_id, part_index),
    UNIQUE(account_id, provider_message_lookup_hash)
);
CREATE INDEX IF NOT EXISTS idx_outbound_receipts_reply
ON outbound_receipts(account_id, binding_id, provider_message_lookup_hash);
CREATE TRIGGER IF NOT EXISTS outbound_messages_consistent_insert
BEFORE INSERT ON outbound_messages
WHEN NOT EXISTS (
    SELECT 1 FROM channel_bindings b
    JOIN connector_accounts a ON a.account_id=b.account_id
    JOIN external_identities e ON e.external_identity_id=b.external_identity_id
                              AND e.provider=a.provider
    WHERE b.binding_id=NEW.binding_id
      AND b.account_id=NEW.account_id
      AND (NEW.provider IS NULL OR a.provider=NEW.provider)
) OR (
    COALESCE(NEW.source_kind, 'inbound')='inbound' AND (
        NEW.inbound_id IS NULL OR NOT EXISTS (
            SELECT 1 FROM inbound_messages i
            WHERE i.inbound_id=NEW.inbound_id
              AND i.account_id=NEW.account_id
              AND i.binding_id=NEW.binding_id
        )
    )
) OR (NEW.source_kind IN ('cron', 'collaboration') AND NEW.inbound_id IS NOT NULL)
BEGIN
    SELECT RAISE(ABORT, 'outbound account binding mismatch');
END;
CREATE TRIGGER IF NOT EXISTS outbound_messages_ownership_immutable
BEFORE UPDATE OF inbound_id, account_id, binding_id, provider, source_kind,
                 source_id, binding_sequence, dispatch_scope
ON outbound_messages
BEGIN
    SELECT RAISE(ABORT, 'outbound ownership is immutable');
END;
"""


class ChannelIdentityStore:
    def __init__(
        self,
        crypto: ChannelCrypto,
        control_home: str | Path,
        *,
        global_home: str | Path,
        busy_timeout_ms: int = 5000,
    ) -> None:
        self.crypto = crypto
        raw_home = Path(control_home).expanduser()
        raw_global_home = Path(global_home).expanduser()
        if not raw_global_home.is_absolute():
            raise ValueError("channel identity global_home must be absolute")
        self.global_home = raw_global_home.resolve()
        self.control_home = raw_home.absolute()
        self.path = self.control_home / _DB_FILENAME
        self.busy_timeout_ms = int(busy_timeout_ms)
        self._prepare_path()
        self._initialize()

    def _prepare_path(self) -> None:
        parent = self.path.parent
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if parent.is_symlink() or not parent.is_dir():
            raise RuntimeError("channel identity database parent must be a real directory")
        if self.path.exists() and (self.path.is_symlink() or not self.path.is_file()):
            raise RuntimeError("channel identity database must be a regular file")
        if os.name != "nt":
            parent.chmod(0o700)
            if parent.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
                raise RuntimeError("channel identity database parent has unsafe permissions")

    def connect(self) -> sqlite3.Connection:
        self._prepare_path()
        conn = sqlite3.connect(self.path, timeout=self.busy_timeout_ms / 1000, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        conn.execute("PRAGMA journal_mode=WAL")
        if os.name != "nt":
            self.path.chmod(0o600)
        return conn

    def _initialize(self) -> None:
        with self.connect() as conn:
            # SQLite cannot rebuild a referenced parent table while foreign-key
            # enforcement is enabled. Initialization uses one isolated write
            # transaction and validates every foreign key before committing.
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute("BEGIN IMMEDIATE")
            try:
                has_meta = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='channel_identity_meta'"
                ).fetchone()
                if has_meta is None:
                    self._execute_schema(conn)
                    conn.execute(
                        "INSERT INTO channel_identity_meta(key, value) VALUES ('schema_version', ?)",
                        (str(SCHEMA_VERSION),),
                    )
                else:
                    row = conn.execute(
                        "SELECT value FROM channel_identity_meta WHERE key='schema_version'"
                    ).fetchone()
                    if row is None:
                        raise RuntimeError("channel identity database schema is corrupt")
                    try:
                        version = int(row["value"])
                    except (TypeError, ValueError) as exc:
                        raise RuntimeError("channel identity database schema is corrupt") from exc
                    if version > SCHEMA_VERSION:
                        raise RuntimeError(
                            "channel identity database schema is newer than supported"
                        )
                    while version < SCHEMA_VERSION:
                        if version == 1:
                            self._migrate_v1_to_v2(conn)
                            version = 2
                        elif version == 2:
                            self._migrate_v2_to_v3(conn)
                            version = 3
                        elif version == 3:
                            self._migrate_v3_to_v4(conn)
                            version = 4
                        elif version == 4:
                            self._migrate_v4_to_v5(conn)
                            version = 5
                        elif version == 5:
                            self._migrate_v5_to_v6(conn)
                            version = 6
                        elif version == 6:
                            self._migrate_v6_to_v7(conn)
                            version = 7
                        elif version == 7:
                            self._migrate_v7_to_v8(conn)
                            version = 8
                        elif version == 8:
                            self._migrate_v8_to_v9(conn)
                            version = 9
                        elif version == 9:
                            self._migrate_v9_to_v10(conn)
                            version = 10
                        elif version == 10:
                            self._migrate_v10_to_v11(conn)
                            version = 11
                        elif version == 11:
                            self._migrate_v11_to_v12(conn)
                            version = 12
                        elif version == 12:
                            self._migrate_v12_to_v13(conn)
                            version = 13
                        elif version == 13:
                            self._migrate_v13_to_v14(conn)
                            version = 14
                        elif version == 14:
                            self._migrate_v14_to_v15(conn)
                            version = 15
                        elif version == 15:
                            self._migrate_v15_to_v16(conn)
                            version = 16
                        elif version == 16:
                            self._migrate_v16_to_v17(conn)
                            version = 17
                        else:
                            raise RuntimeError(
                                "channel identity database schema is older than supported"
                            )
                self._execute_schema(conn)
                self._validate_schema(conn)
                self._validate_referenced_key_versions(conn)
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise

    @staticmethod
    def _execute_schema(conn: sqlite3.Connection) -> None:
        statement = ""
        for line in _SCHEMA.splitlines(keepends=True):
            statement += line
            if sqlite3.complete_statement(statement):
                conn.execute(statement)
                statement = ""
        if statement.strip():
            raise RuntimeError("channel identity schema is incomplete")

    @staticmethod
    def _migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            ALTER TABLE enrollment_attempts
            ADD COLUMN target_canonical_user_id TEXT
                REFERENCES canonical_users(canonical_user_id)
            """
        )
        conn.execute(
            "UPDATE channel_identity_meta SET value='2' WHERE key='schema_version'"
        )

    @staticmethod
    def _migrate_v2_to_v3(conn: sqlite3.Connection) -> None:
        existing = {
            row["name"] for row in conn.execute("PRAGMA table_info(outbound_messages)")
        }
        additions = (
            ("chunk_count", "INTEGER"),
            ("next_chunk_index", "INTEGER NOT NULL DEFAULT 0"),
            ("chunk_attempts", "INTEGER NOT NULL DEFAULT 0"),
            ("failed_chunk_index", "INTEGER"),
        )
        for column, declaration in additions:
            if column not in existing:
                conn.execute(
                    f"ALTER TABLE outbound_messages ADD COLUMN {column} {declaration}"
                )
        conn.execute(
            "UPDATE channel_identity_meta SET value='3' WHERE key='schema_version'"
        )

    @staticmethod
    def _migrate_v3_to_v4(conn: sqlite3.Connection) -> None:
        existing = {
            row["name"] for row in conn.execute("PRAGMA table_info(inbound_messages)")
        }
        additions = (
            ("payload_kind", "TEXT NOT NULL DEFAULT 'text'"),
            ("attempts", "INTEGER NOT NULL DEFAULT 0"),
            ("next_attempt_at", "REAL NOT NULL DEFAULT 0"),
            ("last_error", "TEXT"),
        )
        for column, declaration in additions:
            if column not in existing:
                conn.execute(
                    f"ALTER TABLE inbound_messages ADD COLUMN {column} {declaration}"
                )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_inbound_due "
            "ON inbound_messages(status, next_attempt_at, binding_id, created_at)"
        )
        conn.execute(
            "UPDATE channel_identity_meta SET value='4' WHERE key='schema_version'"
        )

    @staticmethod
    def _migrate_v4_to_v5(conn: sqlite3.Connection) -> None:
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(ilink_accounts)")}
        if "provider" not in existing:
            conn.execute(
                "ALTER TABLE ilink_accounts ADD COLUMN provider TEXT NOT NULL "
                "DEFAULT 'weixin_ilink' CHECK(length(trim(provider)) > 0)"
            )
        if "provider_account_id" not in existing:
            conn.execute(
                "ALTER TABLE ilink_accounts ADD COLUMN provider_account_id TEXT NOT NULL "
                "DEFAULT 'legacy-unknown' CHECK(length(trim(provider_account_id)) > 0)"
            )
        conn.execute("DROP TRIGGER IF EXISTS ilink_accounts_ownership_immutable")
        conn.execute(
            "UPDATE ilink_accounts SET provider='weixin_ilink', "
            "provider_account_id=bot_id_lookup_hash"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_channel_accounts_provider_account "
            "ON ilink_accounts(provider, provider_account_id)"
        )
        conn.execute(
            "UPDATE channel_identity_meta SET value='5' WHERE key='schema_version'"
        )

    @staticmethod
    def _migrate_v5_to_v6(conn: sqlite3.Connection) -> None:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS binding_sequences ("
            "binding_id TEXT PRIMARY KEY REFERENCES channel_bindings(binding_id), "
            "last_sequence INTEGER NOT NULL DEFAULT 0 CHECK(last_sequence >= 0))"
        )
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(inbound_messages)")}
        if "binding_sequence" not in existing:
            conn.execute("ALTER TABLE inbound_messages ADD COLUMN binding_sequence INTEGER")
        conn.execute("DROP TRIGGER IF EXISTS inbound_messages_ownership_immutable")
        conn.execute("DROP TRIGGER IF EXISTS inbound_messages_sequence_immutable")
        rows = conn.execute(
            "SELECT inbound_id, binding_id FROM inbound_messages "
            "WHERE binding_id IS NOT NULL "
            "ORDER BY binding_id, created_at, rowid"
        ).fetchall()
        current_binding = None
        sequence = 0
        for row in rows:
            if row["binding_id"] != current_binding:
                current_binding = row["binding_id"]
                sequence = 1
            else:
                sequence += 1
            conn.execute(
                "UPDATE inbound_messages SET binding_sequence=? WHERE inbound_id=?",
                (sequence, row["inbound_id"]),
            )
        conn.execute(
            "UPDATE channel_identity_meta SET value='6' WHERE key='schema_version'"
        )

    @staticmethod
    def _migrate_v6_to_v7(conn: sqlite3.Connection) -> None:
        conn.execute("DROP TRIGGER IF EXISTS outbound_messages_consistent_insert")
        conn.execute("DROP TRIGGER IF EXISTS outbound_messages_fill_legacy_identity")
        conn.execute("DROP TRIGGER IF EXISTS outbound_messages_ownership_immutable")
        conn.execute("DROP INDEX IF EXISTS idx_outbound_status_time")
        conn.execute("DROP INDEX IF EXISTS idx_outbound_binding_sequence")
        conn.execute("ALTER TABLE outbound_messages RENAME TO outbound_messages_v6")
        conn.execute(
            """
            CREATE TABLE outbound_messages (
                outbound_id TEXT PRIMARY KEY,
                inbound_id TEXT UNIQUE REFERENCES inbound_messages(inbound_id),
                account_id TEXT NOT NULL REFERENCES ilink_accounts(account_id),
                binding_id TEXT NOT NULL REFERENCES channel_bindings(binding_id),
                provider TEXT CHECK(provider IS NULL OR length(trim(provider)) > 0),
                source_kind TEXT CHECK(source_kind IS NULL OR source_kind IN ('inbound', 'cron', 'collaboration')),
                source_id TEXT UNIQUE,
                binding_sequence INTEGER,
                client_message_id TEXT NOT NULL UNIQUE,
                payload_ciphertext BLOB,
                payload_key_version INTEGER,
                context_ciphertext BLOB,
                context_key_version INTEGER,
                status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                chunk_count INTEGER,
                next_chunk_index INTEGER NOT NULL DEFAULT 0,
                chunk_attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at REAL NOT NULL,
                claimed_by TEXT,
                claimed_at REAL,
                last_error TEXT,
                failed_chunk_index INTEGER,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO outbound_messages
              (outbound_id, inbound_id, account_id, binding_id, provider,
               source_kind, source_id, binding_sequence, client_message_id,
               payload_ciphertext, payload_key_version, context_ciphertext,
               context_key_version, status, attempts, chunk_count,
               next_chunk_index, chunk_attempts, next_attempt_at, claimed_by,
               claimed_at, last_error, failed_chunk_index, created_at, updated_at)
            SELECT o.outbound_id, o.inbound_id, o.account_id, o.binding_id,
                   a.provider, 'inbound', 'inbound:' || o.inbound_id,
                   i.binding_sequence, o.client_message_id,
                   o.payload_ciphertext, o.payload_key_version,
                   o.context_ciphertext, o.context_key_version, o.status,
                   o.attempts, o.chunk_count, o.next_chunk_index,
                   o.chunk_attempts, o.next_attempt_at, o.claimed_by,
                   o.claimed_at, o.last_error, o.failed_chunk_index,
                   o.created_at, o.updated_at
            FROM outbound_messages_v6 o
            JOIN inbound_messages i ON i.inbound_id=o.inbound_id
            JOIN ilink_accounts a ON a.account_id=o.account_id
            """
        )
        conn.execute("DROP TABLE outbound_messages_v6")
        sequence_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(binding_sequences)")
        }
        if "dispatch_scope" not in sequence_columns:
            conn.execute(
                "ALTER TABLE binding_sequences ADD COLUMN dispatch_scope "
                "TEXT NOT NULL DEFAULT ''"
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_binding_sequences_scope "
                "ON binding_sequences(binding_id, dispatch_scope)"
            )
        conn.execute(
            """
            INSERT INTO binding_sequences(binding_id, dispatch_scope, last_sequence)
            SELECT binding_id, '', MAX(sequence) FROM (
                SELECT binding_id, binding_sequence AS sequence FROM inbound_messages
                WHERE binding_id IS NOT NULL
                UNION ALL
                SELECT binding_id, binding_sequence AS sequence FROM outbound_messages
            ) GROUP BY binding_id
            ON CONFLICT(binding_id, dispatch_scope) DO UPDATE SET
                last_sequence=MAX(binding_sequences.last_sequence, excluded.last_sequence)
            """
        )
        conn.execute("UPDATE channel_identity_meta SET value='7' WHERE key='schema_version'")

    @staticmethod
    def _migrate_v7_to_v8(conn: sqlite3.Connection) -> None:
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "ilink_accounts" not in tables or "connector_accounts" in tables:
            raise RuntimeError("channel account schema is inconsistent")
        account_count = conn.execute(
            "SELECT COUNT(*) AS count FROM ilink_accounts"
        ).fetchone()["count"]
        conn.execute("DROP TRIGGER IF EXISTS ilink_accounts_ownership_immutable")
        conn.execute("ALTER TABLE ilink_accounts RENAME TO connector_accounts")
        migrated_count = conn.execute(
            "SELECT COUNT(*) AS count FROM connector_accounts"
        ).fetchone()["count"]
        if migrated_count != account_count:
            raise RuntimeError("channel account migration lost rows")
        conn.execute("UPDATE channel_identity_meta SET value='8' WHERE key='schema_version'")

    def _migrate_v8_to_v9(self, conn: sqlite3.Connection) -> None:
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(connector_accounts)")
        }
        legacy_columns = {
            "bot_id_lookup_hash",
            "bot_id_ciphertext",
            "bot_id_key_version",
            "bot_token_ciphertext",
            "bot_token_key_version",
            "base_url",
        }
        if not legacy_columns <= columns:
            raise RuntimeError("legacy connector account schema is inconsistent")
        rows = conn.execute(
            "SELECT * FROM connector_accounts ORDER BY account_id"
        ).fetchall()
        migrated: list[tuple[bytes, int, str]] = []
        for row in rows:
            bot_id = self.crypto.decrypt_text(
                row["bot_id_ciphertext"],
                table=ACCOUNT_CREDENTIAL_AAD_TABLE,
                record_id=row["account_id"],
                field="bot_id",
                version=row["bot_id_key_version"],
            )
            bot_token = self.crypto.decrypt_text(
                row["bot_token_ciphertext"],
                table=ACCOUNT_CREDENTIAL_AAD_TABLE,
                record_id=row["account_id"],
                field="bot_token",
                version=row["bot_token_key_version"],
            )
            credentials = json.dumps(
                {
                    "base_url": row["base_url"],
                    "bot_id": bot_id,
                    "bot_token": bot_token,
                },
                separators=(",", ":"),
                sort_keys=True,
            )
            ciphertext, version = self.crypto.encrypt_text(
                credentials,
                table=ACCOUNT_CREDENTIAL_AAD_TABLE,
                record_id=row["account_id"],
                field="credentials",
            )
            migrated.append((ciphertext, version, row["account_id"]))

        conn.execute("DROP TRIGGER IF EXISTS connector_accounts_ownership_immutable")
        conn.execute("DROP TRIGGER IF EXISTS managed_feishu_accounts_provider_insert")
        conn.execute("DROP TRIGGER IF EXISTS employee_channel_bindings_provider_insert")
        conn.execute("DROP TRIGGER IF EXISTS channel_bindings_identity_consistent_insert")
        conn.execute("DROP TRIGGER IF EXISTS inbound_messages_binding_consistent_insert")
        conn.execute("DROP TRIGGER IF EXISTS outbound_messages_consistent_insert")
        conn.execute("DROP INDEX IF EXISTS idx_channel_accounts_provider_account")
        conn.execute(
            """
            CREATE TABLE connector_accounts_v9 (
                account_id TEXT PRIMARY KEY,
                provider TEXT NOT NULL CHECK(length(trim(provider)) > 0),
                provider_account_id TEXT NOT NULL
                    CHECK(length(trim(provider_account_id)) > 0),
                account_lookup_hash TEXT NOT NULL,
                credentials_ciphertext BLOB NOT NULL,
                credentials_key_version INTEGER NOT NULL,
                credential_version INTEGER NOT NULL,
                status TEXT NOT NULL
                    CHECK(status IN ('pending', 'active', 'suspended', 'revoked')),
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
        migrated_by_id = {
            account_id: (ciphertext, version)
            for ciphertext, version, account_id in migrated
        }
        for row in rows:
            ciphertext, key_version = migrated_by_id[row["account_id"]]
            conn.execute(
                """
                INSERT INTO connector_accounts_v9
                  (account_id, provider, provider_account_id,
                   account_lookup_hash, credentials_ciphertext,
                   credentials_key_version, credential_version, status,
                   cursor_ciphertext, cursor_key_version, poll_holder,
                   poll_generation, poll_health, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["account_id"],
                    row["provider"],
                    row["provider_account_id"],
                    row["bot_id_lookup_hash"],
                    ciphertext,
                    key_version,
                    row["credential_version"],
                    row["status"],
                    row["cursor_ciphertext"],
                    row["cursor_key_version"],
                    row["poll_holder"],
                    row["poll_generation"],
                    row["poll_health"],
                    row["created_at"],
                    row["updated_at"],
                ),
            )
        conn.execute("DROP TABLE connector_accounts")
        conn.execute("ALTER TABLE connector_accounts_v9 RENAME TO connector_accounts")
        conn.execute(
            "CREATE UNIQUE INDEX idx_connector_accounts_lookup_hash "
            "ON connector_accounts(account_lookup_hash)"
        )
        conn.execute(
            "CREATE UNIQUE INDEX idx_channel_accounts_provider_account "
            "ON connector_accounts(provider, provider_account_id)"
        )
        conn.execute("UPDATE channel_identity_meta SET value='9' WHERE key='schema_version'")

    @staticmethod
    def _migrate_v9_to_v10(conn: sqlite3.Connection) -> None:
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(connector_accounts)")
        }
        required = {
            "account_id",
            "provider",
            "provider_account_id",
            "account_lookup_hash",
            "credentials_ciphertext",
            "credentials_key_version",
        }
        if not required <= columns:
            raise RuntimeError("connector account schema is inconsistent")
        conn.execute("DROP TRIGGER IF EXISTS connector_accounts_ownership_immutable")
        conn.execute("DROP TRIGGER IF EXISTS managed_feishu_accounts_provider_insert")
        conn.execute("DROP TRIGGER IF EXISTS employee_channel_bindings_provider_insert")
        conn.execute("DROP TRIGGER IF EXISTS channel_bindings_identity_consistent_insert")
        conn.execute("DROP TRIGGER IF EXISTS outbound_messages_consistent_insert")
        if "external_identity_id" in columns:
            before = conn.execute(
                "SELECT COUNT(*) AS count FROM connector_accounts"
            ).fetchone()["count"]
            conn.execute("ALTER TABLE connector_accounts DROP COLUMN external_identity_id")
            after = conn.execute(
                "SELECT COUNT(*) AS count FROM connector_accounts"
            ).fetchone()["count"]
            if after != before:
                raise RuntimeError("connector account migration lost rows")
        conn.execute(
            "UPDATE channel_identity_meta SET value='10' WHERE key='schema_version'"
        )

    @staticmethod
    def _migrate_v10_to_v11(conn: sqlite3.Connection) -> None:
        # Existing Feishu accounts predate account-level Owner attribution. They
        # remain generic and therefore cannot be selected as managed employees
        # until an authenticated Owner explicitly claims an unambiguous account.
        conn.execute(
            "UPDATE channel_identity_meta SET value='11' WHERE key='schema_version'"
        )

    @staticmethod
    def _migrate_v11_to_v12(conn: sqlite3.Connection) -> None:
        conn.execute("DROP TRIGGER IF EXISTS inbound_messages_assign_sequence")
        conn.execute("DROP TRIGGER IF EXISTS inbound_messages_ownership_immutable")
        conn.execute("DROP TRIGGER IF EXISTS outbound_messages_ownership_immutable")
        inbound_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(inbound_messages)")
        }
        if "dispatch_scope" not in inbound_columns:
            conn.execute(
                "ALTER TABLE inbound_messages ADD COLUMN dispatch_scope "
                "TEXT NOT NULL DEFAULT ''"
            )
        if "profile_revision" not in inbound_columns:
            conn.execute(
                "ALTER TABLE inbound_messages ADD COLUMN profile_revision INTEGER"
            )
        conn.execute("DROP INDEX IF EXISTS idx_inbound_binding_sequence")
        conn.execute(
            "CREATE UNIQUE INDEX idx_inbound_binding_sequence "
            "ON inbound_messages(binding_id, dispatch_scope, binding_sequence) "
            "WHERE binding_id IS NOT NULL"
        )
        outbound_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(outbound_messages)")
        }
        if "dispatch_scope" not in outbound_columns:
            conn.execute(
                "ALTER TABLE outbound_messages ADD COLUMN dispatch_scope "
                "TEXT NOT NULL DEFAULT ''"
            )
        conn.execute("DROP INDEX IF EXISTS idx_outbound_binding_sequence")
        conn.execute(
            "CREATE UNIQUE INDEX idx_outbound_binding_sequence "
            "ON outbound_messages(binding_id, dispatch_scope, binding_sequence) "
            "WHERE binding_sequence IS NOT NULL"
        )

        conn.execute("ALTER TABLE channel_sessions RENAME TO channel_sessions_v11")
        conn.execute(
            """
            CREATE TABLE channel_sessions (
                binding_id TEXT NOT NULL REFERENCES channel_bindings(binding_id),
                dispatch_scope TEXT NOT NULL DEFAULT '',
                owner_key TEXT NOT NULL,
                stored_session_id TEXT NOT NULL,
                worker_generation INTEGER,
                profile_revision INTEGER,
                updated_at REAL NOT NULL,
                PRIMARY KEY(binding_id, dispatch_scope)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO channel_sessions
              (binding_id, dispatch_scope, owner_key, stored_session_id,
               worker_generation, profile_revision, updated_at)
            SELECT binding_id, '', owner_key, stored_session_id,
                   worker_generation, NULL, updated_at
            FROM channel_sessions_v11
            """
        )
        conn.execute("DROP TABLE channel_sessions_v11")

        conn.execute("ALTER TABLE binding_sequences RENAME TO binding_sequences_v11")
        conn.execute(
            """
            CREATE TABLE binding_sequences (
                binding_id TEXT NOT NULL REFERENCES channel_bindings(binding_id),
                dispatch_scope TEXT NOT NULL DEFAULT '',
                last_sequence INTEGER NOT NULL DEFAULT 0 CHECK(last_sequence >= 0),
                PRIMARY KEY(binding_id, dispatch_scope)
            )
            """
        )
        conn.execute(
            "INSERT INTO binding_sequences(binding_id, dispatch_scope, last_sequence) "
            "SELECT binding_id, '', last_sequence FROM binding_sequences_v11"
        )
        conn.execute("DROP TABLE binding_sequences_v11")
        conn.execute(
            "UPDATE channel_identity_meta SET value='12' WHERE key='schema_version'"
        )

    @staticmethod
    def _migrate_v12_to_v13(conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS feishu_employee_collaboration_policies (
                account_id TEXT PRIMARY KEY
                    REFERENCES managed_feishu_accounts(account_id),
                may_participate INTEGER NOT NULL DEFAULT 1
                    CHECK(may_participate IN (0, 1)),
                may_create_groups INTEGER NOT NULL DEFAULT 0
                    CHECK(may_create_groups IN (0, 1)),
                invite_quota INTEGER DEFAULT 5
                    CHECK(invite_quota IS NULL OR invite_quota >= 0),
                updated_by_owner_key TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        conn.execute(
            "UPDATE channel_identity_meta SET value='13' WHERE key='schema_version'"
        )

    @staticmethod
    def _migrate_v13_to_v14(conn: sqlite3.Connection) -> None:
        inbound_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(inbound_messages)")
        }
        if "conversation_kind" not in inbound_columns:
            conn.execute(
                "ALTER TABLE inbound_messages ADD COLUMN conversation_kind TEXT "
                "CHECK(conversation_kind IS NULL OR conversation_kind IN ('direct', 'group'))"
            )
        if "thread_id" not in inbound_columns:
            conn.execute(
                "ALTER TABLE inbound_messages ADD COLUMN thread_id TEXT NOT NULL DEFAULT ''"
            )
        conn.execute("DROP TRIGGER IF EXISTS inbound_messages_ownership_immutable")
        conn.execute("DROP TRIGGER IF EXISTS outbound_messages_consistent_insert")
        conn.execute("DROP TRIGGER IF EXISTS outbound_messages_ownership_immutable")
        conn.execute("DROP INDEX IF EXISTS idx_outbound_status_time")
        conn.execute("DROP INDEX IF EXISTS idx_outbound_binding_sequence")
        conn.execute("ALTER TABLE outbound_messages RENAME TO outbound_messages_v13")
        conn.execute(
            """
            CREATE TABLE outbound_messages (
                outbound_id TEXT PRIMARY KEY,
                inbound_id TEXT UNIQUE REFERENCES inbound_messages(inbound_id),
                account_id TEXT NOT NULL REFERENCES connector_accounts(account_id),
                binding_id TEXT NOT NULL REFERENCES channel_bindings(binding_id),
                provider TEXT CHECK(provider IS NULL OR length(trim(provider)) > 0),
                source_kind TEXT CHECK(source_kind IS NULL OR source_kind IN ('inbound', 'cron', 'collaboration')),
                source_id TEXT UNIQUE,
                binding_sequence INTEGER,
                dispatch_scope TEXT NOT NULL DEFAULT '',
                client_message_id TEXT NOT NULL UNIQUE,
                payload_ciphertext BLOB,
                payload_key_version INTEGER,
                context_ciphertext BLOB,
                context_key_version INTEGER,
                status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                chunk_count INTEGER,
                next_chunk_index INTEGER NOT NULL DEFAULT 0,
                chunk_attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at REAL NOT NULL,
                claimed_by TEXT,
                claimed_at REAL,
                last_error TEXT,
                failed_chunk_index INTEGER,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO outbound_messages
              (outbound_id, inbound_id, account_id, binding_id, provider,
               source_kind, source_id, binding_sequence, dispatch_scope,
               client_message_id, payload_ciphertext, payload_key_version,
               context_ciphertext, context_key_version, status, attempts,
               chunk_count, next_chunk_index, chunk_attempts, next_attempt_at,
               claimed_by, claimed_at, last_error, failed_chunk_index,
               created_at, updated_at)
            SELECT outbound_id, inbound_id, account_id, binding_id, provider,
                   source_kind, source_id, binding_sequence, dispatch_scope,
                   client_message_id, payload_ciphertext, payload_key_version,
                   context_ciphertext, context_key_version, status, attempts,
                   chunk_count, next_chunk_index, chunk_attempts, next_attempt_at,
                   claimed_by, claimed_at, last_error, failed_chunk_index,
                   created_at, updated_at
            FROM outbound_messages_v13
            """
        )
        conn.execute("DROP TABLE outbound_messages_v13")
        conn.execute(
            "UPDATE channel_identity_meta SET value='14' WHERE key='schema_version'"
        )

    @staticmethod
    def _migrate_v14_to_v15(conn: sqlite3.Connection) -> None:
        legacy_tables = (
            "managed_feishu_accounts",
            "feishu_employee_profiles",
            "feishu_employee_collaboration_policies",
        )
        for table in legacy_tables:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            if exists is not None:
                count = conn.execute(
                    f"SELECT COUNT(*) AS count FROM {table}"
                ).fetchone()["count"]
                if count:
                    raise RuntimeError(
                        "channel identity schema v14 contains legacy Feishu employee data; "
                        "remove it before upgrading"
                    )
        for trigger in (
            "feishu_employee_collaboration_policy_identity_immutable",
            "managed_feishu_accounts_owner_immutable",
            "managed_feishu_accounts_provider_insert",
            "feishu_employee_profiles_identity_immutable",
        ):
            conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        conn.execute("DROP INDEX IF EXISTS idx_feishu_employee_current")
        conn.execute("DROP INDEX IF EXISTS idx_managed_feishu_owner")
        for table in (
            "feishu_employee_collaboration_policies",
            "feishu_employee_profiles",
            "managed_feishu_accounts",
        ):
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.execute(
            "UPDATE channel_identity_meta SET value='15' WHERE key='schema_version'"
        )

    @staticmethod
    def _migrate_v15_to_v16(conn: sqlite3.Connection) -> None:
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(employees)")
        }
        if not columns:
            conn.execute(
                """
                CREATE TABLE employees (
                    employee_id TEXT PRIMARY KEY CHECK(employee_id GLOB 'emp_*'),
                    canonical_user_id TEXT NOT NULL
                        REFERENCES canonical_users(canonical_user_id),
                    lifecycle_status TEXT NOT NULL
                        CHECK(lifecycle_status IN ('active', 'suspended', 'revoked')),
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
        if "employee_kind" not in columns:
            conn.execute(
                "ALTER TABLE employees ADD COLUMN employee_kind TEXT NOT NULL "
                "DEFAULT 'managed' CHECK(employee_kind IN ('managed', 'builtin_assistant'))"
            )
        for row in conn.execute(
            "SELECT canonical_user_id, created_at FROM owner_bindings"
        ).fetchall():
            employee_id = _builtin_assistant_employee_id(row["canonical_user_id"])
            conn.execute(
                """
                INSERT INTO employees
                  (employee_id, canonical_user_id, employee_kind, lifecycle_status,
                   created_at, updated_at)
                VALUES (?, ?, 'builtin_assistant', 'active', ?, ?)
                ON CONFLICT(employee_id) DO NOTHING
                """,
                (
                    employee_id,
                    row["canonical_user_id"],
                    row["created_at"],
                    row["created_at"],
                ),
            )
            inserted = conn.execute(
                "SELECT canonical_user_id, employee_kind, lifecycle_status "
                "FROM employees WHERE employee_id=?",
                (employee_id,),
            ).fetchone()
            if (
                inserted is None
                or inserted["canonical_user_id"] != row["canonical_user_id"]
                or inserted["employee_kind"] != "builtin_assistant"
                or inserted["lifecycle_status"] != "active"
            ):
                raise RuntimeError("builtin assistant employee migration is inconsistent")
        inconsistent_owner = conn.execute(
            """
            SELECT o.canonical_user_id
            FROM owner_bindings o
            LEFT JOIN employees e
              ON e.canonical_user_id=o.canonical_user_id
             AND e.employee_kind='builtin_assistant'
            GROUP BY o.canonical_user_id
            HAVING COUNT(e.employee_id)<>1
            LIMIT 1
            """
        ).fetchone()
        if inconsistent_owner is not None:
            raise RuntimeError("builtin assistant employee migration is inconsistent")
        conn.execute(
            "UPDATE channel_identity_meta SET value='16' WHERE key='schema_version'"
        )

    @staticmethod
    def _migrate_v16_to_v17(conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS builtin_assistant_policy (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                model_registration_id TEXT NOT NULL
                    CHECK(length(trim(model_registration_id)) > 0),
                reasoning_effort TEXT NOT NULL,
                revision INTEGER NOT NULL CHECK(revision >= 1),
                updated_by_account_id TEXT NOT NULL
                    CHECK(length(trim(updated_by_account_id)) > 0),
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        conn.execute(
            "UPDATE channel_identity_meta SET value='17' WHERE key='schema_version'"
        )

    @staticmethod
    def _validate_schema(conn: sqlite3.Connection) -> None:
        foreign_key_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_key_errors:
            raise RuntimeError("channel identity database foreign keys are inconsistent")
        integrity = conn.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or integrity[0] != "ok":
            raise RuntimeError("channel identity database integrity check failed")
        inconsistent = conn.execute(
            """
            SELECT 1
            FROM channel_bindings b
            LEFT JOIN connector_accounts a ON a.account_id=b.account_id
            LEFT JOIN external_identities e ON e.external_identity_id=b.external_identity_id
            WHERE a.account_id IS NULL OR e.external_identity_id IS NULL
               OR a.provider<>e.provider
            UNION ALL
            SELECT 1
            FROM employee_channel_bindings eb
            LEFT JOIN connector_accounts a
              ON a.account_id=eb.connector_account_id
            LEFT JOIN employees employee ON employee.employee_id=eb.employee_id
            WHERE a.account_id IS NULL OR employee.employee_id IS NULL
               OR a.provider<>eb.provider
            UNION ALL
            SELECT 1
            FROM inbound_messages i
            LEFT JOIN channel_bindings b ON b.binding_id=i.binding_id
            WHERE i.binding_id IS NOT NULL
              AND (b.binding_id IS NULL OR b.account_id<>i.account_id
                   OR i.binding_sequence IS NULL OR i.binding_sequence<1)
            UNION ALL
            SELECT 1
            FROM outbound_messages o
            LEFT JOIN inbound_messages i ON i.inbound_id=o.inbound_id
            LEFT JOIN channel_bindings b ON b.binding_id=o.binding_id
            LEFT JOIN connector_accounts a ON a.account_id=o.account_id
            LEFT JOIN external_identities e ON e.external_identity_id=b.external_identity_id
            WHERE b.binding_id IS NULL OR a.account_id IS NULL
               OR b.account_id<>o.account_id OR a.provider<>o.provider
               OR e.external_identity_id IS NULL OR e.provider<>o.provider
               OR (o.source_kind='cron' AND (
                    o.binding_sequence IS NULL OR o.binding_sequence<1
                    OR NOT EXISTS (
                        SELECT 1 FROM binding_sequences s
                        WHERE s.binding_id=o.binding_id
                          AND s.last_sequence>=o.binding_sequence)))
               OR (o.source_kind='inbound' AND (
                    i.inbound_id IS NULL OR i.account_id<>o.account_id
                    OR i.binding_id<>o.binding_id))
               OR (o.source_kind='cron' AND o.inbound_id IS NOT NULL)
            LIMIT 1
            """
        ).fetchone()
        if inconsistent is not None:
            raise RuntimeError("channel identity database invariants are inconsistent")

    def _validate_referenced_key_versions(self, conn: sqlite3.Connection) -> None:
        encrypted_columns = (
            ("enrollment_attempts", "qr_key_version"),
            ("enrollment_attempts", "confirmed_key_version"),
            ("external_identities", "subject_key_version"),
            ("connector_accounts", "credentials_key_version"),
            ("connector_accounts", "cursor_key_version"),
            ("employee_profiles", "profile_key_version"),
            ("channel_bindings", "peer_key_version"),
            ("context_tokens", "token_key_version"),
            ("inbound_messages", "payload_key_version"),
            ("inbound_messages", "context_key_version"),
            ("outbound_messages", "payload_key_version"),
            ("outbound_messages", "context_key_version"),
        )
        for table, column in encrypted_columns:
            rows = conn.execute(
                f"SELECT DISTINCT {column} AS version FROM {table} WHERE {column} IS NOT NULL"
            ).fetchall()
            for row in rows:
                self.crypto.encryption.key(int(row["version"]))

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()
