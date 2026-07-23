"""Root-owned bare-metal policy for authenticated Bubblewrap executors."""
from __future__ import annotations

import hashlib
import json
import os
import platform
import stat
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping

from hermes_cli.owner_worker.executor_identity import EgressProfile, parse_egress_profile
from hermes_cli.owner_worker.host_sandbox_attestation import attest_host_bubblewrap_process
from hermes_cli.owner_worker.tool_executor_sandbox import (
    ExecutorIsolationUnavailable,
    SandboxDeploymentPolicy,
    SandboxLaunchBinding,
    SandboxMountPolicy,
    SandboxReadonlyMount,
    SandboxSecurityPolicy,
    SandboxSyscallFilter,
    SandboxVerificationInvalid,
    SandboxVerificationPolicy,
    SandboxVerificationRecord,
    _require_bubblewrap_support,
)

_DEFAULT_POLICY_PATH = Path("/etc/hermes/executor-sandbox.json")
_MAX_POLICY_BYTES = 64 << 10
_DIGEST_RE = "sha256:"


class HostSandboxInvalid(SandboxVerificationInvalid):
    """The root-owned host policy is missing or unsafe."""


class HostSandboxUnavailable(ExecutorIsolationUnavailable):
    """The bare-metal host cannot satisfy its sandbox policy."""


@dataclass(frozen=True)
class HostSandboxConfig:
    """Strictly parsed host policy and verified immutable artifact paths."""

    architecture: str
    owner_root: Path
    uid: int
    gid: int
    bwrap_binary: Path
    release_root: Path
    runtime_root: Path
    python_executable: PurePosixPath
    readonly_mounts: tuple[SandboxReadonlyMount, ...]
    syscall_policy_id: str
    syscall_policy_digest: str
    seccomp_artifact: Path
    image_digest: str
    require_root_owner: bool = True
    profile: str = "executor-bwrap-v1"
    security_backend: str = "host-bwrap-seccomp-v1"
    network_mode: str = "isolated-tool-network"
    verifier: str = "host-sandbox-policy-v1"
    record_ttl_seconds: int = 30
    root_tmpfs_bytes: int = 64 << 20
    executor_tmpfs_bytes: int = 32 << 20
    allowed_egress_profiles: tuple[EgressProfile, ...] = (EgressProfile.TOOL_NONE,)


def load_host_sandbox_config(
    policy_path: str | Path = _DEFAULT_POLICY_PATH,
    *,
    require_root_owner: bool = True,
    platform_name: str | None = None,
    machine: str | None = None,
) -> HostSandboxConfig:
    """Read and validate the complete root-owned host policy without following links."""
    if (platform_name or platform.system()).lower() != "linux":
        raise HostSandboxUnavailable("bare-metal executor sandbox requires Linux")
    path = Path(policy_path)
    fd = _open_nofollow(path, "host sandbox policy")
    try:
        status = os.fstat(fd)
        _require_regular_mode(status, "host sandbox policy", 0o644, require_root_owner=require_root_owner)
        if require_root_owner:
            _require_protected_ancestors(path, "host sandbox policy")
        if status.st_size <= 0 or status.st_size > _MAX_POLICY_BYTES:
            raise HostSandboxInvalid("host sandbox policy size is invalid")
        raw = _read_all(fd, status.st_size)
    finally:
        os.close(fd)
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise HostSandboxInvalid("host sandbox policy JSON is invalid") from exc
    return parse_host_sandbox_config(
        document,
        require_root_owner=require_root_owner,
        machine=machine,
    )


def parse_host_sandbox_config(
    value: Mapping[str, Any],
    *,
    require_root_owner: bool = True,
    machine: str | None = None,
) -> HostSandboxConfig:
    """Parse an exact v1 host policy; unknown or omitted fields fail closed."""
    required = {
        "schema_version", "architecture", "owner_root", "uid", "gid", "bwrap_binary",
        "release_root", "runtime_root", "python_executable", "readonly_mounts",
        "syscall_policy_id", "syscall_policy_digest", "seccomp_artifact", "image_digest",
        "profile", "security_backend", "network_mode", "verifier", "record_ttl_seconds",
        "root_tmpfs_bytes", "executor_tmpfs_bytes", "allowed_egress_profiles",
    }
    if not isinstance(value, Mapping) or set(value) != required or value.get("schema_version") != 1:
        raise HostSandboxInvalid("host sandbox policy schema is invalid")
    architecture = _text(value["architecture"], "host sandbox architecture")
    if architecture != _normalized_architecture(machine or platform.machine()):
        raise HostSandboxInvalid("host sandbox architecture does not match host")
    uid, gid = value["uid"], value["gid"]
    if not isinstance(uid, int) or isinstance(uid, bool) or not isinstance(gid, int) or isinstance(gid, bool) or uid <= 0 or gid <= 0:
        raise HostSandboxInvalid("host sandbox uid/gid must be non-root")

    owner_root = _directory(
        value["owner_root"], "host sandbox owner root",
        require_root_owner=False, mode=0o750,
        owner_uid=(uid if require_root_owner else None),
        owner_gid=(gid if require_root_owner else None),
    )
    bwrap = _file(value["bwrap_binary"], "Bubblewrap executable", require_root_owner=require_root_owner, mode=0o755)
    release = _directory(value["release_root"], "host sandbox release root", require_root_owner=require_root_owner, mode=0o755)
    runtime = _directory(value["runtime_root"], "host sandbox runtime root", require_root_owner=require_root_owner, mode=0o755)
    if require_root_owner:
        for path, field in (
            (bwrap, "Bubblewrap executable"),
            (release, "host sandbox release root"),
            (runtime, "host sandbox runtime root"),
        ):
            _require_protected_ancestors(path, field)
    if release == runtime or release in runtime.parents or runtime in release.parents:
        raise HostSandboxInvalid("host sandbox release and runtime roots must not overlap")
    if any(_overlaps(item, owner_root) for item in (bwrap, release, runtime)):
        raise HostSandboxInvalid("host sandbox artifacts overlap owner root")

    mounts_raw = value["readonly_mounts"]
    if not isinstance(mounts_raw, list) or not mounts_raw:
        raise HostSandboxInvalid("host sandbox readonly mounts are required")
    mounts: list[SandboxReadonlyMount] = []
    required_roots = {
        (release, PurePosixPath("/opt/hermes/release")),
        (runtime, PurePosixPath("/opt/hermes/python")),
    }
    allowed_toolchain_destinations = {
        PurePosixPath("/bin"),
        PurePosixPath("/usr/bin"),
        PurePosixPath("/lib"),
        PurePosixPath("/lib64"),
        PurePosixPath("/usr/lib"),
        PurePosixPath("/usr/lib64"),
        PurePosixPath("/usr/share"),
        PurePosixPath("/etc/fonts"),
    }
    for item in mounts_raw:
        if not isinstance(item, Mapping) or set(item) != {"source", "destination"}:
            raise HostSandboxInvalid("host sandbox readonly mount is invalid")
        mount = SandboxReadonlyMount(item["source"], PurePosixPath(str(item["destination"])))
        if (mount.source, mount.destination) not in required_roots:
            toolchain = runtime / "toolchain"
            if (
                toolchain not in mount.source.parents
                or mount.destination not in allowed_toolchain_destinations
                or mount.source.relative_to(toolchain) != Path(str(mount.destination).lstrip("/"))
            ):
                raise HostSandboxInvalid("host sandbox readonly mount is outside the packaged runtime")
            toolchain_status = mount.source.stat()
            if (
                not stat.S_ISDIR(toolchain_status.st_mode)
                or stat.S_IMODE(toolchain_status.st_mode) != 0o755
                or (require_root_owner and toolchain_status.st_uid != 0)
            ):
                raise HostSandboxInvalid("host sandbox toolchain mount is not protected")
        mounts.append(mount)
    if not required_roots.issubset({(mount.source, mount.destination) for mount in mounts}):
        raise HostSandboxInvalid("host sandbox release and runtime mounts are required")
    if len({mount.destination for mount in mounts}) != len(mounts):
        raise HostSandboxInvalid("host sandbox mount destinations must be unique")

    python_executable = PurePosixPath(_text(value["python_executable"], "sandbox Python executable"))
    if not python_executable.is_absolute() or ".." in python_executable.parts:
        raise HostSandboxInvalid("sandbox Python executable is invalid")
    python_mount = next(
        (
            mount for mount in mounts
            if mount.destination == python_executable or mount.destination in python_executable.parents
        ),
        None,
    )
    if python_mount is None:
        raise HostSandboxInvalid("sandbox Python executable is outside readonly mounts")
    relative_python = python_executable.relative_to(python_mount.destination)
    python_path = python_mount.source / Path(relative_python.as_posix())
    try:
        resolved_python = python_path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise HostSandboxInvalid("host sandbox Python executable is unavailable") from exc
    if runtime not in resolved_python.parents:
        raise HostSandboxInvalid("host sandbox Python executable resolves outside runtime")
    python_status = resolved_python.stat()
    if (
        not stat.S_ISREG(python_status.st_mode)
        or stat.S_IMODE(python_status.st_mode) & 0o022
        or not stat.S_IMODE(python_status.st_mode) & 0o111
    ):
        raise HostSandboxInvalid("host sandbox Python executable mode is invalid")
    if require_root_owner and python_status.st_uid != 0:
        raise HostSandboxInvalid("host sandbox Python executable must be root-owned")

    syscall_digest = _digest(value["syscall_policy_digest"], "host sandbox syscall policy digest")
    seccomp = _file(
        value["seccomp_artifact"], "host sandbox seccomp artifact",
        require_root_owner=require_root_owner, mode=0o444,
    )
    if any(_overlaps(seccomp, item) for item in (owner_root, release, runtime)):
        raise HostSandboxInvalid("host sandbox seccomp artifact is not independently protected")
    if require_root_owner:
        _require_protected_ancestors(seccomp, "host sandbox seccomp artifact")
    actual_digest = _sha256_path(seccomp)
    if actual_digest != syscall_digest:
        raise HostSandboxInvalid("host sandbox seccomp artifact digest does not match policy")

    egress_raw = value["allowed_egress_profiles"]
    if egress_raw != [EgressProfile.TOOL_NONE.value]:
        raise HostSandboxInvalid("bare-metal host sandbox permits only tool-none egress")
    return HostSandboxConfig(
        architecture=architecture,
        owner_root=owner_root,
        uid=uid,
        gid=gid,
        bwrap_binary=bwrap,
        release_root=release,
        runtime_root=runtime,
        python_executable=python_executable,
        readonly_mounts=tuple(mounts),
        syscall_policy_id=_text(value["syscall_policy_id"], "host sandbox syscall policy"),
        syscall_policy_digest=syscall_digest,
        seccomp_artifact=seccomp,
        image_digest=_digest(value["image_digest"], "host sandbox image digest"),
        require_root_owner=require_root_owner,
        profile=_text(value["profile"], "host sandbox profile"),
        security_backend=_text(value["security_backend"], "host sandbox security backend"),
        network_mode=_text(value["network_mode"], "host sandbox network mode"),
        verifier=_text(value["verifier"], "host sandbox verifier"),
        record_ttl_seconds=_ttl(value["record_ttl_seconds"]),
        root_tmpfs_bytes=_tmpfs_size(value["root_tmpfs_bytes"], "root"),
        executor_tmpfs_bytes=_tmpfs_size(value["executor_tmpfs_bytes"], "executor"),
        allowed_egress_profiles=(parse_egress_profile(egress_raw[0], executor_admissible=True),),
    )


def build_host_sandbox_deployment_policy(
    config: HostSandboxConfig,
    *,
    runner: Callable[..., object] | None = None,
    clock: Callable[[], float] = time.time,
) -> SandboxDeploymentPolicy:
    """Build a fail-closed bare-metal deployment policy from verified artifacts."""
    if not isinstance(config, HostSandboxConfig):
        raise HostSandboxInvalid("host sandbox configuration is invalid")
    if runner is not None:
        _require_bubblewrap_support(str(config.bwrap_binary), runner=runner)
    security = SandboxSecurityPolicy(
        config.profile, config.security_backend, config.uid, config.gid,
        config.syscall_policy_id, config.syscall_policy_digest,
    )
    verification = SandboxVerificationPolicy(config.image_digest, config.network_mode, security)

    def verification_source(binding: SandboxLaunchBinding, mount_policy: SandboxMountPolicy, invocation: object) -> SandboxVerificationRecord:
        profile = parse_egress_profile(getattr(invocation, "egress_profile", None), executor_admissible=True)
        if profile is not EgressProfile.TOOL_NONE:
            raise HostSandboxUnavailable("bare-metal host sandbox permits only tool-none egress")
        observed_at = int(clock())
        identity = binding.identity
        return SandboxVerificationRecord(
            schema_version=1, verifier=config.verifier, observed_at=observed_at,
            expires_at=observed_at + config.record_ttl_seconds, image_digest=config.image_digest,
            profile=security.profile, security_backend=security.backend,
            syscall_policy_id=security.syscall_policy_id, syscall_policy_digest=security.syscall_policy_digest,
            owner_key=identity.owner_key, worker_id=identity.worker_id, worker_generation=identity.worker_generation,
            lease_version=identity.lease_version, recovery_generation=identity.recovery_generation,
            executor_id=identity.executor_id, executor_generation=identity.executor_generation,
            sandbox_id=binding.sandbox_id, uid=security.uid, gid=security.gid,
            mount_view_id=binding.mount_view_id, mount_policy_id=mount_policy.mount_policy_id,
            tmpfs_id=binding.tmpfs_id, security_subject_id=binding.security_subject_id,
            network_mode=config.network_mode, egress_profile=profile, rootfs_readonly=True,
            no_new_privileges=True, capabilities_dropped=True, namespaces=("user", "pid", "ipc", "net"),
        )

    def post_spawn_verification_source(
        binding: SandboxLaunchBinding,
        mount_policy: SandboxMountPolicy,
        invocation: object,
        sandbox_pid: int,
    ) -> SandboxVerificationRecord:
        attest_host_bubblewrap_process(
            sandbox_pid,
            mount_policy=mount_policy,
            security_policy=security,
        )
        return verification_source(binding, mount_policy, invocation)

    def syscall_filter_source(_binding: SandboxLaunchBinding, policy: SandboxSecurityPolicy) -> SandboxSyscallFilter:
        if policy != security:
            raise HostSandboxUnavailable("host sandbox security policy changed")
        fd = _open_nofollow(config.seccomp_artifact, "host sandbox seccomp artifact")
        try:
            status = os.fstat(fd)
            _require_regular_mode(
                status, "host sandbox seccomp artifact", 0o444,
                require_root_owner=config.require_root_owner,
            )
            digest = _sha256_fd(fd)
            if digest != config.syscall_policy_digest:
                raise HostSandboxUnavailable("host sandbox seccomp artifact digest changed")
            os.lseek(fd, 0, os.SEEK_SET)
            return SandboxSyscallFilter(fd, config.syscall_policy_id, digest)
        except Exception:
            os.close(fd)
            raise

    return SandboxDeploymentPolicy(
        verification, verification_source, syscall_filter_source, (), config.owner_root,
        readonly_mounts=config.readonly_mounts, python_executable=config.python_executable,
        bubblewrap_binary=config.bwrap_binary,
        post_spawn_verification_source=post_spawn_verification_source,
        allowed_egress_profiles=config.allowed_egress_profiles,
        root_tmpfs_bytes=config.root_tmpfs_bytes,
        executor_tmpfs_bytes=config.executor_tmpfs_bytes,
    )


def host_sandbox_deployment_policy(
    policy_path: str | Path = _DEFAULT_POLICY_PATH,
) -> SandboxDeploymentPolicy:
    """Production factory for an operator-installed bare-metal policy."""
    return build_host_sandbox_deployment_policy(load_host_sandbox_config(policy_path))


def _open_nofollow(path: Path, field: str) -> int:
    if not hasattr(os, "O_NOFOLLOW"):
        raise HostSandboxUnavailable("host sandbox requires nofollow file opens")
    try:
        return os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
    except OSError as exc:
        raise HostSandboxUnavailable(f"{field} is unavailable") from exc


def _read_all(fd: int, expected: int) -> bytes:
    chunks: list[bytes] = []
    remaining = expected
    while remaining:
        chunk = os.read(fd, min(remaining, 65536))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    if remaining or os.read(fd, 1):
        raise HostSandboxInvalid("host sandbox policy changed while reading")
    return b"".join(chunks)


def _require_regular_mode(status: os.stat_result, field: str, mode: int, *, require_root_owner: bool) -> None:
    if not stat.S_ISREG(status.st_mode) or stat.S_IMODE(status.st_mode) != mode:
        raise HostSandboxInvalid(f"{field} mode is invalid")
    if require_root_owner and status.st_uid != 0:
        raise HostSandboxInvalid(f"{field} must be root-owned")


def _directory(
    value: Any,
    field: str,
    *,
    require_root_owner: bool,
    mode: int,
    owner_uid: int | None = None,
    owner_gid: int | None = None,
) -> Path:
    path = _canonical(value, field)
    status = path.stat()
    if not stat.S_ISDIR(status.st_mode) or stat.S_IMODE(status.st_mode) != mode:
        raise HostSandboxInvalid(f"{field} mode is invalid")
    if require_root_owner and status.st_uid != 0:
        raise HostSandboxInvalid(f"{field} must be root-owned")
    if owner_uid is not None and (status.st_uid, status.st_gid) != (owner_uid, owner_gid):
        raise HostSandboxInvalid(f"{field} ownership is invalid")
    return path


def _file(value: Any, field: str, *, require_root_owner: bool, mode: int) -> Path:
    path = _canonical(value, field)
    status = path.stat()
    if not stat.S_ISREG(status.st_mode) or stat.S_IMODE(status.st_mode) != mode:
        raise HostSandboxInvalid(f"{field} mode is invalid")
    if require_root_owner and status.st_uid != 0:
        raise HostSandboxInvalid(f"{field} must be root-owned")
    return path


def _require_protected_ancestors(path: Path, field: str) -> None:
    """Reject replacement through non-root-owned or writable parent directories."""
    current = path.parent
    while True:
        try:
            status = current.stat()
        except OSError as exc:
            raise HostSandboxInvalid(f"{field} parent directory is unavailable") from exc
        mode = stat.S_IMODE(status.st_mode)
        if not stat.S_ISDIR(status.st_mode) or status.st_uid != 0 or mode & 0o022:
            raise HostSandboxInvalid(f"{field} parent directory is not protected")
        if current == current.parent:
            return
        current = current.parent


def _canonical(value: Any, field: str) -> Path:
    try:
        raw = str(value) if isinstance(value, Path) else _text(value, field)
        path = Path(raw)
        if path.is_symlink():
            raise HostSandboxInvalid(f"{field} must not be a symlink")
        result = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise HostSandboxInvalid(f"{field} is unavailable") from exc
    if not result.is_absolute():
        raise HostSandboxInvalid(f"{field} is invalid")
    return result


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip() or "\x00" in value:
        raise HostSandboxInvalid(f"{field} is invalid")
    return value


def _digest(value: Any, field: str) -> str:
    text = _text(value, field)
    if not text.startswith(_DIGEST_RE) or len(text) != 71 or any(ch not in "0123456789abcdef" for ch in text[7:]):
        raise HostSandboxInvalid(f"{field} is invalid")
    return text


def _ttl(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 < value <= 300:
        raise HostSandboxInvalid("host sandbox verification ttl is invalid")
    return value


def _tmpfs_size(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 < value <= 1 << 30:
        raise HostSandboxInvalid(f"host sandbox {field} tmpfs size is invalid")
    return value


def _normalized_architecture(value: str) -> str:
    normalized = str(value or "").strip().lower()
    aliases = {"amd64": "x86_64", "x64": "x86_64", "arm64": "aarch64"}
    normalized = aliases.get(normalized, normalized)
    if normalized not in {"x86_64", "aarch64"}:
        raise HostSandboxInvalid("host sandbox architecture is unsupported")
    return normalized


def _sha256_path(path: Path) -> str:
    fd = _open_nofollow(path, "host sandbox seccomp artifact")
    try:
        return _sha256_fd(fd)
    finally:
        os.close(fd)


def _sha256_fd(fd: int) -> str:
    digest = hashlib.sha256()
    while True:
        chunk = os.read(fd, 65536)
        if not chunk:
            break
        digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _overlaps(first: Path, second: Path) -> bool:
    return first == second or first in second.parents or second in first.parents
