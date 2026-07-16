"""Operator-owned Docker admission for authenticated Bubblewrap executors.

This module deliberately does not create Docker containers.  It verifies the
already-running, operator-provisioned container that hosts an Owner Worker,
then supplies the immutable policy and per-launch verification record consumed
by :mod:`tool_executor_supervisor`.  Docker access is injected so the security
contract is unit-testable without a daemon.
"""
from __future__ import annotations

import json
import os
import platform
import re
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Protocol, Sequence

from hermes_cli.owner_worker.executor_identity import EgressProfile, parse_egress_profile
from hermes_cli.owner_worker.tool_executor_sandbox import (
    ExecutorIsolationUnavailable,
    SandboxDeploymentPolicy,
    SandboxLaunchBinding,
    SandboxMountPolicy,
    SandboxSecurityPolicy,
    SandboxSyscallFilter,
    SandboxVerificationInvalid,
    SandboxVerificationPolicy,
    SandboxVerificationRecord,
)

_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}$")
_REQUIRED_BWRAP_OPTIONS = ("--bind-fd", "--size", "--uid", "--gid", "--cap-drop", "--seccomp")
_SENSITIVE_MOUNT_ROOTS = (Path("/"), Path("/proc"), Path("/sys"), Path("/dev"), Path("/run"), Path("/var/run"))


class DockerSandboxInvalid(SandboxVerificationInvalid):
    """The operator-supplied Docker sandbox configuration is unsafe."""


class DockerSandboxUnavailable(ExecutorIsolationUnavailable):
    """The host cannot prove the configured Docker sandbox is safe."""


class DockerInspectClient(Protocol):
    """Minimal inspect boundary; tests provide this instead of a Docker daemon."""

    def inspect_container(self, container_id: str) -> Mapping[str, Any]:
        """Return the Docker ``inspect`` document for ``container_id``."""


@dataclass(frozen=True)
class DockerSandboxMount:
    """One exact, read-only Docker mount approved by the deployment operator."""

    source: Path
    destination: Path
    mount_type: str = "bind"

    def __post_init__(self) -> None:
        mount_type = _required_text(self.mount_type, "Docker mount type")
        # This profile deliberately supports only explicit host bind mounts.
        # Docker volumes and tmpfs are daemon-managed objects that require
        # separate, observable policy models rather than path matching.
        if mount_type != "bind":
            raise DockerSandboxInvalid("Docker mount type must be bind")
        source = _canonical_host_path(self.source, "Docker mount source")
        destination = _container_path(self.destination, "Docker mount destination")
        if _is_sensitive_host_mount(source) or _is_forbidden_container_mount(destination):
            raise DockerSandboxInvalid("Docker mount is too broad or sensitive")
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "destination", destination)
        object.__setattr__(self, "mount_type", mount_type)


@dataclass(frozen=True)
class DockerSandboxConfig:
    """Strict, operator-only deployment inputs for an Owner Worker container."""

    container_id: str
    image_digest: str
    owner_root: Path
    readonly_global_roots: tuple[Path, ...]
    uid: int
    gid: int
    network_mode: str
    mounts: tuple[DockerSandboxMount, ...]
    bwrap_binary: Path
    syscall_policy_id: str
    syscall_policy_digest: str
    seccomp_fd: int
    profile: str = "executor-bwrap-v1"
    security_backend: str = "docker-bwrap-seccomp-v1"
    verifier: str = "docker-sandbox-inspect-v1"
    record_ttl_seconds: int = 30
    allowed_egress_profiles: tuple[EgressProfile, ...] = (
        EgressProfile.TOOL_NONE,
        EgressProfile.TOOL_PUBLIC,
        EgressProfile.PROTECTED_TARGET,
    )

    def __post_init__(self) -> None:
        container_id = _required_text(self.container_id, "Docker container id")
        image_digest = _digest(self.image_digest, "Docker image digest")
        owner_root = _canonical_host_path(self.owner_root, "Docker owner root")
        bwrap_binary = _canonical_host_path(self.bwrap_binary, "Bubblewrap executable")
        profile = _required_text(self.profile, "Docker sandbox profile")
        backend = _required_text(self.security_backend, "Docker sandbox backend")
        verifier = _required_text(self.verifier, "Docker verification source")
        syscall_policy_id = _required_text(self.syscall_policy_id, "Docker syscall policy")
        syscall_policy_digest = _digest(self.syscall_policy_digest, "Docker syscall policy digest")
        if not isinstance(self.uid, int) or not isinstance(self.gid, int) or self.uid <= 0 or self.gid <= 0:
            raise DockerSandboxInvalid("Docker sandbox uid/gid must be non-root")
        if not isinstance(self.seccomp_fd, int) or self.seccomp_fd < 0:
            raise DockerSandboxInvalid("Docker sandbox seccomp descriptor is invalid")
        if not isinstance(self.record_ttl_seconds, int) or not 0 < self.record_ttl_seconds <= 300:
            raise DockerSandboxInvalid("Docker verification record ttl is invalid")
        network_mode = _required_text(self.network_mode, "Docker network mode")
        roots: list[Path] = []
        for root in self.readonly_global_roots:
            parsed = _canonical_host_path(root, "Docker readonly global root")
            if _is_sensitive_host_mount(parsed) or parsed == owner_root or owner_root in parsed.parents or parsed in owner_root.parents:
                raise DockerSandboxInvalid("Docker readonly global root is unsafe")
            if parsed not in roots:
                roots.append(parsed)
        if not roots:
            raise DockerSandboxInvalid("Docker readonly global roots are required")
        mounts = tuple(self.mounts)
        if len({(item.source, item.destination, item.mount_type) for item in mounts}) != len(mounts):
            raise DockerSandboxInvalid("Docker mounts must be unique")
        try:
            egress_profiles = tuple(parse_egress_profile(item, executor_admissible=True) for item in self.allowed_egress_profiles)
        except Exception as exc:
            raise DockerSandboxInvalid("Docker egress profile is invalid") from exc
        if not egress_profiles or len(set(egress_profiles)) != len(egress_profiles):
            raise DockerSandboxInvalid("Docker egress profiles are invalid")
        object.__setattr__(self, "container_id", container_id)
        object.__setattr__(self, "image_digest", image_digest)
        object.__setattr__(self, "owner_root", owner_root)
        object.__setattr__(self, "readonly_global_roots", tuple(roots))
        object.__setattr__(self, "network_mode", network_mode)
        object.__setattr__(self, "mounts", mounts)
        object.__setattr__(self, "bwrap_binary", bwrap_binary)
        object.__setattr__(self, "profile", profile)
        object.__setattr__(self, "security_backend", backend)
        object.__setattr__(self, "verifier", verifier)
        object.__setattr__(self, "syscall_policy_id", syscall_policy_id)
        object.__setattr__(self, "syscall_policy_digest", syscall_policy_digest)
        object.__setattr__(self, "allowed_egress_profiles", egress_profiles)


def parse_docker_sandbox_config(value: Mapping[str, Any]) -> DockerSandboxConfig:
    """Parse the complete operator document; unknown fields fail closed."""
    if not isinstance(value, Mapping):
        raise DockerSandboxInvalid("Docker sandbox configuration must be an object")
    required = {
        "container_id", "image_digest", "owner_root", "readonly_global_roots", "uid", "gid", "network_mode",
        "mounts", "bwrap_binary", "syscall_policy_id", "syscall_policy_digest", "seccomp_fd",
    }
    optional = {"profile", "security_backend", "verifier", "record_ttl_seconds", "allowed_egress_profiles"}
    unknown = set(value) - required - optional
    missing = required - set(value)
    if unknown or missing:
        raise DockerSandboxInvalid("Docker sandbox configuration fields are invalid")
    roots = _path_list(value["readonly_global_roots"], "Docker readonly global roots")
    mounts_raw = value["mounts"]
    if not isinstance(mounts_raw, list):
        raise DockerSandboxInvalid("Docker mounts must be a list")
    mounts: list[DockerSandboxMount] = []
    for item in mounts_raw:
        if not isinstance(item, Mapping) or set(item) - {"source", "destination", "type"} or {"source", "destination"} - set(item):
            raise DockerSandboxInvalid("Docker mount configuration is invalid")
        mounts.append(DockerSandboxMount(item["source"], item["destination"], item.get("type", "bind")))
    egress = value.get("allowed_egress_profiles", [
        EgressProfile.TOOL_NONE.value,
        EgressProfile.TOOL_PUBLIC.value,
        EgressProfile.PROTECTED_TARGET.value,
    ])
    if not isinstance(egress, list):
        raise DockerSandboxInvalid("Docker egress profiles must be a list")
    return DockerSandboxConfig(
        container_id=value["container_id"], image_digest=value["image_digest"], owner_root=value["owner_root"],
        readonly_global_roots=tuple(roots), uid=value["uid"], gid=value["gid"], network_mode=value["network_mode"],
        mounts=tuple(mounts), bwrap_binary=value["bwrap_binary"], syscall_policy_id=value["syscall_policy_id"],
        syscall_policy_digest=value["syscall_policy_digest"], seccomp_fd=value["seccomp_fd"], profile=value.get("profile", "executor-bwrap-v1"),
        security_backend=value.get("security_backend", "docker-bwrap-seccomp-v1"), verifier=value.get("verifier", "docker-sandbox-inspect-v1"),
        record_ttl_seconds=value.get("record_ttl_seconds", 30), allowed_egress_profiles=tuple(egress),
    )


def docker_sandbox_config_from_environment(environment: Mapping[str, str] | None = None) -> DockerSandboxConfig:
    """Load the sole supported operator configuration variable.

    Child/tool inputs are never consulted.  Deployments should pass this JSON
    only through their owner-worker service definition.
    """
    environment = os.environ if environment is None else environment
    raw = str(environment.get("HERMES_DOCKER_SANDBOX_CONFIG", "") or "")
    try:
        document = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise DockerSandboxInvalid("HERMES_DOCKER_SANDBOX_CONFIG is required") from exc
    return parse_docker_sandbox_config(document)


def _inspect(client: DockerInspectClient | Callable[[str], Mapping[str, Any]], container_id: str) -> Mapping[str, Any]:
    try:
        result = client.inspect_container(container_id) if hasattr(client, "inspect_container") else client(container_id)  # type: ignore[operator]
    except Exception as exc:
        raise DockerSandboxUnavailable("Docker inspection is unavailable") from exc
    if not isinstance(result, Mapping):
        raise DockerSandboxUnavailable("Docker inspection is invalid")
    return result


def preflight_docker_sandbox(
    config: DockerSandboxConfig,
    *,
    inspect_client: DockerInspectClient | Callable[[str], Mapping[str, Any]],
    platform_name: str | None = None,
    runner: Callable[..., object] = subprocess.run,
) -> Mapping[str, Any]:
    """Prove host, Bubblewrap, and current Docker container controls are safe."""
    if not isinstance(config, DockerSandboxConfig):
        raise DockerSandboxInvalid("Docker sandbox configuration is invalid")
    if (platform_name or platform.system()).lower() != "linux":
        raise DockerSandboxUnavailable("authenticated Docker sandbox requires Linux")
    try:
        mode = config.bwrap_binary.stat().st_mode
    except OSError as exc:
        raise DockerSandboxUnavailable("Bubblewrap executable is unavailable") from exc
    if not config.bwrap_binary.is_file() or not mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
        raise DockerSandboxUnavailable("Bubblewrap executable is not executable")
    try:
        probe = runner([str(config.bwrap_binary), "--help"], capture_output=True, text=True, timeout=5, check=False, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        raise DockerSandboxUnavailable("Bubblewrap capability probe failed") from exc
    output = f"{getattr(probe, 'stdout', '')}\n{getattr(probe, 'stderr', '')}"
    if getattr(probe, "returncode", 1) != 0 or any(option not in output for option in _REQUIRED_BWRAP_OPTIONS):
        raise DockerSandboxUnavailable("Bubblewrap does not support required isolation controls")
    try:
        os.fstat(config.seccomp_fd)
    except OSError as exc:
        raise DockerSandboxUnavailable("Docker sandbox seccomp descriptor is unavailable") from exc
    inspection = _inspect(inspect_client, config.container_id)
    _validate_docker_inspection(inspection, config)
    return inspection


def build_docker_sandbox_deployment_policy(
    config: DockerSandboxConfig,
    *,
    inspect_client: DockerInspectClient | Callable[[str], Mapping[str, Any]],
    platform_name: str | None = None,
    runner: Callable[..., object] = subprocess.run,
    clock: Callable[[], float] = time.time,
) -> SandboxDeploymentPolicy:
    """Preflight Docker once and return a policy that re-inspects every launch."""
    preflight_docker_sandbox(config, inspect_client=inspect_client, platform_name=platform_name, runner=runner)
    security = SandboxSecurityPolicy(
        config.profile, config.security_backend, config.uid, config.gid,
        config.syscall_policy_id, config.syscall_policy_digest,
    )
    verification = SandboxVerificationPolicy(config.image_digest, config.network_mode, security)

    def verification_source(binding: SandboxLaunchBinding, mount_policy: SandboxMountPolicy, invocation: object) -> SandboxVerificationRecord:
        inspection = _inspect(inspect_client, config.container_id)
        _validate_docker_inspection(inspection, config)
        profile = parse_egress_profile(getattr(invocation, "egress_profile", None), executor_admissible=True)
        if profile not in config.allowed_egress_profiles:
            raise DockerSandboxUnavailable("Docker network profile does not permit executor egress")
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

    def syscall_filter_source(_binding: SandboxLaunchBinding, policy: SandboxSecurityPolicy) -> SandboxSyscallFilter:
        if policy != security:
            raise DockerSandboxUnavailable("Docker sandbox security policy changed")
        try:
            inherited_fd = os.dup(config.seccomp_fd)
        except OSError as exc:
            raise DockerSandboxUnavailable("Docker sandbox seccomp descriptor is unavailable") from exc
        return SandboxSyscallFilter(inherited_fd, config.syscall_policy_id, config.syscall_policy_digest)

    return SandboxDeploymentPolicy(verification, verification_source, syscall_filter_source, config.readonly_global_roots, config.owner_root)


class DockerCliInspectClient:
    """Small production adapter; tests should inject a fake inspect client instead."""

    def __init__(self, docker_binary: str = "docker", runner: Callable[..., object] = subprocess.run) -> None:
        self.docker_binary = _required_text(docker_binary, "Docker executable")
        self._runner = runner

    def inspect_container(self, container_id: str) -> Mapping[str, Any]:
        try:
            result = self._runner([self.docker_binary, "inspect", "--type", "container", container_id], capture_output=True, text=True, timeout=5, check=False, stdin=subprocess.DEVNULL)
            if getattr(result, "returncode", 1) != 0:
                raise ValueError("non-zero status")
            documents = json.loads(str(getattr(result, "stdout", "")))
            if not isinstance(documents, list) or len(documents) != 1 or not isinstance(documents[0], Mapping):
                raise ValueError("unexpected inspect document")
            return documents[0]
        except (OSError, subprocess.SubprocessError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise DockerSandboxUnavailable("Docker inspection is unavailable") from exc


def docker_sandbox_deployment_policy() -> SandboxDeploymentPolicy:
    """Factory for ``HERMES_SANDBOX_DEPLOYMENT_POLICY`` production wiring."""
    config = docker_sandbox_config_from_environment()
    return build_docker_sandbox_deployment_policy(config, inspect_client=DockerCliInspectClient())


def _validate_docker_inspection(inspection: Mapping[str, Any], config: DockerSandboxConfig) -> None:
    config_section = _mapping(inspection, "Config")
    host = _mapping(inspection, "HostConfig")
    state = _mapping(inspection, "State")
    if state.get("Running") is not True:
        raise DockerSandboxUnavailable("Docker sandbox container is not running")
    if not _matches_digest(inspection, config.image_digest):
        raise DockerSandboxUnavailable("Docker sandbox image digest does not match")
    if str(config_section.get("User", "")) != f"{config.uid}:{config.gid}":
        raise DockerSandboxUnavailable("Docker sandbox must run as configured non-root uid:gid")
    if host.get("ReadonlyRootfs") is not True or host.get("Privileged") is not False:
        raise DockerSandboxUnavailable("Docker sandbox root filesystem or privilege controls are invalid")
    if host.get("NetworkMode") != config.network_mode:
        raise DockerSandboxUnavailable("Docker sandbox network mode does not match")
    security_options = host.get("SecurityOpt")
    if not isinstance(security_options, list) or "no-new-privileges:true" not in security_options:
        raise DockerSandboxUnavailable("Docker sandbox no-new-privileges is required")
    cap_add = host.get("CapAdd")
    cap_drop = host.get("CapDrop")
    if cap_add not in (None, []) or not isinstance(cap_drop, list) or "ALL" not in cap_drop:
        raise DockerSandboxUnavailable("Docker sandbox capability controls are invalid")
    _validate_mounts(inspection.get("Mounts"), config.mounts)


def _validate_mounts(actual: object, expected: Sequence[DockerSandboxMount]) -> None:
    if not isinstance(actual, list):
        raise DockerSandboxUnavailable("Docker sandbox mounts are invalid")
    parsed: set[tuple[Path, Path, str]] = set()
    for item in actual:
        if not isinstance(item, Mapping):
            raise DockerSandboxUnavailable("Docker sandbox mounts are invalid")
        source, destination, mount_type = item.get("Source"), item.get("Destination"), item.get("Type")
        if item.get("RW") is not False:
            raise DockerSandboxUnavailable("Docker sandbox mounts must be read-only")
        try:
            parsed.add((Path(str(source)), Path(str(destination)), str(mount_type)))
        except (TypeError, ValueError) as exc:
            raise DockerSandboxUnavailable("Docker sandbox mounts are invalid") from exc
    approved = {(mount.source, mount.destination, mount.mount_type) for mount in expected}
    if parsed != approved:
        raise DockerSandboxUnavailable("Docker sandbox mounts do not match operator policy")


def _matches_digest(inspection: Mapping[str, Any], expected: str) -> bool:
    candidates = [inspection.get("Image")]
    config_section = inspection.get("Config")
    if isinstance(config_section, Mapping):
        candidates.append(config_section.get("Image"))
    repo_digests = inspection.get("RepoDigests")
    if isinstance(repo_digests, list):
        candidates.extend(repo_digests)
    return any(isinstance(value, str) and (value == expected or value.endswith("@" + expected)) for value in candidates)


def _mapping(value: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    result = value.get(name)
    if not isinstance(result, Mapping):
        raise DockerSandboxUnavailable("Docker inspection is incomplete")
    return result


def _required_text(value: object, field: str) -> str:
    text = str(value or "").strip()
    if not text or "\x00" in text:
        raise DockerSandboxInvalid(f"{field} is invalid")
    return text


def _digest(value: object, field: str) -> str:
    digest = _required_text(value, field)
    if not _DIGEST_RE.fullmatch(digest):
        raise DockerSandboxInvalid(f"{field} is invalid")
    return digest


def _absolute_path(value: object, field: str) -> Path:
    text = _required_text(value, field)
    path = Path(text)
    if not path.is_absolute():
        raise DockerSandboxInvalid(f"{field} is invalid")
    return path


def _path_list(value: object, field: str) -> list[Path]:
    if not isinstance(value, list):
        raise DockerSandboxInvalid(f"{field} must be a list")
    return [_canonical_host_path(item, field) for item in value]


def _canonical_host_path(value: object, field: str) -> Path:
    path = _absolute_path(value, field)
    try:
        return path.resolve(strict=True)
    except OSError as exc:
        raise DockerSandboxInvalid(f"{field} is unavailable") from exc


def _container_path(value: object, field: str) -> Path:
    text = _required_text(value, field)
    path = PurePosixPath(text)
    if not path.is_absolute() or ".." in path.parts or str(path) == ".":
        raise DockerSandboxInvalid(f"{field} is invalid")
    return Path(str(path))


def _is_sensitive_host_mount(path: Path) -> bool:
    if path == Path("/"):
        return True
    return any(path == root or root in path.parents for root in _SENSITIVE_MOUNT_ROOTS if root != Path("/"))


def _is_forbidden_container_mount(path: Path) -> bool:
    return path in {Path("/var/run/docker.sock"), Path("/run/docker.sock")} or any(
        path == root or root in path.parents
        for root in (Path("/proc"), Path("/sys"), Path("/dev"), Path("/run"), Path("/var/run"))
    )
