"""Shared SQLite primitives for the small per-profile / board stores."""

from __future__ import annotations

import contextlib
import hashlib
import os
import sqlite3
import stat
from collections.abc import Callable
from pathlib import Path


SQLITE_HEADER = b"SQLite format 3\x00"
SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


def _looks_like_tls_record_at(data: bytes, offset: int) -> bool:
    """Return whether ``data[offset:]`` starts with a plausible TLS record."""
    if len(data) < offset + 5:
        return False
    content_type = data[offset]
    major = data[offset + 1]
    minor = data[offset + 2]
    length = int.from_bytes(data[offset + 3:offset + 5], "big")
    return (
        content_type in {0x14, 0x15, 0x16, 0x17}
        and major == 0x03
        and minor in {0x00, 0x01, 0x02, 0x03, 0x04}
        and 0 < length <= 18432
    )


def classify_sqlite_header(path: Path) -> str | None:
    """Return an invalid-header reason, or ``None`` when none is detected.

    Missing, empty, and unreadable files are left to the caller's normal open
    path. TLS record signatures at offsets seen in page-zero clobbers are added
    to the reason so callers can distinguish that corruption shape without this
    policy-neutral classifier deciding how to handle it.
    """
    try:
        stat = path.stat()
    except OSError:
        return None
    if stat.st_size == 0:
        return None
    try:
        with path.open("rb") as handle:
            head = handle.read(64)
    except OSError:
        return None
    if head.startswith(SQLITE_HEADER):
        return None
    signature = ""
    if head.startswith(b"SQLit") and _looks_like_tls_record_at(head, 5):
        signature = " (TLS record header detected at byte offset 5)"
    elif _looks_like_tls_record_at(head, 0):
        signature = " (TLS record header detected at byte offset 0)"
    return (
        "file is not a database: invalid SQLite header for "
        f"{path}{signature}; first_32={head[:32].hex(' ')}"
    )


def sha256_file(path: Path) -> str:
    """Return the lowercase SHA-256 hex digest of ``path``."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def copy_sqlite_forensics(path: Path) -> Path | None:
    """Preserve a SQLite file and existing WAL/SHM sidecars by content hash.

    The main copy is named from the main database's SHA-256 and all writes stay
    in its resolved parent directory. Repeated calls for unchanged bytes reuse
    the same files. A main-file read or copy failure returns ``None``; sidecar
    failures are best-effort and do not invalidate a preserved main copy.
    """
    resolved = path.resolve()
    parent = resolved.parent
    base_name = resolved.name
    try:
        token = sha256_file(resolved)[:16]
    except OSError:
        return None
    candidate = parent / f"{base_name}.corrupt.{token}.bak"
    if candidate.parent != parent:
        return None
    if not candidate.exists():
        try:
            source_fd = os.open(resolved, os.O_RDONLY)
            try:
                target_fd = os.open(
                    candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
                )
                try:
                    while chunk := os.read(source_fd, 1024 * 1024):
                        view = memoryview(chunk)
                        while view:
                            written = os.write(target_fd, view)
                            view = view[written:]
                    os.fsync(target_fd)
                finally:
                    os.close(target_fd)
            finally:
                os.close(source_fd)
        except FileExistsError:
            pass
        except OSError:
            return None
    if os.name != "nt":
        candidate.chmod(stat.S_IRUSR | stat.S_IWUSR)
    for suffix in SQLITE_SIDECAR_SUFFIXES:
        sidecar = parent / (base_name + suffix)
        if sidecar.parent != parent or not sidecar.exists():
            continue
        sidecar_backup = parent / (candidate.name + suffix)
        if sidecar_backup.parent != parent or sidecar_backup.exists():
            continue
        try:
            source_fd = os.open(sidecar, os.O_RDONLY)
            try:
                target_fd = os.open(
                    sidecar_backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
                )
                try:
                    while chunk := os.read(source_fd, 1024 * 1024):
                        view = memoryview(chunk)
                        while view:
                            written = os.write(target_fd, view)
                            view = view[written:]
                    os.fsync(target_fd)
                finally:
                    os.close(target_fd)
            finally:
                os.close(source_fd)
        except FileExistsError:
            pass
        except OSError:
            pass
    return candidate


def probe_sqlite_integrity(
    path: Path,
    connect: Callable[[Path], sqlite3.Connection],
) -> str | None:
    """Return a corruption reason, or ``None`` when integrity is healthy.

    ``connect`` must open read/write so SQLite can recover or checkpoint a
    healthy WAL/hot-journal database. SQLite exceptions propagate unchanged;
    an active failure window is not stable evidence of corruption. Only a
    completed integrity check with a non-``ok`` result returns a corruption
    reason for the caller's policy.
    """
    probe = connect(path)
    try:
        row = probe.execute("PRAGMA integrity_check").fetchone()
    finally:
        probe.close()
    if not row or (row[0] or "").lower() != "ok":
        return f"integrity_check returned {row[0] if row else '<no row>'!r}"
    return None


def add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> bool:
    """``ALTER TABLE <table> ADD COLUMN <ddl>``, idempotent across races.

    Returns ``True`` when this call added the column. Swallows the
    ``duplicate column name`` error a concurrent migrator may have run first
    (issue #21708). ``column`` is the human-readable name for the call site;
    ``ddl`` carries the actual definition.
    """
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
        return True
    except sqlite3.OperationalError as exc:
        if "duplicate column name" in str(exc).lower():
            return False
        raise


@contextlib.contextmanager
def write_txn(conn: sqlite3.Connection):
    """An IMMEDIATE write transaction: at most one concurrent writer wins.

    The explicit ROLLBACK is guarded so a SQLite auto-rollback (no active
    transaction left under EIO / lock contention / corruption) cannot shadow
    the original exception with a spurious rollback error.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass
        raise
    else:
        conn.execute("COMMIT")
