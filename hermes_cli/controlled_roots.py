"""App-owned directory descriptor roots for authenticated owner workers.

All object access in this module is rooted at an already-open trusted directory
FD. Callers must continue using returned descriptors or the operations below;
a canonical diagnostic path is never an authorization input.
"""

from __future__ import annotations

import ctypes
import errno
import os
import platform
import secrets
import stat
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterable, Mapping

from hermes_cli.owner_runtime import OwnerWorkerRuntimePaths


RESOLVE_NO_XDEV = 0x01
RESOLVE_NO_MAGICLINKS = 0x02
RESOLVE_NO_SYMLINKS = 0x04
RESOLVE_BENEATH = 0x08
_REQUIRED_RESOLVE_FLAGS = RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS | RESOLVE_NO_MAGICLINKS
_OPENAT2_SYSCALLS = {
    "aarch64": 437,
    "ppc64le": 437,
    "riscv64": 437,
    "s390x": 437,
    "x86_64": 437,
}


class RootKind(str, Enum):
    """Trusted root capabilities owned by one authenticated worker app."""

    GLOBAL_READONLY = "global_readonly"
    OWNER_WRITABLE = "owner_writable"
    WORKSPACE = "workspace"
    TEMPORARY = "temporary"


class ExpectedType(str, Enum):
    """Filesystem object types accepted by ``open_relative``."""

    REGULAR_FILE = "regular_file"
    DIRECTORY = "directory"


@dataclass(frozen=True)
class ControlledRoot:
    """Immutable metadata for an already-open trusted directory descriptor."""

    kind: RootKind
    directory_fd: int
    writable: bool
    canonical_path: Path


@dataclass(frozen=True)
class DirectoryEntry:
    """Descriptor-derived directory entry metadata for a relative child path."""

    name: str
    relative_path: str
    is_directory: bool
    size: int | None
    mtime: float


class AtomicFileWriter:
    """An FD-relative temporary file that can be atomically promoted once."""

    def __init__(
        self,
        *,
        parent_fd: int,
        temporary_fd: int,
        temporary_name: str,
        leaf: str,
        relative_path: str,
        root_device: int,
    ) -> None:
        self._parent_fd = parent_fd
        self._temporary_fd: int | None = temporary_fd
        self._temporary_name = temporary_name
        self._leaf = leaf
        self._relative_path = relative_path
        self._root_device = root_device
        self._finished = False
        self.bytes_written = 0

    def write(self, data: bytes) -> None:
        """Append bytes to the unlinked-on-failure temporary file."""
        if self._finished or self._temporary_fd is None:
            raise RuntimeError("atomic file writer is closed")
        if not isinstance(data, bytes):
            raise TypeError("data must be bytes")
        _write_all(self._temporary_fd, data)
        self.bytes_written += len(data)

    def commit(self) -> DirectoryEntry:
        """Sync and promote the completed temporary file using its parent FD."""
        if self._finished or self._temporary_fd is None:
            raise RuntimeError("atomic file writer is closed")
        try:
            os.fsync(self._temporary_fd)
            os.close(self._temporary_fd)
            self._temporary_fd = None
            os.replace(
                self._temporary_name,
                self._leaf,
                src_dir_fd=self._parent_fd,
                dst_dir_fd=self._parent_fd,
            )
            metadata = os.stat(self._leaf, dir_fd=self._parent_fd, follow_symlinks=False)
            _validate_stat(metadata, ExpectedType.REGULAR_FILE, root_device=self._root_device, enforce_no_xdev=True)
            self._finished = True
            return DirectoryEntry(
                self._leaf,
                self._relative_path,
                False,
                metadata.st_size,
                metadata.st_mtime,
            )
        finally:
            if self._finished:
                os.close(self._parent_fd)
                self._parent_fd = -1
            else:
                self.abort()

    def abort(self) -> None:
        """Close descriptors and remove an unpromoted temporary sibling file."""
        if self._finished:
            return
        self._finished = True
        if self._temporary_fd is not None:
            try:
                os.close(self._temporary_fd)
            finally:
                self._temporary_fd = None
        if self._parent_fd >= 0:
            try:
                os.unlink(self._temporary_name, dir_fd=self._parent_fd)
            except FileNotFoundError:
                pass
            finally:
                os.close(self._parent_fd)
                self._parent_fd = -1


class _OpenHow(ctypes.Structure):
    _fields_ = [
        ("flags", ctypes.c_uint64),
        ("mode", ctypes.c_uint64),
        ("resolve", ctypes.c_uint64),
    ]


class ControlledRoots:
    """Closeable collection of descriptor roots for one owner-worker app."""

    def __init__(self, roots: Mapping[RootKind, ControlledRoot]) -> None:
        required = set(RootKind)
        actual = set(roots)
        if actual != required:
            missing = ", ".join(kind.value for kind in sorted(required - actual, key=lambda item: item.value))
            extra = ", ".join(kind.value for kind in sorted(actual - required, key=lambda item: item.value))
            details = ", ".join(part for part in (f"missing: {missing}" if missing else "", f"extra: {extra}" if extra else "") if part)
            raise RuntimeError(f"controlled root set is incomplete ({details})")
        self._roots = dict(roots)
        self._closed = False

    def get(self, kind: RootKind) -> ControlledRoot:
        """Return one root descriptor and diagnostic metadata."""
        return self._roots[kind]

    @property
    def roots(self) -> Mapping[RootKind, ControlledRoot]:
        """Expose immutable root metadata without a mutable backing mapping."""
        return self._roots.copy()

    def open_relative(
        self,
        kind: RootKind,
        relative_path: str,
        *,
        expected_type: ExpectedType,
        writable: bool = False,
        enforce_no_xdev: bool = True,
    ) -> int:
        """Open one existing safe object beneath a trusted root and return its FD.

        The path is a relative POSIX path only. On Linux, a single ``openat2``
        lookup is preferred; systems without that syscall use a descriptor-only
        component walk. This API never reopens ``canonical_path / relative_path``.
        It intentionally does not create, truncate, rename, or delete objects.
        """
        self._require_linux()
        components = _relative_components(relative_path)
        root = self._require_root(kind, writable=writable)
        if not isinstance(expected_type, ExpectedType):
            raise TypeError("expected_type must be an ExpectedType")
        if not isinstance(enforce_no_xdev, bool):
            raise TypeError("enforce_no_xdev must be a bool")

        flags = _object_open_flags(expected_type, writable=writable)
        resolve_flags = _REQUIRED_RESOLVE_FLAGS | (RESOLVE_NO_XDEV if enforce_no_xdev else 0)
        root_device = os.fstat(root.directory_fd).st_dev
        fd = _openat2(root.directory_fd, relative_path, flags, resolve_flags)
        if fd is None:
            return _open_relative_fallback(
                root.directory_fd,
                components,
                flags,
                expected_type,
                root_device=root_device,
                enforce_no_xdev=enforce_no_xdev,
            )
        try:
            _validate_opened_fd(fd, expected_type, root_device=root_device, enforce_no_xdev=enforce_no_xdev)
            os.set_inheritable(fd, False)
            return fd
        except BaseException:
            os.close(fd)
            raise

    def list_directory(self, kind: RootKind, relative_path: str = "") -> list[DirectoryEntry]:
        """List regular-file and directory children without following symlinks."""
        self._require_linux()
        root = self._require_root(kind)
        directory_fd = self._open_directory_or_root(root, relative_path)
        try:
            prefix = "" if not relative_path else "/".join(_relative_components(relative_path))
            entries: list[DirectoryEntry] = []
            for name in os.listdir(directory_fd):
                try:
                    metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if stat.S_ISREG(metadata.st_mode):
                    is_directory = False
                    size: int | None = metadata.st_size
                elif stat.S_ISDIR(metadata.st_mode):
                    is_directory = True
                    size = None
                else:
                    continue
                entries.append(
                    DirectoryEntry(
                        name=name,
                        relative_path=f"{prefix}/{name}" if prefix else name,
                        is_directory=is_directory,
                        size=size,
                        mtime=metadata.st_mtime,
                    )
                )
            return sorted(entries, key=lambda item: (not item.is_directory, item.name.lower()))
        finally:
            os.close(directory_fd)

    def mkdirs(self, kind: RootKind, relative_path: str) -> None:
        """Create a directory tree via verified parent descriptors only."""
        self._require_linux()
        root = self._require_root(kind, writable=True)
        components = _relative_components(relative_path)
        current_fd = _duplicate_directory_fd(root.directory_fd)
        root_device = os.fstat(root.directory_fd).st_dev
        try:
            for component in components:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=current_fd)
                except FileExistsError:
                    pass
                next_fd = _open_child_directory(current_fd, component, root_device=root_device)
                os.close(current_fd)
                current_fd = next_fd
        finally:
            os.close(current_fd)

    def begin_atomic_replace(
        self,
        kind: RootKind,
        relative_path: str,
        *,
        overwrite: bool = True,
    ) -> AtomicFileWriter:
        """Create an FD-relative temporary file for a bounded streaming write.

        The returned writer owns its parent and temporary descriptors. Callers
        must call ``commit()`` on success or ``abort()`` in every failure or
        cancellation path; both paths close descriptors and leave no pathname
        authorization escape hatch.
        """
        self._require_linux()
        if not isinstance(overwrite, bool):
            raise TypeError("overwrite must be a bool")
        root = self._require_root(kind, writable=True)
        parent_fd, leaf, root_device = self._open_parent(root, relative_path, create_parents=True)
        try:
            try:
                existing = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                existing = None
            if existing is not None:
                if stat.S_ISDIR(existing.st_mode):
                    raise IsADirectoryError(leaf)
                _validate_stat(
                    existing,
                    ExpectedType.REGULAR_FILE,
                    root_device=root_device,
                    enforce_no_xdev=True,
                )
                if not overwrite:
                    raise FileExistsError(leaf)
            temporary_name = f".{leaf}.{secrets.token_hex(16)}.upload"
            temporary_fd = os.open(
                temporary_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | _close_on_exec_flag(),
                0o600,
                dir_fd=parent_fd,
            )
            try:
                os.set_inheritable(temporary_fd, False)
                return AtomicFileWriter(
                    parent_fd=parent_fd,
                    temporary_fd=temporary_fd,
                    temporary_name=temporary_name,
                    leaf=leaf,
                    relative_path="/".join(_relative_components(relative_path)),
                    root_device=root_device,
                )
            except BaseException:
                os.close(temporary_fd)
                raise
        except BaseException:
            os.close(parent_fd)
            raise

    def open_append_file(self, kind: RootKind, relative_path: str) -> int:
        """Open one root-contained regular file for append, creating it safely.

        The returned descriptor is non-inheritable. Callers that deliberately
        hand it to a child must duplicate it and explicitly pass that duplicate
        through the child-launch API.
        """
        self._require_linux()
        root = self._require_root(kind, writable=True)
        parent_fd, leaf, root_device = self._open_parent(root, relative_path, create_parents=True)
        try:
            fd = os.open(
                leaf,
                os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | _close_on_exec_flag(),
                0o600,
                dir_fd=parent_fd,
            )
            try:
                _validate_opened_fd(
                    fd,
                    ExpectedType.REGULAR_FILE,
                    root_device=root_device,
                    enforce_no_xdev=True,
                )
                os.set_inheritable(fd, False)
                return fd
            except BaseException:
                os.close(fd)
                raise
        finally:
            os.close(parent_fd)

    def replace_bytes(
        self,
        kind: RootKind,
        relative_path: str,
        data: bytes,
        *,
        overwrite: bool = True,
    ) -> DirectoryEntry:
        """Atomically replace a regular file using a sibling FD-relative temp file."""
        if not isinstance(data, bytes):
            raise TypeError("data must be bytes")
        writer = self.begin_atomic_replace(kind, relative_path, overwrite=overwrite)
        try:
            writer.write(data)
            return writer.commit()
        except BaseException:
            writer.abort()
            raise

    def remove(self, kind: RootKind, relative_path: str, *, recursive: bool = False) -> None:
        """Remove one non-root entry, recursively only through verified directories."""
        self._require_linux()
        root = self._require_root(kind, writable=True)
        parent_fd, leaf, root_device = self._open_parent(root, relative_path, create_parents=False)
        try:
            metadata = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
            if stat.S_ISDIR(metadata.st_mode):
                _validate_stat(metadata, ExpectedType.DIRECTORY, root_device=root_device, enforce_no_xdev=True)
                if recursive:
                    child_fd = _open_child_directory(parent_fd, leaf, root_device=root_device)
                    try:
                        _remove_tree(child_fd, root_device=root_device)
                    finally:
                        os.close(child_fd)
                os.rmdir(leaf, dir_fd=parent_fd)
            else:
                _validate_stat(metadata, ExpectedType.REGULAR_FILE, root_device=root_device, enforce_no_xdev=True)
                os.unlink(leaf, dir_fd=parent_fd)
        finally:
            os.close(parent_fd)

    def remove_tree_for_cleanup(self, kind: RootKind, relative_path: str) -> None:
        """Remove one lifecycle-owned tree without following filesystem links."""
        self._require_linux()
        root = self._require_root(kind, writable=True)
        try:
            parent_fd, leaf, root_device = self._open_parent(
                root,
                relative_path,
                create_parents=False,
            )
        except FileNotFoundError:
            return
        try:
            try:
                metadata = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                return
            if not stat.S_ISDIR(metadata.st_mode):
                os.unlink(leaf, dir_fd=parent_fd)
                return
            _validate_stat(
                metadata,
                ExpectedType.DIRECTORY,
                root_device=root_device,
                enforce_no_xdev=True,
            )
            try:
                child_fd = _open_child_directory(parent_fd, leaf, root_device=root_device)
            except FileNotFoundError:
                return
            try:
                opened = os.fstat(child_fd)
                _remove_tree_for_cleanup(child_fd, root_device=root_device)
                try:
                    current = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
                except FileNotFoundError:
                    return
                if (
                    not stat.S_ISDIR(current.st_mode)
                    or current.st_dev != opened.st_dev
                    or current.st_ino != opened.st_ino
                ):
                    raise RuntimeError("lifecycle cleanup target changed during removal")
            finally:
                os.close(child_fd)
            try:
                os.rmdir(leaf, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
        finally:
            os.close(parent_fd)

    def rename(self, kind: RootKind, source: str, destination: str, *, overwrite: bool = False) -> None:
        """Rename one entry within the same controlled root using verified parents."""
        self._require_linux()
        root = self._require_root(kind, writable=True)
        source_parent_fd, source_leaf, root_device = self._open_parent(root, source, create_parents=False)
        destination_parent_fd, destination_leaf, _ = self._open_parent(root, destination, create_parents=False)
        try:
            source_metadata = os.stat(source_leaf, dir_fd=source_parent_fd, follow_symlinks=False)
            if stat.S_ISDIR(source_metadata.st_mode):
                _validate_stat(source_metadata, ExpectedType.DIRECTORY, root_device=root_device, enforce_no_xdev=True)
            else:
                _validate_stat(source_metadata, ExpectedType.REGULAR_FILE, root_device=root_device, enforce_no_xdev=True)
            try:
                os.stat(destination_leaf, dir_fd=destination_parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                if not overwrite:
                    raise FileExistsError(destination_leaf)
            os.rename(source_leaf, destination_leaf, src_dir_fd=source_parent_fd, dst_dir_fd=destination_parent_fd)
        finally:
            os.close(source_parent_fd)
            os.close(destination_parent_fd)

    def close(self) -> None:
        """Close all root descriptors once; repeated cleanup is safe."""
        if self._closed:
            return
        self._closed = True
        for root in self._roots.values():
            try:
                os.close(root.directory_fd)
            except OSError:
                pass

    def _require_linux(self) -> None:
        if sys.platform != "linux":
            raise RuntimeError("safe descriptor-relative operations require Linux")

    def _require_root(self, kind: RootKind, *, writable: bool = False) -> ControlledRoot:
        if not isinstance(kind, RootKind):
            raise TypeError("kind must be a RootKind")
        if self._closed:
            raise RuntimeError("controlled roots are closed")
        root = self.get(kind)
        if writable and not root.writable:
            raise PermissionError(f"controlled root {kind.value} is read-only")
        return root

    def _open_directory_or_root(self, root: ControlledRoot, relative_path: str) -> int:
        if not relative_path:
            return _duplicate_directory_fd(root.directory_fd)
        return self.open_relative(root.kind, relative_path, expected_type=ExpectedType.DIRECTORY)

    def _open_parent(self, root: ControlledRoot, relative_path: str, *, create_parents: bool) -> tuple[int, str, int]:
        components = _relative_components(relative_path)
        root_device = os.fstat(root.directory_fd).st_dev
        current_fd = _duplicate_directory_fd(root.directory_fd)
        try:
            for component in components[:-1]:
                if create_parents:
                    try:
                        os.mkdir(component, mode=0o700, dir_fd=current_fd)
                    except FileExistsError:
                        pass
                next_fd = _open_child_directory(current_fd, component, root_device=root_device)
                os.close(current_fd)
                current_fd = next_fd
            return current_fd, components[-1], root_device
        except BaseException:
            os.close(current_fd)
            raise


def _application_root() -> Path:
    """Return the trusted application root, independent of runtime inputs."""
    return Path(__file__).resolve().parent.parent


def _directory_open_flags() -> int:
    """Return the minimum flags required for directory-root capabilities."""
    if not hasattr(os, "O_DIRECTORY"):
        raise RuntimeError("controlled root descriptors require O_DIRECTORY support")
    return os.O_RDONLY | os.O_DIRECTORY | _close_on_exec_flag()


def _close_on_exec_flag() -> int:
    return getattr(os, "O_CLOEXEC", 0)


def _object_open_flags(expected_type: ExpectedType, *, writable: bool) -> int:
    """Return object-open flags that cannot create or follow a symlink."""
    if not hasattr(os, "O_NOFOLLOW"):
        raise RuntimeError("safe descriptor-relative operations require O_NOFOLLOW")
    flags = (os.O_RDWR if writable else os.O_RDONLY) | os.O_NOFOLLOW | _close_on_exec_flag()
    if expected_type is ExpectedType.DIRECTORY:
        if not hasattr(os, "O_DIRECTORY"):
            raise RuntimeError("safe descriptor-relative operations require O_DIRECTORY")
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    return flags


def _relative_components(relative_path: str) -> tuple[str, ...]:
    """Validate a POSIX relative path without resolving it against any pathname."""
    if not isinstance(relative_path, str):
        raise TypeError("relative_path must be a str")
    if not relative_path or "\x00" in relative_path or relative_path.startswith("/"):
        raise ValueError("relative_path must be a non-empty relative path")
    components = tuple(relative_path.split("/"))
    if any(component in {"", ".", ".."} for component in components):
        raise ValueError("relative_path must not contain empty, dot, or parent components")
    return components


def _open_directory(path: Path) -> int:
    """Open, validate, and make a directory FD non-inheritable."""
    fd = os.open(path, _directory_open_flags())
    try:
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            raise RuntimeError(f"controlled root is not a directory: {path}")
        os.set_inheritable(fd, False)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _duplicate_directory_fd(fd: int) -> int:
    duplicate = os.dup(fd)
    try:
        if not stat.S_ISDIR(os.fstat(duplicate).st_mode):
            raise RuntimeError("controlled root descriptor is not a directory")
        os.set_inheritable(duplicate, False)
        return duplicate
    except BaseException:
        os.close(duplicate)
        raise


def _open_child_directory(parent_fd: int, name: str, *, root_device: int) -> int:
    fd = os.open(name, _directory_open_flags() | os.O_NOFOLLOW, dir_fd=parent_fd)
    try:
        _validate_opened_fd(fd, ExpectedType.DIRECTORY, root_device=root_device, enforce_no_xdev=True)
        os.set_inheritable(fd, False)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _openat2(directory_fd: int, relative_path: str, flags: int, resolve_flags: int) -> int | None:
    """Use Linux ``openat2`` or return ``None`` only when it is unavailable."""
    syscall_number = _OPENAT2_SYSCALLS.get(platform.machine().lower())
    if syscall_number is None:
        return None
    try:
        libc = ctypes.CDLL(None, use_errno=True)
    except OSError:
        return None
    pathname = os.fsencode(relative_path)
    how = _OpenHow(flags=flags, mode=0, resolve=resolve_flags)
    libc.syscall.restype = ctypes.c_long
    result = libc.syscall(
        ctypes.c_long(syscall_number),
        ctypes.c_int(directory_fd),
        ctypes.c_char_p(pathname),
        ctypes.byref(how),
        ctypes.c_size_t(ctypes.sizeof(how)),
    )
    if result >= 0:
        return int(result)
    error = ctypes.get_errno()
    if error == errno.ENOSYS:
        return None
    raise OSError(error, os.strerror(error), relative_path)


def _fallback_supported() -> bool:
    """Return whether the required descriptor-only component walk is available."""
    return hasattr(os, "O_DIRECTORY") and hasattr(os, "O_NOFOLLOW") and os.open in os.supports_dir_fd


def _open_relative_fallback(
    root_fd: int,
    components: tuple[str, ...],
    final_flags: int,
    expected_type: ExpectedType,
    *,
    root_device: int,
    enforce_no_xdev: bool,
) -> int:
    """Open a validated relative path with descriptor-only component traversal."""
    if not _fallback_supported():
        raise RuntimeError("safe descriptor-relative fallback is unavailable")
    current_fd = _duplicate_directory_fd(root_fd)
    try:
        for component in components[:-1]:
            next_fd = _open_child_directory(current_fd, component, root_device=root_device)
            os.close(current_fd)
            current_fd = next_fd
        final_fd = os.open(components[-1], final_flags, dir_fd=current_fd)
        try:
            _validate_opened_fd(final_fd, expected_type, root_device=root_device, enforce_no_xdev=enforce_no_xdev)
            os.set_inheritable(final_fd, False)
            return final_fd
        except BaseException:
            os.close(final_fd)
            raise
    finally:
        os.close(current_fd)


def _validate_opened_fd(
    fd: int,
    expected_type: ExpectedType,
    *,
    root_device: int,
    enforce_no_xdev: bool,
) -> None:
    _validate_stat(os.fstat(fd), expected_type, root_device=root_device, enforce_no_xdev=enforce_no_xdev)


def _validate_stat(metadata: os.stat_result, expected_type: ExpectedType, *, root_device: int, enforce_no_xdev: bool) -> None:
    matches_type = stat.S_ISREG(metadata.st_mode) if expected_type is ExpectedType.REGULAR_FILE else stat.S_ISDIR(metadata.st_mode)
    if not matches_type:
        raise RuntimeError(f"opened object does not match expected {expected_type.value}")
    # A second hard link can name the same inode outside the controlled root.
    # There is no portable dirfd-based proof that every link is root-contained,
    # so owner-facing regular-file operations reject multiply-linked inodes.
    if expected_type is ExpectedType.REGULAR_FILE and getattr(metadata, "st_nlink", 1) != 1:
        raise RuntimeError("refusing a multiply-linked regular file")
    if enforce_no_xdev and metadata.st_dev != root_device:
        raise RuntimeError("opened object crosses the controlled root filesystem boundary")


def _remove_tree(directory_fd: int, *, root_device: int) -> None:
    for name in os.listdir(directory_fd):
        metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISDIR(metadata.st_mode):
            _validate_stat(metadata, ExpectedType.DIRECTORY, root_device=root_device, enforce_no_xdev=True)
            child_fd = _open_child_directory(directory_fd, name, root_device=root_device)
            try:
                _remove_tree(child_fd, root_device=root_device)
            finally:
                os.close(child_fd)
            os.rmdir(name, dir_fd=directory_fd)
        elif stat.S_ISREG(metadata.st_mode):
            _validate_stat(metadata, ExpectedType.REGULAR_FILE, root_device=root_device, enforce_no_xdev=True)
            os.unlink(name, dir_fd=directory_fd)
        else:
            raise RuntimeError("refusing to recursively remove a special filesystem entry")


def _remove_tree_for_cleanup(directory_fd: int, *, root_device: int) -> None:
    for name in os.listdir(directory_fd):
        try:
            metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(metadata.st_mode):
            try:
                os.unlink(name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
            continue
        _validate_stat(
            metadata,
            ExpectedType.DIRECTORY,
            root_device=root_device,
            enforce_no_xdev=True,
        )
        try:
            child_fd = _open_child_directory(directory_fd, name, root_device=root_device)
        except FileNotFoundError:
            continue
        try:
            opened = os.fstat(child_fd)
            _remove_tree_for_cleanup(child_fd, root_device=root_device)
            try:
                current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if (
                not stat.S_ISDIR(current.st_mode)
                or current.st_dev != opened.st_dev
                or current.st_ino != opened.st_ino
            ):
                raise RuntimeError("lifecycle cleanup target changed during removal")
        finally:
            os.close(child_fd)
        try:
            os.rmdir(name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def controlled_roots_for(runtime_paths: OwnerWorkerRuntimePaths) -> ControlledRoots:
    """Open the fixed, trusted root descriptors for a validated owner runtime."""
    specifications = (
        (RootKind.GLOBAL_READONLY, _application_root(), False),
        (RootKind.OWNER_WRITABLE, runtime_paths.owner_home, True),
        (RootKind.WORKSPACE, runtime_paths.workspace_root, True),
        (RootKind.TEMPORARY, runtime_paths.paths["temporary_root"], True),
    )
    opened: dict[RootKind, ControlledRoot] = {}
    try:
        for kind, path, writable in specifications:
            canonical_path = Path(path).resolve(strict=True)
            opened[kind] = ControlledRoot(kind, _open_directory(canonical_path), writable, canonical_path)
        return ControlledRoots(opened)
    except BaseException:
        for root in opened.values():
            try:
                os.close(root.directory_fd)
            except OSError:
                pass
        raise
