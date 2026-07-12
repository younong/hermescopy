"""Task-bound, Bubblewrap-isolated Tool Executor supervisor."""
from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from hermes_cli.authenticated_file_context import AuthenticatedWorkspaceContext
from hermes_cli.controlled_roots import ExpectedType, RootKind
from hermes_cli.owner_worker.credential_broker import CredentialBroker
from hermes_cli.owner_worker.executor_identity import ExecutorIdentity, ExecutorInvocation
from hermes_cli.owner_worker.tool_executor_sandbox import (
    BubblewrapLaunchSpec,
    SandboxLaunchBinding,
    build_bubblewrap_launch_spec,
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
        runtime_dependency_roots: tuple[str | Path, ...] | None = None,
    ) -> None:
        self.owner_home = Path(owner_home).resolve()
        self.workspace_context = workspace_context
        self.lease = lease
        self.process_factory = process_factory
        self.credential_broker = credential_broker or CredentialBroker()
        self.sandbox_builder = sandbox_builder
        self.control_home = Path(control_home).resolve() if control_home else None
        self.runtime_dependency_roots = runtime_dependency_roots
        self._lock = threading.RLock()
        self._identities: dict[tuple[str, str], ExecutorIdentity] = {}
        self._live: dict[tuple[tuple, str], _LiveInvocation] = {}
        self._revoked: set[tuple] = set()

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
        egress_profile: str = "tool-none",
    ) -> str:
        identity = self.identity_for(task_id=task_id, session_id=session_id)
        invocation = ExecutorInvocation(
            identity=identity,
            tool_name=function_name,
            arguments=function_args,
            tool_call_id=tool_call_id or "tool-call",
            turn_id=turn_id or "turn",
            api_request_id=api_request_id or "request",
            invocation_id=os.urandom(16).hex(),
            egress_profile=egress_profile,
        )
        return self._dispatch_invocation(invocation)

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
        runtime_home = binding.runtime_home
        tmp_dir = runtime_home / "tmp"
        runtime_home.mkdir(parents=True, exist_ok=True)
        tmp_dir.mkdir(mode=0o700, exist_ok=True)
        if os.name != "nt":
            runtime_home.chmod(stat.S_IRWXU)
            tmp_dir.chmod(stat.S_IRWXU)

        workspace_fd = inherited_workspace_fd = request_read = request_write = response_read = response_write = -1
        process: Any | None = None
        completed = False
        live_key = (identity.stable_key, invocation.invocation_id)
        try:
            workspace_fd = self._workspace_fd()
            inherited_workspace_fd = os.dup(workspace_fd)
            request_read, request_write = os.pipe()
            response_read, response_write = os.pipe()
            for fd in (inherited_workspace_fd, request_read, response_write):
                os.set_inheritable(fd, True)
            environment = build_executor_environment(
                identity,
                runtime_home=runtime_home,
                tmp_dir=tmp_dir,
                workspace_fd=inherited_workspace_fd,
                bootstrap_fd=request_read,
                response_fd=response_write,
                egress_profile=invocation.egress_profile,
            )
            spec = self.sandbox_builder(
                environment=environment,
                workspace_fd=inherited_workspace_fd,
                runtime_home=runtime_home,
                owner_home=self.owner_home,
                workspace_root=self.workspace_context.roots.get(RootKind.WORKSPACE).canonical_path,
                binding=binding,
                control_home=self.control_home,
                runtime_dependency_roots=self.runtime_dependency_roots,
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
                    pass_fds=(inherited_workspace_fd, request_read, response_write),
                    start_new_session=True,
                )
                self._live[live_key] = _LiveInvocation(identity, invocation.invocation_id, process, binding)
            os.close(request_read)
            os.close(response_write)
            request_read = response_write = -1
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
            for fd in (workspace_fd, inherited_workspace_fd, request_read, request_write, response_read, response_write):
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
            os.killpg(pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            return
        try:
            process.wait(timeout=2)
        except (subprocess.TimeoutExpired, TimeoutError):
            try:
                os.killpg(pid, signal.SIGKILL)
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
