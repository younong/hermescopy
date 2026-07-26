"""Import-light runtime contract shared by Session Reader parent and child."""
from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

FORBIDDEN_OWNER_WORKER_ENV_KEYS: tuple[str, ...] = (
    "HERMES_PROFILE",
    "HERMES_SESSION_PROFILE",
    "HERMES_CONFIG",
    "HERMES_ENV",
    "TERMINAL_CWD",
)

_REQUIRED_SESSION_READER_ENV_KEYS: tuple[str, ...] = (
    "HERMES_HOME",
    "HERMES_OWNER_KEY",
    "HERMES_CONTROL_HOME",
    "HERMES_READER_GENERATION",
    "HERMES_READER_ID",
    "HERMES_READER_LEASE_VERSION",
    "HERMES_READER_RECOVERY_GENERATION",
    "HERMES_SESSION_READER_CAPABILITY_ISSUER",
    "HERMES_SESSION_READER_CAPABILITY_PUBLIC_KEY",
    "HERMES_SESSION_READER_CAPABILITY_RETAINED_PUBLIC_KEYS",
)


@dataclass(frozen=True)
class SessionReaderRuntimePaths:
    """Exact owner-local paths available to one read-only Session Reader."""

    owner_home: Path
    reader_runtime_dir: Path
    reader_socket: Path
    state_db: Path


class SessionReaderRuntimePreparationError(RuntimeError):
    """Safe, fixed classification for one rejected Reader runtime component."""

    stage = "runtime_prepare"

    def __init__(self, code: str, component: str) -> None:
        self.code = str(code)
        self.component = str(component)
        super().__init__(f"session reader runtime preparation failed: {self.code}")


def session_reader_runtime_dir(owner_home: str | Path, reader_generation: int) -> Path:
    """Return the canonical runtime directory for one Session Reader generation."""
    home = Path(owner_home).expanduser().resolve()
    generation = int(reader_generation)
    if generation < 1:
        raise ValueError("reader_generation must be positive")
    # Keep the authenticated owner-local socket below AF_UNIX's 104-byte
    # portable limit even for the production owner-home shape.
    return home / "runtime" / "r" / str(generation)


def session_reader_socket_path(owner_home: str | Path, reader_generation: int) -> Path:
    """Return the sole authenticated Reader socket for a generation."""
    return session_reader_runtime_dir(owner_home, reader_generation) / "s"


def _directory_identity(info: os.stat_result) -> tuple[int, int, int]:
    return info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)


def _preparation_error(code: str, component: str) -> SessionReaderRuntimePreparationError:
    return SessionReaderRuntimePreparationError(code, component)


def _prepare_private_directory(
    path: Path,
    *,
    parent_device: int,
    component: str,
) -> os.stat_result:
    try:
        before = path.lstat()
    except FileNotFoundError:
        try:
            path.mkdir(mode=0o700)
        except OSError as exc:
            raise _preparation_error("create_failed", component) from exc
        before = path.lstat()
    if stat.S_ISLNK(before.st_mode):
        raise _preparation_error("symlink", component)
    if not stat.S_ISDIR(before.st_mode):
        raise _preparation_error("not_directory", component)
    if hasattr(os, "getuid") and before.st_uid != os.getuid():
        raise _preparation_error("wrong_owner", component)
    if before.st_dev != parent_device:
        raise _preparation_error("wrong_device", component)

    if os.name == "nt":
        after = path.lstat()
        if _directory_identity(before) != _directory_identity(after):
            raise _preparation_error("identity_changed", component)
        return after

    flags = os.O_RDONLY | os.O_DIRECTORY
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise _preparation_error("open_failed", component) from exc
    try:
        opened = os.fstat(descriptor)
        if _directory_identity(before) != _directory_identity(opened):
            raise _preparation_error("identity_changed", component)
        if opened.st_uid != os.getuid():
            raise _preparation_error("wrong_owner", component)
        try:
            os.fchmod(descriptor, 0o700)
        except OSError as exc:
            raise _preparation_error("harden_failed", component) from exc
        hardened = os.fstat(descriptor)
        if stat.S_IMODE(hardened.st_mode) != 0o700:
            raise _preparation_error("harden_failed", component)
    finally:
        os.close(descriptor)

    after = path.lstat()
    if _directory_identity(before) != _directory_identity(after):
        raise _preparation_error("identity_changed", component)
    if stat.S_IMODE(after.st_mode) != 0o700:
        raise _preparation_error("harden_failed", component)
    return after


def prepare_session_reader_runtime(
    owner_home: str | Path,
    reader_generation: int,
) -> SessionReaderRuntimePaths:
    """Create only the owner-local directories required by one Reader."""
    home = Path(owner_home).expanduser().resolve()
    home_info = home.lstat()
    if not stat.S_ISDIR(home_info.st_mode):
        raise RuntimeError("session reader owner home must be a directory")
    paths = session_reader_runtime_paths(
        owner_home=home,
        reader_generation=reader_generation,
    )
    runtime = home / "runtime"
    runtime_info = _prepare_private_directory(
        runtime,
        parent_device=home_info.st_dev,
        component="runtime",
    )
    _prepare_private_directory(
        runtime / "logs",
        parent_device=runtime_info.st_dev,
        component="runtime_logs",
    )
    readers = runtime / "r"
    readers_info = _prepare_private_directory(
        readers,
        parent_device=runtime_info.st_dev,
        component="readers_root",
    )
    _prepare_private_directory(
        paths.reader_runtime_dir,
        parent_device=readers_info.st_dev,
        component="generation",
    )
    for label, path in (
        ("runtime", runtime),
        ("runtime_logs", runtime / "logs"),
        ("reader_runtime", paths.reader_runtime_dir),
    ):
        _require_under(path, home, label)
    return paths


def session_reader_env_for(
    *,
    owner_key: str,
    owner_home: str | Path,
    control_home: str | Path,
    reader_generation: int,
    reader_id: str,
    lease_version: int,
    recovery_generation: int,
    capability_issuer: str,
    capability_public_key: str,
    capability_retained_public_keys: str = "{}",
) -> dict[str, str]:
    """Return the exact minimal environment for a Session Reader process."""
    generation = int(reader_generation)
    lease = int(lease_version)
    recovery = int(recovery_generation)
    if generation < 1 or lease < 1 or recovery < 0:
        raise ValueError("reader authority values are invalid")
    if not str(owner_key).strip() or not str(reader_id).strip():
        raise ValueError("owner_key and reader_id are required")
    if not str(capability_issuer).strip() or not str(capability_public_key).strip():
        raise ValueError("reader capability verifier is required")
    return {
        "HERMES_HOME": str(Path(owner_home).expanduser().resolve()),
        "HERMES_OWNER_KEY": str(owner_key),
        "HERMES_CONTROL_HOME": str(Path(control_home).expanduser().resolve()),
        "HERMES_READER_GENERATION": str(generation),
        "HERMES_READER_ID": str(reader_id),
        "HERMES_READER_LEASE_VERSION": str(lease),
        "HERMES_READER_RECOVERY_GENERATION": str(recovery),
        "HERMES_SESSION_READER_CAPABILITY_ISSUER": str(capability_issuer),
        "HERMES_SESSION_READER_CAPABILITY_PUBLIC_KEY": str(capability_public_key),
        "HERMES_SESSION_READER_CAPABILITY_RETAINED_PUBLIC_KEYS": str(
            capability_retained_public_keys or "{}"
        ),
    }


def _required_positive_int(
    source: Mapping[str, str],
    key: str,
    *,
    allow_zero: bool = False,
) -> int:
    value = str(source.get(key, "")).strip()
    try:
        number = int(value)
    except ValueError as exc:
        raise RuntimeError(f"{key} is required and must be an integer") from exc
    if number < 0 or (number == 0 and not allow_zero):
        raise RuntimeError(f"{key} is invalid")
    return number


def _require_under(path: Path, root: Path, label: str) -> None:
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise RuntimeError(f"{label} path {resolved} is outside owner home {root}") from exc


def session_reader_runtime_paths(
    *,
    owner_home: str | Path | None = None,
    reader_generation: int | None = None,
) -> SessionReaderRuntimePaths:
    """Return the canonical minimal Reader paths without opening owner data."""
    home = (
        Path(owner_home).expanduser().resolve()
        if owner_home is not None
        else Path(os.environ["HERMES_HOME"]).expanduser().resolve()
    )
    generation = reader_generation
    if generation is None:
        generation = _required_positive_int(os.environ, "HERMES_READER_GENERATION")
    generation = int(generation)
    if generation < 1:
        raise RuntimeError("HERMES_READER_GENERATION is invalid")
    runtime_dir = session_reader_runtime_dir(home, generation)
    return SessionReaderRuntimePaths(
        owner_home=home,
        reader_runtime_dir=runtime_dir,
        reader_socket=session_reader_socket_path(home, generation),
        state_db=(home / "state.db").resolve(),
    )


def validate_session_reader_runtime_environment(
    *,
    owner_home: str | Path | None = None,
    owner_key: str | None = None,
    reader_generation: int | None = None,
    reader_id: str | None = None,
    socket_path: str | Path | None = None,
    source: Mapping[str, str] | None = None,
) -> SessionReaderRuntimePaths:
    """Fail closed unless a Reader has only its complete minimal authority env."""
    env = source if source is not None else os.environ
    missing = [
        key for key in _REQUIRED_SESSION_READER_ENV_KEYS
        if not str(env.get(key, "")).strip()
    ]
    if missing:
        raise RuntimeError(f"session reader environment is incomplete: {', '.join(missing)}")
    leaked = [
        key
        for key in FORBIDDEN_OWNER_WORKER_ENV_KEYS
        if str(env.get(key, "")).strip()
    ]
    if leaked:
        raise RuntimeError(
            "forbidden session reader environment variables present: "
            + ", ".join(sorted(leaked))
        )
    allowed = set(_REQUIRED_SESSION_READER_ENV_KEYS)
    unknown = sorted(
        key
        for key, value in env.items()
        if key.startswith("HERMES_") and value and key not in allowed
    )
    if unknown:
        raise RuntimeError(
            "unexpected session reader environment variables present: "
            + ", ".join(unknown)
        )

    actual_home = Path(str(env["HERMES_HOME"])).expanduser().resolve()
    expected_home = (
        Path(owner_home).expanduser().resolve() if owner_home is not None else actual_home
    )
    if actual_home != expected_home:
        raise RuntimeError("HERMES_HOME does not match owner_home")
    actual_owner = str(env["HERMES_OWNER_KEY"]).strip()
    if not actual_owner or (
        owner_key is not None and actual_owner != str(owner_key).strip()
    ):
        raise RuntimeError("HERMES_OWNER_KEY does not match owner_key")
    generation = _required_positive_int(env, "HERMES_READER_GENERATION")
    if reader_generation is not None and generation != int(reader_generation):
        raise RuntimeError("HERMES_READER_GENERATION does not match reader_generation")
    _required_positive_int(env, "HERMES_READER_LEASE_VERSION")
    _required_positive_int(env, "HERMES_READER_RECOVERY_GENERATION", allow_zero=True)
    actual_reader = str(env["HERMES_READER_ID"]).strip()
    if not actual_reader or (
        reader_id is not None and actual_reader != str(reader_id).strip()
    ):
        raise RuntimeError("HERMES_READER_ID does not match reader_id")
    paths = session_reader_runtime_paths(
        owner_home=expected_home,
        reader_generation=generation,
    )
    if socket_path is not None and (
        Path(socket_path).expanduser().resolve(strict=False)
        != paths.reader_socket.resolve(strict=False)
    ):
        raise RuntimeError("reader socket does not match owner generation")
    for label, path in (
        ("state_db", paths.state_db),
        ("reader_runtime", paths.reader_runtime_dir),
        ("reader_socket", paths.reader_socket),
    ):
        _require_under(path, paths.owner_home, label)
    return paths
