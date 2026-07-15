"""Control-Plane-only authorization epoch and credential replay authority.

The browser ticket signer deliberately does not own replay state.  This store is
located below the Control Plane global home and is never reachable from an
Owner Worker.  SQLite provides atomicity for a single host or a correctly shared
control volume; callers must not substitute a process-local fallback when this
store cannot be reached.
"""
from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
import stat
import threading
import time
from enum import StrEnum

from hermes_constants import get_hermes_home
from dataclasses import dataclass
from pathlib import Path


_SCHEMA_VERSION = 5
_DB_NAME = "authority.sqlite3"


class WorkerLeaseState(StrEnum):
    """Fenced admission state for the one active generation per owner."""

    STARTING = "starting"
    ACTIVE = "active"
    DRAINING = "draining"
    REVOKED = "revoked"


class WorkerGenerationState(StrEnum):
    """Durable lifecycle states for an Owner Worker generation."""

    STARTING = "starting"
    ACTIVE = "active"
    DRAINING = "draining"
    REVOKED = "revoked"
    TERMINATED = "terminated"
    FAILED = "failed"


_WORKER_GENERATION_TRANSITIONS: dict[WorkerGenerationState, frozenset[WorkerGenerationState]] = {
    WorkerGenerationState.STARTING: frozenset({WorkerGenerationState.ACTIVE, WorkerGenerationState.REVOKED, WorkerGenerationState.FAILED}),
    WorkerGenerationState.ACTIVE: frozenset({WorkerGenerationState.DRAINING, WorkerGenerationState.REVOKED, WorkerGenerationState.FAILED}),
    WorkerGenerationState.DRAINING: frozenset({WorkerGenerationState.REVOKED, WorkerGenerationState.TERMINATED, WorkerGenerationState.FAILED}),
    WorkerGenerationState.REVOKED: frozenset({WorkerGenerationState.TERMINATED}),
    WorkerGenerationState.TERMINATED: frozenset(),
    WorkerGenerationState.FAILED: frozenset(),
}

# A lease is an admission fence, not generation history. Terminal generation
# records therefore release their current lease by moving it to ``revoked``.
# Keep the paired transition table narrow so callers cannot, for example,
# mark a live generation terminated without first fencing it as draining.
_WORKER_LEASE_TRANSITIONS: dict[
    WorkerLeaseState, frozenset[tuple[WorkerLeaseState, WorkerGenerationState]]
] = {
    WorkerLeaseState.STARTING: frozenset({
        (WorkerLeaseState.ACTIVE, WorkerGenerationState.ACTIVE),
        (WorkerLeaseState.REVOKED, WorkerGenerationState.REVOKED),
        (WorkerLeaseState.REVOKED, WorkerGenerationState.FAILED),
    }),
    WorkerLeaseState.ACTIVE: frozenset({
        (WorkerLeaseState.DRAINING, WorkerGenerationState.DRAINING),
        (WorkerLeaseState.REVOKED, WorkerGenerationState.REVOKED),
        (WorkerLeaseState.REVOKED, WorkerGenerationState.FAILED),
    }),
    WorkerLeaseState.DRAINING: frozenset({
        (WorkerLeaseState.REVOKED, WorkerGenerationState.TERMINATED),
        (WorkerLeaseState.REVOKED, WorkerGenerationState.REVOKED),
        (WorkerLeaseState.REVOKED, WorkerGenerationState.FAILED),
    }),
    WorkerLeaseState.REVOKED: frozenset(),
}


def control_plane_home() -> Path:
    """Return the authoritative Control Plane directory without owner fallback."""
    worker_owner = str(os.environ.get("HERMES_OWNER_KEY") or "").strip()
    explicit_home = str(os.environ.get("HERMES_CONTROL_HOME") or "").strip()
    if worker_owner and not explicit_home:
        raise AuthorityUnavailable("control plane home is required in owner workers")
    home = Path(explicit_home) if explicit_home else get_hermes_home() / "control-plane"
    try:
        if home.exists() or home.is_symlink():
            status = home.lstat()
            if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
                raise AuthorityUnavailable("control plane home must be a directory")
            if os.name != "nt" and status.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                raise AuthorityUnavailable("control plane home has unsafe permissions")
    except AuthorityUnavailable:
        raise
    except OSError as exc:
        raise AuthorityUnavailable("control plane home cannot be inspected") from exc
    return home


class AuthorityUnavailable(RuntimeError):
    """Authority storage is unavailable or unsafe; callers must fail closed."""


class AuthorizationRejected(RuntimeError):
    """A credential or authorization scope did not pass the authority check."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class AuthorizationScope:
    """Trusted provider authorization state; no raw credentials are retained."""

    provider: str
    tenant_id: str
    user_id: str
    session_id: str
    membership_revision: str

    def __post_init__(self) -> None:
        for name in ("provider", "tenant_id", "user_id", "session_id", "membership_revision"):
            if not str(getattr(self, name) or "").strip():
                raise ValueError(f"{name} is required for authorization scope")

    @property
    def digest(self) -> str:
        material = "\x1f".join((
            self.provider,
            self.tenant_id,
            self.user_id,
            self.session_id,
        )).encode("utf-8")
        return hashlib.sha256(material).hexdigest()

    @property
    def principal_digest(self) -> str:
        # A verified user changing tenants must revoke the prior tenant scope;
        # the tenant remains in ``digest`` for strict scope isolation, while
        # this subject-level digest identifies mutually exclusive active
        # dashboard sessions for the provider/user pair.
        material = "\x1f".join((self.provider, self.user_id)).encode("utf-8")
        return hashlib.sha256(material).hexdigest()


@dataclass(frozen=True)
class AuthorityChange:
    """A de-identified authority transition visible to every Control Plane."""

    sequence: int
    scope_digest: str
    epoch: int
    revoked: bool


@dataclass(frozen=True)
class AuthorizationState:
    epoch: int
    recovery_generation: int
    # Scope digests revoked by the activation transaction. These are safe to
    # hand to the local bridge registry: they contain no raw principal/session
    # material and let the Control Plane terminate already-admitted sockets.
    revoked_scope_digests: tuple[str, ...] = ()
    # The complete de-identified transition records let local and remote Control
    # Plan bridge registries distinguish an old epoch from a newly admitted one.
    changes: tuple[AuthorityChange, ...] = ()


@dataclass(frozen=True)
class ConsumeDecision:
    accepted: bool
    epoch: int
    recovery_generation: int


@dataclass(frozen=True)
class ReplayContinuity:
    """Independent witness required before browser credentials are usable."""

    authority_id: str
    recovery_generation: int
    ready: bool


@dataclass(frozen=True)
class WorkerLeaseChange:
    """A de-identified exact Worker lifecycle transition for bridge cleanup."""

    sequence: int
    owner_key: str
    worker_generation: int
    worker_id: str
    lease_version: int
    recovery_generation: int
    lease_state: WorkerLeaseState
    generation_state: WorkerGenerationState


@dataclass(frozen=True)
class WorkerGeneration:
    """A durable, owner-scoped worker identity; PID is deliberately absent."""

    owner_key: str
    worker_generation: int
    worker_id: str
    state: WorkerGenerationState
    recovery_generation: int


@dataclass(frozen=True)
class OwnerWorkerAuthorityLease:
    """Durable fencing identity for the currently admitted owner worker."""

    owner_key: str
    worker_generation: int
    worker_id: str
    state: WorkerLeaseState
    lease_version: int
    recovery_generation: int


@dataclass(frozen=True)
class WorkerStartClaim:
    generation: WorkerGeneration
    lease: OwnerWorkerAuthorityLease


@dataclass(frozen=True)
class OwnerWorkerBootstrapConsumeDecision:
    """Result of atomically admitting one exact Worker bootstrap."""

    accepted: bool
    lease: OwnerWorkerAuthorityLease


class AuthorityStore:
    """SQLite-backed authorization/replay authority rooted in control home."""

    def __init__(self, control_home: str | Path | None = None, *, db_name: str = _DB_NAME):
        self.control_home = Path(control_home) if control_home is not None else control_plane_home()
        self.path = self.control_home / db_name
        self._init_lock = threading.Lock()
        self._initialized = False

    def _ensure_ready(self) -> None:
        if self._initialized:
            self._validate_path()
            return
        with self._init_lock:
            if self._initialized:
                self._validate_path()
                return
            try:
                self.control_home.mkdir(parents=True, exist_ok=True)
                control_stat = self.control_home.lstat()
                if stat.S_ISLNK(control_stat.st_mode) or not stat.S_ISDIR(control_stat.st_mode):
                    raise AuthorityUnavailable(f"control home must be a directory: {self.control_home}")
                if os.name != "nt" and control_stat.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                    raise AuthorityUnavailable(f"control home has unsafe permissions: {self.control_home}")
                if not self.path.exists():
                    try:
                        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    except FileExistsError:
                        pass
                    else:
                        os.close(fd)
                self._validate_path()
                with self._connect() as conn:
                    conn.execute("PRAGMA foreign_keys=ON")
                    conn.execute(
                        "CREATE TABLE IF NOT EXISTS authority_meta (key TEXT PRIMARY KEY, value INTEGER NOT NULL)"
                    )
                    conn.execute(
                        "CREATE TABLE IF NOT EXISTS authorization_scopes ("
                        "scope_digest TEXT PRIMARY KEY, principal_digest TEXT NOT NULL, "
                        "membership_revision TEXT NOT NULL, epoch INTEGER NOT NULL, revoked INTEGER NOT NULL DEFAULT 0)"
                    )
                    conn.execute(
                        "CREATE TABLE IF NOT EXISTS consumed_credentials ("
                        "token_class TEXT NOT NULL, issuer_key_version TEXT NOT NULL, credential_digest TEXT NOT NULL, "
                        "audience TEXT NOT NULL, expires_at INTEGER NOT NULL, "
                        "PRIMARY KEY(token_class, issuer_key_version, credential_digest, audience))"
                    )
                    conn.execute(
                        "CREATE TABLE IF NOT EXISTS authority_changes ("
                        "sequence INTEGER PRIMARY KEY AUTOINCREMENT, scope_digest TEXT NOT NULL, "
                        "epoch INTEGER NOT NULL, revoked INTEGER NOT NULL)"
                    )
                    self._migrate_schema(conn)
                    conn.execute(
                        "INSERT OR IGNORE INTO authority_meta(key, value) VALUES ('schema_version', ?)",
                        (_SCHEMA_VERSION,),
                    )
                    conn.execute(
                        "INSERT OR IGNORE INTO authority_meta(key, value) VALUES ('recovery_generation', 0)"
                    )
                    conn.execute(
                        "INSERT OR IGNORE INTO authority_meta(key, value) VALUES ('recovery_required', 0)"
                    )
                    conn.execute(
                        "INSERT OR IGNORE INTO authority_meta(key, value) VALUES ('keyring_bound', 0)"
                    )
                    conn.execute(
                        "INSERT OR IGNORE INTO authority_meta(key, value) VALUES ('authority_id', ?)",
                        (secrets.token_urlsafe(24),),
                    )
                    row = conn.execute("SELECT value FROM authority_meta WHERE key='schema_version'").fetchone()
                    if row is None or int(row[0]) != _SCHEMA_VERSION:
                        raise AuthorityUnavailable("authority store has unsupported schema")
                if os.name != "nt":
                    self.path.chmod(stat.S_IRUSR | stat.S_IWUSR)
                self._validate_path()
                self._initialized = True
            except AuthorityUnavailable:
                raise
            except (OSError, sqlite3.Error) as exc:
                raise AuthorityUnavailable("authority store is unavailable") from exc

    @staticmethod
    def _migrate_schema(conn: sqlite3.Connection) -> None:
        """Apply the supported authority schema upgrade under initialization lock."""
        row = conn.execute("SELECT value FROM authority_meta WHERE key='schema_version'").fetchone()
        version = 1 if row is None else int(row[0])
        if version > _SCHEMA_VERSION or version < 1:
            raise AuthorityUnavailable("authority store has unsupported schema")
        if version == 1:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS owner_worker_generations ("
                "owner_key TEXT NOT NULL, worker_generation INTEGER NOT NULL, worker_id TEXT NOT NULL UNIQUE, "
                "state TEXT NOT NULL, recovery_generation INTEGER NOT NULL, "
                "PRIMARY KEY(owner_key, worker_generation))"
            )
            version = 2
            conn.execute("UPDATE authority_meta SET value=? WHERE key='schema_version'", (version,))
        if version == 2:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS owner_worker_leases ("
                "owner_key TEXT PRIMARY KEY, worker_generation INTEGER NOT NULL, worker_id TEXT NOT NULL, "
                "state TEXT NOT NULL, lease_version INTEGER NOT NULL, recovery_generation INTEGER NOT NULL)"
            )
            version = 3
            conn.execute("UPDATE authority_meta SET value=? WHERE key='schema_version'", (version,))
        if version == 3:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS owner_worker_bootstrap_consumptions ("
                "issuer_key_version TEXT NOT NULL, credential_digest TEXT NOT NULL, "
                "audience TEXT NOT NULL, expires_at INTEGER NOT NULL, "
                "PRIMARY KEY(issuer_key_version, credential_digest, audience))"
            )
            version = 4
            conn.execute("UPDATE authority_meta SET value=? WHERE key='schema_version'", (version,))
        if version == 4:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS owner_worker_changes ("
                "sequence INTEGER PRIMARY KEY AUTOINCREMENT, owner_key TEXT NOT NULL, "
                "worker_generation INTEGER NOT NULL, worker_id TEXT NOT NULL, "
                "lease_version INTEGER NOT NULL, recovery_generation INTEGER NOT NULL, "
                "lease_state TEXT NOT NULL, generation_state TEXT NOT NULL)"
            )
            version = 5
            conn.execute("UPDATE authority_meta SET value=? WHERE key='schema_version'", (version,))
        if version == _SCHEMA_VERSION:
            generations = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='owner_worker_generations'"
            ).fetchone()
            leases = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='owner_worker_leases'"
            ).fetchone()
            bootstraps = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='owner_worker_bootstrap_consumptions'"
            ).fetchone()
            worker_changes = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='owner_worker_changes'"
            ).fetchone()
            if generations is None or leases is None or bootstraps is None or worker_changes is None:
                raise AuthorityUnavailable("authority store schema is incomplete")

    def _validate_path(self) -> None:
        try:
            if self.path.exists() or self.path.is_symlink():
                path_stat = self.path.lstat()
                if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISREG(path_stat.st_mode):
                    raise AuthorityUnavailable(f"authority store must not be a symlink or non-regular file: {self.path}")
                if os.name != "nt" and path_stat.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
                    raise AuthorityUnavailable(f"authority store has unsafe permissions: {self.path}")
        except AuthorityUnavailable:
            raise
        except OSError as exc:
            raise AuthorityUnavailable("authority store cannot be inspected") from exc

    def _connect(self) -> sqlite3.Connection:
        try:
            return sqlite3.connect(self.path, timeout=5, isolation_level=None)
        except sqlite3.Error as exc:
            raise AuthorityUnavailable("authority store cannot be opened") from exc

    @staticmethod
    def _credential_digest(jti: str) -> str:
        if not str(jti or "").strip():
            raise ValueError("jti is required")
        return hashlib.sha256(jti.encode("utf-8")).hexdigest()

    @staticmethod
    def _recovery_generation(conn: sqlite3.Connection) -> int:
        row = conn.execute("SELECT value FROM authority_meta WHERE key='recovery_generation'").fetchone()
        if row is None:
            raise AuthorityUnavailable("authority recovery generation is missing")
        return int(row[0])

    @staticmethod
    def _worker_generation_from_row(row: tuple[object, ...]) -> WorkerGeneration:
        try:
            return WorkerGeneration(
                owner_key=str(row[0]),
                worker_generation=int(row[1]),
                worker_id=str(row[2]),
                state=WorkerGenerationState(str(row[3])),
                recovery_generation=int(row[4]),
            )
        except (IndexError, TypeError, ValueError) as exc:
            raise AuthorityUnavailable("owner worker generation record is invalid") from exc

    @staticmethod
    def _worker_lease_from_row(row: tuple[object, ...]) -> OwnerWorkerAuthorityLease:
        try:
            return OwnerWorkerAuthorityLease(
                owner_key=str(row[0]),
                worker_generation=int(row[1]),
                worker_id=str(row[2]),
                state=WorkerLeaseState(str(row[3])),
                lease_version=int(row[4]),
                recovery_generation=int(row[5]),
            )
        except (IndexError, TypeError, ValueError) as exc:
            raise AuthorityUnavailable("owner worker lease record is invalid") from exc

    @staticmethod
    def _require_worker_owner_key(owner_key: str) -> str:
        value = str(owner_key or "").strip()
        if not value:
            raise ValueError("owner_key is required")
        return value

    def claim_worker_start(self, owner_key: str, *, worker_id: str | None = None) -> WorkerStartClaim:
        """Atomically fence an owner and allocate its sole starting generation."""
        owner_key = self._require_worker_owner_key(owner_key)
        instance_id = str(worker_id or secrets.token_urlsafe(18)).strip()
        if not instance_id:
            raise ValueError("worker_id is required")
        self._ensure_ready()
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                recovery_generation = self._recovery_generation(conn)
                existing_row = conn.execute(
                    "SELECT owner_key, worker_generation, worker_id, state, lease_version, recovery_generation "
                    "FROM owner_worker_leases WHERE owner_key=?",
                    (owner_key,),
                ).fetchone()
                if existing_row is not None:
                    existing = self._worker_lease_from_row(existing_row)
                    if existing.recovery_generation == recovery_generation and existing.state in {
                        WorkerLeaseState.STARTING, WorkerLeaseState.ACTIVE, WorkerLeaseState.DRAINING,
                    }:
                        raise AuthorizationRejected("worker_lease_already_owned")
                    next_lease_version = existing.lease_version + 1
                else:
                    next_lease_version = 1
                row = conn.execute(
                    "SELECT COALESCE(MAX(worker_generation), 0) FROM owner_worker_generations WHERE owner_key=?",
                    (owner_key,),
                ).fetchone()
                generation = int(row[0]) + 1 if row is not None else 1
                conn.execute(
                    "INSERT INTO owner_worker_generations("
                    "owner_key, worker_generation, worker_id, state, recovery_generation) VALUES (?, ?, ?, ?, ?)",
                    (owner_key, generation, instance_id, WorkerGenerationState.STARTING.value, recovery_generation),
                )
                conn.execute(
                    "INSERT INTO owner_worker_leases("
                    "owner_key, worker_generation, worker_id, state, lease_version, recovery_generation) VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(owner_key) DO UPDATE SET worker_generation=excluded.worker_generation, "
                    "worker_id=excluded.worker_id, state=excluded.state, lease_version=excluded.lease_version, "
                    "recovery_generation=excluded.recovery_generation",
                    (owner_key, generation, instance_id, WorkerLeaseState.STARTING.value, next_lease_version, recovery_generation),
                )
                conn.commit()
                return WorkerStartClaim(
                    WorkerGeneration(owner_key, generation, instance_id, WorkerGenerationState.STARTING, recovery_generation),
                    OwnerWorkerAuthorityLease(owner_key, generation, instance_id, WorkerLeaseState.STARTING, next_lease_version, recovery_generation),
                )
        except AuthorizationRejected:
            raise
        except sqlite3.IntegrityError as exc:
            raise AuthorizationRejected("worker_generation_conflict") from exc
        except (sqlite3.Error, OSError) as exc:
            raise AuthorityUnavailable("authority transaction failed") from exc

    def read_owner_worker_lease(self, owner_key: str) -> OwnerWorkerAuthorityLease | None:
        """Return the current owner fence, if any; it grants no admission alone."""
        owner_key = self._require_worker_owner_key(owner_key)
        self._ensure_ready()
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT owner_key, worker_generation, worker_id, state, lease_version, recovery_generation "
                    "FROM owner_worker_leases WHERE owner_key=?", (owner_key,)
                ).fetchone()
                return None if row is None else self._worker_lease_from_row(row)
        except (sqlite3.Error, OSError) as exc:
            raise AuthorityUnavailable("authority transaction failed") from exc

    def assert_worker_lease(
        self, lease: OwnerWorkerAuthorityLease, *, states: frozenset[WorkerLeaseState] | None = None
    ) -> OwnerWorkerAuthorityLease:
        """Require the exact durable fencing record before using a handle."""
        self._ensure_ready()
        try:
            with self._connect() as conn:
                current = self._recovery_generation(conn)
                row = conn.execute(
                    "SELECT owner_key, worker_generation, worker_id, state, lease_version, recovery_generation "
                    "FROM owner_worker_leases WHERE owner_key=?", (lease.owner_key,)
                ).fetchone()
                if row is None:
                    raise AuthorizationRejected("worker_lease_not_found")
                actual = self._worker_lease_from_row(row)
                if actual != lease or actual.recovery_generation != current:
                    raise AuthorizationRejected("worker_lease_stale")
                if states is not None and actual.state not in states:
                    raise AuthorizationRejected("worker_lease_state_mismatch")
                return actual
        except AuthorizationRejected:
            raise
        except (sqlite3.Error, OSError) as exc:
            raise AuthorityUnavailable("authority transaction failed") from exc

    def transition_worker_lease(
        self, lease: OwnerWorkerAuthorityLease, *, state: WorkerLeaseState, generation_state: WorkerGenerationState
    ) -> OwnerWorkerAuthorityLease:
        """CAS a fenced lease and its matching generation in one transaction."""
        self._ensure_ready()
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                current_recovery = self._recovery_generation(conn)
                row = conn.execute(
                    "SELECT owner_key, worker_generation, worker_id, state, lease_version, recovery_generation "
                    "FROM owner_worker_leases WHERE owner_key=?", (lease.owner_key,)
                ).fetchone()
                if row is None:
                    raise AuthorizationRejected("worker_lease_not_found")
                actual = self._worker_lease_from_row(row)
                if actual != lease or actual.recovery_generation != current_recovery:
                    raise AuthorizationRejected("worker_lease_stale")
                generation_row = conn.execute(
                    "SELECT owner_key, worker_generation, worker_id, state, recovery_generation "
                    "FROM owner_worker_generations WHERE owner_key=? AND worker_generation=?",
                    (lease.owner_key, lease.worker_generation),
                ).fetchone()
                if generation_row is None:
                    raise AuthorizationRejected("worker_generation_not_found")
                generation = self._worker_generation_from_row(generation_row)
                if generation.worker_id != lease.worker_id or generation.recovery_generation != current_recovery:
                    raise AuthorizationRejected("worker_lease_identity_mismatch")
                if (state, generation_state) not in _WORKER_LEASE_TRANSITIONS[actual.state]:
                    raise AuthorizationRejected("worker_lease_invalid_transition")
                if generation_state not in _WORKER_GENERATION_TRANSITIONS[generation.state]:
                    raise AuthorizationRejected("worker_generation_invalid_transition")
                conn.execute(
                    "UPDATE owner_worker_generations SET state=? WHERE owner_key=? AND worker_generation=?",
                    (generation_state.value, lease.owner_key, lease.worker_generation),
                )
                conn.execute(
                    "UPDATE owner_worker_leases SET state=? WHERE owner_key=? AND lease_version=?",
                    (state.value, lease.owner_key, lease.lease_version),
                )
                conn.execute(
                    "INSERT INTO owner_worker_changes("
                    "owner_key, worker_generation, worker_id, lease_version, recovery_generation, "
                    "lease_state, generation_state) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        lease.owner_key,
                        lease.worker_generation,
                        lease.worker_id,
                        lease.lease_version,
                        current_recovery,
                        state.value,
                        generation_state.value,
                    ),
                )
                conn.commit()
                return OwnerWorkerAuthorityLease(
                    lease.owner_key, lease.worker_generation, lease.worker_id, state,
                    lease.lease_version, current_recovery,
                )
        except AuthorizationRejected:
            raise
        except (sqlite3.Error, OSError) as exc:
            raise AuthorityUnavailable("authority transaction failed") from exc

    def allocate_worker_generation(self, owner_key: str, *, worker_id: str | None = None) -> WorkerGeneration:
        """Allocate the next durable ``starting`` generation for one owner."""
        owner_key = self._require_worker_owner_key(owner_key)
        instance_id = str(worker_id or secrets.token_urlsafe(18)).strip()
        if not instance_id:
            raise ValueError("worker_id is required")
        self._ensure_ready()
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                recovery_generation = self._recovery_generation(conn)
                row = conn.execute(
                    "SELECT COALESCE(MAX(worker_generation), 0) FROM owner_worker_generations WHERE owner_key=?",
                    (owner_key,),
                ).fetchone()
                next_generation = int(row[0]) + 1 if row is not None else 1
                conn.execute(
                    "INSERT INTO owner_worker_generations("
                    "owner_key, worker_generation, worker_id, state, recovery_generation) VALUES (?, ?, ?, ?, ?)",
                    (owner_key, next_generation, instance_id, WorkerGenerationState.STARTING.value, recovery_generation),
                )
                conn.commit()
                return WorkerGeneration(owner_key, next_generation, instance_id, WorkerGenerationState.STARTING, recovery_generation)
        except sqlite3.IntegrityError as exc:
            raise AuthorizationRejected("worker_generation_conflict") from exc
        except (sqlite3.Error, OSError) as exc:
            raise AuthorityUnavailable("authority transaction failed") from exc

    def read_worker_generation(self, owner_key: str, worker_generation: int) -> WorkerGeneration:
        """Read one exact owner generation without granting it any authority."""
        owner_key = self._require_worker_owner_key(owner_key)
        generation = int(worker_generation)
        if generation < 1:
            raise ValueError("worker_generation must be positive")
        self._ensure_ready()
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT owner_key, worker_generation, worker_id, state, recovery_generation "
                    "FROM owner_worker_generations WHERE owner_key=? AND worker_generation=?",
                    (owner_key, generation),
                ).fetchone()
                if row is None:
                    raise AuthorizationRejected("worker_generation_not_found")
                return self._worker_generation_from_row(row)
        except AuthorizationRejected:
            raise
        except (sqlite3.Error, OSError) as exc:
            raise AuthorityUnavailable("authority transaction failed") from exc

    def transition_worker_generation(
        self,
        owner_key: str,
        worker_generation: int,
        *,
        worker_id: str,
        state: WorkerGenerationState | str,
        expected_recovery_generation: int | None = None,
    ) -> WorkerGeneration:
        """Advance one exact generation through its irreversible lifecycle."""
        owner_key = self._require_worker_owner_key(owner_key)
        generation = int(worker_generation)
        if generation < 1 or not str(worker_id or "").strip():
            raise ValueError("worker_generation and worker_id are required")
        try:
            target = WorkerGenerationState(str(state))
        except ValueError as exc:
            raise ValueError("invalid worker generation state") from exc
        self._ensure_ready()
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT owner_key, worker_generation, worker_id, state, recovery_generation "
                    "FROM owner_worker_generations WHERE owner_key=? AND worker_generation=?",
                    (owner_key, generation),
                ).fetchone()
                if row is None:
                    raise AuthorizationRejected("worker_generation_not_found")
                current = self._worker_generation_from_row(row)
                if current.worker_id != worker_id:
                    raise AuthorizationRejected("worker_generation_identity_mismatch")
                recovery_generation = self._recovery_generation(conn)
                if current.recovery_generation != recovery_generation or (
                    expected_recovery_generation is not None and recovery_generation != int(expected_recovery_generation)
                ):
                    raise AuthorizationRejected("worker_generation_recovery_mismatch")
                if target not in _WORKER_GENERATION_TRANSITIONS[current.state]:
                    raise AuthorizationRejected("worker_generation_invalid_transition")
                conn.execute(
                    "UPDATE owner_worker_generations SET state=? WHERE owner_key=? AND worker_generation=?",
                    (target.value, owner_key, generation),
                )
                conn.commit()
                return WorkerGeneration(owner_key, generation, current.worker_id, target, recovery_generation)
        except AuthorizationRejected:
            raise
        except (sqlite3.Error, OSError) as exc:
            raise AuthorityUnavailable("authority transaction failed") from exc

    @staticmethod
    def _continuity_in_transaction(conn: sqlite3.Connection) -> ReplayContinuity:
        rows = {
            str(key): value
            for key, value in conn.execute(
                "SELECT key, value FROM authority_meta "
                "WHERE key IN ('authority_id', 'recovery_generation', 'recovery_required', 'keyring_bound')"
            ).fetchall()
        }
        try:
            authority_id = str(rows["authority_id"])
            generation = int(rows["recovery_generation"])
            recovery_required = bool(int(rows["recovery_required"]))
            keyring_bound = bool(int(rows["keyring_bound"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise AuthorityUnavailable("authority replay continuity is incomplete") from exc
        if not authority_id:
            raise AuthorityUnavailable("authority replay continuity is incomplete")
        del keyring_bound
        return ReplayContinuity(authority_id, generation, not recovery_required)

    @staticmethod
    def _keyring_is_bound(conn: sqlite3.Connection) -> bool:
        row = conn.execute("SELECT value FROM authority_meta WHERE key='keyring_bound'").fetchone()
        if row is None:
            raise AuthorityUnavailable("authority replay continuity is incomplete")
        return bool(int(row[0]))

    def keyring_is_bound(self) -> bool:
        self._ensure_ready()
        try:
            with self._connect() as conn:
                return self._keyring_is_bound(conn)
        except (sqlite3.Error, OSError) as exc:
            raise AuthorityUnavailable("authority transaction failed") from exc

    def replay_continuity(self) -> ReplayContinuity:
        """Return the persisted replay witness without authorizing a ticket."""
        self._ensure_ready()
        try:
            with self._connect() as conn:
                return self._continuity_in_transaction(conn)
        except (sqlite3.Error, OSError) as exc:
            raise AuthorityUnavailable("authority transaction failed") from exc

    def bind_replay_continuity(self, witness: ReplayContinuity) -> ReplayContinuity:
        """Bind a newly created keyring witness to this authority exactly once."""
        self._ensure_ready()
        if not witness.ready or not witness.authority_id:
            raise AuthorityUnavailable("authority replay continuity is unavailable")
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                current = self._continuity_in_transaction(conn)
                if current.authority_id != witness.authority_id or current.recovery_generation != witness.recovery_generation:
                    raise AuthorityUnavailable("authority replay continuity mismatch")
                if not self._keyring_is_bound(conn):
                    conn.execute("UPDATE authority_meta SET value=1 WHERE key='keyring_bound'")
                conn.commit()
                return self._continuity_in_transaction(conn)
        except AuthorityUnavailable:
            raise
        except (sqlite3.Error, OSError) as exc:
            raise AuthorityUnavailable("authority transaction failed") from exc

    def assert_replay_continuity(self, witness: ReplayContinuity) -> ReplayContinuity:
        """Require an exact, ready keyring witness before ticket authority use."""
        current = self.replay_continuity()
        if (
            not current.ready
            or not witness.ready
            or current.authority_id != witness.authority_id
            or current.recovery_generation != witness.recovery_generation
        ):
            raise AuthorityUnavailable("authority replay continuity is unavailable")
        return current

    def mark_replay_continuity_untrusted(self, *, reason: str) -> ReplayContinuity:
        """Block ticket use and advance generation once until explicit recovery."""
        del reason
        self._ensure_ready()
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                current = self._continuity_in_transaction(conn)
                if current.ready:
                    conn.execute("UPDATE authority_meta SET value=value+1 WHERE key='recovery_generation'")
                    conn.execute("UPDATE authority_meta SET value=1 WHERE key='recovery_required'")
                conn.commit()
                return self._continuity_in_transaction(conn)
        except (sqlite3.Error, OSError) as exc:
            raise AuthorityUnavailable("authority transaction failed") from exc

    def complete_replay_recovery(self, witness: ReplayContinuity) -> ReplayContinuity:
        """Explicitly reconcile a known witness after invalidating old claims."""
        self._ensure_ready()
        if not witness.authority_id:
            raise AuthorityUnavailable("authority replay continuity is unavailable")
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                current = self._continuity_in_transaction(conn)
                if current.authority_id != witness.authority_id:
                    # A newly created/replaced SQLite file has no bound keyring
                    # witness. Explicit recovery may adopt the independently
                    # persisted keyring identity, but never silently during
                    # normal mint/consume startup.
                    if self._keyring_is_bound(conn):
                        raise AuthorityUnavailable("authority replay continuity mismatch")
                    conn.execute(
                        "UPDATE authority_meta SET value=? WHERE key='authority_id'",
                        (witness.authority_id,),
                    )
                conn.execute(
                    "UPDATE authority_meta SET value=? WHERE key='recovery_generation'",
                    (max(current.recovery_generation, witness.recovery_generation) + 1,),
                )
                conn.execute("UPDATE authority_meta SET value=0 WHERE key='recovery_required'")
                conn.execute("UPDATE authority_meta SET value=1 WHERE key='keyring_bound'")
                conn.commit()
                return self._continuity_in_transaction(conn)
        except AuthorityUnavailable:
            raise
        except (sqlite3.Error, OSError) as exc:
            raise AuthorityUnavailable("authority transaction failed") from exc

    def _state_in_transaction(self, conn: sqlite3.Connection, scope: AuthorizationScope) -> AuthorizationState:
        row = conn.execute(
            "SELECT membership_revision, epoch, revoked FROM authorization_scopes WHERE scope_digest=?",
            (scope.digest,),
        ).fetchone()
        recovery_generation = self._recovery_generation(conn)
        if row is None:
            conn.execute(
                "INSERT INTO authorization_scopes(scope_digest, principal_digest, membership_revision, epoch, revoked) "
                "VALUES (?, ?, ?, 0, 0)",
                (scope.digest, scope.principal_digest, scope.membership_revision),
            )
            return AuthorizationState(epoch=0, recovery_generation=recovery_generation)
        membership_revision, epoch, revoked = str(row[0]), int(row[1]), bool(row[2])
        if revoked:
            raise AuthorizationRejected("session_revoked")
        if membership_revision != scope.membership_revision:
            raise AuthorizationRejected("membership_revision_mismatch")
        return AuthorizationState(epoch=epoch, recovery_generation=recovery_generation)

    @staticmethod
    def _record_change(
        conn: sqlite3.Connection,
        *,
        scope_digest: str,
        epoch: int,
        revoked: bool,
    ) -> AuthorityChange:
        cursor = conn.execute(
            "INSERT INTO authority_changes(scope_digest, epoch, revoked) VALUES (?, ?, ?)",
            (scope_digest, int(epoch), int(revoked)),
        )
        return AuthorityChange(
            sequence=int(cursor.lastrowid),
            scope_digest=scope_digest,
            epoch=int(epoch),
            revoked=bool(revoked),
        )

    def changes_since(self, sequence: int) -> tuple[AuthorityChange, ...]:
        """Read authority transitions after ``sequence`` from shared storage."""
        self._ensure_ready()
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT sequence, scope_digest, epoch, revoked FROM authority_changes "
                    "WHERE sequence>? ORDER BY sequence ASC",
                    (max(0, int(sequence)),),
                ).fetchall()
                return tuple(
                    AuthorityChange(
                        sequence=int(row[0]),
                        scope_digest=str(row[1]),
                        epoch=int(row[2]),
                        revoked=bool(row[3]),
                    )
                    for row in rows
                )
        except (sqlite3.Error, OSError) as exc:
            raise AuthorityUnavailable("authority transaction failed") from exc

    def worker_changes_since(self, sequence: int) -> tuple[WorkerLeaseChange, ...]:
        """Read exact Worker lifecycle transitions after ``sequence``."""
        self._ensure_ready()
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT sequence, owner_key, worker_generation, worker_id, lease_version, "
                    "recovery_generation, lease_state, generation_state FROM owner_worker_changes "
                    "WHERE sequence>? ORDER BY sequence ASC",
                    (max(0, int(sequence)),),
                ).fetchall()
                return tuple(
                    WorkerLeaseChange(
                        sequence=int(row[0]),
                        owner_key=str(row[1]),
                        worker_generation=int(row[2]),
                        worker_id=str(row[3]),
                        lease_version=int(row[4]),
                        recovery_generation=int(row[5]),
                        lease_state=WorkerLeaseState(str(row[6])),
                        generation_state=WorkerGenerationState(str(row[7])),
                    )
                    for row in rows
                )
        except (sqlite3.Error, OSError, TypeError, ValueError) as exc:
            raise AuthorityUnavailable("authority worker change transaction failed") from exc

    def activate(self, scope: AuthorizationScope) -> AuthorizationState:
        """Create/read active scope, invalidating a changed membership revision."""
        self._ensure_ready()
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                continuity = self._continuity_in_transaction(conn)
                # Initial bootstrap cannot know the keyring yet. Every later
                # ticket operation first calls assert_replay_continuity().
                if not continuity.ready and self._keyring_is_bound(conn):
                    raise AuthorityUnavailable("authority replay continuity is unavailable")
                existing = conn.execute(
                    "SELECT scope_digest, membership_revision, epoch FROM authorization_scopes "
                    "WHERE principal_digest=? AND revoked=0 ORDER BY epoch DESC",
                    (scope.principal_digest,),
                ).fetchone()
                revoked_scope_digests: tuple[str, ...] = ()
                changes: tuple[AuthorityChange, ...] = ()
                if existing is not None and str(existing[0]) == scope.digest and str(existing[1]) != scope.membership_revision:
                    # The provider supplied a newer authorization revision for the
                    # same verified session. Advance its epoch in-place so old
                    # claims and already-admitted bridges are both invalidated.
                    previous_epoch = int(existing[2])
                    revoked_scope_digests = (scope.digest,)
                    conn.execute(
                        "UPDATE authorization_scopes SET membership_revision=?, epoch=epoch+1 WHERE scope_digest=?",
                        (scope.membership_revision, scope.digest),
                    )
                    changes = (
                        self._record_change(
                            conn,
                            scope_digest=scope.digest,
                            epoch=previous_epoch + 1,
                            revoked=False,
                        ),
                    )
                elif existing is not None and str(existing[0]) != scope.digest:
                    # A newly verified session for the same principal supersedes
                    # the prior active session without reopening explicitly
                    # revoked sessions.
                    prior_scopes = tuple(
                        (str(row[0]), int(row[1])) for row in conn.execute(
                            "SELECT scope_digest, epoch FROM authorization_scopes "
                            "WHERE principal_digest=? AND revoked=0",
                            (scope.principal_digest,),
                        ).fetchall()
                    )
                    revoked_scope_digests = tuple(digest for digest, _epoch in prior_scopes)
                    conn.execute(
                        "UPDATE authorization_scopes SET revoked=1, epoch=epoch+1 WHERE principal_digest=? AND revoked=0",
                        (scope.principal_digest,),
                    )
                    changes = tuple(
                        self._record_change(
                            conn,
                            scope_digest=digest,
                            epoch=epoch + 1,
                            revoked=True,
                        )
                        for digest, epoch in prior_scopes
                    )
                state = self._state_in_transaction(conn, scope)
                conn.commit()
                return AuthorizationState(
                    epoch=state.epoch,
                    recovery_generation=state.recovery_generation,
                    revoked_scope_digests=revoked_scope_digests,
                    changes=changes,
                )
        except AuthorizationRejected:
            raise
        except (sqlite3.Error, OSError) as exc:
            raise AuthorityUnavailable("authority transaction failed") from exc

    def read_state(self, scope: AuthorizationScope) -> AuthorizationState:
        self._ensure_ready()
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                if not self._continuity_in_transaction(conn).ready:
                    raise AuthorityUnavailable("authority replay continuity is unavailable")
                state = self._state_in_transaction(conn, scope)
                conn.commit()
                return state
        except AuthorizationRejected:
            raise
        except (sqlite3.Error, OSError) as exc:
            raise AuthorityUnavailable("authority transaction failed") from exc

    def revoke_principal_and_bump(
        self, *, provider: str, user_id: str, reason: str
    ) -> AuthorizationState:
        """Revoke every active scope for one verified dashboard principal.

        Account administration can invalidate credentials without possessing a
        target user's raw browser token or session id. The provider/user pair
        is the same de-identified principal key used by :meth:`activate`, so
        this transaction fences every active tenant/session scope for that
        account and emits bridge-close changes for each one.
        """
        del reason  # reasons belong in the caller's sanitized audit event.
        provider = str(provider or "").strip()
        user_id = str(user_id or "").strip()
        if not provider or not user_id:
            raise ValueError("provider and user_id are required")
        material = "\x1f".join((provider, user_id)).encode("utf-8")
        principal_digest = hashlib.sha256(material).hexdigest()
        self._ensure_ready()
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                scopes = tuple(
                    (str(row[0]), int(row[1]))
                    for row in conn.execute(
                        "SELECT scope_digest, epoch FROM authorization_scopes "
                        "WHERE principal_digest=? AND revoked=0",
                        (principal_digest,),
                    ).fetchall()
                )
                if scopes:
                    conn.execute(
                        "UPDATE authorization_scopes SET revoked=1, epoch=epoch+1 "
                        "WHERE principal_digest=? AND revoked=0",
                        (principal_digest,),
                    )
                changes = tuple(
                    self._record_change(
                        conn, scope_digest=digest, epoch=epoch + 1, revoked=True
                    )
                    for digest, epoch in scopes
                )
                recovery_generation = self._recovery_generation(conn)
                conn.commit()
                return AuthorizationState(
                    epoch=max((epoch + 1 for _digest, epoch in scopes), default=0),
                    recovery_generation=recovery_generation,
                    revoked_scope_digests=tuple(digest for digest, _epoch in scopes),
                    changes=changes,
                )
        except (sqlite3.Error, OSError) as exc:
            raise AuthorityUnavailable("authority transaction failed") from exc

    def revoke_and_bump(self, scope: AuthorizationScope, *, reason: str) -> AuthorizationState:
        """Revoke this verified session scope and increment its epoch."""
        del reason  # reason is carried by the caller's sanitized audit event.
        self._ensure_ready()
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT membership_revision, epoch FROM authorization_scopes WHERE scope_digest=?",
                    (scope.digest,),
                ).fetchone()
                if row is not None and str(row[0]) != scope.membership_revision:
                    raise AuthorizationRejected("membership_revision_mismatch")
                epoch = (int(row[1]) if row is not None else 0) + 1
                conn.execute(
                    "INSERT INTO authorization_scopes(scope_digest, principal_digest, membership_revision, epoch, revoked) "
                    "VALUES (?, ?, ?, ?, 1) "
                    "ON CONFLICT(scope_digest) DO UPDATE SET epoch=excluded.epoch, revoked=1",
                    (scope.digest, scope.principal_digest, scope.membership_revision, epoch),
                )
                recovery_generation = self._recovery_generation(conn)
                change = self._record_change(
                    conn,
                    scope_digest=scope.digest,
                    epoch=epoch,
                    revoked=True,
                )
                conn.commit()
                return AuthorizationState(
                    epoch=epoch,
                    recovery_generation=recovery_generation,
                    revoked_scope_digests=(scope.digest,),
                    changes=(change,),
                )
        except AuthorizationRejected:
            raise
        except (sqlite3.Error, OSError) as exc:
            raise AuthorityUnavailable("authority transaction failed") from exc

    def check_and_consume(
        self,
        scope: AuthorizationScope,
        *,
        token_class: str,
        issuer_key_version: str,
        jti: str,
        audience: str,
        expires_at: int,
        claim_epoch: int,
        claim_recovery_generation: int,
        now: int | None = None,
    ) -> ConsumeDecision:
        """Atomically validate scope/epoch and consume one exact-audience jti."""
        self._ensure_ready()
        current_time = int(time.time()) if now is None else int(now)
        if current_time > int(expires_at):
            raise AuthorizationRejected("credential_expired")
        if not str(token_class or "").strip() or not str(issuer_key_version or "").strip() or not str(audience or "").strip():
            raise ValueError("credential class, issuer key version, and audience are required")
        digest = self._credential_digest(jti)
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                if not self._continuity_in_transaction(conn).ready:
                    raise AuthorityUnavailable("authority replay continuity is unavailable")
                state = self._state_in_transaction(conn, scope)
                if int(claim_recovery_generation) != state.recovery_generation:
                    raise AuthorizationRejected("recovery_generation_mismatch")
                if int(claim_epoch) != state.epoch:
                    raise AuthorizationRejected("epoch_mismatch")
                conn.execute("DELETE FROM consumed_credentials WHERE expires_at < ?", (current_time,))
                try:
                    conn.execute(
                        "INSERT INTO consumed_credentials(token_class, issuer_key_version, credential_digest, audience, expires_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (token_class, issuer_key_version, digest, audience, int(expires_at)),
                    )
                except sqlite3.IntegrityError as exc:
                    raise AuthorizationRejected("credential_replayed") from exc
                conn.commit()
                return ConsumeDecision(True, state.epoch, state.recovery_generation)
        except AuthorizationRejected:
            raise
        except (sqlite3.Error, OSError) as exc:
            raise AuthorityUnavailable("authority transaction failed") from exc

    def check_and_consume_owner_worker_bootstrap(
        self,
        lease: OwnerWorkerAuthorityLease,
        *,
        issuer_key_version: str,
        jti: str,
        audience: str,
        expires_at: int,
        now: int | None = None,
    ) -> OwnerWorkerBootstrapConsumeDecision:
        """Atomically admit one bootstrap bound to the exact active Worker lease."""
        self._ensure_ready()
        current_time = int(time.time()) if now is None else int(now)
        if current_time > int(expires_at):
            raise AuthorizationRejected("credential_expired")
        if not str(issuer_key_version or "").strip() or not str(audience or "").strip() or not str(jti or "").strip():
            raise ValueError("bootstrap issuer key version, audience, and jti are required")
        digest = self._credential_digest(jti)
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                if not self._continuity_in_transaction(conn).ready:
                    raise AuthorityUnavailable("authority replay continuity is unavailable")
                recovery_generation = self._recovery_generation(conn)
                row = conn.execute(
                    "SELECT owner_key, worker_generation, worker_id, state, lease_version, recovery_generation "
                    "FROM owner_worker_leases WHERE owner_key=?",
                    (lease.owner_key,),
                ).fetchone()
                if row is None:
                    raise AuthorizationRejected("worker_lease_not_found")
                current_lease = self._worker_lease_from_row(row)
                if current_lease != lease or lease.recovery_generation != recovery_generation:
                    raise AuthorizationRejected("worker_lease_stale")
                if current_lease.state is not WorkerLeaseState.ACTIVE:
                    raise AuthorizationRejected("worker_lease_state_mismatch")
                conn.execute("DELETE FROM owner_worker_bootstrap_consumptions WHERE expires_at < ?", (current_time,))
                try:
                    conn.execute(
                        "INSERT INTO owner_worker_bootstrap_consumptions("
                        "issuer_key_version, credential_digest, audience, expires_at) VALUES (?, ?, ?, ?)",
                        (str(issuer_key_version), digest, str(audience), int(expires_at)),
                    )
                except sqlite3.IntegrityError as exc:
                    raise AuthorizationRejected("credential_replayed") from exc
                conn.commit()
                return OwnerWorkerBootstrapConsumeDecision(True, current_lease)
        except (AuthorizationRejected, AuthorityUnavailable):
            raise
        except (sqlite3.Error, OSError) as exc:
            raise AuthorityUnavailable("authority transaction failed") from exc

    def invalidate_outstanding_credentials(self, *, reason: str) -> int:
        """Advance recovery generation; all previously minted claims become stale."""
        del reason
        self._ensure_ready()
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("UPDATE authority_meta SET value=value+1 WHERE key='recovery_generation'")
                value = self._recovery_generation(conn)
                conn.commit()
                return value
        except (sqlite3.Error, OSError) as exc:
            raise AuthorityUnavailable("authority transaction failed") from exc
