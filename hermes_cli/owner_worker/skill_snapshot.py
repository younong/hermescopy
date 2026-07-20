"""Generation-scoped, read-only snapshots for owner-local skills."""
from __future__ import annotations

import errno
import hashlib
import os
import shutil
import stat
from pathlib import Path, PurePosixPath

SANDBOX_SKILL_SNAPSHOT_ROOT = PurePosixPath("/executor/skill-snapshots")
_DEFAULT_MAX_BYTES = 16 << 20
_DEFAULT_MAX_FILES = 1024
_MAX_RELATIVE_PATH_BYTES = 1024
_COPY_CHUNK_BYTES = 1 << 20


class SkillSnapshotError(RuntimeError):
    """A selected skill cannot safely cross the executor boundary."""


def _remove_tree(directory: Path) -> None:
    for child in directory.rglob("*"):
        try:
            child.chmod(0o700 if child.is_dir() else 0o600)
        except OSError:
            pass
    try:
        directory.chmod(0o700)
    except OSError:
        pass
    shutil.rmtree(directory, ignore_errors=True)


def _is_matching_snapshot(
    destination: Path,
    *,
    content_id: str,
    max_bytes: int,
    max_files: int,
) -> bool:
    if destination.is_symlink() or not destination.is_dir():
        return False
    try:
        _existing_files, existing_content_id = _relative_files(
            destination,
            max_bytes=max_bytes,
            max_files=max_files,
        )
    except SkillSnapshotError:
        return False
    return existing_content_id == content_id


def _relative_files(source: Path, *, max_bytes: int, max_files: int) -> tuple[list[Path], str]:
    if source.is_symlink() or not source.is_dir():
        raise SkillSnapshotError("skill package is not a regular directory")
    source = source.resolve(strict=True)
    files: list[Path] = []
    digest = hashlib.sha256()
    total_bytes = 0
    for root, directories, names in os.walk(source, topdown=True, followlinks=False):
        root_path = Path(root)
        for entry_name in sorted((*directories, *names)):
            entry = root_path / entry_name
            if entry.is_symlink():
                raise SkillSnapshotError("skill package contains a symbolic link")
        directories.sort()
        names.sort()
        for name in names:
            entry = root_path / name
            try:
                metadata = entry.lstat()
                relative = entry.relative_to(source)
            except (OSError, ValueError) as exc:
                raise SkillSnapshotError("skill package changed during validation") from exc
            if not stat.S_ISREG(metadata.st_mode):
                raise SkillSnapshotError("skill package contains a non-regular file")
            encoded_relative = relative.as_posix().encode("utf-8")
            if not encoded_relative or len(encoded_relative) > _MAX_RELATIVE_PATH_BYTES:
                raise SkillSnapshotError("skill package contains an invalid path")
            files.append(relative)
            if len(files) > max_files:
                raise SkillSnapshotError("skill package contains too many files")
            total_bytes += metadata.st_size
            if total_bytes > max_bytes:
                raise SkillSnapshotError("skill package is too large")
            digest.update(len(encoded_relative).to_bytes(4, "big"))
            digest.update(encoded_relative)
            digest.update(metadata.st_size.to_bytes(8, "big"))
            descriptor = -1
            try:
                descriptor = os.open(entry, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                opened = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or opened.st_dev != metadata.st_dev
                    or opened.st_ino != metadata.st_ino
                    or opened.st_size != metadata.st_size
                ):
                    raise SkillSnapshotError("skill package changed during validation")
                while True:
                    chunk = os.read(descriptor, _COPY_CHUNK_BYTES)
                    if not chunk:
                        break
                    digest.update(chunk)
            except OSError as exc:
                raise SkillSnapshotError("skill package could not be read safely") from exc
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
    return files, digest.hexdigest()


def materialize_skill_snapshot(
    source: str | Path,
    runtime_home: str | Path,
    *,
    max_bytes: int = _DEFAULT_MAX_BYTES,
    max_files: int = _DEFAULT_MAX_FILES,
) -> str:
    """Copy one selected skill into its executor generation and return its sandbox path."""
    if not isinstance(max_bytes, int) or max_bytes < 1 or not isinstance(max_files, int) or max_files < 1:
        raise SkillSnapshotError("skill snapshot limits are invalid")
    source_path = Path(source)
    files, content_id = _relative_files(
        source_path,
        max_bytes=max_bytes,
        max_files=max_files,
    )
    source_path = source_path.resolve(strict=True)
    snapshot_root = Path(runtime_home).resolve() / SANDBOX_SKILL_SNAPSHOT_ROOT.name
    destination = snapshot_root / content_id
    if _is_matching_snapshot(
        destination,
        content_id=content_id,
        max_bytes=max_bytes,
        max_files=max_files,
    ):
        return str(SANDBOX_SKILL_SNAPSHOT_ROOT / content_id)
    if destination.exists() or destination.is_symlink():
        raise SkillSnapshotError("skill snapshot destination is invalid")

    snapshot_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = snapshot_root / f".{content_id}.{os.urandom(8).hex()}.tmp"
    try:
        temporary.mkdir(mode=0o700)
        copied_bytes = 0
        for relative in files:
            source_file = source_path / relative
            target_file = temporary / relative
            target_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            source_fd = target_fd = -1
            try:
                metadata = source_file.lstat()
                if source_file.is_symlink() or not stat.S_ISREG(metadata.st_mode):
                    raise SkillSnapshotError("skill package changed during materialization")
                source_fd = os.open(source_file, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                opened = os.fstat(source_fd)
                if (
                    opened.st_dev != metadata.st_dev
                    or opened.st_ino != metadata.st_ino
                    or opened.st_size != metadata.st_size
                ):
                    raise SkillSnapshotError("skill package changed during materialization")
                target_fd = os.open(target_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
                while True:
                    chunk = os.read(source_fd, _COPY_CHUNK_BYTES)
                    if not chunk:
                        break
                    copied_bytes += len(chunk)
                    if copied_bytes > max_bytes:
                        raise SkillSnapshotError("skill package is too large")
                    os.write(target_fd, chunk)
                final = os.fstat(source_fd)
                if final.st_size != opened.st_size:
                    raise SkillSnapshotError("skill package changed during materialization")
            except OSError as exc:
                raise SkillSnapshotError("skill package could not be materialized safely") from exc
            finally:
                if target_fd >= 0:
                    os.close(target_fd)
                if source_fd >= 0:
                    os.close(source_fd)
        _copied_files, copied_content_id = _relative_files(
            temporary,
            max_bytes=max_bytes,
            max_files=max_files,
        )
        if copied_content_id != content_id:
            raise SkillSnapshotError("skill package changed during materialization")
        for child in temporary.rglob("*"):
            child.chmod(0o555 if child.is_dir() else 0o444)
        try:
            temporary.rename(destination)
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EEXIST, errno.ENOTEMPTY} or not _is_matching_snapshot(
                destination,
                content_id=content_id,
                max_bytes=max_bytes,
                max_files=max_files,
            ):
                raise SkillSnapshotError("skill snapshot could not be published safely") from exc
        else:
            destination.chmod(0o555)
    finally:
        if temporary.exists():
            _remove_tree(temporary)
    if not destination.is_dir() or destination.is_symlink():
        raise SkillSnapshotError("skill snapshot is unavailable")
    return str(SANDBOX_SKILL_SNAPSHOT_ROOT / content_id)
