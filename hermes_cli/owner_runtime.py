"""Owner-worker runtime path helpers.

Small, import-light helpers used by authenticated owner workers and subprocess
spawners.  They intentionally read environment variables at call time so a
fresh worker process can set HERMES_HOME before importing owner-sensitive code.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, MutableMapping

from hermes_constants import get_hermes_home

REQUIRED_OWNER_DIRS: tuple[Path, ...] = (
    Path("runtime"),
    Path("runtime") / "logs",
    Path("runtime") / "checkpoints",
    Path("runtime") / "tmp",
    Path("logs"),
    Path("checkpoints"),
    Path("sessions"),
    Path("workspaces") / "default",
    Path("skills"),
    Path("memories"),
)

OWNER_WORKER_DEPLOYMENT_RUNTIME_ENV_KEYS: tuple[str, ...] = (
    "HERMES_DEPLOYMENT_INFERENCE_PROVIDER",
    "HERMES_DEPLOYMENT_INFERENCE_MODEL",
    "HERMES_DEPLOYMENT_INFERENCE_API_MODE",
    "HERMES_DEPLOYMENT_INFERENCE_POLICY_ID",
    "HERMES_DEPLOYMENT_INFERENCE_ALLOWED_MODELS",
    "HERMES_DEPLOYMENT_INFERENCE_SUPPORTS_VISION",
    "HERMES_DEPLOYMENT_INFERENCE_RELAY_FD",
    "HERMES_DEPLOYMENT_INFERENCE_RELAY_BASE_URL",
    "HERMES_DEPLOYMENT_IMAGE_PROVIDER",
    "HERMES_DEPLOYMENT_IMAGE_MODEL",
    "HERMES_DEPLOYMENT_IMAGE_POLICY_ID",
    "HERMES_DEPLOYMENT_IMAGE_ALLOWED_MODELS",
    "HERMES_DEPLOYMENT_IMAGE_MAX_REFERENCES",
    "HERMES_DEPLOYMENT_IMAGE_MAX_REFERENCE_BYTES",
    "HERMES_DEPLOYMENT_IMAGE_MAX_TOTAL_REFERENCE_BYTES",
    "HERMES_DEPLOYMENT_IMAGE_MAX_OUTPUT_BYTES",
    "HERMES_DEPLOYMENT_IMAGE_RELAY_FD",
)

OWNER_ENV_KEYS: tuple[str, ...] = (
    "HERMES_HOME",
    "HERMES_OWNER_KEY",
    "HERMES_TENANT_ID",
    "HERMES_OWNER_USER_ID",
    "HERMES_AUTH_PROVIDER",
    "HERMES_CONTROL_HOME",
    "HERMES_WORKSPACE_ROOT",
    "HERMES_WORKER_GENERATION",
    "HERMES_WORKER_ID",
    "HERMES_WORKER_LEASE_VERSION",
    "HERMES_WORKER_RECOVERY_GENERATION",
    "HERMES_OWNER_WORKER_CAPABILITY_ISSUER",
    "HERMES_OWNER_WORKER_CAPABILITY_PUBLIC_KEY",
    "HERMES_OWNER_WORKER_CAPABILITY_RETAINED_PUBLIC_KEYS",
    "HERMES_OWNER_WORKER_CONTROL_WS_BASE",
    "HERMES_SANDBOX_DEPLOYMENT_POLICY",
    "HERMES_DISABLE_LAZY_INSTALLS",
    *OWNER_WORKER_DEPLOYMENT_RUNTIME_ENV_KEYS,
)

FORBIDDEN_OWNER_WORKER_ENV_KEYS: tuple[str, ...] = (
    "HERMES_PROFILE",
    "HERMES_SESSION_PROFILE",
    "HERMES_CONFIG",
    "HERMES_ENV",
    "TERMINAL_CWD",
)

_REQUIRED_OWNER_WORKER_ENV_KEYS: tuple[str, ...] = (
    "HERMES_HOME",
    "HERMES_OWNER_KEY",
    "HERMES_CONTROL_HOME",
    "HERMES_WORKSPACE_ROOT",
    "HERMES_WORKER_GENERATION",
    "HERMES_WORKER_ID",
    "HERMES_WORKER_LEASE_VERSION",
    "HERMES_WORKER_RECOVERY_GENERATION",
    "HERMES_OWNER_WORKER_CAPABILITY_ISSUER",
    "HERMES_OWNER_WORKER_CAPABILITY_PUBLIC_KEY",
    "HERMES_OWNER_WORKER_CAPABILITY_RETAINED_PUBLIC_KEYS",
)


@dataclass(frozen=True)
class OwnerWorkerRuntimePaths:
    """Exact owner-local destinations accepted by an authenticated Worker."""

    owner_home: Path
    workspace_root: Path
    default_workspace: Path
    worker_socket: Path
    paths: Mapping[str, Path]


def owner_worker_env_values(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return owner-scoping env vars present in *source* or ``os.environ``."""
    src = source if source is not None else os.environ
    return {key: str(src[key]) for key in OWNER_ENV_KEYS if src.get(key)}


def propagate_owner_env(env: MutableMapping[str, str], source: Mapping[str, str] | None = None) -> None:
    """Copy explicit owner worker env vars into a child process environment."""
    env.update(owner_worker_env_values(source))


def strip_owner_worker_deployment_runtime_env(env: MutableMapping[str, str]) -> None:
    """Remove owner-worker-only deployment metadata from a child environment."""
    for key in OWNER_WORKER_DEPLOYMENT_RUNTIME_ENV_KEYS:
        env.pop(key, None)


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


def owner_worker_socket_path(owner_home: str | Path, worker_generation: int) -> Path:
    """Return the sole authenticated worker socket location for a generation."""
    home = Path(owner_home).expanduser().resolve()
    generation = int(worker_generation)
    if generation < 1:
        raise ValueError("worker_generation must be positive")
    return home / "runtime" / "workers" / str(generation) / "worker.sock"


def owner_worker_env_for(
    *,
    owner_key: str,
    owner_home: str | Path,
    tenant_id: str = "",
    owner_user_id: str = "",
    auth_provider: str = "",
    control_home: str | Path | None = None,
    worker_generation: int | None = None,
    worker_id: str = "",
    lease_version: int | None = None,
    recovery_generation: int | None = None,
    capability_issuer: str = "",
    capability_public_key: str = "",
    capability_retained_public_keys: str = "",
    deployment_inference_descriptor: object | None = None,
    deployment_image_descriptor: object | None = None,
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
    if worker_generation is not None:
        generation = int(worker_generation)
        if generation < 1:
            raise ValueError("worker_generation must be positive")
        env["HERMES_WORKER_GENERATION"] = str(generation)
    if worker_id:
        env["HERMES_WORKER_ID"] = str(worker_id)
    elif worker_generation is not None:
        raise ValueError("worker_id is required with worker_generation")
    if lease_version is not None:
        if int(lease_version) < 1:
            raise ValueError("lease_version must be positive")
        env["HERMES_WORKER_LEASE_VERSION"] = str(int(lease_version))
    if recovery_generation is not None:
        if int(recovery_generation) < 0:
            raise ValueError("recovery_generation must not be negative")
        env["HERMES_WORKER_RECOVERY_GENERATION"] = str(int(recovery_generation))
    if bool(capability_issuer) != bool(capability_public_key):
        raise ValueError("capability issuer and public key must be supplied together")
    if capability_retained_public_keys and not capability_issuer:
        raise ValueError("retained capability public keys require an active issuer")
    if capability_issuer:
        env["HERMES_OWNER_WORKER_CAPABILITY_ISSUER"] = str(capability_issuer)
        env["HERMES_OWNER_WORKER_CAPABILITY_PUBLIC_KEY"] = str(capability_public_key)
        env["HERMES_OWNER_WORKER_CAPABILITY_RETAINED_PUBLIC_KEYS"] = str(capability_retained_public_keys or "{}")
    if deployment_inference_descriptor is not None:
        from hermes_cli.deployment_inference import DeploymentInferenceDescriptor

        if not isinstance(deployment_inference_descriptor, DeploymentInferenceDescriptor):
            raise ValueError("deployment inference descriptor is invalid")
        env.update({
            "HERMES_DEPLOYMENT_INFERENCE_PROVIDER": deployment_inference_descriptor.provider,
            "HERMES_DEPLOYMENT_INFERENCE_MODEL": deployment_inference_descriptor.model,
            "HERMES_DEPLOYMENT_INFERENCE_API_MODE": deployment_inference_descriptor.api_mode,
            "HERMES_DEPLOYMENT_INFERENCE_POLICY_ID": deployment_inference_descriptor.policy_id,
            "HERMES_DEPLOYMENT_INFERENCE_ALLOWED_MODELS": ",".join(deployment_inference_descriptor.allowed_models),
        })
        if deployment_inference_descriptor.supports_vision is not None:
            env["HERMES_DEPLOYMENT_INFERENCE_SUPPORTS_VISION"] = (
                "true" if deployment_inference_descriptor.supports_vision else "false"
            )
    if deployment_image_descriptor is not None:
        from hermes_cli.deployment_image import DeploymentImageDescriptor

        if not isinstance(deployment_image_descriptor, DeploymentImageDescriptor):
            raise ValueError("deployment image descriptor is invalid")
        env.update({
            "HERMES_DEPLOYMENT_IMAGE_PROVIDER": deployment_image_descriptor.provider,
            "HERMES_DEPLOYMENT_IMAGE_MODEL": deployment_image_descriptor.model,
            "HERMES_DEPLOYMENT_IMAGE_POLICY_ID": deployment_image_descriptor.policy_id,
            "HERMES_DEPLOYMENT_IMAGE_ALLOWED_MODELS": ",".join(deployment_image_descriptor.allowed_models),
            "HERMES_DEPLOYMENT_IMAGE_MAX_REFERENCES": str(deployment_image_descriptor.max_reference_images),
            "HERMES_DEPLOYMENT_IMAGE_MAX_REFERENCE_BYTES": str(deployment_image_descriptor.max_reference_bytes),
            "HERMES_DEPLOYMENT_IMAGE_MAX_TOTAL_REFERENCE_BYTES": str(deployment_image_descriptor.max_total_reference_bytes),
            "HERMES_DEPLOYMENT_IMAGE_MAX_OUTPUT_BYTES": str(deployment_image_descriptor.max_output_bytes),
        })
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


def _required_positive_int(source: Mapping[str, str], key: str, *, allow_zero: bool = False) -> int:
    value = str(source.get(key, "")).strip()
    try:
        number = int(value)
    except ValueError as exc:
        raise RuntimeError(f"{key} is required and must be an integer") from exc
    if number < 0 or (number == 0 and not allow_zero):
        raise RuntimeError(f"{key} is invalid")
    return number


def owner_worker_runtime_paths(
    *,
    owner_home: str | Path | None = None,
    worker_generation: int | None = None,
) -> OwnerWorkerRuntimePaths:
    """Return exact approved runtime locations without importing owner services."""
    home = Path(owner_home).expanduser().resolve() if owner_home is not None else get_runtime_owner_home()
    generation = worker_generation
    if generation is None:
        generation = _required_positive_int(os.environ, "HERMES_WORKER_GENERATION")
    generation = int(generation)
    if generation < 1:
        raise RuntimeError("HERMES_WORKER_GENERATION is invalid")
    workspace_root = (home / "workspaces").resolve()
    paths = {
        "state_db": home / "state.db",
        "runtime": home / "runtime",
        "logs": home / "logs",
        "runtime_logs": home / "runtime" / "logs",
        "temporary_root": home / "runtime" / "tmp",
        "checkpoints": home / "checkpoints",
        "sessions_index": home / "sessions" / "sessions.json",
        "channel_directory": home / "channel_directory.json",
        "channel_aliases": home / "channel_aliases.json",
        "mirror_sessions_index": home / "sessions" / "sessions.json",
        "process_registry": home / "processes.json",
        "restart_marker": home / "runtime" / "restart.marker",
        "skills": home / "skills",
        "memories": home / "memories",
        "workspace_root": workspace_root,
        "default_workspace": workspace_root / "default",
    }
    return OwnerWorkerRuntimePaths(
        owner_home=home,
        workspace_root=workspace_root,
        default_workspace=(workspace_root / "default").resolve(),
        worker_socket=owner_worker_socket_path(home, generation),
        paths={label: path.resolve() for label, path in paths.items()},
    )


def validate_owner_worker_runtime_environment(
    *,
    owner_home: str | Path | None = None,
    owner_key: str | None = None,
    worker_generation: int | None = None,
    worker_id: str | None = None,
    socket_path: str | Path | None = None,
    source: Mapping[str, str] | None = None,
) -> OwnerWorkerRuntimePaths:
    """Fail closed unless an authenticated Worker has one complete minimal env."""
    env = source if source is not None else os.environ
    missing = [key for key in _REQUIRED_OWNER_WORKER_ENV_KEYS if not str(env.get(key, "")).strip()]
    if missing:
        raise RuntimeError(f"owner worker environment is incomplete: {', '.join(missing)}")
    leaked = [key for key in FORBIDDEN_OWNER_WORKER_ENV_KEYS if str(env.get(key, "")).strip()]
    if leaked:
        raise RuntimeError(f"forbidden owner worker environment variables present: {', '.join(sorted(leaked))}")
    unknown = sorted(
        key for key, value in env.items()
        if key.startswith("HERMES_") and value and key not in OWNER_ENV_KEYS
    )
    if unknown:
        raise RuntimeError(f"unexpected owner worker environment variables present: {', '.join(unknown)}")

    actual_home = Path(str(env["HERMES_HOME"])).expanduser().resolve()
    expected_home = Path(owner_home).expanduser().resolve() if owner_home is not None else actual_home
    if actual_home != expected_home:
        raise RuntimeError("HERMES_HOME does not match owner_home")
    actual_owner_key = str(env["HERMES_OWNER_KEY"]).strip()
    if not actual_owner_key or (owner_key is not None and actual_owner_key != str(owner_key).strip()):
        raise RuntimeError("HERMES_OWNER_KEY does not match owner_key")
    generation = _required_positive_int(env, "HERMES_WORKER_GENERATION")
    if worker_generation is not None and generation != int(worker_generation):
        raise RuntimeError("HERMES_WORKER_GENERATION does not match worker_generation")
    _required_positive_int(env, "HERMES_WORKER_LEASE_VERSION")
    _required_positive_int(env, "HERMES_WORKER_RECOVERY_GENERATION", allow_zero=True)
    actual_worker_id = str(env["HERMES_WORKER_ID"]).strip()
    if not actual_worker_id or (worker_id is not None and actual_worker_id != str(worker_id).strip()):
        raise RuntimeError("HERMES_WORKER_ID does not match worker_id")

    paths = owner_worker_runtime_paths(owner_home=expected_home, worker_generation=generation)
    configured_workspace = Path(str(env["HERMES_WORKSPACE_ROOT"])).expanduser().resolve()
    if configured_workspace != paths.workspace_root:
        raise RuntimeError("HERMES_WORKSPACE_ROOT is not the canonical owner workspace")
    if socket_path is not None and Path(socket_path).expanduser().resolve(strict=False) != paths.worker_socket.resolve(strict=False):
        raise RuntimeError("worker socket does not match owner generation")
    for label, path in paths.paths.items():
        _require_under(path, paths.owner_home, label)
    _require_under(paths.worker_socket, paths.owner_home, "worker_socket")
    return paths


def assert_owner_runtime_paths(
    extra_paths: Iterable[tuple[str, Path]] | None = None,
    *,
    expected_paths: OwnerWorkerRuntimePaths | None = None,
) -> None:
    """Fail closed if owner-sensitive paths escape or differ from runtime contract."""
    runtime_owner_home = get_runtime_owner_home()
    expected_owner = os.environ.get("HERMES_OWNER_KEY", "").strip()
    if expected_owner:
        paths = expected_paths or validate_owner_worker_runtime_environment()
    else:
        paths = expected_paths or owner_worker_runtime_paths(owner_home=runtime_owner_home, worker_generation=1)
    observed = list(paths.paths.items())
    if extra_paths:
        observed.extend(extra_paths)
    for label, path in observed:
        _require_under(path, paths.owner_home, label)
        expected = paths.paths.get(label)
        if expected is not None and Path(path).expanduser().resolve() != expected:
            raise RuntimeError(f"{label} path does not match canonical owner runtime path")
