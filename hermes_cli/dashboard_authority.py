"""Offline operator commands for Control Plane authority recovery."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from hermes_cli.dashboard_auth.audit import (
    AuthorityAuditEvent,
    AuthorityAuditReason,
)
from hermes_cli.dashboard_auth.authority import (
    AuthorityCorrupt,
    AuthorityStore,
    AuthorityUnavailable,
    ReaderGenerationState,
    ReaderLeaseState,
    ReplayContinuity,
    WorkerGenerationState,
    WorkerLeaseState,
    control_plane_home,
)
from hermes_cli.dashboard_auth.lifecycle import (
    AuthorityLifecycleLockError,
    acquire_authority_server_lock,
    authority_lifecycle_lock,
)
from hermes_cli.dashboard_auth.ws_tickets import (
    TicketInvalid,
    load_ticket_keyring_for_recovery,
    write_ticket_keyring_for_recovery,
)
from hermes_cli.sqlite_util import (
    SQLITE_HEADER,
    SQLITE_SIDECAR_SUFFIXES,
    sha256_file,
    write_txn,
)

_REQUIRED_TABLES = frozenset({
    "authority_meta",
    "authorization_scopes",
    "consumed_credentials",
    "authority_changes",
    "owner_worker_generations",
    "owner_worker_leases",
    "owner_worker_bootstrap_consumptions",
    "owner_worker_changes",
    "session_reader_generations",
    "session_reader_leases",
})
_EXACT_TLS_OFFSET_5 = bytes.fromhex("17 03 03 00 13")
_CORRUPTION_CLASSIFICATIONS = frozenset({
    "tls_record_at_offset_5",
    "tls_record_at_offset_0",
    "invalid_sqlite_header",
    "zero_length_database",
    "sqlite_integrity_failure",
})


class AuthorityRecoveryError(RuntimeError):
    """An offline recovery precondition or validation failed."""


@dataclass(frozen=True)
class AuthorityStatus:
    state: str
    schema_version: int | None
    recovery_generation: int | None
    incident_id: str | None
    classification: str | None
    sha256: str | None
    size: int | None
    preserved: bool | None


def _validated_marker(store: AuthorityStore) -> dict[str, object] | None:
    marker = store._read_recovery_marker()  # noqa: SLF001 - operator boundary
    if marker is None:
        return None
    try:
        version = int(marker["version"])
        incident_id = str(marker["incident_id"])
        classification = str(marker["classification"])
        digest = str(marker["sha256"])
        size = int(marker["size"])
        detected_at = int(marker["detected_at"])
        preserved = marker["preserved"]
    except (KeyError, TypeError, ValueError) as exc:
        raise AuthorityRecoveryError("authority recovery marker is invalid") from exc
    if (
        version != 1
        or len(incident_id) != 16
        or any(ch not in "0123456789abcdef" for ch in incident_id)
        or classification not in _CORRUPTION_CLASSIFICATIONS
        or len(digest) != 64
        or any(ch not in "0123456789abcdef" for ch in digest)
        or size < 0
        or detected_at < 0
        or not isinstance(preserved, bool)
    ):
        raise AuthorityRecoveryError("authority recovery marker is invalid")
    return marker


def authority_status(control_home: Path | None = None) -> AuthorityStatus:
    """Inspect authority health without creating or migrating storage."""
    home = Path(control_home) if control_home is not None else control_plane_home()
    store = AuthorityStore(home)
    marker = _validated_marker(store)
    if marker is not None:
        return AuthorityStatus(
            state="recovery_required",
            schema_version=None,
            recovery_generation=None,
            incident_id=str(marker["incident_id"]),
            classification=str(marker["classification"]),
            sha256=str(marker["sha256"]),
            size=int(marker["size"]),
            preserved=bool(marker["preserved"]),
        )
    if not store.path.exists():
        return AuthorityStatus("uninitialized", None, None, None, None, None, None, None)
    store._validate_path()  # noqa: SLF001 - read-only operator inspection
    if store.path.stat().st_size == 0:
        raise AuthorityRecoveryError("authority status is unavailable")
    uri = f"file:{store.path.resolve().as_posix()}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True, timeout=5, isolation_level=None) as conn:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()
            if not integrity or str(integrity[0]).lower() != "ok":
                raise AuthorityRecoveryError("authority integrity check failed")
            values = dict(conn.execute(
                "SELECT key, value FROM authority_meta WHERE key IN ('schema_version', 'recovery_generation')"
            ).fetchall())
            schema = int(values["schema_version"])
            generation = int(values["recovery_generation"])
    except (sqlite3.Error, KeyError, TypeError, ValueError) as exc:
        raise AuthorityRecoveryError("authority status is unavailable") from exc
    return AuthorityStatus("healthy", schema, generation, None, None, sha256_file(store.path), store.path.stat().st_size, None)


def preserve_authority(control_home: Path | None = None) -> AuthorityStatus:
    """Idempotently create or verify forensic evidence for a corrupt authority."""
    home = Path(control_home) if control_home is not None else control_plane_home()
    store = AuthorityStore(home)
    marker = _validated_marker(store)
    if marker is None:
        try:
            store._validate_existing_database()  # noqa: SLF001 - explicit preservation command
        except AuthorityCorrupt:
            marker = _validated_marker(store)
        else:
            raise AuthorityRecoveryError("authority is healthy; no recovery evidence is required")
    if marker is None:
        raise AuthorityRecoveryError("authority recovery marker is unavailable")
    if store.path.exists() and sha256_file(store.path) != str(marker["sha256"]):
        raise AuthorityRecoveryError("live authority digest does not match recovery marker")
    if not bool(marker["preserved"]):
        if not store.path.exists():
            raise AuthorityRecoveryError("authority source is unavailable for preservation")
        if not store._retry_forensic_preservation(marker):  # noqa: SLF001
            raise AuthorityRecoveryError("authority evidence preservation failed")
    return authority_status(home)


def _copy_regular_file(source: Path, destination: Path) -> None:
    with source.open("rb") as reader, destination.open("xb") as writer:
        os.chmod(destination, 0o600)
        shutil.copyfileobj(reader, writer, 1024 * 1024)
        writer.flush()
        os.fsync(writer.fileno())


def _copy_candidate_source(source: Path, destination: Path) -> None:
    try:
        source_status = source.lstat()
    except OSError as exc:
        raise AuthorityRecoveryError("recovery source is unavailable") from exc
    if stat.S_ISLNK(source_status.st_mode) or not stat.S_ISREG(source_status.st_mode):
        raise AuthorityRecoveryError("recovery source must be a regular file")
    source = source.resolve(strict=True)
    _copy_regular_file(source, destination)
    for suffix in SQLITE_SIDECAR_SUFFIXES:
        sidecar = source.with_name(source.name + suffix)
        try:
            sidecar_status = sidecar.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise AuthorityRecoveryError("recovery source sidecar is unavailable") from exc
        if stat.S_ISLNK(sidecar_status.st_mode) or not stat.S_ISREG(sidecar_status.st_mode):
            raise AuthorityRecoveryError("recovery source sidecar must be a regular file")
        target = destination.with_name(destination.name + suffix)
        _copy_regular_file(sidecar, target)


def _repair_exact_tls_offset_5(path: Path) -> None:
    page_size = 4096
    try:
        size = path.stat().st_size
        with path.open("r+b") as handle:
            head = bytearray(handle.read(96))
            if (
                size < page_size
                or size % page_size
                or head[:5] != b"SQLit"
                or head[5:10] != _EXACT_TLS_OFFSET_5
            ):
                raise AuthorityRecoveryError(
                    "source does not match the exact TLS-at-offset-5 signature"
                )
            head[:16] = SQLITE_HEADER
            head[16:18] = page_size.to_bytes(2, "big")
            head[18:20] = b"\x02\x02"
            head[20:24] = b"\x00\x40\x20\x20"
            head[24:28] = head[92:96]
            head[28:32] = (size // page_size).to_bytes(4, "big")
            handle.seek(0)
            handle.write(head[:32])
            handle.flush()
            os.fsync(handle.fileno())
    except AuthorityRecoveryError:
        raise
    except OSError as exc:
        raise AuthorityRecoveryError("recovery source is unavailable") from exc


def _validate_candidate(path: Path) -> ReplayContinuity:
    try:
        with sqlite3.connect(path, timeout=5, isolation_level=None) as conn:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()
            if not integrity or str(integrity[0]).lower() != "ok":
                raise AuthorityRecoveryError("recovery candidate integrity check failed")
            tables = {str(row[0]) for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
            if not _REQUIRED_TABLES.issubset(tables):
                raise AuthorityRecoveryError("recovery candidate schema is incomplete")
            values = dict(conn.execute(
                "SELECT key, value FROM authority_meta WHERE key IN "
                "('schema_version', 'authority_id', 'recovery_generation', 'recovery_required', 'keyring_bound')"
            ).fetchall())
            if int(values["schema_version"]) != 6:
                raise AuthorityRecoveryError("recovery candidate schema is unsupported")
            authority_id = str(values["authority_id"])
            generation = int(values["recovery_generation"])
            if not authority_id or generation < 0:
                raise AuthorityRecoveryError("recovery candidate continuity is invalid")
            for table, states in (
                ("owner_worker_generations", {item.value for item in WorkerGenerationState}),
                ("owner_worker_leases", {item.value for item in WorkerLeaseState}),
                ("session_reader_generations", {item.value for item in ReaderGenerationState}),
                ("session_reader_leases", {item.value for item in ReaderLeaseState}),
            ):
                if any(str(row[0]) not in states for row in conn.execute(f"SELECT state FROM {table}")):
                    raise AuthorityRecoveryError("recovery candidate contains invalid lifecycle records")
            return ReplayContinuity(authority_id, generation, not bool(int(values["recovery_required"])))
    except AuthorityRecoveryError:
        raise
    except (sqlite3.Error, KeyError, TypeError, ValueError) as exc:
        raise AuthorityRecoveryError("recovery candidate validation failed") from exc


def _fence_candidate(path: Path, *, keyring_generation: int) -> ReplayContinuity:
    current = _validate_candidate(path)
    with sqlite3.connect(path, timeout=5, isolation_level=None) as conn:
        with write_txn(conn):
            generation = max(current.recovery_generation, keyring_generation) + 1
            conn.execute("UPDATE authority_meta SET value=? WHERE key='recovery_generation'", (generation,))
            conn.execute("UPDATE authority_meta SET value=1 WHERE key='recovery_required'")
            conn.execute("UPDATE authority_meta SET value=1 WHERE key='keyring_bound'")
            conn.execute("UPDATE authorization_scopes SET epoch=epoch+1, revoked=1 WHERE revoked=0")
            conn.execute("DELETE FROM consumed_credentials")
            conn.execute("DELETE FROM owner_worker_bootstrap_consumptions")
            conn.execute("UPDATE owner_worker_generations SET state=?", (WorkerGenerationState.REVOKED.value,))
            conn.execute("UPDATE owner_worker_leases SET state=?, lease_version=lease_version+1", (WorkerLeaseState.REVOKED.value,))
            conn.execute("UPDATE session_reader_generations SET state=?", (ReaderGenerationState.REVOKED.value,))
            conn.execute("UPDATE session_reader_leases SET state=?, lease_version=lease_version+1", (ReaderLeaseState.REVOKED.value,))
    compact = path.with_name(path.name + ".fenced")
    _sqlite_rebuild(path, compact)
    os.replace(compact, path)
    for suffix in SQLITE_SIDECAR_SUFFIXES:
        try:
            path.with_name(path.name + suffix).unlink()
        except FileNotFoundError:
            pass
    return ReplayContinuity(current.authority_id, generation, False)


def _sqlite_rebuild(source: Path, destination: Path) -> None:
    source_conn = sqlite3.connect(source, timeout=5, isolation_level=None)
    try:
        destination_conn = sqlite3.connect(destination, timeout=5, isolation_level=None)
        try:
            source_conn.backup(destination_conn)
            destination_conn.execute("PRAGMA wal_checkpoint(FULL)")
        finally:
            destination_conn.close()
    finally:
        source_conn.close()
    os.chmod(destination, 0o600)
    with destination.open("rb") as handle:
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def recover_authority(
    *,
    incident_id: str,
    source: Path,
    expected_sha256: str,
    repair_tls_offset_5: bool,
    control_home: Path | None = None,
) -> AuthorityStatus:
    """Recover from one digest-pinned source while fencing all old authority."""
    home = Path(control_home) if control_home is not None else control_plane_home()
    store = AuthorityStore(home)
    marker = _validated_marker(store)
    if marker is None:
        raise AuthorityRecoveryError("authority recovery is not required")
    if str(marker["incident_id"]) != str(incident_id):
        raise AuthorityRecoveryError("incident ID does not match recovery marker")
    expected = str(expected_sha256).lower()
    if expected != str(marker["sha256"]) or len(expected) != 64:
        raise AuthorityRecoveryError("recovery digest does not match recovery marker")
    source = Path(source)
    try:
        source_status = source.lstat()
    except OSError as exc:
        raise AuthorityRecoveryError("recovery source is unavailable") from exc
    if stat.S_ISLNK(source_status.st_mode) or not stat.S_ISREG(source_status.st_mode):
        raise AuthorityRecoveryError("recovery source must be a regular file")
    try:
        actual_digest = sha256_file(source)
    except OSError as exc:
        raise AuthorityRecoveryError("recovery source is unavailable") from exc
    if actual_digest != expected:
        raise AuthorityRecoveryError("recovery source digest mismatch")

    digest = str(marker["sha256"])
    store._audit_recovery_event(  # noqa: SLF001 - explicit operator lifecycle
        AuthorityAuditEvent.RECOVERY_STARTED,
        AuthorityAuditReason.RECOVERY_STARTED,
        digest=digest,
    )
    try:
        with authority_lifecycle_lock(home, exclusive=True):
            candidate_fd, candidate_name = tempfile.mkstemp(prefix=".authority-recovery-source.", dir=home)
            os.close(candidate_fd)
            Path(candidate_name).unlink()
            rebuilt_fd, rebuilt_name = tempfile.mkstemp(prefix=".authority-recovery-built.", dir=home)
            os.close(rebuilt_fd)
            Path(rebuilt_name).unlink()
            candidate = Path(candidate_name)
            rebuilt = Path(rebuilt_name)
            try:
                _copy_candidate_source(source, candidate)
                if sha256_file(candidate) != expected:
                    raise AuthorityRecoveryError("recovery source changed while being copied")
                if repair_tls_offset_5:
                    _repair_exact_tls_offset_5(candidate)
                _validate_candidate(candidate)
                _sqlite_rebuild(candidate, rebuilt)

                keyring = load_ticket_keyring_for_recovery(store)
                witness = keyring["replay_continuity"]
                fenced = _fence_candidate(
                    rebuilt,
                    keyring_generation=witness.recovery_generation,
                )
                _validate_candidate(rebuilt)
                # A WAL/SHM/journal belonging to the quarantined inode must never
                # be replayed against the rebuilt database after replacement.
                for suffix in SQLITE_SIDECAR_SUFFIXES:
                    try:
                        store.path.with_name(store.path.name + suffix).unlink()
                    except FileNotFoundError:
                        pass
                _fsync_directory(home)
                os.replace(rebuilt, store.path)
                _fsync_directory(home)

                recovered = ReplayContinuity(fenced.authority_id, fenced.recovery_generation, True)
                keyring["replay_continuity"] = recovered
                write_ticket_keyring_for_recovery(keyring)
                _fsync_directory(home)
                with sqlite3.connect(store.path, timeout=5, isolation_level=None) as conn:
                    with write_txn(conn):
                        conn.execute(
                            "UPDATE authority_meta SET value=0 "
                            "WHERE key='recovery_required'"
                        )
                _validate_candidate(store.path)
                store.recovery_marker_path.unlink()
                _fsync_directory(home)
            finally:
                for path in (candidate, rebuilt):
                    for suffix in ("", *SQLITE_SIDECAR_SUFFIXES):
                        try:
                            path.with_name(path.name + suffix).unlink()
                        except FileNotFoundError:
                            pass
    except (
        AuthorityLifecycleLockError,
        AuthorityRecoveryError,
        AuthorityUnavailable,
        TicketInvalid,
        OSError,
        sqlite3.Error,
    ) as exc:
        store._audit_recovery_event(  # noqa: SLF001
            AuthorityAuditEvent.RECOVERY_FAILED,
            AuthorityAuditReason.RECOVERY_FAILED,
            digest=digest,
        )
        if isinstance(exc, AuthorityRecoveryError):
            raise
        if isinstance(exc, AuthorityLifecycleLockError):
            raise AuthorityRecoveryError(str(exc)) from exc
        raise AuthorityRecoveryError("authority recovery failed; recovery remains required") from exc

    store._audit_recovery_event(  # noqa: SLF001
        AuthorityAuditEvent.RECOVERY_COMPLETED,
        AuthorityAuditReason.RECOVERY_COMPLETED,
        digest=digest,
    )
    return authority_status(home)


def cmd_dashboard_authority(args) -> None:
    """Dispatch ``hermes dashboard authority`` operator commands."""
    try:
        if args.dashboard_authority_action == "status":
            status = authority_status()
        elif args.dashboard_authority_action == "preserve":
            status = preserve_authority()
        elif args.dashboard_authority_action == "recover":
            status = recover_authority(
                incident_id=args.incident,
                source=Path(args.source),
                expected_sha256=args.sha256,
                repair_tls_offset_5=bool(args.repair_tls_offset_5),
            )
        else:  # pragma: no cover - argparse requires an action
            raise AuthorityRecoveryError("authority action is required")
    except (AuthorityRecoveryError, AuthorityUnavailable) as exc:
        print(f"Authority operation failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    payload = asdict(status)
    if getattr(args, "json", False):
        print(json.dumps(payload, sort_keys=True))
    else:
        for key, value in payload.items():
            if value is not None:
                print(f"{key}: {str(value).lower() if isinstance(value, bool) else value}")
