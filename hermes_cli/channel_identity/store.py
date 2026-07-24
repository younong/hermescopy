"""Durable SQLite store for external channel identities and queues."""

from __future__ import annotations

import os
import sqlite3
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from hermes_constants import get_hermes_home

from .crypto import ChannelCrypto

SCHEMA_VERSION = 2
_DB_RELATIVE_PATH = Path("control-plane") / "channel_identities.sqlite3"

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
    provider TEXT NOT NULL,
    subject_lookup_hash TEXT NOT NULL,
    subject_ciphertext BLOB NOT NULL,
    subject_key_version INTEGER NOT NULL,
    canonical_user_id TEXT NOT NULL REFERENCES canonical_users(canonical_user_id),
    status TEXT NOT NULL CHECK(status IN ('active', 'suspended', 'revoked')),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(provider, subject_lookup_hash)
);
CREATE TABLE IF NOT EXISTS ilink_accounts (
    account_id TEXT PRIMARY KEY,
    external_identity_id TEXT NOT NULL REFERENCES external_identities(external_identity_id),
    bot_id_lookup_hash TEXT NOT NULL UNIQUE,
    bot_id_ciphertext BLOB NOT NULL,
    bot_id_key_version INTEGER NOT NULL,
    bot_token_ciphertext BLOB NOT NULL,
    bot_token_key_version INTEGER NOT NULL,
    base_url TEXT NOT NULL,
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
CREATE TABLE IF NOT EXISTS channel_bindings (
    binding_id TEXT PRIMARY KEY,
    external_identity_id TEXT NOT NULL REFERENCES external_identities(external_identity_id),
    account_id TEXT NOT NULL REFERENCES ilink_accounts(account_id),
    peer_lookup_hash TEXT NOT NULL,
    peer_ciphertext BLOB NOT NULL,
    peer_key_version INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'active', 'suspended', 'revoked')),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(account_id, peer_lookup_hash)
);
CREATE TABLE IF NOT EXISTS context_tokens (
    account_id TEXT NOT NULL REFERENCES ilink_accounts(account_id),
    peer_lookup_hash TEXT NOT NULL,
    token_ciphertext BLOB NOT NULL,
    token_key_version INTEGER NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY(account_id, peer_lookup_hash)
);
CREATE TABLE IF NOT EXISTS channel_sessions (
    binding_id TEXT PRIMARY KEY REFERENCES channel_bindings(binding_id),
    owner_key TEXT NOT NULL,
    stored_session_id TEXT NOT NULL,
    worker_generation INTEGER,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS inbound_messages (
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
);
CREATE INDEX IF NOT EXISTS idx_inbound_binding_status ON inbound_messages(binding_id, status, created_at);
CREATE TABLE IF NOT EXISTS outbound_messages (
    outbound_id TEXT PRIMARY KEY,
    inbound_id TEXT NOT NULL UNIQUE REFERENCES inbound_messages(inbound_id),
    account_id TEXT NOT NULL REFERENCES ilink_accounts(account_id),
    binding_id TEXT NOT NULL REFERENCES channel_bindings(binding_id),
    client_message_id TEXT NOT NULL UNIQUE,
    payload_ciphertext BLOB,
    payload_key_version INTEGER,
    context_ciphertext BLOB,
    context_key_version INTEGER,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL,
    claimed_by TEXT,
    claimed_at REAL,
    last_error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_outbound_status_time ON outbound_messages(status, next_attempt_at);
"""


class ChannelIdentityStore:
    def __init__(
        self,
        crypto: ChannelCrypto,
        *,
        path: str | Path | None = None,
        busy_timeout_ms: int = 5000,
    ) -> None:
        self.crypto = crypto
        raw_path = Path(path).expanduser() if path is not None else get_hermes_home() / _DB_RELATIVE_PATH
        self.path = raw_path.absolute()
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
            conn.executescript(_SCHEMA)
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT value FROM channel_identity_meta WHERE key='schema_version'"
                ).fetchone()
                if row is None:
                    conn.execute(
                        "INSERT INTO channel_identity_meta(key, value) VALUES ('schema_version', ?)",
                        (str(SCHEMA_VERSION),),
                    )
                else:
                    try:
                        version = int(row["value"])
                    except (TypeError, ValueError) as exc:
                        raise RuntimeError("channel identity database schema is corrupt") from exc
                    if version == 1:
                        self._migrate_v1_to_v2(conn)
                    elif version != SCHEMA_VERSION:
                        direction = "newer" if version > SCHEMA_VERSION else "older"
                        raise RuntimeError(
                            f"channel identity database schema is {direction} than supported"
                        )
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        self._validate_referenced_key_versions()

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

    def _validate_referenced_key_versions(self) -> None:
        encrypted_columns = (
            ("enrollment_attempts", "qr_key_version"),
            ("enrollment_attempts", "confirmed_key_version"),
            ("external_identities", "subject_key_version"),
            ("ilink_accounts", "bot_id_key_version"),
            ("ilink_accounts", "bot_token_key_version"),
            ("ilink_accounts", "cursor_key_version"),
            ("channel_bindings", "peer_key_version"),
            ("context_tokens", "token_key_version"),
            ("inbound_messages", "payload_key_version"),
            ("inbound_messages", "context_key_version"),
            ("outbound_messages", "payload_key_version"),
            ("outbound_messages", "context_key_version"),
        )
        with self.connect() as conn:
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
