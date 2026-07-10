"""Owner-worker runtime path helpers.

Small, import-light helpers used by authenticated owner workers and subprocess
spawners.  They intentionally read environment variables at call time so a
fresh worker process can set HERMES_HOME before importing owner-sensitive code.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Mapping, MutableMapping

from hermes_constants import get_hermes_home

REQUIRED_OWNER_DIRS: tuple[Path, ...] = (
    Path("runtime"),
    Path("runtime") / "logs",
    Path("runtime") / "checkpoints",
    Path("logs"),
    Path("checkpoints"),
    Path("sessions"),
    Path("workspaces") / "default",
    Path("skills"),
    Path("memories"),
)

OWNER_ENV_KEYS: tuple[str, ...] = (
    "HERMES_HOME",
    "HERMES_OWNER_KEY",
    "HERMES_TENANT_ID",
    "HERMES_OWNER_USER_ID",
    "HERMES_AUTH_PROVIDER",
    "HERMES_WORKSPACE_ROOT",
)

FORBIDDEN_OWNER_WORKER_ENV_KEYS: tuple[str, ...] = (
    "HERMES_PROFILE",
    "HERMES_SESSION_PROFILE",
    "TERMINAL_CWD",
)


def owner_worker_env_values(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return owner-scoping env vars present in *source* or ``os.environ``."""
    src = source if source is not None else os.environ
    return {key: str(src[key]) for key in OWNER_ENV_KEYS if src.get(key)}


def propagate_owner_env(env: MutableMapping[str, str], source: Mapping[str, str] | None = None) -> None:
    """Copy explicit owner worker env vars into a child process environment."""
    env.update(owner_worker_env_values(source))


def is_owner_worker_env(source: Mapping[str, str] | None = None) -> bool:
    src = source if source is not None else os.environ
    return bool(str(src.get("HERMES_OWNER_KEY", "")).strip())


def _chmod_private(path: Path, mode: int = 0o700) -> None:
    """Best-effort owner-only permissions for POSIX filesystems."""
    if os.name == "nt":
        return
    try:
        path.chmod(mode)
    except OSError:
        pass


def ensure_owner_runtime_dirs(owner_home: str | Path | None = None) -> Path:
    """Create the canonical owner-local runtime/data directories."""
    home = Path(owner_home).expanduser().resolve() if owner_home is not None else get_hermes_home().expanduser().resolve()
    home.mkdir(parents=True, exist_ok=True)
    _chmod_private(home)
    for rel in REQUIRED_OWNER_DIRS:
        path = home / rel
        path.mkdir(parents=True, exist_ok=True)
        _require_under(path, home, str(rel))
        current = path
        while current != home:
            _chmod_private(current)
            current = current.parent
    return home


def owner_worker_env_for(
    *,
    owner_key: str,
    owner_home: str | Path,
    tenant_id: str = "",
    owner_user_id: str = "",
    auth_provider: str = "",
    control_home: str | Path | None = None,
) -> dict[str, str]:
    """Return the canonical owner-worker environment values."""
    home = Path(owner_home).expanduser().resolve()
    env = {
        "HERMES_HOME": str(home),
        "HERMES_OWNER_KEY": owner_key,
        "HERMES_WORKSPACE_ROOT": str(home / "workspaces"),
    }
    if tenant_id:
        env["HERMES_TENANT_ID"] = str(tenant_id)
    if owner_user_id:
        env["HERMES_OWNER_USER_ID"] = str(owner_user_id)
    if auth_provider:
        env["HERMES_AUTH_PROVIDER"] = str(auth_provider)
    if control_home:
        env["HERMES_CONTROL_HOME"] = str(Path(control_home).expanduser().resolve())
    return env


def get_runtime_owner_home() -> Path:
    """Return the worker-local runtime view of the current owner home.

    This is deliberately environment-derived. It may equal the Control Plane's
    host owner path in a local-process deployment, but callers must not use that
    equality as proof of the authenticated owner identity.
    """

    return get_hermes_home().expanduser().resolve()


def get_workspace_root(*, create: bool = False) -> Path:
    """Return the workspace root for the current owner worker.

    ``HERMES_WORKSPACE_ROOT`` wins when set; otherwise owner-worker mode defaults
    to ``<HERMES_HOME>/workspaces``.  Local legacy mode gets the same dynamic
    default but callers typically only enforce sandboxing when HERMES_OWNER_KEY
    is present.
    """
    raw = os.environ.get("HERMES_WORKSPACE_ROOT", "").strip()
    root = Path(raw) if raw else get_runtime_owner_home() / "workspaces"
    root = root.expanduser().resolve()
    if create:
        root.mkdir(parents=True, exist_ok=True)
    return root


def get_default_workspace(*, create: bool = False) -> Path:
    workspace = get_workspace_root(create=create) / "default"
    workspace = workspace.resolve()
    if create:
        workspace.mkdir(parents=True, exist_ok=True)
    return workspace


def resolve_workspace_cwd(cwd: str | Path | None = None, *, create_default: bool = False) -> Path:
    """Resolve and validate an owner-worker cwd under HERMES_WORKSPACE_ROOT.

    ``None`` or an empty value resolves to ``workspaces/default``.  Any resolved
    path outside the workspace root is rejected, including ``..`` and symlink
    escapes.  Existing symlink escapes fail because ``Path.resolve()`` follows
    them before the containment check.
    """
    root = get_workspace_root(create=create_default)
    candidate = get_default_workspace(create=create_default) if not cwd else Path(cwd).expanduser().resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"cwd {candidate} escapes HERMES_WORKSPACE_ROOT {root}") from exc
    return candidate


def _require_under(path: Path, root: Path, label: str) -> None:
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise RuntimeError(f"{label} path {resolved} is outside owner home {root}") from exc


def assert_owner_runtime_paths(extra_paths: Iterable[tuple[str, Path]] | None = None) -> None:
    """Fail closed if owner-sensitive runtime paths escape HERMES_HOME.

    Intended for owner worker startup.  It is safe to call in local mode; without
    ``HERMES_OWNER_KEY`` it only validates the current dynamic Hermes home.
    """
    runtime_owner_home = get_runtime_owner_home()
    expected_owner = os.environ.get("HERMES_OWNER_KEY", "").strip()
    if expected_owner and not os.environ.get("HERMES_HOME", "").strip():
        raise RuntimeError("HERMES_OWNER_KEY is set but HERMES_HOME is missing")
    if expected_owner:
        leaked = [key for key in FORBIDDEN_OWNER_WORKER_ENV_KEYS if os.environ.get(key, "").strip()]
        if leaked:
            raise RuntimeError(f"forbidden owner worker environment variables present: {', '.join(sorted(leaked))}")

    paths: list[tuple[str, Path]] = [
        ("state_db", runtime_owner_home / "state.db"),
        ("sessions_index", runtime_owner_home / "sessions" / "sessions.json"),
        ("runtime", runtime_owner_home / "runtime"),
        ("logs", runtime_owner_home / "logs"),
        ("runtime_logs", runtime_owner_home / "runtime" / "logs"),
        ("checkpoints", runtime_owner_home / "checkpoints"),
        ("process_registry", runtime_owner_home / "processes.json"),
        ("restart_marker", runtime_owner_home / "runtime" / "restart.marker"),
        ("skills", runtime_owner_home / "skills"),
        ("memories", runtime_owner_home / "memories"),
        ("workspace_root", get_workspace_root()),
        ("default_workspace", get_default_workspace()),
    ]
    if extra_paths:
        paths.extend(extra_paths)
    for label, path in paths:
        _require_under(path, runtime_owner_home, label)
