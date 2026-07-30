"""Cross-process lifecycle lock for dashboard authority storage."""
from __future__ import annotations

import stat
from pathlib import Path

try:  # pragma: no cover - platform import
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

try:  # pragma: no cover - platform import
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None  # type: ignore[assignment]


_LOCK_NAME = "authority.lifecycle.lock"


class AuthorityLifecycleLockError(RuntimeError):
    """The authority lifecycle lock is unsafe, unavailable, or already held."""


class AuthorityLifecycleLock:
    """Process-lifetime shared lock for servers and exclusive lock for recovery."""

    def __init__(self, path: Path, *, exclusive: bool):
        self.path = path
        self.exclusive = exclusive
        self._handle = None

    def acquire(self) -> "AuthorityLifecycleLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() or self.path.is_symlink():
            status = self.path.lstat()
            if stat.S_ISLNK(status.st_mode) or not stat.S_ISREG(status.st_mode):
                raise AuthorityLifecycleLockError(
                    "authority lifecycle lock path is unsafe"
                )
        if msvcrt is not None and (
            not self.path.exists() or self.path.stat().st_size == 0
        ):
            self.path.write_text(" ", encoding="utf-8")
        handle = self.path.open(
            "r+" if msvcrt is not None else "a+", encoding="utf-8"
        )
        try:
            if fcntl is not None:
                operation = fcntl.LOCK_EX if self.exclusive else fcntl.LOCK_SH
                fcntl.flock(handle.fileno(), operation | fcntl.LOCK_NB)
            elif msvcrt is not None:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:  # pragma: no cover - unsupported platform
                raise AuthorityLifecycleLockError(
                    "authority lifecycle locking is unavailable"
                )
        except (BlockingIOError, OSError, PermissionError) as exc:
            handle.close()
            role = "dashboard or owner process" if self.exclusive else "authority recovery"
            raise AuthorityLifecycleLockError(
                f"authority is locked by an active {role}"
            ) from exc
        self._handle = handle
        return self

    def close(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            elif msvcrt is not None:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            handle.close()

    def __enter__(self) -> "AuthorityLifecycleLock":
        return self.acquire()

    def __exit__(self, *_args) -> None:
        self.close()


def authority_lifecycle_lock(
    control_home: Path, *, exclusive: bool
) -> AuthorityLifecycleLock:
    """Return an unacquired shared or exclusive authority lifecycle lock."""
    return AuthorityLifecycleLock(
        control_home / _LOCK_NAME,
        exclusive=exclusive,
    )


def acquire_authority_server_lock(control_home: Path) -> AuthorityLifecycleLock:
    """Acquire the shared lifecycle lock held by an authenticated server."""
    return authority_lifecycle_lock(control_home, exclusive=False).acquire()
