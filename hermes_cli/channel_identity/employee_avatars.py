"""Private image storage for owner-scoped employee avatars."""

from __future__ import annotations

import hashlib
import io
import os
import stat
import tempfile
import warnings
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from .store import ChannelIdentityStore

MAX_AVATAR_UPLOAD_BYTES = 5 * 1024 * 1024
_MAX_AVATAR_PIXELS = 25_000_000
_AVATAR_SIZE = (512, 512)
_ALLOWED_FORMATS = frozenset({"JPEG", "PNG", "WEBP"})


class EmployeeAvatarInvalid(ValueError):
    """An uploaded employee avatar is not a supported bounded image."""


def _avatar_directory(store: ChannelIdentityStore) -> Path:
    directory = store.control_home / "employee-avatars"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink() or not directory.is_dir():
        raise RuntimeError("employee avatar directory must be a real directory")
    if os.name != "nt":
        directory.chmod(0o700)
    return directory


def employee_avatar_path(store: ChannelIdentityStore, employee_id: str) -> Path:
    digest = hashlib.sha256(str(employee_id).encode("utf-8")).hexdigest()
    return _avatar_directory(store) / f"{digest}.webp"


def employee_avatar_version(store: ChannelIdentityStore, employee_id: str) -> int | None:
    target = employee_avatar_path(store, employee_id)
    try:
        metadata = target.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        return None
    return metadata.st_mtime_ns


def normalize_employee_avatar(data: bytes) -> bytes:
    if not data or len(data) > MAX_AVATAR_UPLOAD_BYTES:
        raise EmployeeAvatarInvalid("employee avatar is invalid")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as source:
                if source.format not in _ALLOWED_FORMATS:
                    raise EmployeeAvatarInvalid("employee avatar is invalid")
                width, height = source.size
                if width < 1 or height < 1 or width * height > _MAX_AVATAR_PIXELS:
                    raise EmployeeAvatarInvalid("employee avatar is invalid")
                source.load()
                image = ImageOps.exif_transpose(source)
                image.thumbnail(_AVATAR_SIZE, Image.Resampling.LANCZOS)
                if image.mode not in {"RGB", "RGBA"}:
                    image = image.convert("RGBA" if "transparency" in image.info else "RGB")
                output = io.BytesIO()
                image.save(output, format="WEBP", quality=88, method=6)
                return output.getvalue()
    except EmployeeAvatarInvalid:
        raise
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        UnidentifiedImageError,
        OSError,
        ValueError,
    ) as exc:
        raise EmployeeAvatarInvalid("employee avatar is invalid") from exc


def save_employee_avatar(
    store: ChannelIdentityStore,
    employee_id: str,
    data: bytes,
) -> Path:
    _reject_builtin_avatar_mutation(store, employee_id)
    normalized = normalize_employee_avatar(data)
    target = employee_avatar_path(store, employee_id)
    fd, temporary_name = tempfile.mkstemp(prefix=".avatar-", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(normalized)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name != "nt":
            temporary.chmod(0o600)
        os.replace(temporary, target)
        if os.name != "nt":
            target.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def _reject_builtin_avatar_mutation(
    store: ChannelIdentityStore, employee_id: str
) -> None:
    with store.read() as conn:
        row = conn.execute(
            "SELECT employee_kind FROM employees WHERE employee_id=?",
            (employee_id,),
        ).fetchone()
    if row is not None and row["employee_kind"] == "builtin_assistant":
        from .employees import BuiltinEmployeeProtected

        raise BuiltinEmployeeProtected("built-in assistant employee is protected")


def delete_employee_avatar(store: ChannelIdentityStore, employee_id: str) -> bool:
    _reject_builtin_avatar_mutation(store, employee_id)
    target = employee_avatar_path(store, employee_id)
    if target.is_symlink():
        raise RuntimeError("employee avatar must be a regular file")
    try:
        target.unlink()
    except FileNotFoundError:
        return False
    return True
