"""Supervisor for per-owner Hermes worker processes."""
from __future__ import annotations

import errno
import math
import os
import socket
import stat
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from hermes_constants import get_hermes_home
from hermes_cli.dashboard_auth.audit import (
    AuthorityAuditEvent,
    AuthorityAuditReason,
    audit_authority,
    new_authority_correlation_id,
)
from hermes_cli.dashboard_auth.authority import (
    AuthorityStore,
    AuthorizationRejected,
    OwnerWorkerAuthorityLease,
    WorkerGeneration,
    WorkerGenerationState,
    WorkerLeaseState,
)
from hermes_cli.controlled_roots import ControlledRoots, ExpectedType, RootKind, controlled_roots_for
from hermes_cli.deployment_inference import DeploymentInferencePolicy
from hermes_cli.owner_worker.inference_relay import DeploymentInferenceBroker
from hermes_cli.owner_runtime import (
    OwnerWorkerRuntimePaths,
    ensure_owner_runtime_dirs,
    owner_worker_env_for,
    owner_worker_runtime_paths,
    owner_worker_socket_path,
)

from .client import OwnerWorkerClient, OwnerWorkerHealthError
from .tokens import owner_worker_capability_public_config


@dataclass
class OwnerWorkerHandle:
    owner_key: str
    owner_home: Path
    worker_generation: int
    worker_id: str
    lease_version: int
    recovery_generation: int
    socket_path: Path
    process: subprocess.Popen[Any]
    pid: int
    started_at: float = field(default_factory=time.time)
    last_used_at: float = field(default_factory=time.time)
    last_health: dict[str, Any] = field(default_factory=dict)
    active_uses: int = 0


class OwnerWorkerUnavailableError(RuntimeError):
    """Raised when an Owner Worker cannot be admitted or started yet."""


class OwnerWorkerStartupError(OwnerWorkerUnavailableError):
    """Raised when an Owner Worker exits or fails health checks during startup."""


class OwnerWorkerLease:
    """Reference-counted active-use lease for an owner worker handle."""

    def __init__(self, supervisor: "OwnerWorkerSupervisor", handle: OwnerWorkerHandle) -> None:
        self._supervisor = supervisor
        self._handle = handle
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._supervisor.release_use(self._handle)

    def __enter__(self) -> "OwnerWorkerLease":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.release()


_OWNER_WORKER_ENV_ALLOW: frozenset[str] = frozenset({
    "CONDA_DEFAULT_ENV",
    "CONDA_PREFIX",
    "CURL_CA_BUNDLE",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "PATH",
    "PYTHONPATH",
    "REQUESTS_CA_BUNDLE",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "TMP",
    "TMPDIR",
    "TEMP",
    "VIRTUAL_ENV",
})
if os.name == "nt":
    _OWNER_WORKER_ENV_ALLOW = _OWNER_WORKER_ENV_ALLOW | frozenset({
        "COMSPEC",
        "PATHEXT",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "WINDIR",
    })

_OWNER_WORKER_ENV_EXPLICIT_KEEP: frozenset[str] = frozenset({
    "HERMES_DISABLE_LAZY_INSTALLS",
    # Operator-owned factory specification, passed to the authenticated worker
    # only so its startup can construct the mandatory deployment policy.
    "HERMES_SANDBOX_DEPLOYMENT_POLICY",
})


def _configured_env_allowlist() -> set[str]:
    """Return operator-approved extra env keys for owner worker subprocesses.

    This is intentionally key-only and opt-in.  It lets deployments forward
    non-secret runtime necessities such as HTTPS_PROXY or a custom CA variable
    without falling back to broad Control Plane environment inheritance.
    """
    raw = os.environ.get("HERMES_OWNER_WORKER_ENV_ALLOWLIST", "")
    return {key.strip() for key in raw.split(",") if key.strip()}


class OwnerWorkerSupervisor:
    """Start and track one OS process per authenticated owner key."""

    def __init__(
        self,
        *,
        control_home: str | Path | None = None,
        global_home: str | Path | None = None,
        client_cls: type[OwnerWorkerClient] = OwnerWorkerClient,
        process_factory: Callable[..., subprocess.Popen[Any]] = subprocess.Popen,
        startup_timeout: float = 5.0,
        poll_interval: float = 0.05,
        max_workers: int | None = None,
        startup_cooldown: float | None = None,
        idle_timeout: float | None = None,
        max_owner_concurrency: int | None = None,
        control_ws_base: str | None = None,
        authority_store_factory: Callable[[Path], AuthorityStore] = AuthorityStore,
        generation_bridge_revoker: Callable[[OwnerWorkerAuthorityLease], None] | None = None,
        deployment_inference_policy: DeploymentInferencePolicy | None = None,
    ) -> None:
        self.global_home = Path(global_home).resolve() if global_home else get_hermes_home().resolve()
        self.control_home = Path(control_home).resolve() if control_home else self.global_home / "control-plane"
        self.client_cls = client_cls
        self.process_factory = process_factory
        self.startup_timeout = startup_timeout
        self.poll_interval = poll_interval
        self.max_workers = max(1, int(max_workers or os.environ.get("HERMES_OWNER_WORKER_MAX", "16") or 16))
        self.startup_cooldown = max(
            0.0,
            float(startup_cooldown if startup_cooldown is not None else os.environ.get("HERMES_OWNER_WORKER_STARTUP_COOLDOWN", "1") or 1),
        )
        self.idle_timeout = max(
            1.0,
            float(idle_timeout if idle_timeout is not None else os.environ.get("HERMES_OWNER_WORKER_IDLE_TIMEOUT", "1800") or 1800),
        )
        try:
            configured_concurrency = max_owner_concurrency if max_owner_concurrency is not None else os.environ.get(
                "HERMES_OWNER_WORKER_MAX_CONCURRENCY", "32"
            )
            self.max_owner_concurrency = int(configured_concurrency)
        except (TypeError, ValueError) as exc:
            raise ValueError("owner worker concurrency limit is invalid") from exc
        if self.max_owner_concurrency < 1:
            raise ValueError("owner worker concurrency limit is invalid")
        self.control_ws_base = (control_ws_base or os.environ.get("HERMES_OWNER_WORKER_CONTROL_WS_BASE", "")).strip()
        self.authority_store = authority_store_factory(self.control_home)
        self.generation_bridge_revoker = generation_bridge_revoker
        self.deployment_inference_policy = deployment_inference_policy
        self.deployment_inference_broker = (
            DeploymentInferenceBroker(
                policy=deployment_inference_policy,
                authority_store=self.authority_store,
            )
            if deployment_inference_policy is not None
            else None
        )
        self._handles: dict[str, OwnerWorkerHandle] = {}
        # A detached handle remains counted until its synchronous bridge revocation
        # and process teardown complete. This prevents duplicate retirement, new
        # use leases, and replacement admission while the old process is live.
        self._terminating_handles: dict[str, OwnerWorkerHandle] = {}
        self._last_start_attempt: dict[str, float] = {}
        self._starting_owner_keys: set[str] = set()
        self._in_flight_starts = 0
        self._lock = threading.RLock()
        self._start_finished = threading.Condition(self._lock)

    @staticmethod
    def _audit_generation(reason: AuthorityAuditReason, lease: OwnerWorkerAuthorityLease) -> None:
        try:
            audit_authority(
                AuthorityAuditEvent.WORKER_GENERATION,
                correlation_id=new_authority_correlation_id(),
                reason=reason,
                audience_class="none",
                worker_generation=lease.worker_generation,
                recovery_generation=lease.recovery_generation,
            )
        except Exception:
            # Observability cannot alter worker fencing or cleanup behavior.
            pass

    def get_or_start(self, owner: Any, *, timeout: float | None = None) -> OwnerWorkerHandle:
        startup_timeout = self._startup_deadline_timeout(timeout)
        deadline: float | None = None
        owner_key = self._owner_key(owner)
        owner_home = self._owner_home(owner)
        while True:
            # These methods only select handles while locked; their synchronous
            # revoker/process work runs after releasing the supervisor lock.
            self._reap_exited()
            self._stop_idle(now=time.time())

            with self._start_finished:
                existing = self._handles.get(owner_key)
                if existing is not None:
                    if existing.owner_home.resolve() != owner_home.resolve():
                        raise RuntimeError("owner worker exact owner_home mismatch for owner_key")
                    existing.last_used_at = time.time()
                elif owner_key in self._starting_owner_keys or owner_key in self._terminating_handles:
                    if deadline is None:
                        deadline = time.monotonic() + startup_timeout
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not self._start_finished.wait(timeout=remaining):
                        raise TimeoutError("timed out waiting for owner worker startup")
                    continue
                else:
                    eviction = self._admit_start(owner_key, owner_home, now=time.time())
                    if eviction is None:
                        self._starting_owner_keys.add(owner_key)
                        self._in_flight_starts += 1
                        deadline = time.monotonic() + startup_timeout
                        break

            if existing is not None:
                if existing.process.poll() is not None:
                    self._terminate_handle(owner_key, existing)
                    continue
                try:
                    self.authority_store.assert_worker_lease(
                        self._lease_for_handle(existing), states=frozenset({WorkerLeaseState.ACTIVE})
                    )
                    health = self.client_cls(existing.socket_path, control_home=self.control_home).verify_health(
                        owner_key=owner_key,
                        owner_home=owner_home,
                        worker_generation=existing.worker_generation,
                        worker_id=existing.worker_id,
                        lease_version=existing.lease_version,
                        recovery_generation=existing.recovery_generation,
                        lease=self._lease_for_handle(existing),
                    )
                    if int(health["pid"]) != existing.pid:
                        raise RuntimeError("owner worker pid mismatch")
                except (AuthorizationRejected, OwnerWorkerHealthError, RuntimeError):
                    # A local cache never grants authority. Revoke and close the
                    # exact local generation; a stale CAS cannot affect a replacement.
                    self._terminate_handle(owner_key, existing)
                    continue
                with self._lock:
                    if self._handles.get(owner_key) is existing:
                        existing.last_health = health
                        return existing
                continue

            # Capacity eviction was reserved while locked. Complete its strict
            # bridge-revoke/process teardown outside the lock, then retry admission.
            self._teardown_terminated_handle(eviction[0], eviction[1])

        try:
            return self._start_owner_worker(owner, owner_key, owner_home, deadline=deadline or time.monotonic())
        finally:
            with self._start_finished:
                self._starting_owner_keys.remove(owner_key)
                self._in_flight_starts -= 1
                self._start_finished.notify_all()

    def _startup_deadline_timeout(self, timeout: float | None) -> float:
        value = self.startup_timeout if timeout is None else timeout
        try:
            value = float(value)
        except (TypeError, ValueError):
            value = 5.0
        if not math.isfinite(value) or value < 0:
            value = 5.0
        return value

    def _start_owner_worker(
        self,
        owner: Any,
        owner_key: str,
        owner_home: Path,
        *,
        deadline: float,
    ) -> OwnerWorkerHandle:
        ensure_owner_runtime_dirs(owner_home)
        try:
            claim = self.authority_store.claim_worker_start(owner_key, worker_id=uuid.uuid4().hex)
        except AuthorizationRejected as exc:
            if (
                str(exc) != "worker_lease_already_owned"
                or not self._reconcile_missing_local_worker(owner_key, owner_home)
            ):
                raise OwnerWorkerUnavailableError(f"owner worker is already owned: {exc}") from exc
            claim = self.authority_store.claim_worker_start(owner_key, worker_id=uuid.uuid4().hex)
        generation = claim.generation
        socket_path = self.socket_path_for(owner, generation.worker_generation)
        env = self._env_for(owner, generation, claim.lease)
        relay_fd = None
        if self.deployment_inference_broker is not None:
            relay_fd = self.deployment_inference_broker.register(claim.lease)
            env["HERMES_DEPLOYMENT_INFERENCE_RELAY_FD"] = str(relay_fd)
        runtime_paths = owner_worker_runtime_paths(
            owner_home=owner_home,
            worker_generation=generation.worker_generation,
        )
        controlled_roots = self._controlled_roots_for(runtime_paths)
        try:
            controlled_roots.mkdirs(
                RootKind.OWNER_WRITABLE,
                f"runtime/workers/{generation.worker_generation}",
            )
        except BaseException:
            controlled_roots.close()
            raise
        cwd_fd = None
        stdout_handle = None
        stderr_handle = None
        try:
            cwd_fd = controlled_roots.open_relative(
                RootKind.WORKSPACE,
                "default",
                expected_type=ExpectedType.DIRECTORY,
            )
            stdout_handle = controlled_roots.open_append_file(
                RootKind.OWNER_WRITABLE,
                "runtime/logs/owner-worker.stdout.log",
            )
            stderr_handle = controlled_roots.open_append_file(
                RootKind.OWNER_WRITABLE,
                "runtime/logs/owner-worker.stderr.log",
            )
            os.fchmod(stdout_handle, stat.S_IRUSR | stat.S_IWUSR)
            os.fchmod(stderr_handle, stat.S_IRUSR | stat.S_IWUSR)
            inherited_cwd_fd = os.dup(cwd_fd)
            os.set_inheritable(inherited_cwd_fd, True)

            def _set_descriptor_cwd() -> None:
                os.fchdir(inherited_cwd_fd)
                os.close(inherited_cwd_fd)

            try:
                process = self.process_factory(
                    self._argv_for(owner, socket_path, generation),
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_handle,
                    stderr=stderr_handle,
                    close_fds=True,
                    preexec_fn=_set_descriptor_cwd,
                    pass_fds=tuple(
                        fd for fd in (inherited_cwd_fd, relay_fd) if fd is not None
                    ),
                )
            finally:
                os.close(inherited_cwd_fd)
                if relay_fd is not None:
                    os.close(relay_fd)
                    relay_fd = None
        except Exception as exc:
            if self.deployment_inference_broker is not None:
                self.deployment_inference_broker.revoke(claim.lease)
            self._audit_generation(AuthorityAuditReason.GENERATION_START_FAILED, claim.lease)
            try:
                self.authority_store.transition_worker_lease(
                    claim.lease,
                    state=WorkerLeaseState.REVOKED,
                    generation_state=WorkerGenerationState.FAILED,
                )
            except AuthorizationRejected:
                pass
            raise OwnerWorkerStartupError(f"owner worker process launch failed: {exc}") from exc
        finally:
            if cwd_fd is not None:
                os.close(cwd_fd)
            if stdout_handle is not None:
                os.close(stdout_handle)
            if stderr_handle is not None:
                os.close(stderr_handle)
            controlled_roots.close()
        try:
            health = self._wait_until_healthy(
                process=process,
                socket_path=socket_path,
                owner_key=owner_key,
                owner_home=owner_home,
                worker_generation=generation.worker_generation,
                worker_id=generation.worker_id,
                lease=claim.lease,
                deadline=deadline,
            )
            self._chmod_private_file(socket_path)
            self._verify_socket_path(socket_path, owner_home, generation.worker_generation)
            active_lease = self.authority_store.transition_worker_lease(
                claim.lease,
                state=WorkerLeaseState.ACTIVE,
                generation_state=WorkerGenerationState.ACTIVE,
            )
        except Exception as exc:
            if self.deployment_inference_broker is not None:
                self.deployment_inference_broker.revoke(claim.lease)
            self._audit_generation(AuthorityAuditReason.GENERATION_START_FAILED, claim.lease)
            try:
                self.authority_store.transition_worker_lease(
                    claim.lease,
                    state=WorkerLeaseState.REVOKED,
                    generation_state=WorkerGenerationState.FAILED,
                )
            except AuthorizationRejected:
                pass
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        pass
            else:
                process.wait()
            if isinstance(exc, (OwnerWorkerUnavailableError, TimeoutError)):
                raise
            raise OwnerWorkerStartupError("owner worker startup failed") from exc

        handle = OwnerWorkerHandle(
            owner_key=owner_key,
            owner_home=owner_home,
            worker_generation=generation.worker_generation,
            worker_id=generation.worker_id,
            lease_version=active_lease.lease_version,
            recovery_generation=active_lease.recovery_generation,
            socket_path=socket_path,
            process=process,
            pid=int(health["pid"]),
            last_health=health,
        )
        if self.deployment_inference_broker is not None:
            try:
                self.deployment_inference_broker.activate(active_lease)
            except Exception:
                self.deployment_inference_broker.revoke(active_lease)
                raise
        with self._lock:
            self._handles[owner_key] = handle
        self._audit_generation(AuthorityAuditReason.GENERATION_ACTIVE, active_lease)
        return handle

    @staticmethod
    def _canonical_socket_is_absent(socket_path: Path) -> bool:
        """Return true only for an unambiguous absent/refused local UDS peer."""
        if not socket_path.exists():
            return True
        try:
            if not stat.S_ISSOCK(socket_path.stat().st_mode):
                return False
        except OSError:
            return False
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            client.settimeout(0.25)
            client.connect(str(socket_path))
        except FileNotFoundError:
            return True
        except ConnectionRefusedError:
            return True
        except OSError as exc:
            # Do not reclaim when permission, timeout, or any unexpected local
            # condition leaves peer liveness uncertain.
            return exc.errno in {errno.ENOENT, errno.ECONNREFUSED}
        else:
            return False
        finally:
            client.close()

    def _reconcile_missing_local_worker(self, owner_key: str, owner_home: Path) -> bool:
        """Release one conclusively absent local Worker fence, if safe.

        A fresh Dashboard process has no handle map for children left by an
        unclean predecessor.  Local UDS absence is sufficient evidence only for
        the canonical per-generation socket; any existing socket is treated as
        a potentially live peer and is never reclaimed here.
        """
        lease = self.authority_store.read_owner_worker_lease(owner_key)
        # A STARTING fence may belong to a concurrent supervisor between
        # claim and socket bind. Without a durable process identity/liveness
        # witness, leave it fail-closed rather than racing that startup.
        if lease is None or lease.state not in {
            WorkerLeaseState.ACTIVE,
            WorkerLeaseState.DRAINING,
        }:
            return False
        socket_path = owner_worker_socket_path(owner_home, lease.worker_generation)
        if not self._canonical_socket_is_absent(socket_path):
            return False
        try:
            # Re-read the exact fence after observing socket absence. This
            # protects against an authority replacement racing this supervisor.
            lease = self.authority_store.assert_worker_lease(lease)
            if lease.state is WorkerLeaseState.STARTING:
                self.authority_store.transition_worker_lease(
                    lease,
                    state=WorkerLeaseState.REVOKED,
                    generation_state=WorkerGenerationState.FAILED,
                )
            elif lease.state is WorkerLeaseState.ACTIVE:
                draining = self.authority_store.transition_worker_lease(
                    lease,
                    state=WorkerLeaseState.DRAINING,
                    generation_state=WorkerGenerationState.DRAINING,
                )
                self.authority_store.transition_worker_lease(
                    draining,
                    state=WorkerLeaseState.REVOKED,
                    generation_state=WorkerGenerationState.REVOKED,
                )
            elif lease.state is WorkerLeaseState.DRAINING:
                self.authority_store.transition_worker_lease(
                    lease,
                    state=WorkerLeaseState.REVOKED,
                    generation_state=WorkerGenerationState.REVOKED,
                )
            else:  # pragma: no cover - state filter above is exhaustive
                return False
        except AuthorizationRejected:
            return False
        return True

    def shutdown(self) -> None:
        """Drain every locally owned generation before the Dashboard exits."""
        with self._lock:
            handles = tuple(self._handles.items())
            reserved = [
                (owner_key, handle)
                for owner_key, handle in handles
                if self._reserve_termination_locked(owner_key, handle)
            ]
        for owner_key, handle in reserved:
            self._teardown_terminated_handle(owner_key, handle)
        if self.deployment_inference_broker is not None:
            self.deployment_inference_broker.close()

    def _admit_start(
        self, owner_key: str, owner_home: Path, *, now: float
    ) -> tuple[str, OwnerWorkerHandle] | None:
        """Apply cold-start checks and reserve an idle eviction if needed.

        Callers hold ``_lock``. Returned eviction work must be completed after
        releasing it so synchronous bridge revocation cannot invert locks.
        """
        if owner_key in self._handles:
            current = self._handles[owner_key]
            if current.owner_home.resolve() != owner_home.resolve():
                raise RuntimeError("owner worker exact owner_home mismatch for owner_key")
            return None
        last_attempt = self._last_start_attempt.get(owner_key, 0.0)
        if self.startup_cooldown and now - last_attempt < self.startup_cooldown:
            raise OwnerWorkerUnavailableError("owner worker startup throttled")
        if len(self._handles) + len(self._terminating_handles) + self._in_flight_starts >= self.max_workers:
            eviction = self._reserve_oldest_idle_locked()
            if eviction is None:
                raise OwnerWorkerUnavailableError("owner worker limit reached")
            return eviction
        self._last_start_attempt[owner_key] = now
        return None

    def acquire_use(self, handle: OwnerWorkerHandle) -> OwnerWorkerLease:
        """Mark a worker as actively serving an HTTP stream or WS bridge."""
        with self._lock:
            current = self._handles.get(handle.owner_key)
            if current is not handle:
                raise RuntimeError("owner worker handle is no longer active")
            self.authority_store.assert_worker_lease(
                self._lease_for_handle(handle), states=frozenset({WorkerLeaseState.ACTIVE})
            )
            if handle.active_uses >= self.max_owner_concurrency:
                raise OwnerWorkerUnavailableError("owner worker concurrency limit reached")
            handle.active_uses += 1
            handle.last_used_at = time.time()
        return OwnerWorkerLease(self, handle)

    def release_use(self, handle: OwnerWorkerHandle) -> None:
        """Release an active-use lease acquired by :meth:`acquire_use`."""
        with self._lock:
            if handle.active_uses > 0:
                handle.active_uses -= 1
            handle.last_used_at = time.time()

    @staticmethod
    def _controlled_roots_for(runtime_paths: OwnerWorkerRuntimePaths) -> ControlledRoots:
        """Open app-equivalent trusted roots before launching an owner worker."""
        return controlled_roots_for(runtime_paths)

    @staticmethod
    def _chmod_private_file(path: Path) -> None:
        if os.name == "nt":
            return
        try:
            path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass

    @staticmethod
    def _verify_socket_path(socket_path: Path, owner_home: Path, worker_generation: int) -> None:
        expected = owner_worker_socket_path(owner_home, worker_generation)
        if socket_path.resolve(strict=False) != expected.resolve(strict=False):
            raise RuntimeError("owner worker socket does not match worker generation")
        resolved = socket_path.resolve()
        if resolved != expected:
            raise RuntimeError("owner worker socket escaped expected generation path")
        if os.name != "nt":
            mode = resolved.stat().st_mode
            if mode & (stat.S_IRWXG | stat.S_IRWXO):
                raise RuntimeError("owner worker socket is group/world accessible")

    @staticmethod
    def _lease_for_handle(handle: OwnerWorkerHandle) -> OwnerWorkerAuthorityLease:
        return OwnerWorkerAuthorityLease(
            handle.owner_key,
            handle.worker_generation,
            handle.worker_id,
            WorkerLeaseState.ACTIVE,
            handle.lease_version,
            handle.recovery_generation,
        )

    def _mark_handle_failed(self, handle: OwnerWorkerHandle) -> None:
        try:
            self.authority_store.transition_worker_lease(
                self._lease_for_handle(handle),
                state=WorkerLeaseState.REVOKED,
                generation_state=WorkerGenerationState.FAILED,
            )
        except AuthorizationRejected:
            # The handle may already be fenced/replaced by a different
            # supervisor. A stale local cleanup must never affect it.
            pass

    def _drain_handle(self, handle: OwnerWorkerHandle) -> OwnerWorkerAuthorityLease | None:
        try:
            return self.authority_store.transition_worker_lease(
                self._lease_for_handle(handle),
                state=WorkerLeaseState.DRAINING,
                generation_state=WorkerGenerationState.DRAINING,
            )
        except AuthorizationRejected:
            return None

    def _finalize_drained_handle(
        self,
        lease: OwnerWorkerAuthorityLease,
        *,
        generation_state: WorkerGenerationState = WorkerGenerationState.TERMINATED,
    ) -> None:
        try:
            self.authority_store.transition_worker_lease(
                lease,
                state=WorkerLeaseState.REVOKED,
                generation_state=generation_state,
            )
        except AuthorizationRejected:
            pass

    def _cleanup_generation_socket(self, handle: OwnerWorkerHandle) -> None:
        expected = owner_worker_socket_path(handle.owner_home, handle.worker_generation)
        if handle.socket_path.resolve(strict=False) != expected.resolve(strict=False):
            raise RuntimeError("owner worker socket does not match worker generation")
        try:
            expected.unlink()
        except FileNotFoundError:
            pass
        try:
            expected.parent.rmdir()
        except OSError:
            pass

    def _reserve_termination_locked(self, owner_key: str, handle: OwnerWorkerHandle) -> bool:
        """Detach an exact handle before running any external teardown work.

        The caller must hold ``_lock``. Detachment blocks new use leases; the
        separate reservation continues to consume worker capacity until teardown
        has synchronously closed bridges and reaped the process.
        """
        if self._handles.get(owner_key) is not handle:
            return False
        if self._terminating_handles.get(owner_key) is not None:
            return False
        self._handles.pop(owner_key, None)
        self._terminating_handles[owner_key] = handle
        return True

    def _terminate_handle(self, owner_key: str, handle: OwnerWorkerHandle) -> None:
        """Retire one exact local handle without holding the supervisor lock."""
        with self._lock:
            if not self._reserve_termination_locked(owner_key, handle):
                return
        self._teardown_terminated_handle(owner_key, handle)

    def _teardown_terminated_handle(self, owner_key: str, handle: OwnerWorkerHandle) -> None:
        """Run ordered external teardown for a previously reserved handle.

        This method deliberately never holds ``_lock`` while calling the bridge
        revoker. The revoker synchronously waits for bridge close, which releases
        an owner-use lease back through ``release_use()`` and must acquire it.
        """
        try:
            # Fence exact capability/bootstrap admission before closing bridge or
            # touching the process. A stale local cleanup can never transition a
            # replacement fence and therefore cannot revoke its authority.
            draining = self._drain_handle(handle)
            if draining is not None:
                self._audit_generation(AuthorityAuditReason.GENERATION_DRAINING, draining)
            retired_lease = draining or self._lease_for_handle(handle)
            if self.deployment_inference_broker is not None:
                self.deployment_inference_broker.revoke(retired_lease)
            if self.generation_bridge_revoker is not None:
                self.generation_bridge_revoker(retired_lease)

            process_exited = handle.process.poll() is not None
            if process_exited:
                handle.process.wait()
            else:
                handle.process.terminate()
                try:
                    handle.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    handle.process.kill()
                    try:
                        handle.process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        pass
                process_exited = handle.process.poll() is not None
            if process_exited:
                self._cleanup_generation_socket(handle)
            if draining is not None:
                terminal_state = (
                    WorkerGenerationState.TERMINATED
                    if process_exited
                    else WorkerGenerationState.REVOKED
                )
                self._finalize_drained_handle(draining, generation_state=terminal_state)
                self._audit_generation(
                    (
                        AuthorityAuditReason.GENERATION_TERMINATED
                        if process_exited
                        else AuthorityAuditReason.GENERATION_REVOKED
                    ),
                    draining,
                )
        finally:
            with self._start_finished:
                if self._terminating_handles.get(owner_key) is handle:
                    self._terminating_handles.pop(owner_key, None)
                self._start_finished.notify_all()

    def _reap_exited(self) -> None:
        with self._lock:
            reserved = [
                (owner_key, handle)
                for owner_key, handle in tuple(self._handles.items())
                if handle.process.poll() is not None and self._reserve_termination_locked(owner_key, handle)
            ]
        for owner_key, handle in reserved:
            self._teardown_terminated_handle(owner_key, handle)

    def _stop_idle(self, *, now: float) -> None:
        with self._lock:
            reserved = []
            for owner_key, handle in tuple(self._handles.items()):
                if handle.process.poll() is not None:
                    if self._reserve_termination_locked(owner_key, handle):
                        reserved.append((owner_key, handle))
                    continue
                if handle.active_uses <= 0 and now - handle.last_used_at >= self.idle_timeout:
                    if self._reserve_termination_locked(owner_key, handle):
                        reserved.append((owner_key, handle))
        for owner_key, handle in reserved:
            self._teardown_terminated_handle(owner_key, handle)

    def _reserve_oldest_idle_locked(self) -> tuple[str, OwnerWorkerHandle] | None:
        live = [
            (owner_key, handle)
            for owner_key, handle in self._handles.items()
            if handle.process.poll() is None and handle.active_uses <= 0
        ]
        if not live:
            return None
        owner_key, handle = min(live, key=lambda item: item[1].last_used_at)
        if self._reserve_termination_locked(owner_key, handle):
            return owner_key, handle
        return None

    def _evict_oldest_idle(self, *, now: float) -> None:
        del now
        with self._lock:
            reserved = self._reserve_oldest_idle_locked()
        if reserved is not None:
            self._teardown_terminated_handle(*reserved)

    def socket_path_for(self, owner: Any, worker_generation: int | None = None) -> Path:
        if worker_generation is None:
            raise ValueError("worker_generation is required for authenticated owner workers")
        return owner_worker_socket_path(self._owner_home(owner), worker_generation)

    def _wait_until_healthy(
        self,
        *,
        process: subprocess.Popen[Any],
        socket_path: Path,
        owner_key: str,
        owner_home: Path,
        worker_generation: int,
        worker_id: str,
        lease: OwnerWorkerAuthorityLease,
        deadline: float,
    ) -> dict[str, Any]:
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise OwnerWorkerStartupError(
                    f"owner worker exited during startup with code {process.returncode}"
                )
            if socket_path.exists():
                try:
                    return self.client_cls(socket_path, control_home=self.control_home).verify_health(
                        owner_key=owner_key,
                        owner_home=owner_home,
                        worker_generation=worker_generation,
                        worker_id=worker_id,
                        lease_version=lease.lease_version,
                        recovery_generation=lease.recovery_generation,
                        lease=lease,
                    )
                except OwnerWorkerHealthError as exc:
                    last_error = exc
            time.sleep(min(self.poll_interval, max(0.0, deadline - time.monotonic())))
        if last_error is not None:
            raise OwnerWorkerStartupError(
                f"owner worker failed health verification: {last_error}"
            ) from last_error
        raise TimeoutError("timed out waiting for owner worker socket")

    def _argv_for(self, owner: Any, socket_path: Path, generation: WorkerGeneration) -> list[str]:
        argv = [
            sys.executable,
            "-m",
            "hermes_cli.owner_worker.entrypoint",
            "--owner-key",
            self._owner_key(owner),
            "--owner-home",
            str(self._owner_home(owner)),
            "--socket",
            str(socket_path),
            "--control-home",
            str(self.control_home),
            "--worker-generation",
            str(generation.worker_generation),
            "--worker-id",
            generation.worker_id,
        ]
        optional = (
            ("--tenant-id", "tenant_id"),
            ("--owner-user-id", "owner_user_id"),
            ("--auth-provider", "auth_provider"),
        )
        for flag, attr in optional:
            value = self._get_attr(owner, attr, "")
            if value:
                argv.extend([flag, str(value)])
        return argv

    def _env_for(
        self,
        owner: Any,
        generation: WorkerGeneration,
        lease: OwnerWorkerAuthorityLease,
    ) -> dict[str, str]:
        keep = _OWNER_WORKER_ENV_ALLOW | _OWNER_WORKER_ENV_EXPLICIT_KEEP | _configured_env_allowlist()
        env = {key: value for key, value in os.environ.items() if key in keep}
        verifier = owner_worker_capability_public_config(self.control_home)
        env.update(
            owner_worker_env_for(
                owner_key=self._owner_key(owner),
                owner_home=self._owner_home(owner),
                tenant_id=str(self._get_attr(owner, "tenant_id", "") or ""),
                owner_user_id=str(self._get_attr(owner, "owner_user_id", "") or ""),
                auth_provider=str(self._get_attr(owner, "auth_provider", "") or ""),
                control_home=self.control_home,
                worker_generation=generation.worker_generation,
                worker_id=generation.worker_id,
                lease_version=lease.lease_version,
                recovery_generation=lease.recovery_generation,
                capability_issuer=verifier["HERMES_OWNER_WORKER_CAPABILITY_ISSUER"],
                capability_public_key=verifier["HERMES_OWNER_WORKER_CAPABILITY_PUBLIC_KEY"],
                capability_retained_public_keys=verifier[
                    "HERMES_OWNER_WORKER_CAPABILITY_RETAINED_PUBLIC_KEYS"
                ],
                deployment_inference_descriptor=(
                    self.deployment_inference_policy.descriptor()
                    if self.deployment_inference_policy is not None
                    else None
                ),
            )
        )
        # The child deliberately starts in the owner's workspace, so Python
        # cannot rely on the Dashboard runner's release-root cwd to resolve
        # ``-m hermes_cli.owner_worker.entrypoint``. Derive the trusted import
        # root from this installed/source package instead of hard-coding a
        # deployment path, and preserve operator-provided entries after it.
        package_import_root = str(Path(__file__).resolve().parents[2])
        inherited_pythonpath = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = (
            package_import_root
            if not inherited_pythonpath
            else f"{package_import_root}{os.pathsep}{inherited_pythonpath}"
        )
        if self.control_ws_base:
            env["HERMES_OWNER_WORKER_CONTROL_WS_BASE"] = self.control_ws_base
        return env

    @staticmethod
    def _get_attr(owner: Any, name: str, default: Any = None) -> Any:
        if isinstance(owner, dict):
            return owner.get(name, default)
        return getattr(owner, name, default)

    @classmethod
    def _owner_key(cls, owner: Any) -> str:
        owner_key = str(cls._get_attr(owner, "owner_key", "")).strip()
        if not owner_key:
            raise ValueError("owner.owner_key is required")
        return owner_key

    @classmethod
    def _owner_home(cls, owner: Any) -> Path:
        value = cls._get_attr(owner, "owner_home", None)
        if value is None:
            raise ValueError("owner.owner_home is required")
        return Path(value).expanduser().resolve()
