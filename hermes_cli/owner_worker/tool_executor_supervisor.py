"""Task-bound, Bubblewrap-isolated Tool Executor supervisor."""
from __future__ import annotations

import json
import os
import signal
import select
import stat
import subprocess
import threading
import time
from dataclasses import dataclass, field
from types import MappingProxyType
from pathlib import Path
from typing import Any, Callable, Mapping

from hermes_cli.authenticated_file_context import AuthenticatedWorkspaceContext
from hermes_cli.controlled_roots import ExpectedType, RootKind
from hermes_cli.dashboard_auth.audit import AuthorityAuditEvent, AuthorityAuditReason
from hermes_cli.owner_worker.credential_broker import CredentialBroker
from hermes_cli.owner_worker.executor_identity import (
    EgressProfile,
    ExecutorIdentity,
    ExecutorIdentityInvalid,
    ExecutorInvocation,
    ExecutorResourceDecision,
    default_executor_resource_decision,
    parse_egress_profile,
)
from hermes_cli.owner_worker.tool_executor_sandbox import (
    BubblewrapLaunchSpec,
    SandboxDeploymentPolicy,
    SandboxLaunchBinding,
    SandboxMountPolicy,
    SandboxSecurityPolicy,
    SandboxSyscallFilter,
    SandboxVerificationInvalid,
    validate_sandbox_syscall_filter,
    SandboxVerificationPolicy,
    SandboxVerificationRecord,
    default_readonly_global_mount_roots,
    build_bubblewrap_launch_spec,
    validate_sandbox_verification_record,
)
from hermes_cli.tool_executor_runtime.env import build_executor_environment


@dataclass(frozen=True)
class ExecutorDispatchContext:
    """Trusted owner-worker inputs used to create an executor identity."""

    identity: ExecutorIdentity
    workspace_context: AuthenticatedWorkspaceContext
    owner_home: Path


@dataclass
class _LiveInvocation:
    identity: ExecutorIdentity
    invocation_id: str
    process: Any
    sandbox_binding: SandboxLaunchBinding | None = None
    verification_record: SandboxVerificationRecord | None = None


_NETWORK_TOOL_NAMES = frozenset({
    "web_search", "web_extract", "browser_navigate", "browser_snapshot",
    "browser_click", "browser_type", "browser_scroll", "browser_back",
    "browser_press", "browser_get_images", "browser_vision", "browser_console",
    "image_generation", "text_to_speech", "speech_to_text",
})


@dataclass(frozen=True)
class ExecutorEgressPolicy:
    """Owner-side tool-name selection with no child-controlled inputs."""

    by_tool_name: Mapping[str, EgressProfile | str] = field(
        default_factory=lambda: {
            name: EgressProfile.TOOL_PUBLIC for name in _NETWORK_TOOL_NAMES
        }
    )
    default: EgressProfile = EgressProfile.TOOL_NONE

    def __post_init__(self) -> None:
        selected = {str(name): parse_egress_profile(profile) for name, profile in dict(self.by_tool_name).items()}
        if any(not name or "\x00" in name for name in selected):
            raise ExecutorIdentityInvalid("executor egress tool mapping is invalid")
        object.__setattr__(self, "by_tool_name", MappingProxyType(selected))
        object.__setattr__(self, "default", parse_egress_profile(self.default))

    def select(self, function_name: str) -> EgressProfile:
        profile = self.by_tool_name.get(str(function_name), self.default)
        return parse_egress_profile(profile, executor_admissible=True)


class ToolExecutorSupervisor:
    """Launch one namespace-isolated process per authenticated invocation.

    There is deliberately no raw-Python fallback: authenticated execution must
    fail admission if the Linux Bubblewrap boundary is unavailable.
    """

    def __init__(
        self,
        *,
        owner_home: str | Path,
        workspace_context: AuthenticatedWorkspaceContext,
        lease: Any,
        process_factory: Callable[..., subprocess.Popen] = subprocess.Popen,
        credential_broker: CredentialBroker | None = None,
        sandbox_builder: Callable[..., BubblewrapLaunchSpec] = build_bubblewrap_launch_spec,
        control_home: str | Path | None = None,
        readonly_global_mount_roots: tuple[str | Path, ...] | None = None,
        owner_root: str | Path | None = None,
        deployment_policy: SandboxDeploymentPolicy | None = None,
        root_tmpfs_bytes: int = 64 << 20,
        executor_tmpfs_bytes: int = 32 << 20,
        sandbox_verification_source: Callable[[SandboxLaunchBinding, SandboxMountPolicy, ExecutorInvocation], SandboxVerificationRecord | None] | None = None,
        sandbox_verification_policy: SandboxVerificationPolicy | None = None,
        sandbox_syscall_filter_source: Callable[[SandboxLaunchBinding, SandboxSecurityPolicy], SandboxSyscallFilter | None] | None = None,
        egress_policy: ExecutorEgressPolicy | None = None,
        resource_decision_source: Callable[[ExecutorIdentity], ExecutorResourceDecision] = default_executor_resource_decision,
        audit_reporter: Callable[[AuthorityAuditEvent, AuthorityAuditReason, ExecutorIdentity], None] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.owner_home = Path(owner_home).resolve()
        self.workspace_context = workspace_context
        self.lease = lease
        self.process_factory = process_factory
        self.credential_broker = credential_broker or CredentialBroker()
        self.sandbox_builder = sandbox_builder
        self.control_home = Path(control_home).resolve() if control_home else None
        if deployment_policy is not None:
            if not isinstance(deployment_policy, SandboxDeploymentPolicy):
                raise SandboxVerificationInvalid("sandbox deployment policy is invalid")
            if any(value is not None for value in (
                readonly_global_mount_roots,
                owner_root,
                sandbox_verification_source,
                sandbox_verification_policy,
                sandbox_syscall_filter_source,
            )):
                raise SandboxVerificationInvalid("sandbox deployment policy cannot be mixed with individual sources")
            self.deployment_policy = deployment_policy
            self.readonly_global_mount_roots = deployment_policy.readonly_global_roots
            self.readonly_mounts = deployment_policy.readonly_mounts
            self.python_executable = deployment_policy.python_executable
            self.bubblewrap_binary = deployment_policy.bubblewrap_binary
            self.owner_root = deployment_policy.owner_root
            self.allowed_egress_profiles = deployment_policy.allowed_egress_profiles
            self.sandbox_verification_source = deployment_policy.verification_source
            self.sandbox_post_spawn_verification_source = deployment_policy.post_spawn_verification_source
            self.sandbox_verification_policy = deployment_policy.verification_policy
            self.sandbox_syscall_filter_source = deployment_policy.syscall_filter_source
            root_tmpfs_bytes = deployment_policy.root_tmpfs_bytes
            executor_tmpfs_bytes = deployment_policy.executor_tmpfs_bytes
        else:
            # Retained for direct unit construction. Authenticated worker startup
            # supplies a deployment policy and never accepts these defaults.
            self.deployment_policy = None
            self.readonly_global_mount_roots = readonly_global_mount_roots or default_readonly_global_mount_roots()
            self.readonly_mounts = ()
            self.python_executable = None
            self.bubblewrap_binary = None
            self.owner_root = Path(owner_root).resolve() if owner_root else None
            self.allowed_egress_profiles = (
                EgressProfile.TOOL_NONE, EgressProfile.TOOL_PUBLIC, EgressProfile.PROTECTED_TARGET,
            )
            self.sandbox_verification_source = sandbox_verification_source
            self.sandbox_post_spawn_verification_source = None
            self.sandbox_verification_policy = sandbox_verification_policy
            self.sandbox_syscall_filter_source = sandbox_syscall_filter_source
        self.root_tmpfs_bytes = root_tmpfs_bytes
        self.executor_tmpfs_bytes = executor_tmpfs_bytes
        self.egress_policy = egress_policy or ExecutorEgressPolicy()
        self.resource_decision_source = resource_decision_source
        self.audit_reporter = audit_reporter
        if (
            not isinstance(self.egress_policy, ExecutorEgressPolicy)
            or not callable(self.resource_decision_source)
            or (self.audit_reporter is not None and not callable(self.audit_reporter))
        ):
            raise ExecutorIdentityInvalid("executor egress policy is invalid")
        self._clock = clock
        self._lock = threading.RLock()
        self._identities: dict[tuple[str, str], ExecutorIdentity] = {}
        self._live: dict[tuple[tuple, str], _LiveInvocation] = {}
        self._revoked: set[tuple] = set()

    def _report(self, event: AuthorityAuditEvent, reason: AuthorityAuditReason, identity: ExecutorIdentity) -> None:
        if self.audit_reporter is None:
            return
        try:
            self.audit_reporter(event, reason, identity)
        except Exception:
            # Audit delivery cannot relax executor admission or revocation.
            pass

    def identity_for(self, *, task_id: str, session_id: str) -> ExecutorIdentity:
        key = (str(task_id or ""), str(session_id or ""))
        if not all(key):
            raise ValueError("authenticated executor requires task_id and session_id")
        with self._lock:
            identity = self._identities.get(key)
            if identity is None:
                identity = ExecutorIdentity.for_task(
                    self.lease,
                    workspace_prefix=self.workspace_context.workspace_prefix,
                    task_id=key[0],
                    session_id=key[1],
                )
                self._identities[key] = identity
            if identity.stable_key in self._revoked:
                raise PermissionError("executor generation is revoked")
            return identity

    def dispatch(
        self,
        *,
        function_name: str,
        function_args: dict[str, Any],
        task_id: str,
        session_id: str,
        tool_call_id: str,
        turn_id: str,
        api_request_id: str,
    ) -> str:
        identity = self.identity_for(task_id=task_id, session_id=session_id)
        try:
            egress_profile = self.egress_policy.select(function_name)
            if egress_profile not in self.allowed_egress_profiles:
                raise ExecutorIdentityInvalid("authenticated network egress is not configured")
        except ExecutorIdentityInvalid:
            self._report(AuthorityAuditEvent.EGRESS_REJECTED, AuthorityAuditReason.EGRESS_PROFILE_REJECTED, identity)
            raise
        try:
            resource_decision = self._resource_decision_for(identity)
        except ExecutorIdentityInvalid as exc:
            self._report(
                AuthorityAuditEvent.RESOURCE_REJECTED,
                (
                    AuthorityAuditReason.RESOURCE_DECISION_UNAVAILABLE
                    if "unavailable" in str(exc)
                    else AuthorityAuditReason.RESOURCE_DECISION_INVALID
                ),
                identity,
            )
            raise
        invocation = ExecutorInvocation(
            identity=identity,
            tool_name=function_name,
            arguments=function_args,
            tool_call_id=tool_call_id or "tool-call",
            turn_id=turn_id or "turn",
            api_request_id=api_request_id or "request",
            invocation_id=os.urandom(16).hex(),
            egress_profile=egress_profile,
            resource_decision=resource_decision,
        )
        try:
            return self._dispatch_invocation(invocation)
        except PermissionError:
            self._report(AuthorityAuditEvent.EXECUTOR_REJECTED, AuthorityAuditReason.EXECUTOR_LEASE_REJECTED, identity)
            raise
        except SandboxVerificationInvalid as exc:
            self._report(
                AuthorityAuditEvent.EXECUTOR_REJECTED,
                (
                    AuthorityAuditReason.SYSCALL_FILTER_REJECTED
                    if "syscall filter" in str(exc)
                    else AuthorityAuditReason.SANDBOX_REJECTED
                ),
                identity,
            )
            raise

    def _resource_decision_for(self, identity: ExecutorIdentity) -> ExecutorResourceDecision:
        try:
            decision = self.resource_decision_source(identity)
        except ExecutorIdentityInvalid:
            raise
        except Exception as exc:
            raise ExecutorIdentityInvalid("executor resource decision is unavailable") from exc
        if not isinstance(decision, ExecutorResourceDecision):
            raise ExecutorIdentityInvalid("executor resource decision is invalid")
        decision.require_identity(identity)
        return decision

    def _require_current_lease_identity(self, identity: ExecutorIdentity) -> None:
        expected = (
            self.lease.owner_key,
            self.lease.worker_id,
            self.lease.worker_generation,
            self.lease.lease_version,
            self.lease.recovery_generation,
        )
        actual = (
            identity.owner_key,
            identity.worker_id,
            identity.worker_generation,
            identity.lease_version,
            identity.recovery_generation,
        )
        if actual != expected:
            raise PermissionError("executor identity does not match the active owner-worker lease")

    def _sandbox_binding(self, identity: ExecutorIdentity) -> SandboxLaunchBinding:
        self._require_current_lease_identity(identity)
        runtime_home = self.owner_home / "runtime" / "executors" / identity.executor_id / f"gen-{identity.executor_generation}"
        return SandboxLaunchBinding(
            identity=identity,
            sandbox_id=os.urandom(16).hex(),
            owner_home=self.owner_home,
            runtime_home=runtime_home,
        )

    def _sandbox_mount_policy(self, binding: SandboxLaunchBinding) -> SandboxMountPolicy:
        return SandboxMountPolicy(
            binding=binding,
            readonly_global_roots=(
                () if self.readonly_mounts else tuple(self.readonly_global_mount_roots)
            ),
            workspace_root=self.workspace_context.roots.get(RootKind.WORKSPACE).canonical_path,
            control_home=self.control_home,
            owner_root=self.owner_root,
            readonly_mounts=tuple(self.readonly_mounts),
            python_executable=self.python_executable,
            root_tmpfs_bytes=self.root_tmpfs_bytes,
            executor_tmpfs_bytes=self.executor_tmpfs_bytes,
        )

    def _require_verified_sandbox(
        self,
        binding: SandboxLaunchBinding,
        mount_policy: SandboxMountPolicy,
        invocation: ExecutorInvocation,
    ) -> SandboxVerificationRecord:
        if self.sandbox_verification_source is None or self.sandbox_verification_policy is None:
            raise SandboxVerificationInvalid("trusted sandbox verification is required")
        try:
            record = self.sandbox_verification_source(binding, mount_policy, invocation)
        except SandboxVerificationInvalid:
            raise
        except Exception as exc:
            raise SandboxVerificationInvalid("trusted sandbox verification is unavailable") from exc
        return validate_sandbox_verification_record(
            record,
            binding=binding,
            mount_policy=mount_policy,
            egress_profile=invocation.egress_profile,
            policy=self.sandbox_verification_policy,
            now=int(self._clock()),
        )

    def _require_syscall_filter(self, binding: SandboxLaunchBinding) -> SandboxSyscallFilter:
        if self.sandbox_verification_policy is None or self.sandbox_syscall_filter_source is None:
            raise SandboxVerificationInvalid("trusted sandbox syscall filter is required")
        try:
            syscall_filter = self.sandbox_syscall_filter_source(binding, self.sandbox_verification_policy.security_policy)
            return validate_sandbox_syscall_filter(
                syscall_filter, policy=self.sandbox_verification_policy.security_policy
            )
        except SandboxVerificationInvalid:
            raise
        except Exception as exc:
            raise SandboxVerificationInvalid("trusted sandbox syscall filter is unavailable") from exc

    @staticmethod
    def _bubblewrap_child_pid(info_fd: int, *, timeout: float = 5.0) -> int:
        """Read Bubblewrap's exact host-visible sandbox PID from ``--info-fd``."""
        deadline = time.monotonic() + timeout
        raw = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SandboxVerificationInvalid("Bubblewrap sandbox identity timed out")
            ready, _, _ = select.select([info_fd], [], [], remaining)
            if not ready:
                raise SandboxVerificationInvalid("Bubblewrap sandbox identity timed out")
            chunk = os.read(info_fd, 4096)
            if not chunk:
                break
            raw.extend(chunk)
            if len(raw) > 65536:
                raise SandboxVerificationInvalid("Bubblewrap sandbox identity is invalid")
            try:
                text = raw.decode("utf-8")
                value, offset = json.JSONDecoder().raw_decode(text)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if text[offset:].strip():
                raise SandboxVerificationInvalid("Bubblewrap sandbox identity is invalid")
            break
        try:
            text = raw.decode("utf-8")
            value, offset = json.JSONDecoder().raw_decode(text)
            if text[offset:].strip():
                raise ValueError
            pid = value["child-pid"]
        except (UnicodeDecodeError, TypeError, KeyError, ValueError, json.JSONDecodeError) as exc:
            raise SandboxVerificationInvalid("Bubblewrap sandbox identity is invalid") from exc
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            raise SandboxVerificationInvalid("Bubblewrap sandbox identity is invalid")
        return pid

    def _post_spawn_verification(
        self,
        binding: SandboxLaunchBinding,
        mount_policy: SandboxMountPolicy,
        invocation: ExecutorInvocation,
        sandbox_pid: int,
    ) -> SandboxVerificationRecord:
        source = self.sandbox_post_spawn_verification_source
        if source is None:
            raise SandboxVerificationInvalid("trusted post-spawn sandbox verification is required")
        try:
            record = source(binding, mount_policy, invocation, sandbox_pid)
        except SandboxVerificationInvalid:
            raise
        except Exception as exc:
            raise SandboxVerificationInvalid("trusted post-spawn sandbox verification is unavailable") from exc
        return validate_sandbox_verification_record(
            record,
            binding=binding,
            mount_policy=mount_policy,
            egress_profile=invocation.egress_profile,
            policy=self.sandbox_verification_policy,
            now=int(self._clock()),
        )

    def _workspace_fd(self) -> int:
        try:
            return self.workspace_context.roots.open_relative(
                RootKind.WORKSPACE,
                self.workspace_context.workspace_prefix,
                expected_type=ExpectedType.DIRECTORY,
            )
        except RuntimeError as exc:
            # ControlledRoots intentionally requires Linux. This fallback only
            # supports injected unit-test launchers on development platforms.
            if "require Linux" not in str(exc):
                raise
            return os.dup(self.workspace_context.roots.get(RootKind.WORKSPACE).directory_fd)

    def _dispatch_invocation(self, invocation: ExecutorInvocation) -> str:
        identity = invocation.identity
        binding = self._sandbox_binding(identity)
        mount_policy = self._sandbox_mount_policy(binding)
        verification_record = (
            None
            if self.sandbox_post_spawn_verification_source is not None
            else self._require_verified_sandbox(binding, mount_policy, invocation)
        )
        syscall_filter = self._require_syscall_filter(binding)
        inherited_security_fd = -1
        try:
            inherited_security_fd = os.dup(syscall_filter.fd)
            os.set_inheritable(inherited_security_fd, True)
            os.close(syscall_filter.fd)
            syscall_filter = SandboxSyscallFilter(
                inherited_security_fd, syscall_filter.syscall_policy_id, syscall_filter.syscall_policy_digest
            )
        except OSError as exc:
            if inherited_security_fd >= 0:
                try:
                    os.close(inherited_security_fd)
                except OSError:
                    pass
            raise SandboxVerificationInvalid("sandbox syscall filter descriptor is unavailable") from exc
        runtime_home = binding.runtime_home
        runtime_home.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            runtime_home.chmod(stat.S_IRWXU)

        workspace_fd = inherited_workspace_fd = request_read = request_write = response_read = response_write = -1
        gate_read = gate_write = info_read = info_write = -1
        process: Any | None = None
        spec: BubblewrapLaunchSpec | None = None
        completed = False
        live_key = (identity.stable_key, invocation.invocation_id)
        try:
            workspace_fd = self._workspace_fd()
            inherited_workspace_fd = os.dup(workspace_fd)
            request_read, request_write = os.pipe()
            response_read, response_write = os.pipe()
            gate_read, gate_write = os.pipe()
            info_read, info_write = os.pipe()
            for fd in (inherited_workspace_fd, request_read, response_write, gate_read, info_write):
                os.set_inheritable(fd, True)
            environment = build_executor_environment(
                identity,
                runtime_home=runtime_home,
                workspace_fd=inherited_workspace_fd,
                bootstrap_fd=request_read,
                response_fd=response_write,
                start_gate_fd=gate_read,
                egress_profile=invocation.egress_profile,
            )
            spec = self.sandbox_builder(
                environment=environment,
                workspace_fd=inherited_workspace_fd,
                binding=binding,
                mount_policy=mount_policy,
                security_policy=self.sandbox_verification_policy.security_policy,
                syscall_filter=SandboxSyscallFilter(
                    inherited_security_fd,
                    syscall_filter.syscall_policy_id,
                    syscall_filter.syscall_policy_digest,
                ),
                bubblewrap_binary=(str(self.bubblewrap_binary) if self.bubblewrap_binary else None),
                info_fd=info_write,
            )
            with self._lock:
                if identity.stable_key in self._revoked:
                    raise PermissionError("executor generation is revoked")
                process = self.process_factory(
                    list(spec.argv),
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                    pass_fds=tuple(dict.fromkeys((
                        inherited_workspace_fd, request_read, response_write, gate_read,
                        info_write, inherited_security_fd, *spec.inherited_security_fds,
                    ))),
                    start_new_session=True,
                )
                self._live[live_key] = _LiveInvocation(
                    identity, invocation.invocation_id, process, binding, verification_record
                )
            for fd in (request_read, response_write, gate_read, info_write):
                os.close(fd)
            request_read = response_write = gate_read = info_write = -1
            for fd in spec.inherited_security_fds:
                if fd != inherited_security_fd:
                    os.close(fd)
            spec = BubblewrapLaunchSpec(
                spec.argv, spec.bubblewrap_path, spec.runtime_home,
                spec.binding, (inherited_security_fd,),
            )
            sandbox_pid = self._bubblewrap_child_pid(info_read)
            os.close(info_read)
            info_read = -1
            if self.sandbox_post_spawn_verification_source is not None:
                verification_record = self._post_spawn_verification(
                    binding, mount_policy, invocation, sandbox_pid
                )
            with self._lock:
                if identity.stable_key in self._revoked:
                    raise PermissionError("executor generation is revoked")
                live = self._live.get(live_key)
                if live is None or live.process is not process:
                    raise PermissionError("executor generation is revoked")
                live.verification_record = verification_record
            with os.fdopen(gate_write, "wb", closefd=True) as stream:
                stream.write(b"1")
                stream.flush()
            gate_write = -1
            with os.fdopen(request_write, "wb", closefd=True) as stream:
                stream.write(json.dumps(invocation.to_payload(), ensure_ascii=False).encode("utf-8"))
                stream.flush()
            request_write = -1
            with os.fdopen(response_read, "rb", closefd=True) as stream:
                raw = stream.read()
            response_read = -1
            try:
                response = json.loads(raw.decode("utf-8"))
                result = response["result"]
            except (UnicodeDecodeError, TypeError, KeyError, json.JSONDecodeError) as exc:
                raise RuntimeError("executor returned an invalid response") from exc
            if process.wait(timeout=30) != 0 and not raw:
                raise RuntimeError("executor process failed")
            completed = True
            return str(result)
        finally:
            if process is not None:
                if not completed:
                    self._terminate(process)
                with self._lock:
                    self._live.pop(live_key, None)
            extra_spec_fds = (
                () if spec is None else tuple(
                    fd for fd in spec.inherited_security_fds
                    if fd != inherited_security_fd
                )
            )
            for fd in (
                workspace_fd, inherited_workspace_fd, request_read, request_write,
                response_read, response_write, gate_read, gate_write,
                info_read, info_write, inherited_security_fd, *extra_spec_fds,
            ):
                if isinstance(fd, int) and fd >= 0:
                    try:
                        os.close(fd)
                    except OSError:
                        pass

    @staticmethod
    def _terminate(process: Any) -> None:
        pid = getattr(process, "pid", None)
        if not isinstance(pid, int) or pid <= 0:
            return
        try:
            os.killpg(pid, signal.SIGTERM)  # windows-footgun: ok
        except (OSError, ProcessLookupError):
            return
        try:
            process.wait(timeout=2)
        except (subprocess.TimeoutExpired, TimeoutError):
            try:
                os.killpg(pid, signal.SIGKILL)  # windows-footgun: ok
            except (OSError, ProcessLookupError):
                pass
            try:
                process.wait(timeout=2)
            except (subprocess.TimeoutExpired, TimeoutError):
                pass

    def _revoke_live(self, predicate: Callable[[ExecutorIdentity], bool]) -> None:
        with self._lock:
            live = [entry for entry in self._live.values() if predicate(entry.identity)]
            for entry in live:
                self._live.pop((entry.identity.stable_key, entry.invocation_id), None)
        for entry in live:
            self._terminate(entry.process)

    @staticmethod
    def _reap_registry_descendants(identity: ExecutorIdentity) -> None:
        """Terminate only process-registry descendants of one exact executor."""
        from tools.process_registry import process_registry

        process_registry.kill_executor_generation(identity)

    def revoke_executor(self, identity: ExecutorIdentity) -> int:
        with self._lock:
            self._revoked.add(identity.stable_key)
        revoked = self.credential_broker.revoke_executor(identity)
        self._reap_registry_descendants(identity)
        self._revoke_live(lambda candidate: candidate == identity)
        return revoked

    def stop_generation(self) -> int:
        with self._lock:
            identities = [
                identity for identity in self._identities.values()
                if (
                    identity.owner_key == self.lease.owner_key
                    and identity.worker_id == self.lease.worker_id
                    and identity.worker_generation == self.lease.worker_generation
                )
            ]
            for identity in identities:
                self._revoked.add(identity.stable_key)
        revoked = self.credential_broker.revoke_worker_generation(
            owner_key=self.lease.owner_key,
            worker_generation=self.lease.worker_generation,
            worker_id=self.lease.worker_id,
        )
        for identity in identities:
            self._reap_registry_descendants(identity)
        self._revoke_live(
            lambda identity: (
                identity.owner_key == self.lease.owner_key
                and identity.worker_id == self.lease.worker_id
                and identity.worker_generation == self.lease.worker_generation
            )
        )
        return revoked
