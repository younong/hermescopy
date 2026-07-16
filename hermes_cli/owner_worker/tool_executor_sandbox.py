"""Fail-closed Linux Bubblewrap launch contract for authenticated executors.

This module owns the kernel isolation boundary for an authenticated Tool
Executor.  It deliberately accepts only trusted owner-worker inputs: the
workspace is mounted by an already-authorized directory descriptor and all
other mounts are derived from fixed runtime inputs.
"""
from __future__ import annotations

import importlib
import os
import platform
import hashlib
import json
import re
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Sequence

from hermes_cli.owner_worker.executor_identity import EgressProfile, ExecutorIdentity, ExecutorIdentityInvalid, parse_egress_profile


class ExecutorIsolationUnavailable(RuntimeError):
    """Authenticated executor isolation cannot be admitted safely."""


class SandboxVerificationInvalid(ExecutorIsolationUnavailable):
    """Host-observed sandbox evidence did not satisfy launch policy."""


@dataclass(frozen=True)
class SandboxSecurityPolicy:
    """Concrete kernel controls required for one authenticated sandbox profile."""

    profile: str
    backend: str
    uid: int
    gid: int
    syscall_policy_id: str
    syscall_policy_digest: str
    rootfs_readonly: bool = True
    no_new_privileges: bool = True
    capabilities: tuple[str, ...] = ()
    namespaces: tuple[str, ...] = ("user", "pid", "ipc", "net")

    def __post_init__(self) -> None:
        for value, field_name in (
            (self.profile, "security profile"), (self.backend, "security backend"),
            (self.syscall_policy_id, "syscall policy"),
        ):
            if not str(value or "").strip() or "\x00" in str(value):
                raise SandboxVerificationInvalid(f"sandbox {field_name} is invalid")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(self.syscall_policy_digest or "")):
            raise SandboxVerificationInvalid("sandbox syscall policy digest is invalid")
        if not isinstance(self.uid, int) or not isinstance(self.gid, int) or self.uid <= 0 or self.gid <= 0:
            raise SandboxVerificationInvalid("sandbox uid/gid must be non-root")
        if not self.rootfs_readonly or not self.no_new_privileges:
            raise SandboxVerificationInvalid("sandbox security policy is incomplete")
        if tuple(self.capabilities):
            raise SandboxVerificationInvalid("sandbox capability policy must drop all capabilities")
        if tuple(self.namespaces) != ("user", "pid", "ipc", "net"):
            raise SandboxVerificationInvalid("sandbox namespace policy is invalid")


@dataclass(frozen=True)
class SandboxVerificationPolicy:
    """Host-owned facts that a verified launch must exactly satisfy."""

    image_digest: str
    network_mode: str
    security_policy: SandboxSecurityPolicy

    def __post_init__(self) -> None:
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(self.image_digest or "")):
            raise SandboxVerificationInvalid("sandbox image digest is invalid")
        if not str(self.network_mode or "").strip() or "\x00" in str(self.network_mode):
            raise SandboxVerificationInvalid("sandbox network mode is invalid")
        if not isinstance(self.security_policy, SandboxSecurityPolicy):
            raise SandboxVerificationInvalid("sandbox security policy is invalid")


@dataclass(frozen=True)
class SandboxSyscallFilter:
    """Trusted inherited seccomp-program descriptor bound to one policy."""

    fd: int
    syscall_policy_id: str
    syscall_policy_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.fd, int) or self.fd < 0:
            raise SandboxVerificationInvalid("sandbox syscall filter descriptor is invalid")
        if not str(self.syscall_policy_id or "").strip() or not re.fullmatch(
            r"sha256:[0-9a-f]{64}", str(self.syscall_policy_digest or "")
        ):
            raise SandboxVerificationInvalid("sandbox syscall filter identity is invalid")


def validate_sandbox_syscall_filter(
    syscall_filter: SandboxSyscallFilter | None, *, policy: SandboxSecurityPolicy
) -> SandboxSyscallFilter:
    """Require a live trusted seccomp descriptor for the exact security policy."""
    if not isinstance(syscall_filter, SandboxSyscallFilter):
        raise SandboxVerificationInvalid("sandbox syscall filter is required")
    if (syscall_filter.syscall_policy_id, syscall_filter.syscall_policy_digest) != (
        policy.syscall_policy_id, policy.syscall_policy_digest,
    ):
        raise SandboxVerificationInvalid("sandbox syscall filter does not match security policy")
    try:
        os.fstat(syscall_filter.fd)
    except OSError as exc:
        raise SandboxVerificationInvalid("sandbox syscall filter descriptor is unavailable") from exc
    return syscall_filter


@dataclass(frozen=True)
class SandboxDeploymentPolicy:
    """Operator-owned inputs required to admit authenticated executors.

    This object is constructed by the deployment integration, never from tool
    input or permissive interpreter/source-root defaults. The callbacks remain
    outside the child process and must provide evidence/filter FDs for each
    exact binding.
    """

    verification_policy: SandboxVerificationPolicy
    verification_source: Callable[["SandboxLaunchBinding", "SandboxMountPolicy", object], "SandboxVerificationRecord | None"]
    syscall_filter_source: Callable[["SandboxLaunchBinding", SandboxSecurityPolicy], SandboxSyscallFilter | None]
    readonly_global_roots: tuple[Path, ...]
    owner_root: Path

    def __post_init__(self) -> None:
        if not isinstance(self.verification_policy, SandboxVerificationPolicy):
            raise SandboxVerificationInvalid("sandbox deployment verification policy is required")
        if not callable(self.verification_source) or not callable(self.syscall_filter_source):
            raise SandboxVerificationInvalid("sandbox deployment sources are required")
        owner_root = _canonical(self.owner_root, field="deployment owner root")
        if not owner_root.is_dir():
            raise ExecutorIsolationUnavailable("deployment owner root must be a directory")
        roots: list[Path] = []
        for root in self.readonly_global_roots:
            resolved = _canonical(root, field="deployment readonly global mount root")
            if not resolved.is_dir() or _overlaps(resolved, owner_root):
                raise ExecutorIsolationUnavailable("deployment readonly root overlaps owner root")
            if resolved not in roots:
                roots.append(resolved)
        if not roots:
            raise ExecutorIsolationUnavailable("deployment readonly global mount roots are required")
        object.__setattr__(self, "owner_root", owner_root)
        object.__setattr__(self, "readonly_global_roots", tuple(roots))


def load_sandbox_deployment_policy(spec: str) -> SandboxDeploymentPolicy:
    """Load the explicit operator factory named by ``module:attribute``.

    The contract intentionally has no default: authenticated startup remains
    unavailable until an operator wires a trusted provider.
    """
    module_name, separator, attribute = str(spec or "").strip().partition(":")
    if not separator or not module_name or not attribute or "." in attribute:
        raise SandboxVerificationInvalid("sandbox deployment policy factory is required")
    try:
        factory = getattr(importlib.import_module(module_name), attribute)
    except (ImportError, AttributeError) as exc:
        raise SandboxVerificationInvalid("sandbox deployment policy factory is unavailable") from exc
    if not callable(factory):
        raise SandboxVerificationInvalid("sandbox deployment policy factory is invalid")
    try:
        policy = factory()
    except Exception as exc:
        raise SandboxVerificationInvalid("sandbox deployment policy factory failed") from exc
    if not isinstance(policy, SandboxDeploymentPolicy):
        raise SandboxVerificationInvalid("sandbox deployment policy factory returned invalid policy")
    return policy


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


_MAX_TMPFS_BYTES = 1 << 30


@dataclass(frozen=True)
class SandboxMountPolicy:
    """Immutable allowlist for all host-visible sandbox mounts."""

    binding: SandboxLaunchBinding
    readonly_global_roots: tuple[Path, ...]
    workspace_root: Path | None
    control_home: Path | None
    owner_root: Path | None = None
    root_tmpfs_bytes: int = 64 << 20
    executor_tmpfs_bytes: int = 32 << 20
    mount_policy_id: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.binding, SandboxLaunchBinding):
            raise ExecutorIsolationUnavailable("sandbox mount policy requires a launch binding")
        for value, field_name in ((self.root_tmpfs_bytes, "root tmpfs"), (self.executor_tmpfs_bytes, "executor tmpfs")):
            if not isinstance(value, int) or not 0 < value <= _MAX_TMPFS_BYTES:
                raise ExecutorIsolationUnavailable(f"sandbox {field_name} size is invalid")
        workspace = _canonical(self.workspace_root, field="workspace root") if self.workspace_root else None
        control = _canonical(self.control_home, field="control home") if self.control_home else None
        owner_root = _canonical(self.owner_root, field="deployment owner root") if self.owner_root else None
        if owner_root is not None and self.binding.owner_home.parent != owner_root:
            raise ExecutorIsolationUnavailable("sandbox owner home does not match deployment owner root")
        protected = [self.binding.owner_home, self.binding.runtime_home]
        if owner_root:
            protected.append(owner_root)
        if workspace:
            protected.append(workspace)
        if control:
            protected.append(control)
        forbidden = tuple(Path(value) for value in ("/", "/proc", "/sys", "/dev", "/run", "/var/run"))
        roots: list[Path] = []
        for root in self.readonly_global_roots:
            requested = Path(root)
            if requested == Path("/") or any(
                requested == item or item in requested.parents or requested in item.parents
                for item in forbidden
                if item != Path("/")
            ):
                raise ExecutorIsolationUnavailable("readonly global mount root is too broad")
            resolved = _canonical(root, field="readonly global mount root")
            if (
                not resolved.is_dir()
                or any(_overlaps(resolved, item) for item in protected)
            ):
                raise ExecutorIsolationUnavailable("readonly global mount root overlaps protected owner data")
            if resolved not in roots:
                roots.append(resolved)
        if not roots:
            raise ExecutorIsolationUnavailable("readonly global mount roots are required")
        object.__setattr__(self, "readonly_global_roots", tuple(roots))
        object.__setattr__(self, "workspace_root", workspace)
        object.__setattr__(self, "control_home", control)
        object.__setattr__(self, "owner_root", owner_root)
        manifest = {
            "owner_runtime": str(self.binding.runtime_home),
            "globals": [str(root) for root in roots],
            "workspace": self.binding.workspace_mount,
            "runtime": self.binding.runtime_mount,
            "tmp": self.binding.tmp_mount,
            "root_tmpfs_bytes": self.root_tmpfs_bytes,
            "executor_tmpfs_bytes": self.executor_tmpfs_bytes,
        }
        encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
        object.__setattr__(self, "mount_policy_id", hashlib.sha256(encoded).hexdigest())


@dataclass(frozen=True)
class SandboxVerificationRecord:
    """Immutable host-observed evidence for one exact sandbox instance."""

    schema_version: int
    verifier: str
    observed_at: int
    expires_at: int
    image_digest: str
    profile: str
    security_backend: str
    syscall_policy_id: str
    syscall_policy_digest: str
    owner_key: str
    worker_id: str
    worker_generation: int
    lease_version: int
    recovery_generation: int
    executor_id: str
    executor_generation: int
    sandbox_id: str
    uid: int
    gid: int
    mount_view_id: str
    mount_policy_id: str
    tmpfs_id: str
    security_subject_id: str
    network_mode: str
    egress_profile: EgressProfile | str
    rootfs_readonly: bool
    no_new_privileges: bool
    capabilities_dropped: bool
    namespaces: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise SandboxVerificationInvalid("sandbox verification schema is invalid")
        if not str(self.verifier or "").strip() or "\x00" in str(self.verifier):
            raise SandboxVerificationInvalid("sandbox verification provenance is invalid")
        if not isinstance(self.observed_at, int) or not isinstance(self.expires_at, int) or self.expires_at <= self.observed_at:
            raise SandboxVerificationInvalid("sandbox verification lifetime is invalid")
        SandboxSecurityPolicy(
            self.profile,
            self.security_backend,
            self.uid,
            self.gid,
            self.syscall_policy_id,
            self.syscall_policy_digest,
            self.rootfs_readonly,
            self.no_new_privileges,
            () if self.capabilities_dropped else ("unexpected",),
            tuple(self.namespaces),
        )
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(self.image_digest or "")):
            raise SandboxVerificationInvalid("sandbox image digest is invalid")
        for value, field_name in (
            (self.owner_key, "owner"), (self.worker_id, "worker"), (self.executor_id, "executor"),
            (self.sandbox_id, "sandbox"), (self.mount_view_id, "mount view"),
            (self.mount_policy_id, "mount policy"), (self.tmpfs_id, "tmpfs"),
            (self.security_subject_id, "security subject"),
            (self.egress_profile, "egress profile"),
        ):
            if not str(value or "").strip() or "\x00" in str(value):
                raise SandboxVerificationInvalid(f"sandbox verification {field_name} is invalid")
        try:
            object.__setattr__(self, "egress_profile", parse_egress_profile(self.egress_profile, executor_admissible=True))
        except ExecutorIdentityInvalid as exc:
            raise SandboxVerificationInvalid("sandbox verification egress profile is invalid") from exc
        if min(self.worker_generation, self.lease_version, self.recovery_generation, self.executor_generation) < 0:
            raise SandboxVerificationInvalid("sandbox verification generation is invalid")


def validate_sandbox_verification_record(
    record: SandboxVerificationRecord | None,
    *,
    binding: SandboxLaunchBinding,
    mount_policy: SandboxMountPolicy,
    egress_profile: str,
    policy: SandboxVerificationPolicy,
    now: int,
) -> SandboxVerificationRecord:
    """Require current host evidence to exactly match one launch binding."""
    if not isinstance(record, SandboxVerificationRecord):
        raise SandboxVerificationInvalid("sandbox verification record is required")
    if not isinstance(now, int) or now < record.observed_at or now >= record.expires_at:
        raise SandboxVerificationInvalid("sandbox verification record is stale")
    if mount_policy.binding != binding:
        raise SandboxVerificationInvalid("sandbox mount policy does not match launch binding")
    try:
        egress_profile = parse_egress_profile(egress_profile, executor_admissible=True)
    except ExecutorIdentityInvalid as exc:
        raise SandboxVerificationInvalid("sandbox verification egress profile is invalid") from exc
    identity = binding.identity
    security = policy.security_policy
    expected = (
        policy.image_digest, security.profile, security.backend, security.syscall_policy_id,
        security.syscall_policy_digest, identity.owner_key, identity.worker_id,
        identity.worker_generation, identity.lease_version, identity.recovery_generation,
        identity.executor_id, identity.executor_generation, binding.sandbox_id,
        security.uid, security.gid, binding.mount_view_id, mount_policy.mount_policy_id, binding.tmpfs_id,
        binding.security_subject_id, policy.network_mode, egress_profile.value,
        security.rootfs_readonly, security.no_new_privileges, True, security.namespaces,
    )
    actual = (
        record.image_digest, record.profile, record.security_backend, record.syscall_policy_id,
        record.syscall_policy_digest, record.owner_key, record.worker_id,
        record.worker_generation, record.lease_version, record.recovery_generation,
        record.executor_id, record.executor_generation, record.sandbox_id,
        record.uid, record.gid, record.mount_view_id, record.mount_policy_id, record.tmpfs_id,
        record.security_subject_id, record.network_mode, record.egress_profile.value,
        record.rootfs_readonly, record.no_new_privileges, record.capabilities_dropped,
        record.namespaces,
    )
    if actual != expected:
        raise SandboxVerificationInvalid("sandbox verification record does not match launch binding")
    return record


@dataclass(frozen=True)
class BubblewrapLaunchSpec:
    """Fully validated Bubblewrap command for one executor invocation."""

    argv: tuple[str, ...]
    bubblewrap_path: str
    runtime_home: Path
    binding: SandboxLaunchBinding | None = None
    inherited_security_fds: tuple[int, ...] = ()


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


def default_readonly_global_mount_roots() -> tuple[Path, ...]:
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


def _require_bubblewrap_support(binary: str, *, runner: Callable[..., object]) -> None:
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
    # Bubblewrap loads --seccomp through its kernel seccomp path, which requires
    # no_new_privs (or an otherwise privileged caller). The user namespace and
    # exact --cap-drop ALL contract prohibit the latter; host evidence separately
    # attests the resulting no-new-privileges state. Do not mistake --new-session
    # (a session-management flag) for an NNP control.
    required = ("--bind-fd", "--size", "--uid", "--gid", "--cap-drop", "--seccomp")
    if getattr(result, "returncode", 1) != 0 or any(option not in output for option in required):
        raise ExecutorIsolationUnavailable(
            "Bubblewrap does not support required --bind-fd/--size/--uid/--gid/--cap-drop/--seccomp isolation"
        )


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
    binding: SandboxLaunchBinding,
    mount_policy: SandboxMountPolicy,
    security_policy: SandboxSecurityPolicy,
    syscall_filter: SandboxSyscallFilter,
    runtime_home: str | Path | None = None,
    owner_home: str | Path | None = None,
    workspace_root: str | Path | None = None,
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

    if not isinstance(mount_policy, SandboxMountPolicy) or mount_policy.binding != binding:
        raise ExecutorIsolationUnavailable("sandbox mount policy does not match launch binding")
    if not isinstance(security_policy, SandboxSecurityPolicy):
        raise ExecutorIsolationUnavailable("sandbox security policy is invalid")
    syscall_filter = validate_sandbox_syscall_filter(syscall_filter, policy=security_policy)
    if runtime_home is not None and _canonical(runtime_home, field="executor runtime home") != binding.runtime_home:
        raise ExecutorIsolationUnavailable("sandbox binding does not match launch paths")
    if owner_home is not None and _canonical(owner_home, field="owner home") != binding.owner_home:
        raise ExecutorIsolationUnavailable("sandbox binding does not match launch paths")
    if runtime_dependency_roots is not None:
        raise ExecutorIsolationUnavailable("raw runtime dependency roots are not allowed")
    if workspace_root is not None and mount_policy.workspace_root != _canonical(workspace_root, field="workspace root"):
        raise ExecutorIsolationUnavailable("sandbox mount policy does not match workspace root")
    if control_home is not None and mount_policy.control_home != _canonical(control_home, field="control home"):
        raise ExecutorIsolationUnavailable("sandbox mount policy does not match control home")
    bubblewrap = _resolve_bubblewrap(bubblewrap_binary, which=which)
    _require_bubblewrap_support(bubblewrap, runner=runner)
    runtime = binding.runtime_home
    dependency_roots = mount_policy.readonly_global_roots

    argv: list[str] = [
        bubblewrap,
        "--die-with-parent",
        "--unshare-user",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-net",
        "--uid", str(security_policy.uid),
        "--gid", str(security_policy.gid),
        "--cap-drop", "ALL",
        "--seccomp", str(syscall_filter.fd),
        "--size", str(mount_policy.root_tmpfs_bytes),
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
        "--size", str(mount_policy.executor_tmpfs_bytes),
        "--tmpfs", "/executor/tmp",
        "--chdir", "/workspace",
        "--",
        str(Path(python_executable or sys.executable).resolve()),
        "-m", "hermes_cli.tool_executor_runtime.entrypoint",
    ))
    return BubblewrapLaunchSpec(tuple(argv), bubblewrap, runtime, binding, (syscall_filter.fd,))
