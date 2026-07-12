"""Fail-closed Linux Bubblewrap launch contract for authenticated executors.

This module owns the kernel isolation boundary for an authenticated Tool
Executor.  It deliberately accepts only trusted owner-worker inputs: the
workspace is mounted by an already-authorized directory descriptor and all
other mounts are derived from fixed runtime inputs.
"""
from __future__ import annotations

import os
import platform
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Sequence

from hermes_cli.owner_worker.executor_identity import ExecutorIdentity


class ExecutorIsolationUnavailable(RuntimeError):
    """Authenticated executor isolation cannot be admitted safely."""


@dataclass(frozen=True)
class SandboxLaunchBinding:
    """Immutable owner/generation fence for one sandbox process."""

    identity: ExecutorIdentity
    sandbox_id: str
    owner_home: Path
    runtime_home: Path
    workspace_mount: str = "/workspace"
    runtime_mount: str = "/executor"
    tmp_mount: str = "/executor/tmp"
    mount_view_id: str = field(init=False)
    tmpfs_id: str = field(init=False)
    security_subject_id: str = field(init=False)

    def __post_init__(self) -> None:
        sandbox_id = str(self.sandbox_id or "").strip()
        if not sandbox_id or "\x00" in sandbox_id:
            raise ExecutorIsolationUnavailable("sandbox instance identity is invalid")
        owner = _canonical(self.owner_home, field="owner home")
        runtime = Path(self.runtime_home).resolve()
        expected_runtime = owner / "runtime" / "executors" / self.identity.executor_id / f"gen-{self.identity.executor_generation}"
        if not runtime.is_absolute() or "\x00" in str(runtime) or runtime != expected_runtime or runtime == owner:
            raise ExecutorIsolationUnavailable("executor runtime home does not match sandbox identity")
        if (self.workspace_mount, self.runtime_mount, self.tmp_mount) != ("/workspace", "/executor", "/executor/tmp"):
            raise ExecutorIsolationUnavailable("sandbox mount view is invalid")
        object.__setattr__(self, "owner_home", owner)
        object.__setattr__(self, "runtime_home", runtime)
        object.__setattr__(self, "sandbox_id", sandbox_id)
        fence = ":".join((
            self.identity.owner_digest,
            self.identity.worker_id,
            str(self.identity.worker_generation),
            str(self.identity.lease_version),
            str(self.identity.recovery_generation),
            self.identity.executor_id,
            str(self.identity.executor_generation),
            sandbox_id,
        ))
        object.__setattr__(self, "mount_view_id", f"mount:{fence}")
        object.__setattr__(self, "tmpfs_id", f"tmpfs:{fence}")
        object.__setattr__(self, "security_subject_id", f"subject:{fence}")


@dataclass(frozen=True)
class BubblewrapLaunchSpec:
    """Fully validated Bubblewrap command for one executor invocation."""

    argv: tuple[str, ...]
    bubblewrap_path: str
    runtime_home: Path
    binding: SandboxLaunchBinding | None = None


def _canonical(path: str | Path, *, field: str) -> Path:
    try:
        result = Path(path).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ExecutorIsolationUnavailable(f"{field} is unavailable") from exc
    if not result.is_absolute() or "\x00" in str(result):
        raise ExecutorIsolationUnavailable(f"{field} is invalid")
    return result


def _overlaps(first: Path, second: Path) -> bool:
    return first == second or first in second.parents or second in first.parents


def _default_runtime_dependency_roots() -> tuple[Path, ...]:
    """Return deployment-local interpreter/application roots, never host root."""
    # Production installs are normally importable from the interpreter prefix;
    # source/editable installs additionally need the trusted application root.
    # It is subsequently rejected if it overlaps any owner/control/workspace root.
    application_root = Path(__file__).resolve().parents[2]
    candidates = (Path(sys.prefix), Path(sys.base_prefix), application_root)
    result: list[Path] = []
    for candidate in candidates:
        resolved = _canonical(candidate, field="executor runtime dependency root")
        if resolved not in result:
            result.append(resolved)
    return tuple(result)


def _resolve_bubblewrap(binary: str | None, *, which: Callable[[str], str | None]) -> str:
    candidate = binary or which("bwrap")
    if not candidate:
        raise ExecutorIsolationUnavailable("Bubblewrap is required for authenticated executor isolation")
    path = _canonical(candidate, field="Bubblewrap executable")
    try:
        mode = path.stat().st_mode
    except OSError as exc:
        raise ExecutorIsolationUnavailable("Bubblewrap executable is unavailable") from exc
    if not path.is_file() or not mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
        raise ExecutorIsolationUnavailable("Bubblewrap executable is not executable")
    return str(path)


def _require_bind_fd_support(binary: str, *, runner: Callable[..., object]) -> None:
    try:
        result = runner(
            [binary, "--help"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ExecutorIsolationUnavailable("Bubblewrap capability probe failed") from exc
    output = f"{getattr(result, 'stdout', '')}\n{getattr(result, 'stderr', '')}"
    if getattr(result, "returncode", 1) != 0 or "--bind-fd" not in output:
        raise ExecutorIsolationUnavailable("Bubblewrap does not support required --bind-fd isolation")


def _sandbox_destination_dirs(paths: Sequence[Path]) -> tuple[Path, ...]:
    """Return empty in-sandbox directories required before bind destinations."""
    directories: set[Path] = {Path("/workspace")}
    for path in paths:
        current = path
        while current != Path("/"):
            directories.add(current)
            current = current.parent
    return tuple(sorted(directories, key=lambda item: (len(item.parts), str(item))))


def _validated_dependency_roots(
    roots: Sequence[str | Path],
    *,
    owner_home: str | Path,
    control_home: str | Path | None,
    workspace_root: str | Path | None,
) -> tuple[Path, ...]:
    protected = [_canonical(owner_home, field="owner home")]
    if control_home:
        protected.append(_canonical(control_home, field="control home"))
    if workspace_root:
        protected.append(_canonical(workspace_root, field="workspace root"))
    forbidden = tuple(Path(value) for value in ("/", "/proc", "/sys", "/dev", "/run", "/var/run"))

    validated: list[Path] = []
    for root in roots:
        resolved = _canonical(root, field="executor runtime dependency root")
        if any(_overlaps(resolved, protected_root) for protected_root in protected):
            raise ExecutorIsolationUnavailable("executor runtime dependency root overlaps protected owner data")
        # `/` must never be mounted.  The remaining entries reject both a
        # sensitive tree and a parent broad enough to expose it, while allowing
        # a normal runtime root such as `/usr`.
        if resolved == Path("/") or any(
            resolved == forbidden_root
            or forbidden_root in resolved.parents
            or resolved in forbidden_root.parents
            for forbidden_root in forbidden
            if forbidden_root != Path("/")
        ):
            raise ExecutorIsolationUnavailable("executor runtime dependency root is too broad")
        if resolved not in validated:
            validated.append(resolved)
    if not validated:
        raise ExecutorIsolationUnavailable("executor runtime dependency roots are required")
    return tuple(validated)


def build_bubblewrap_launch_spec(
    *,
    environment: Mapping[str, str],
    workspace_fd: int,
    runtime_home: str | Path,
    owner_home: str | Path,
    workspace_root: str | Path | None,
    binding: SandboxLaunchBinding | None = None,
    control_home: str | Path | None = None,
    runtime_dependency_roots: Sequence[str | Path] | None = None,
    bubblewrap_binary: str | None = None,
    platform_name: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
    runner: Callable[..., object] = subprocess.run,
    python_executable: str | Path | None = None,
) -> BubblewrapLaunchSpec:
    """Build a private namespace launch command or fail before child startup."""
    if (platform_name or platform.system()).lower() != "linux":
        raise ExecutorIsolationUnavailable("authenticated executor isolation requires Linux Bubblewrap")
    if not isinstance(workspace_fd, int) or workspace_fd < 0:
        raise ExecutorIsolationUnavailable("workspace descriptor is invalid")

    bubblewrap = _resolve_bubblewrap(bubblewrap_binary, which=which)
    _require_bind_fd_support(bubblewrap, runner=runner)
    runtime = _canonical(runtime_home, field="executor runtime home")
    owner = _canonical(owner_home, field="owner home")
    if binding is not None:
        if binding.runtime_home != runtime or binding.owner_home != owner:
            raise ExecutorIsolationUnavailable("sandbox binding does not match launch paths")
    elif not _overlaps(runtime, owner) or runtime == owner:
        raise ExecutorIsolationUnavailable("executor runtime home must be beneath owner home")
    dependency_roots = _validated_dependency_roots(
        runtime_dependency_roots or _default_runtime_dependency_roots(),
        owner_home=owner,
        control_home=control_home,
        workspace_root=workspace_root,
    )

    argv: list[str] = [
        bubblewrap,
        "--die-with-parent",
        "--unshare-user",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-net",
        "--tmpfs", "/",
        "--dev", "/dev",
        "--proc", "/proc",
        "--clearenv",
    ]
    for key, value in sorted((str(key), str(value)) for key, value in environment.items()):
        argv.extend(("--setenv", key, value))
    sandbox_runtime = Path("/executor")
    for directory in _sandbox_destination_dirs((*dependency_roots, sandbox_runtime)):
        argv.extend(("--dir", str(directory)))
    for root in dependency_roots:
        argv.extend(("--ro-bind", str(root), str(root)))
    # `--bind-fd` consumes only the descriptor passed through Popen.pass_fds;
    # the workspace host path is never an argv or environment authority input.
    argv.extend((
        "--bind-fd", str(workspace_fd), "/workspace",
        "--bind", str(runtime), "/executor",
        "--chdir", "/workspace",
        "--",
        str(Path(python_executable or sys.executable).resolve()),
        "-m", "hermes_cli.tool_executor_runtime.entrypoint",
    ))
    return BubblewrapLaunchSpec(tuple(argv), bubblewrap, runtime, binding)
