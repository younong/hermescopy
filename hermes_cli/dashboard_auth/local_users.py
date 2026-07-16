"""Durable local dashboard account and session store.

This module deliberately owns only the local-user persistence primitive.  A
provider and administrative CLI can use it later without duplicating password,
session, revocation, or filesystem safety rules.  It never stores plaintext
passwords or bearer tokens: session credentials are high-entropy opaque values
and SQLite retains only domain-separated keyed digests.

The store is Control Plane state, not owner state.  In particular, an Owner
Worker must receive an explicit ``HERMES_CONTROL_HOME`` to access it; falling
back to an owner-local ``HERMES_HOME`` would create a separate and unsafe
identity authority.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import stat
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal, Sequence

from hermes_constants import get_hermes_home


_SCHEMA_VERSION = 2
_DB_NAME = "local-users.sqlite3"
_ACCESS_PREFIX = "hlu1.at."
_REFRESH_PREFIX = "hlu1.rt."
_PASSWORD_SCHEME = "scrypt"
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
_SCRYPT_SALT_BYTES = 16
_MAX_PASSWORD_BYTES = 4096
_USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,63}$")

# ``pending_reset`` remains readable only for v1 migration. New writes use an
# active account plus ``must_change_password`` so the temporary credential can
# establish the narrowly scoped session required to complete the change.
AccountStatus = Literal["active", "disabled", "pending_reset"]
AccountRole = Literal["admin", "member"]


class LocalUserStoreUnavailable(RuntimeError):
    """The local account authority is missing, unsafe, or cannot be read.

    Callers should fail closed rather than replacing this store with a
    process-local fallback.
    """


class LocalUserStoreConflict(RuntimeError):
    """A requested account lifecycle operation conflicts with existing state."""


@dataclass(frozen=True)
class LocalAccount:
    """Non-secret durable local account metadata."""

    account_id: str
    username: str
    display_name: str
    role: AccountRole
    status: AccountStatus
    must_change_password: bool
    auth_revision: int
    created_at: int
    updated_at: int
    password_changed_at: int
    disabled_at: int | None


@dataclass(frozen=True)
class LocalSession:
    """Fresh opaque credentials.  Do not log or persist this object."""

    session_id: str
    account: LocalAccount
    access_token: str
    refresh_token: str
    access_expires_at: int
    refresh_expires_at: int


@dataclass(frozen=True)
class VerifiedLocalSession:
    """A verified session; it intentionally contains no raw credentials."""

    session_id: str
    account: LocalAccount
    access_expires_at: int
    refresh_expires_at: int


def control_plane_home() -> Path:
    """Resolve the Control Plane directory with the authority-store contract.

    ``authority.control_plane_home`` is intentionally not imported here: this
    storage primitive must remain importable in reduced installations before
    the broader authority subsystem is registered.  The validation semantics
    match it, including refusing an owner-worker fallback.
    """
    worker_owner = str(os.environ.get("HERMES_OWNER_KEY") or "").strip()
    explicit_home = str(os.environ.get("HERMES_CONTROL_HOME") or "").strip()
    if worker_owner and not explicit_home:
        raise LocalUserStoreUnavailable(
            "control plane home is required in owner workers"
        )
    home = Path(explicit_home) if explicit_home else get_hermes_home() / "control-plane"
    try:
        if home.exists() or home.is_symlink():
            mode = home.lstat().st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                raise LocalUserStoreUnavailable("control plane home must be a directory")
            if os.name != "nt" and mode & (stat.S_IWGRP | stat.S_IWOTH):
                raise LocalUserStoreUnavailable("control plane home has unsafe permissions")
    except LocalUserStoreUnavailable:
        raise
    except OSError as exc:
        raise LocalUserStoreUnavailable(
            "control plane home cannot be inspected"
        ) from exc
    return home


def normalize_username(username: str) -> str:
    """Return the canonical local-login name or reject ambiguous names."""
    normalized = str(username or "").strip().lower()
    if not _USERNAME_RE.fullmatch(normalized):
        raise ValueError("username must match [a-z0-9][a-z0-9._-]{2,63}")
    return normalized


def hash_password(password: str) -> str:
    """Create a versioned, salted scrypt password representation."""
    encoded = _password_bytes(password)
    salt = secrets.token_bytes(_SCRYPT_SALT_BYTES)
    derived = hashlib.scrypt(
        encoded,
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
        maxmem=0,
    )
    return (
        f"{_PASSWORD_SCHEME}${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}$"
        f"{base64.b64encode(salt).decode('ascii')}"
        f"${base64.b64encode(derived).decode('ascii')}"
    )


def verify_password(password: str, encoded: str) -> bool:
    """Verify a bounded scrypt record in constant time; malformed is false."""
    try:
        scheme, n_s, r_s, p_s, salt_text, expected_text = encoded.split("$")
        if scheme != _PASSWORD_SCHEME:
            return False
        n, r, p = int(n_s), int(r_s), int(p_s)
        # Stored parameters are untrusted database input.  Bound them before
        # calling scrypt to prevent a corrupted/tampered DB becoming a memory
        # exhaustion primitive.
        if (
            n != _SCRYPT_N
            or r != _SCRYPT_R
            or p != _SCRYPT_P
            or n & (n - 1)
        ):
            return False
        salt = base64.b64decode(salt_text.encode("ascii"), validate=True)
        expected = base64.b64decode(expected_text.encode("ascii"), validate=True)
        if len(salt) != _SCRYPT_SALT_BYTES or len(expected) != _SCRYPT_DKLEN:
            return False
        actual = hashlib.scrypt(
            _password_bytes(password),
            salt=salt,
            n=n,
            r=r,
            p=p,
            dklen=len(expected),
            maxmem=0,
        )
    except (ValueError, TypeError, UnicodeEncodeError, MemoryError):
        return False
    return hmac.compare_digest(actual, expected)


def _password_bytes(password: str) -> bytes:
    if not isinstance(password, str):
        raise ValueError("password must be text")
    encoded = password.encode("utf-8")
    if not encoded or len(encoded) > _MAX_PASSWORD_BYTES:
        raise ValueError("password has an invalid length")
    return encoded


class LocalUserStore:
    """SQLite authority for local dashboard accounts and opaque sessions."""

    def __init__(
        self,
        *,
        secret: bytes,
        control_home: str | Path | None = None,
        db_name: str = _DB_NAME,
        max_accounts: int = 5,
    ) -> None:
        if not isinstance(secret, bytes) or len(secret) < 32:
            raise ValueError("local user store secret must be at least 32 bytes")
        if not db_name or Path(db_name).name != db_name:
            raise ValueError("db_name must be a basename")
        if not isinstance(max_accounts, int) or max_accounts < 1:
            raise ValueError("max_accounts must be positive")
        self.control_home = (
            Path(control_home) if control_home is not None else control_plane_home()
        )
        self.path = self.control_home / db_name
        self.max_accounts = max_accounts
        self._digest_key = hmac.new(
            secret, b"hermes-dashboard-local-users-token-digest-v1", hashlib.sha256
        ).digest()
        self._init_lock = threading.Lock()
        self._initialized = False

    # -- account lifecycle -------------------------------------------------

    def bootstrap_accounts(
        self,
        accounts: Sequence[tuple[str, str, str] | tuple[str, str]],
        *,
        expected_count: int = 5,
        now: int | None = None,
    ) -> tuple[LocalAccount, ...]:
        """Atomically create the initial, exact-size local account set.

        Each entry is ``(username, password)`` or
        ``(username, password, display_name)``.  This method rejects any
        partially initialized database and leaves no rows on invalid input.
        """
        if expected_count != self.max_accounts:
            raise ValueError("bootstrap count must equal the configured account cap")
        if len(accounts) != expected_count:
            raise ValueError("bootstrap must create exactly the configured account cap")
        prepared = self._prepare_accounts(accounts)
        timestamp = _now(now)
        self._ensure_ready()
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                if conn.execute("SELECT 1 FROM accounts LIMIT 1").fetchone() is not None:
                    raise LocalUserStoreConflict("local accounts are already initialized")
                for username, password_hash, display_name in prepared:
                    conn.execute(
                        "INSERT INTO accounts("
                        "account_id, username, display_name, password_hash, role, status, "
                        "must_change_password, auth_revision, created_at, updated_at, "
                        "password_changed_at, disabled_at"
                        ") VALUES (?, ?, ?, ?, ?, 'active', 0, 1, ?, ?, ?, NULL)",
                        (
                            _new_id(), username, display_name, password_hash,
                            "member", timestamp, timestamp, timestamp,
                        ),
                    )
                rows = conn.execute(
                    "SELECT * FROM accounts ORDER BY username"
                ).fetchall()
                conn.execute("COMMIT")
                return tuple(self._account_from_row(row) for row in rows)
        except LocalUserStoreConflict:
            raise
        except (sqlite3.Error, OSError) as exc:
            raise LocalUserStoreUnavailable("local user store is unavailable") from exc

    def create_account(
        self,
        *,
        username: str,
        password: str,
        display_name: str = "",
        role: AccountRole = "member",
        status: AccountStatus = "active",
        must_change_password: bool = False,
        now: int | None = None,
    ) -> LocalAccount:
        """Create one account while enforcing the configured hard account cap."""
        if role not in ("admin", "member"):
            raise ValueError("invalid account role")
        if status not in ("active", "disabled"):
            raise ValueError("invalid account status")
        if not isinstance(must_change_password, bool):
            raise ValueError("must_change_password must be boolean")
        if status == "disabled" and must_change_password:
            raise ValueError("disabled account cannot require a password change")
        canonical = normalize_username(username)
        password_hash = hash_password(password)
        display = _display_name(display_name, canonical)
        timestamp = _now(now)
        self._ensure_ready()
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                count = int(conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0])
                if count >= self.max_accounts:
                    raise LocalUserStoreConflict("local account cap has been reached")
                account_id = _new_id()
                conn.execute(
                    "INSERT INTO accounts("
                    "account_id, username, display_name, password_hash, role, status, "
                    "must_change_password, auth_revision, created_at, updated_at, "
                    "password_changed_at, disabled_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)",
                    (
                        account_id, canonical, display, password_hash, role, status,
                        int(must_change_password), timestamp, timestamp, timestamp,
                        timestamp if status == "disabled" else None,
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM accounts WHERE account_id=?", (account_id,)
                ).fetchone()
                conn.execute("COMMIT")
                return self._account_from_row(row)
        except LocalUserStoreConflict:
            raise
        except sqlite3.IntegrityError as exc:
            raise LocalUserStoreConflict("local account already exists") from exc
        except (sqlite3.Error, OSError) as exc:
            raise LocalUserStoreUnavailable("local user store is unavailable") from exc

    def get_account(self, username: str) -> LocalAccount | None:
        """Look up public account metadata by a canonical login name."""
        canonical = normalize_username(username)
        self._ensure_ready()
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM accounts WHERE username=?", (canonical,)
                ).fetchone()
                return self._account_from_row(row) if row is not None else None
        except (sqlite3.Error, OSError) as exc:
            raise LocalUserStoreUnavailable("local user store is unavailable") from exc

    def get_account_by_id(self, account_id: str) -> LocalAccount | None:
        """Look up non-secret account metadata for a provider-verified ID."""
        if not isinstance(account_id, str) or not account_id:
            return None
        self._ensure_ready()
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM accounts WHERE account_id=?", (account_id,)
                ).fetchone()
                return self._account_from_row(row) if row is not None else None
        except (sqlite3.Error, OSError) as exc:
            raise LocalUserStoreUnavailable("local user store is unavailable") from exc

    def list_accounts(self) -> tuple[LocalAccount, ...]:
        """Return all non-secret account metadata in canonical-name order."""
        self._ensure_ready()
        try:
            with self._connect() as conn:
                rows = conn.execute("SELECT * FROM accounts ORDER BY username").fetchall()
                return tuple(self._account_from_row(row) for row in rows)
        except (sqlite3.Error, OSError) as exc:
            raise LocalUserStoreUnavailable("local user store is unavailable") from exc

    def verify_credentials(
        self, *, username: str, password: str
    ) -> LocalAccount | None:
        """Validate credentials without revealing unknown/disabled account state.

        The caller deliberately receives only ``None`` for every rejected
        credential.  A dummy scrypt verification runs for missing users and
        malformed stored hashes to keep the expensive path comparable.
        """
        try:
            canonical = normalize_username(username)
            _password_bytes(password)
        except ValueError:
            # Still execute scrypt for syntactically invalid user input.
            verify_password("invalid", _DUMMY_PASSWORD_HASH)
            return None
        self._ensure_ready()
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM accounts WHERE username=?", (canonical,)
                ).fetchone()
                password_hash = str(row["password_hash"]) if row is not None else _DUMMY_PASSWORD_HASH
                valid_password = verify_password(password, password_hash)
                if row is None or not valid_password:
                    return None
                account = self._account_from_row(row)
                # A reset-required account remains active so it can establish
                # the narrowly scoped session needed to replace its temporary
                # password. The HTTP gate restricts all normal capabilities.
                return account if account.status == "active" else None
        except (sqlite3.Error, OSError) as exc:
            raise LocalUserStoreUnavailable("local user store is unavailable") from exc

    def set_password(
        self,
        *,
        username: str,
        password: str,
        require_reset: bool = False,
        now: int | None = None,
    ) -> LocalAccount:
        """Change a password, revoke all sessions, and advance auth revision."""
        canonical = normalize_username(username)
        password_hash = hash_password(password)
        timestamp = _now(now)
        return self._update_account_auth(
            canonical,
            password_hash=password_hash,
            must_change_password=require_reset,
            now=timestamp,
        )

    def set_account_display_name(
        self,
        *,
        username: str,
        display_name: str,
        now: int | None = None,
    ) -> LocalAccount:
        """Update display metadata without changing credentials or sessions."""
        canonical = normalize_username(username)
        display = _display_name(display_name, canonical)
        timestamp = _now(now)
        self._ensure_ready()
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT * FROM accounts WHERE username=?", (canonical,)
                ).fetchone()
                if row is None:
                    raise LocalUserStoreConflict("local account does not exist")
                account = self._account_from_row(row)
                conn.execute(
                    "UPDATE accounts SET display_name=?, updated_at=? WHERE account_id=?",
                    (display, timestamp, account.account_id),
                )
                updated = conn.execute(
                    "SELECT * FROM accounts WHERE account_id=?", (account.account_id,)
                ).fetchone()
                conn.execute("COMMIT")
                return self._account_from_row(updated)
        except LocalUserStoreConflict:
            raise
        except (sqlite3.Error, OSError) as exc:
            raise LocalUserStoreUnavailable("local user store is unavailable") from exc

    def set_account_status(
        self,
        *,
        username: str,
        status: AccountStatus,
        now: int | None = None,
    ) -> LocalAccount:
        """Enable or disable an account and invalidate all of its sessions."""
        if status not in ("active", "disabled"):
            raise ValueError("invalid account status")
        return self._update_account_auth(
            normalize_username(username),
            status=status,
            must_change_password=False if status == "disabled" else None,
            now=_now(now),
        )

    def revoke_all_sessions(self, *, username: str, now: int | None = None) -> LocalAccount:
        """Invalidate every session for an account by advancing its revision."""
        return self._update_account_auth(normalize_username(username), now=_now(now))

    def set_account_role(
        self, *, username: str, role: AccountRole, now: int | None = None
    ) -> LocalAccount:
        """Set an account role while preserving at least one active administrator."""
        if role not in ("admin", "member"):
            raise ValueError("invalid account role")
        canonical = normalize_username(username)
        timestamp = _now(now)
        self._ensure_ready()
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT * FROM accounts WHERE username=?", (canonical,)
                ).fetchone()
                if row is None:
                    raise LocalUserStoreConflict("local account does not exist")
                old = self._account_from_row(row)
                if old.role == role:
                    conn.execute("COMMIT")
                    return old
                if old.role == "admin" and old.status == "active" and role != "admin":
                    active_admins = int(conn.execute(
                        "SELECT COUNT(*) FROM accounts WHERE role='admin' AND status='active'"
                    ).fetchone()[0])
                    if active_admins <= 1:
                        raise LocalUserStoreConflict("cannot remove the last active admin")
                conn.execute(
                    "UPDATE accounts SET role=?, auth_revision=?, updated_at=? "
                    "WHERE account_id=?",
                    (role, old.auth_revision + 1, timestamp, old.account_id),
                )
                self._revoke_sessions_in_transaction(
                    conn, old.account_id, timestamp, "account_role_changed"
                )
                updated = conn.execute(
                    "SELECT * FROM accounts WHERE account_id=?", (old.account_id,)
                ).fetchone()
                conn.execute("COMMIT")
                return self._account_from_row(updated)
        except LocalUserStoreConflict:
            raise
        except (sqlite3.Error, OSError) as exc:
            raise LocalUserStoreUnavailable("local user store is unavailable") from exc

    # -- session lifecycle -------------------------------------------------

    def create_session(
        self,
        *,
        account: LocalAccount,
        access_ttl_seconds: int,
        refresh_ttl_seconds: int,
        now: int | None = None,
    ) -> LocalSession:
        """Mint one opaque access/refresh pair for a current active account."""
        if access_ttl_seconds < 60 or refresh_ttl_seconds < access_ttl_seconds:
            raise ValueError("session TTLs are invalid")
        timestamp = _now(now)
        self._ensure_ready()
        access_token, refresh_token = self._new_tokens()
        session_id = _new_id()
        access_expiry = timestamp + int(access_ttl_seconds)
        refresh_expiry = timestamp + int(refresh_ttl_seconds)
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT * FROM accounts WHERE account_id=?", (account.account_id,)
                ).fetchone()
                if row is None:
                    raise LocalUserStoreConflict("local account no longer exists")
                current = self._account_from_row(row)
                if current.status != "active" or current.auth_revision != account.auth_revision:
                    raise LocalUserStoreConflict("local account authorization changed")
                conn.execute(
                    "INSERT INTO sessions("
                    "session_id, account_id, access_token_digest, refresh_token_digest, "
                    "access_expires_at, refresh_expires_at, auth_revision_at_issue, created_at, "
                    "last_used_at, revoked_at, revoked_reason"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)",
                    (
                        session_id, current.account_id, self._digest(access_token),
                        self._digest(refresh_token), access_expiry, refresh_expiry,
                        current.auth_revision, timestamp, timestamp,
                    ),
                )
                conn.execute("COMMIT")
                return LocalSession(
                    session_id=session_id, account=current, access_token=access_token,
                    refresh_token=refresh_token, access_expires_at=access_expiry,
                    refresh_expires_at=refresh_expiry,
                )
        except LocalUserStoreConflict:
            raise
        except (sqlite3.Error, OSError) as exc:
            raise LocalUserStoreUnavailable("local user store is unavailable") from exc

    def verify_access_token(
        self, token: str, *, now: int | None = None
    ) -> VerifiedLocalSession | None:
        """Return the still-current session for an opaque access credential."""
        if not self._recognizes(token, _ACCESS_PREFIX):
            return None
        timestamp = _now(now)
        self._ensure_ready()
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT s.*, a.account_id AS a_account_id, a.username AS a_username, "
                    "a.display_name AS a_display_name, a.role AS a_role, a.status AS a_status, "
                    "a.must_change_password AS a_must_change_password, "
                    "a.auth_revision AS a_auth_revision, a.created_at AS a_created_at, "
                    "a.updated_at AS a_updated_at, a.password_changed_at AS a_password_changed_at, "
                    "a.disabled_at AS a_disabled_at "
                    "FROM sessions s JOIN accounts a ON a.account_id=s.account_id "
                    "WHERE s.access_token_digest=?",
                    (self._digest(token),),
                ).fetchone()
                verified = self._verified_session_from_join(row, timestamp)
                if verified is not None:
                    conn.execute(
                        "UPDATE sessions SET last_used_at=? WHERE session_id=?",
                        (timestamp, verified.session_id),
                    )
                conn.execute("COMMIT")
                return verified
        except (sqlite3.Error, OSError) as exc:
            raise LocalUserStoreUnavailable("local user store is unavailable") from exc

    def rotate_refresh_token(
        self,
        token: str,
        *,
        access_ttl_seconds: int,
        refresh_ttl_seconds: int,
        now: int | None = None,
    ) -> LocalSession | None:
        """Consume a refresh token once and rotate both session credentials.

        Reuse of a previously rotated refresh token is treated as theft: every
        session for that exact account is revoked and its authorization
        revision advances before ``None`` is returned.
        """
        if not self._recognizes(token, _REFRESH_PREFIX):
            return None
        if access_ttl_seconds < 60 or refresh_ttl_seconds < access_ttl_seconds:
            raise ValueError("session TTLs are invalid")
        timestamp = _now(now)
        token_digest = self._digest(token)
        self._ensure_ready()
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                history = conn.execute(
                    "SELECT account_id FROM refresh_token_history "
                    "WHERE token_digest=? AND expires_at>?",
                    (token_digest, timestamp),
                ).fetchone()
                if history is not None:
                    self._revoke_account_in_transaction(
                        conn, str(history["account_id"]), timestamp, "refresh_reuse"
                    )
                    conn.execute("COMMIT")
                    return None
                row = conn.execute(
                    "SELECT s.*, a.account_id AS a_account_id, a.username AS a_username, "
                    "a.display_name AS a_display_name, a.role AS a_role, a.status AS a_status, "
                    "a.must_change_password AS a_must_change_password, "
                    "a.auth_revision AS a_auth_revision, a.created_at AS a_created_at, "
                    "a.updated_at AS a_updated_at, a.password_changed_at AS a_password_changed_at, "
                    "a.disabled_at AS a_disabled_at "
                    "FROM sessions s JOIN accounts a ON a.account_id=s.account_id "
                    "WHERE s.refresh_token_digest=?",
                    (token_digest,),
                ).fetchone()
                verified = self._verified_session_from_join(row, timestamp, refresh=True)
                if verified is None:
                    conn.execute("COMMIT")
                    return None
                access_token, refresh_token = self._new_tokens()
                access_expiry = timestamp + int(access_ttl_seconds)
                refresh_expiry = timestamp + int(refresh_ttl_seconds)
                conn.execute(
                    "INSERT INTO refresh_token_history(token_digest, account_id, session_id, expires_at, used_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (token_digest, verified.account.account_id, verified.session_id,
                     int(row["refresh_expires_at"]), timestamp),
                )
                conn.execute(
                    "UPDATE sessions SET access_token_digest=?, refresh_token_digest=?, "
                    "access_expires_at=?, refresh_expires_at=?, last_used_at=? WHERE session_id=?",
                    (
                        self._digest(access_token), self._digest(refresh_token), access_expiry,
                        refresh_expiry, timestamp, verified.session_id,
                    ),
                )
                conn.execute("COMMIT")
                return LocalSession(
                    session_id=verified.session_id, account=verified.account,
                    access_token=access_token, refresh_token=refresh_token,
                    access_expires_at=access_expiry, refresh_expires_at=refresh_expiry,
                )
        except (sqlite3.Error, OSError) as exc:
            raise LocalUserStoreUnavailable("local user store is unavailable") from exc

    def revoke_refresh_token(self, token: str, *, now: int | None = None) -> bool:
        """Best-effort revoke of one recognized, current refresh credential."""
        if not self._recognizes(token, _REFRESH_PREFIX):
            return False
        timestamp = _now(now)
        self._ensure_ready()
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                result = conn.execute(
                    "UPDATE sessions SET revoked_at=?, revoked_reason='logout' "
                    "WHERE refresh_token_digest=? AND revoked_at IS NULL",
                    (timestamp, self._digest(token)),
                )
                conn.execute("COMMIT")
                return result.rowcount == 1
        except (sqlite3.Error, OSError) as exc:
            raise LocalUserStoreUnavailable("local user store is unavailable") from exc

    # -- storage initialization -------------------------------------------

    def _ensure_ready(self) -> None:
        if self._initialized:
            self._validate_paths()
            return
        with self._init_lock:
            if self._initialized:
                self._validate_paths()
                return
            try:
                self.control_home.mkdir(parents=True, exist_ok=True, mode=0o700)
                self._validate_control_home()
                if not self.path.exists():
                    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                    flags |= getattr(os, "O_NOFOLLOW", 0)
                    try:
                        fd = os.open(self.path, flags, 0o600)
                    except FileExistsError:
                        pass
                    else:
                        os.close(fd)
                self._validate_paths()
                with self._connect() as conn:
                    conn.execute("PRAGMA foreign_keys=ON")
                    self._migrate_schema(conn)
                    row = conn.execute(
                        "SELECT value FROM local_user_meta WHERE key='schema_version'"
                    ).fetchone()
                    if row is None or int(row[0]) != _SCHEMA_VERSION:
                        raise LocalUserStoreUnavailable("local user store has unsupported schema")
                    integrity = conn.execute("PRAGMA integrity_check").fetchone()
                    if integrity is None or str(integrity[0]).lower() != "ok":
                        raise LocalUserStoreUnavailable("local user store integrity check failed")
                if os.name != "nt":
                    self.path.chmod(stat.S_IRUSR | stat.S_IWUSR)
                self._validate_paths()
                self._initialized = True
            except LocalUserStoreUnavailable:
                raise
            except (sqlite3.Error, OSError) as exc:
                raise LocalUserStoreUnavailable("local user store is unavailable") from exc

    @staticmethod
    def _migrate_schema(conn: sqlite3.Connection) -> None:
        """Create v1 or upgrade a v1 authority to v2 atomically."""
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS local_user_meta ("
                "key TEXT PRIMARY KEY, value INTEGER NOT NULL)"
            )
            row = conn.execute(
                "SELECT value FROM local_user_meta WHERE key='schema_version'"
            ).fetchone()
            if row is None:
                version = 1
                conn.execute(
                    "INSERT INTO local_user_meta(key, value) VALUES ('schema_version', 1)"
                )
            else:
                version = int(row[0])
            if version == 1:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS accounts ("
                    "account_id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE, "
                    "display_name TEXT NOT NULL, password_hash TEXT NOT NULL, "
                    "status TEXT NOT NULL CHECK(status IN ('active','disabled','pending_reset')), "
                    "auth_revision INTEGER NOT NULL, created_at INTEGER NOT NULL, "
                    "updated_at INTEGER NOT NULL, password_changed_at INTEGER NOT NULL, "
                    "disabled_at INTEGER)"
                )
                columns = {
                    str(column[1]) for column in conn.execute("PRAGMA table_info(accounts)")
                }
                if "role" not in columns:
                    conn.execute(
                        "ALTER TABLE accounts ADD COLUMN role TEXT NOT NULL "
                        "DEFAULT 'member' CHECK(role IN ('admin','member'))"
                    )
                if "must_change_password" not in columns:
                    conn.execute(
                        "ALTER TABLE accounts ADD COLUMN must_change_password "
                        "INTEGER NOT NULL DEFAULT 0 CHECK(must_change_password IN (0,1))"
                    )
                # v1 accounts deliberately retain member authority. Operators
                # must explicitly elevate an administrator after upgrading. A
                # legacy pending-reset account cannot complete its reset while
                # blocked at login, so migrate it to the active, forced-change
                # representation without touching credentials or revisions.
                conn.execute("UPDATE accounts SET role='member'")
                conn.execute(
                    "UPDATE accounts SET must_change_password=0 "
                    "WHERE status IN ('active', 'disabled')"
                )
                conn.execute(
                    "UPDATE accounts SET status='active', must_change_password=1 "
                    "WHERE status='pending_reset'"
                )
                conn.execute(
                    "UPDATE local_user_meta SET value=? WHERE key='schema_version'",
                    (_SCHEMA_VERSION,),
                )
                version = _SCHEMA_VERSION
            if version != _SCHEMA_VERSION:
                raise LocalUserStoreUnavailable("local user store has unsupported schema")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS sessions ("
                "session_id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(account_id), "
                "access_token_digest TEXT NOT NULL UNIQUE, refresh_token_digest TEXT NOT NULL UNIQUE, "
                "access_expires_at INTEGER NOT NULL, refresh_expires_at INTEGER NOT NULL, "
                "auth_revision_at_issue INTEGER NOT NULL, created_at INTEGER NOT NULL, "
                "last_used_at INTEGER NOT NULL, revoked_at INTEGER, revoked_reason TEXT)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS sessions_account_idx ON sessions(account_id)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS refresh_token_history ("
                "token_digest TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(account_id), "
                "session_id TEXT NOT NULL, expires_at INTEGER NOT NULL, used_at INTEGER NOT NULL)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS refresh_history_expiry_idx "
                "ON refresh_token_history(expires_at)"
            )
            conn.execute("COMMIT")
        except LocalUserStoreUnavailable:
            conn.execute("ROLLBACK")
            raise
        except sqlite3.Error:
            conn.execute("ROLLBACK")
            raise

    def _validate_control_home(self) -> None:
        try:
            status = self.control_home.lstat()
            if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
                raise LocalUserStoreUnavailable("control plane home must be a directory")
            if os.name != "nt" and status.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                raise LocalUserStoreUnavailable("control plane home has unsafe permissions")
        except LocalUserStoreUnavailable:
            raise
        except OSError as exc:
            raise LocalUserStoreUnavailable("control plane home cannot be inspected") from exc

    def _validate_paths(self) -> None:
        self._validate_control_home()
        try:
            status = self.path.lstat()
            if stat.S_ISLNK(status.st_mode) or not stat.S_ISREG(status.st_mode):
                raise LocalUserStoreUnavailable(
                    "local user store must not be a symlink or non-regular file"
                )
            if os.name != "nt" and status.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
                raise LocalUserStoreUnavailable("local user store has unsafe permissions")
        except LocalUserStoreUnavailable:
            raise
        except OSError as exc:
            raise LocalUserStoreUnavailable("local user store cannot be inspected") from exc

    def _connect(self) -> sqlite3.Connection:
        try:
            conn = sqlite3.connect(self.path, timeout=5, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            return conn
        except sqlite3.Error as exc:
            raise LocalUserStoreUnavailable("local user store cannot be opened") from exc

    # -- internals ---------------------------------------------------------

    def _prepare_accounts(
        self, accounts: Iterable[tuple[str, str, str] | tuple[str, str]]
    ) -> list[tuple[str, str, str]]:
        result: list[tuple[str, str, str]] = []
        seen: set[str] = set()
        for item in accounts:
            if len(item) == 2:
                username, password = item
                display_name = ""
            elif len(item) == 3:
                username, password, display_name = item
            else:
                raise ValueError("each account must contain username, password, and optional display name")
            canonical = normalize_username(username)
            if canonical in seen:
                raise ValueError("bootstrap usernames must be unique")
            seen.add(canonical)
            result.append((canonical, hash_password(password), _display_name(display_name, canonical)))
        return result

    def _update_account_auth(
        self,
        username: str,
        *,
        password_hash: str | None = None,
        status: AccountStatus | None = None,
        must_change_password: bool | None = None,
        now: int,
    ) -> LocalAccount:
        self._ensure_ready()
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT * FROM accounts WHERE username=?", (username,)
                ).fetchone()
                if row is None:
                    raise LocalUserStoreConflict("local account does not exist")
                old = self._account_from_row(row)
                next_status = status if status is not None else old.status
                next_hash = password_hash if password_hash is not None else str(row["password_hash"])
                next_must_change_password = (
                    must_change_password
                    if must_change_password is not None
                    else old.must_change_password
                )
                if next_status not in ("active", "disabled"):
                    raise ValueError("invalid account status")
                if next_status == "disabled" and next_must_change_password:
                    raise ValueError("disabled account cannot require a password change")
                if old.role == "admin" and old.status == "active" and next_status != "active":
                    active_admins = int(conn.execute(
                        "SELECT COUNT(*) FROM accounts WHERE role='admin' AND status='active'"
                    ).fetchone()[0])
                    if active_admins <= 1:
                        raise LocalUserStoreConflict("cannot disable the last active admin")
                disabled_at = now if next_status == "disabled" else None
                conn.execute(
                    "UPDATE accounts SET password_hash=?, status=?, must_change_password=?, "
                    "auth_revision=?, updated_at=?, password_changed_at=?, disabled_at=? "
                    "WHERE account_id=?",
                    (
                        next_hash, next_status, int(next_must_change_password),
                        old.auth_revision + 1, now,
                        now if password_hash is not None else old.password_changed_at,
                        disabled_at, old.account_id,
                    ),
                )
                self._revoke_sessions_in_transaction(conn, old.account_id, now, "account_auth_changed")
                updated = conn.execute(
                    "SELECT * FROM accounts WHERE account_id=?", (old.account_id,)
                ).fetchone()
                conn.execute("COMMIT")
                return self._account_from_row(updated)
        except LocalUserStoreConflict:
            raise
        except (sqlite3.Error, OSError) as exc:
            raise LocalUserStoreUnavailable("local user store is unavailable") from exc

    @staticmethod
    def _revoke_sessions_in_transaction(
        conn: sqlite3.Connection, account_id: str, now: int, reason: str
    ) -> None:
        conn.execute(
            "UPDATE sessions SET revoked_at=COALESCE(revoked_at, ?), "
            "revoked_reason=COALESCE(revoked_reason, ?) WHERE account_id=?",
            (now, reason, account_id),
        )

    def _revoke_account_in_transaction(
        self, conn: sqlite3.Connection, account_id: str, now: int, reason: str
    ) -> None:
        conn.execute(
            "UPDATE accounts SET auth_revision=auth_revision+1, updated_at=? "
            "WHERE account_id=?",
            (now, account_id),
        )
        self._revoke_sessions_in_transaction(conn, account_id, now, reason)

    def _digest(self, token: str) -> str:
        return hmac.new(
            self._digest_key, token.encode("ascii"), hashlib.sha256
        ).hexdigest()

    @staticmethod
    def _recognizes(token: str, prefix: str) -> bool:
        # A bounded ASCII alphabet makes malformed foreign credentials cheap to
        # reject before any persistent-store lookup.
        if not isinstance(token, str) or not token.startswith(prefix):
            return False
        body = token[len(prefix):]
        return 32 <= len(body) <= 128 and all(ch.isascii() and (ch.isalnum() or ch in "-_") for ch in body)

    @staticmethod
    def _new_tokens() -> tuple[str, str]:
        return (
            _ACCESS_PREFIX + secrets.token_urlsafe(32),
            _REFRESH_PREFIX + secrets.token_urlsafe(32),
        )

    @staticmethod
    def _account_from_row(row: sqlite3.Row) -> LocalAccount:
        prefix = "a_" if "a_account_id" in row.keys() else ""
        try:
            role = str(row[f"{prefix}role"])
            status = str(row[f"{prefix}status"])
            must_change_password = int(row[f"{prefix}must_change_password"])
            if role not in ("admin", "member"):
                raise ValueError("invalid account role")
            if status not in ("active", "disabled", "pending_reset"):
                raise ValueError("invalid account status")
            if must_change_password not in (0, 1):
                raise ValueError("invalid must_change_password")
            return LocalAccount(
                account_id=str(row[f"{prefix}account_id"]),
                username=str(row[f"{prefix}username"]),
                display_name=str(row[f"{prefix}display_name"]),
                role=role,  # type: ignore[arg-type]
                status=status,  # type: ignore[arg-type]
                must_change_password=bool(must_change_password),
                auth_revision=int(row[f"{prefix}auth_revision"]),
                created_at=int(row[f"{prefix}created_at"]),
                updated_at=int(row[f"{prefix}updated_at"]),
                password_changed_at=int(row[f"{prefix}password_changed_at"]),
                disabled_at=(
                    int(row[f"{prefix}disabled_at"])
                    if row[f"{prefix}disabled_at"] is not None
                    else None
                ),
            )
        except (IndexError, KeyError, TypeError, ValueError) as exc:
            raise LocalUserStoreUnavailable("local account record is invalid") from exc

    def _verified_session_from_join(
        self, row: sqlite3.Row | None, now: int, *, refresh: bool = False
    ) -> VerifiedLocalSession | None:
        if row is None:
            return None
        try:
            account = self._account_from_row(row)
            expiry_key = "refresh_expires_at" if refresh else "access_expires_at"
            expiry = int(row[expiry_key])
            if (
                row["revoked_at"] is not None
                or expiry <= now
                or account.status != "active"
                or int(row["auth_revision_at_issue"]) != account.auth_revision
            ):
                return None
            return VerifiedLocalSession(
                session_id=str(row["session_id"]), account=account,
                access_expires_at=int(row["access_expires_at"]),
                refresh_expires_at=int(row["refresh_expires_at"]),
            )
        except (IndexError, KeyError, TypeError, ValueError) as exc:
            raise LocalUserStoreUnavailable("local session record is invalid") from exc


def _display_name(display_name: str, username: str) -> str:
    result = str(display_name or "").strip() or username
    if len(result) > 256 or any(ord(char) < 32 for char in result):
        raise ValueError("display_name is invalid")
    return result


def _new_id() -> str:
    return str(uuid.uuid4())


def _now(value: int | None) -> int:
    if value is None:
        return int(time.time())
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("now must be a non-negative integer")
    return value


# Computed once so missing-user verification retains the same scrypt work factor.
_DUMMY_PASSWORD_HASH = hash_password("hermes-local-users-dummy-password")
