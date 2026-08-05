"""Supervisor for per-owner Hermes worker processes."""
from __future__ import annotations

import hashlib
import logging
import math
import os
import stat
import subprocess
import sys
import threading
import time
import uuid
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Callable

from hermes_constants import get_bundled_skills_dir, get_hermes_home
from hermes_cli import __release_date__, __version__
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
from hermes_cli.deployment_image import DeploymentImagePolicy
from hermes_cli.deployment_inference import DeploymentInferencePolicy
from hermes_cli.latency_trace import observe_latency_stage, observed_latency_stage
from hermes_cli.local_socket import canonical_unix_peer_is_absent
from hermes_cli.owner_worker.cgroup_v2 import CgroupScopeLease
from hermes_cli.owner_worker.image_relay import DeploymentImageBroker
from hermes_cli.owner_worker.inference_relay import DeploymentInferenceBroker
from hermes_cli.owner_worker.preloaded_launcher import OwnerWorkerLauncher
from hermes_cli.owner_worker.resource_broker import DeploymentResourceBroker
from hermes_cli.owner_runtime import (
    OwnerWorkerRuntimePaths,
    ensure_owner_runtime_dirs,
    owner_worker_env_for,
    owner_worker_runtime_paths,
    owner_worker_socket_path,
)
from hermes_cli.revision_fingerprint import read_git_revision_fingerprint

from .client import OwnerWorkerClient, OwnerWorkerHealthError
from .tokens import owner_worker_capability_public_config


_log = logging.getLogger(__name__)


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
    accepting: bool = True
    resource_scope: CgroupScopeLease | None = field(default=None, repr=False)


class OwnerWorkerUnavailableError(RuntimeError):
    """Raised when an Owner Worker cannot be admitted or started yet."""


class OwnerWorkerStartupError(OwnerWorkerUnavailableError):
    """Raised when an Owner Worker exits or fails health checks during startup."""


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_OWNER_WORKER_SKILLS_STAMP = ".owner_worker_bundled_sync_stamp"


def _path_state(path: Path) -> str:
    try:
        metadata = path.stat()
        return f"{path.resolve()}:{metadata.st_mtime_ns}:{metadata.st_size}"
    except OSError:
        return f"{path}:missing"


def _owner_worker_skills_fingerprint(owner_home: Path) -> str:
    """Return a cheap invalidation key for owner startup skill synchronization."""
    checkout_skills_dir = (_PROJECT_ROOT / "skills").resolve()
    bundled_dir = get_bundled_skills_dir(checkout_skills_dir).resolve()
    source = (
        read_git_revision_fingerprint(_PROJECT_ROOT)
        if bundled_dir == checkout_skills_dir
        else None
    )
    if not source:
        source = (
            f"skills:{__version__}:{__release_date__}:"
            f"{_path_state(bundled_dir)}"
        )

    # These owner-local inputs can change sync behavior without a Hermes update.
    # Record metadata only; no config or skill content enters the stamp.
    owner_inputs = (
        _path_state(owner_home / ".no-bundled-skills"),
        _path_state(owner_home / "config.yaml"),
        _path_state(owner_home / "skills" / ".curator_suppressed"),
    )
    payload = "\n".join((source, *owner_inputs))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _owner_worker_skills_stamp_path(owner_home: Path) -> Path:
    return owner_home / "skills" / _OWNER_WORKER_SKILLS_STAMP


def _mark_owner_worker_skills_synced(owner_home: Path, fingerprint: str) -> None:
    """Best-effort atomic stamp; failure merely forces another real sync."""
    stamp = _owner_worker_skills_stamp_path(owner_home)
    temp_path: Path | None = None
    try:
        stamp.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=stamp.parent,
            prefix=f".{stamp.name}.",
            delete=False,
        ) as handle:
            handle.write(fingerprint + "\n")
            temp_path = Path(handle.name)
        os.replace(temp_path, stamp)
    except OSError:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except OSError:
                pass


def _sync_owner_skills(
    owner_home: Path,
    *,
    bundled_snapshot: Any | None = None,
) -> dict[str, Any] | None:
    """Synchronize bundled skills into one exact Owner home."""
    if (owner_home / ".no-bundled-skills").exists():
        return {
            "copied": [],
            "updated": [],
            "user_modified": [],
            "skipped_opt_out": True,
        }

    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from tools.skills_sync import sync_skills

    token = set_hermes_home_override(owner_home)
    try:
        return sync_skills(quiet=True, bundled_snapshot=bundled_snapshot)
    except Exception:
        return None
    finally:
        reset_hermes_home_override(token)


def _seed_owner_worker_skills(
    owner_home: Path,
    *,
    bundled_snapshot: Any | None = None,
) -> dict[str, Any]:
    """Synchronize changed bundled skills before the worker imports skill state."""
    fingerprint = _owner_worker_skills_fingerprint(owner_home)
    try:
        if _owner_worker_skills_stamp_path(owner_home).read_text(
            encoding="utf-8"
        ).strip() == fingerprint:
            return {"copied": [], "updated": [], "skipped_fresh": True}
    except OSError:
        pass

    result = _sync_owner_skills(
        owner_home,
        bundled_snapshot=bundled_snapshot,
    )
    if result is None:
        raise RuntimeError("owner bundled skill synchronization failed")
    _mark_owner_worker_skills_synced(owner_home, fingerprint)
    return result


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


_LOCAL_CONTROL_TIMEOUT = 0.2


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
        drain_timeout: float | None = None,
        max_owner_concurrency: int | None = None,
        control_ws_base: str | None = None,
        authority_store_factory: Callable[[Path], AuthorityStore] = AuthorityStore,
        authority_store: AuthorityStore | None = None,
        generation_bridge_revoker: Callable[..., None] | None = None,
        deployment_inference_policy: DeploymentInferencePolicy | None = None,
        deployment_inference_policy_resolver: Callable[[], DeploymentInferencePolicy] | None = None,
        deployment_image_policy: DeploymentImagePolicy | None = None,
        resource_manager: Any | None = None,
        launcher: Any | None = None,
    ) -> None:
        self.global_home = Path(global_home).resolve() if global_home else get_hermes_home().resolve()
        self.control_home = Path(control_home).resolve() if control_home else self.global_home / "control-plane"
        self.client_cls = client_cls
        self.process_factory = process_factory
        # Injected factories retain the direct callable path for isolated unit
        # doubles. Production starts only through the preload-ready launcher.
        self._use_preloaded_launcher = process_factory is subprocess.Popen
        self.launcher = None
        self._bundled_skill_snapshot = None
        if self._use_preloaded_launcher:
            from tools.skills_sync import prepare_bundled_skill_snapshot

            self.launcher = launcher or OwnerWorkerLauncher()
            self._bundled_skill_snapshot = prepare_bundled_skill_snapshot()
        elif launcher is not None:
            raise ValueError("owner worker launcher requires the production process factory")
        self.startup_timeout = startup_timeout
        self.poll_interval = poll_interval
        self.drain_timeout = max(
            0.0,
            float(
                drain_timeout
                if drain_timeout is not None
                else os.environ.get("HERMES_OWNER_WORKER_DRAIN_TIMEOUT", "60") or 60
            ),
        )
        self.resource_manager = resource_manager
        policy_max_workers = (
            resource_manager.policy.global_limits.max_owner_workers
            if resource_manager is not None else None
        )
        if resource_manager is not None and max_workers is not None and int(max_workers) != policy_max_workers:
            raise ValueError("owner worker limit must match the resource policy")
        configured_max_workers = (
            policy_max_workers
            if policy_max_workers is not None
            else max_workers if max_workers is not None
            else os.environ.get("HERMES_OWNER_WORKER_MAX", "16") or 16
        )
        self.max_workers = max(1, int(configured_max_workers))
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
        self.authority_store = authority_store or authority_store_factory(self.control_home)
        if self._use_preloaded_launcher:
            self.authority_store.ensure_ready()
            owner_worker_capability_public_config(self.control_home)
        self.generation_bridge_revoker = generation_bridge_revoker
        self.deployment_inference_policy = deployment_inference_policy
        self.deployment_inference_broker = (
            DeploymentInferenceBroker(
                policy=deployment_inference_policy,
                authority_store=self.authority_store,
                policy_resolver=deployment_inference_policy_resolver,
            )
            if deployment_inference_policy is not None
            else None
        )
        self.deployment_image_policy = deployment_image_policy
        self.deployment_image_broker = (
            DeploymentImageBroker(policy=deployment_image_policy, authority_store=self.authority_store)
            if deployment_image_policy is not None else None
        )
        self.deployment_resource_broker = (
            DeploymentResourceBroker(manager=resource_manager, authority_store=self.authority_store)
            if resource_manager is not None else None
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
        started_at = time.monotonic()
        trace_state = ["cold_start"]
        try:
            handle = self._get_or_start(owner, timeout=timeout, trace_state=trace_state)
        except BaseException:
            observe_latency_stage(
                stage="owner_worker.ready",
                started_at=started_at,
                outcome="error",
                path=trace_state[0],
            )
            raise
        observe_latency_stage(
            stage="owner_worker.ready",
            started_at=started_at,
            path=trace_state[0],
        )
        return handle

    def _get_or_start(
        self,
        owner: Any,
        *,
        timeout: float | None,
        trace_state: list[str],
    ) -> OwnerWorkerHandle:
        startup_timeout = self._startup_deadline_timeout(timeout)
        deadline: float | None = None
        owner_key = self._owner_key(owner)
        owner_home = self._owner_home(owner)
        waited = False
        while True:
            # These methods only select handles while locked; their synchronous
            # revoker/process work runs after releasing the supervisor lock.
            with observed_latency_stage(
                stage="owner_worker.ready.maintenance", path=trace_state[0]
            ):
                with self._lock:
                    requested = self._handles.get(owner_key)
                    requested_exited = (
                        requested is not None and requested.process.poll() is not None
                    )
                if requested_exited:
                    trace_state[0] = "replace_unhealthy"
                    self._terminate_handle(
                        owner_key,
                        requested,
                        readiness_path=trace_state[0],
                    )

            with self._start_finished:
                existing = self._handles.get(owner_key)
                if existing is not None:
                    if existing.owner_home.resolve() != owner_home.resolve():
                        raise RuntimeError("owner worker exact owner_home mismatch for owner_key")
                    if existing.accepting:
                        existing.last_used_at = time.time()
                elif owner_key in self._starting_owner_keys or owner_key in self._terminating_handles:
                    waited = True
                    trace_state[0] = "wait_existing_start"
                    if deadline is None:
                        deadline = time.monotonic() + startup_timeout
                    remaining = deadline - time.monotonic()
                    with observed_latency_stage(
                        stage="owner_worker.ready.start_wait", path=trace_state[0]
                    ):
                        if remaining <= 0 or not self._start_finished.wait(timeout=remaining):
                            raise TimeoutError("timed out waiting for owner worker startup")
                    continue
                else:
                    eviction = self._admit_start(owner_key, owner_home, now=time.time())
                    if eviction is None:
                        self._starting_owner_keys.add(owner_key)
                        self._in_flight_starts += 1
                        deadline = time.monotonic() + startup_timeout
                        if trace_state[0] != "replace_unhealthy":
                            trace_state[0] = "cold_start"
                        break

            if existing is not None:
                if existing.process.poll() is not None:
                    trace_state[0] = "replace_unhealthy"
                    self._terminate_handle(
                        owner_key,
                        existing,
                        readiness_path=trace_state[0],
                    )
                    continue
                try:
                    self.authority_store.assert_worker_lease(
                        self._lease_for_handle(existing),
                        states=frozenset({WorkerLeaseState.ACTIVE}),
                    )
                except AuthorizationRejected:
                    trace_state[0] = "replace_unhealthy"
                    self._terminate_handle(
                        owner_key,
                        existing,
                        readiness_path=trace_state[0],
                    )
                    continue
                with self._lock:
                    if self._handles.get(owner_key) is existing:
                        if not existing.accepting:
                            raise OwnerWorkerUnavailableError(
                                "owner worker is retiring"
                            )
                        if trace_state[0] != "replace_unhealthy":
                            trace_state[0] = (
                                "wait_existing_start"
                                if waited
                                else "hot_active"
                                if existing.active_uses > 0
                                else "hot_cached"
                            )
                        return existing
                continue

            # Capacity eviction was reserved while locked. Drain its admitted
            # work before strict bridge-revoke/process teardown, then retry.
            self._drain_and_teardown_reserved_handle(
                eviction[0],
                eviction[1],
                readiness_path=trace_state[0],
            )

        try:
            with observed_latency_stage(
                stage="owner_worker.ready.runtime_dirs", path=trace_state[0]
            ):
                ensure_owner_runtime_dirs(owner_home)
            with observed_latency_stage(
                stage="owner_worker.ready.skills_sync", path=trace_state[0]
            ):
                try:
                    if self._bundled_skill_snapshot is None:
                        _seed_owner_worker_skills(owner_home)
                    else:
                        _seed_owner_worker_skills(
                            owner_home,
                            bundled_snapshot=self._bundled_skill_snapshot,
                        )
                except Exception as exc:
                    raise OwnerWorkerStartupError(
                        f"owner bundled skill synchronization failed: {exc}"
                    ) from exc
            process_deadline = time.monotonic() + startup_timeout
            return self._start_owner_worker(
                owner,
                owner_key,
                owner_home,
                deadline=process_deadline,
                readiness_path=trace_state[0],
            )
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
        readiness_path: str,
    ) -> OwnerWorkerHandle:
        with observed_latency_stage(
            stage="owner_worker.ready.authority_claim", path=readiness_path
        ):
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
        image_relay_fd = None
        resource_broker_fd = None
        worker_resource_scope = None
        resource_started_at = time.monotonic()
        try:
            if self.resource_manager is not None:
                worker_resource_scope = self.resource_manager.admit_worker(claim.lease)
            if self.deployment_inference_broker is not None:
                relay_fd = self.deployment_inference_broker.register(claim.lease)
                env["HERMES_DEPLOYMENT_INFERENCE_RELAY_FD"] = str(relay_fd)
            if self.deployment_image_broker is not None:
                image_relay_fd = self.deployment_image_broker.register(claim.lease)
                env["HERMES_DEPLOYMENT_IMAGE_RELAY_FD"] = str(image_relay_fd)
            if self.deployment_resource_broker is not None:
                resource_broker_fd = self.deployment_resource_broker.register(claim.lease)
                env["HERMES_DEPLOYMENT_RESOURCE_BROKER_FD"] = str(resource_broker_fd)
            runtime_paths = owner_worker_runtime_paths(
                owner_home=owner_home,
                worker_generation=generation.worker_generation,
            )
            controlled_roots = self._controlled_roots_for(runtime_paths)
        except Exception as exc:
            observe_latency_stage(
                stage="owner_worker.ready.resource_admission",
                started_at=resource_started_at,
                outcome="error",
                path=readiness_path,
            )
            for fd in (relay_fd, image_relay_fd, resource_broker_fd):
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
            if self.deployment_inference_broker is not None:
                self.deployment_inference_broker.revoke(claim.lease)
            if self.deployment_image_broker is not None:
                self.deployment_image_broker.revoke(claim.lease)
            if self.deployment_resource_broker is not None:
                self.deployment_resource_broker.revoke(claim.lease)
            if worker_resource_scope is not None:
                worker_resource_scope.cleanup()
            try:
                self.authority_store.transition_worker_lease(
                    claim.lease, state=WorkerLeaseState.REVOKED,
                    generation_state=WorkerGenerationState.FAILED,
                )
            except AuthorizationRejected:
                pass
            raise OwnerWorkerStartupError("owner worker resource admission failed") from exc
        observe_latency_stage(
            stage="owner_worker.ready.resource_admission",
            started_at=resource_started_at,
            path=readiness_path,
        )
        process_started_at = time.monotonic()
        try:
            controlled_roots.mkdirs(
                RootKind.OWNER_WRITABLE,
                f"runtime/workers/{generation.worker_generation}",
            )
        except BaseException:
            observe_latency_stage(
                stage="owner_worker.ready.process_spawn",
                started_at=process_started_at,
                outcome="error",
                path=readiness_path,
            )
            controlled_roots.close()
            for fd in (relay_fd, image_relay_fd, resource_broker_fd):
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
            if self.deployment_inference_broker is not None:
                self.deployment_inference_broker.revoke(claim.lease)
            if self.deployment_image_broker is not None:
                self.deployment_image_broker.revoke(claim.lease)
            if self.deployment_resource_broker is not None:
                self.deployment_resource_broker.revoke(claim.lease)
            if worker_resource_scope is not None:
                worker_resource_scope.cleanup()
            try:
                self.authority_store.transition_worker_lease(
                    claim.lease, state=WorkerLeaseState.REVOKED,
                    generation_state=WorkerGenerationState.FAILED,
                )
            except AuthorizationRejected:
                pass
            raise
        process = None
        cwd_fd = None
        start_read = None
        start_write = None
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
            start_read, start_write = os.pipe()
            for inherited_fd in (inherited_cwd_fd, start_read):
                os.set_inheritable(inherited_fd, True)
            worker_argv = self._argv_for(owner, socket_path, generation)
            process_kwargs = {
                "env": env,
                "stdin": subprocess.DEVNULL,
                "stdout": stdout_handle,
                "stderr": stderr_handle,
                "close_fds": True,
            }
            try:
                if self._use_preloaded_launcher:
                    if self.launcher is None:
                        raise OwnerWorkerStartupError("owner worker launcher is unavailable")
                    relay_fds = {
                        name: fd
                        for name, fd in (
                            ("inference", relay_fd),
                            ("image", image_relay_fd),
                            ("resource", resource_broker_fd),
                        )
                        if fd is not None
                    }
                    process = self.launcher.spawn(
                        worker_argv[3:],
                        env=env,
                        cwd_fd=inherited_cwd_fd,
                        stdout_fd=stdout_handle,
                        stderr_fd=stderr_handle,
                        start_fd=start_read,
                        relay_fds=relay_fds,
                    )
                    os.close(start_read)
                    start_read = None
                    if worker_resource_scope is not None:
                        worker_resource_scope.attach(process.pid)
                        if not worker_resource_scope.verify_membership(process.pid):
                            raise OwnerWorkerStartupError("owner worker resource membership verification failed")
                    os.write(start_write, b"1")
                    os.close(start_write)
                    start_write = None
                elif worker_resource_scope is not None:
                    launcher_argv = [
                        sys.executable, "-m", "hermes_cli.owner_worker.launch_gate",
                        "--cwd-fd", str(inherited_cwd_fd), "--start-fd", str(start_read),
                        "--", *worker_argv,
                    ]
                    process = self.process_factory(
                        launcher_argv,
                        **process_kwargs,
                        pass_fds=tuple(
                            fd
                            for fd in (
                                inherited_cwd_fd,
                                start_read,
                                relay_fd,
                                image_relay_fd,
                                resource_broker_fd,
                            )
                            if fd is not None
                        ),
                    )
                    os.close(start_read)
                    start_read = None
                    worker_resource_scope.attach(process.pid)
                    if not worker_resource_scope.verify_membership(process.pid):
                        raise OwnerWorkerStartupError("owner worker resource membership verification failed")
                    os.write(start_write, b"1")
                    os.close(start_write)
                    start_write = None
                else:
                    os.close(start_read)
                    os.close(start_write)
                    start_read = start_write = None

                    def _set_descriptor_cwd() -> None:
                        os.fchdir(inherited_cwd_fd)
                        os.close(inherited_cwd_fd)

                    process = self.process_factory(
                        worker_argv,
                        **process_kwargs,
                        preexec_fn=_set_descriptor_cwd,
                        pass_fds=tuple(
                            fd for fd in (inherited_cwd_fd, relay_fd, image_relay_fd)
                            if fd is not None
                        ),
                    )
            finally:
                os.close(inherited_cwd_fd)
                if relay_fd is not None:
                    os.close(relay_fd)
                    relay_fd = None
                if image_relay_fd is not None:
                    os.close(image_relay_fd)
                    image_relay_fd = None
                if resource_broker_fd is not None:
                    os.close(resource_broker_fd)
                    resource_broker_fd = None
        except Exception as exc:
            observe_latency_stage(
                stage="owner_worker.ready.process_spawn",
                started_at=process_started_at,
                outcome="error",
                path=readiness_path,
            )
            for fd in (start_read, start_write):
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        pass
            if self.deployment_inference_broker is not None:
                self.deployment_inference_broker.revoke(claim.lease)
            if self.deployment_image_broker is not None:
                self.deployment_image_broker.revoke(claim.lease)
            if self.deployment_resource_broker is not None:
                self.deployment_resource_broker.revoke(claim.lease)
            if worker_resource_scope is not None:
                worker_resource_scope.cleanup()
                worker_resource_scope = None
            self._try_cleanup_generation_runtime(owner_home, generation.worker_generation)
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
        observe_latency_stage(
            stage="owner_worker.ready.process_spawn",
            started_at=process_started_at,
            path=readiness_path,
        )
        health_started_at = time.monotonic()
        activation_started_at: float | None = None
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
            observe_latency_stage(
                stage="owner_worker.ready.health_wait",
                started_at=health_started_at,
                path=readiness_path,
            )
            activation_started_at = time.monotonic()
            active_lease = self.authority_store.transition_worker_lease(
                claim.lease,
                state=WorkerLeaseState.ACTIVE,
                generation_state=WorkerGenerationState.ACTIVE,
            )
        except Exception as exc:
            observe_latency_stage(
                stage=(
                    "owner_worker.ready.lease_activate"
                    if activation_started_at is not None
                    else "owner_worker.ready.health_wait"
                ),
                started_at=activation_started_at or health_started_at,
                outcome="error",
                path=readiness_path,
            )
            if self.deployment_inference_broker is not None:
                self.deployment_inference_broker.revoke(claim.lease)
            if self.deployment_image_broker is not None:
                self.deployment_image_broker.revoke(claim.lease)
            if self.deployment_resource_broker is not None:
                self.deployment_resource_broker.revoke(claim.lease)
            if worker_resource_scope is not None:
                worker_resource_scope.cleanup()
                worker_resource_scope = None
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
            if process.poll() is not None:
                self._try_cleanup_generation_runtime(
                    owner_home,
                    generation.worker_generation,
                    socket_path=socket_path,
                )
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
            resource_scope=worker_resource_scope,
        )
        try:
            if self.deployment_inference_broker is not None:
                self.deployment_inference_broker.activate(active_lease)
            if self.deployment_image_broker is not None:
                self.deployment_image_broker.activate(active_lease)
            if self.deployment_resource_broker is not None:
                self.deployment_resource_broker.activate(active_lease)
        except Exception as exc:
            observe_latency_stage(
                stage="owner_worker.ready.lease_activate",
                started_at=activation_started_at,
                outcome="error",
                path=readiness_path,
            )
            if self.deployment_inference_broker is not None:
                self.deployment_inference_broker.revoke(active_lease)
            if self.deployment_image_broker is not None:
                self.deployment_image_broker.revoke(active_lease)
            if self.deployment_resource_broker is not None:
                self.deployment_resource_broker.revoke(active_lease)
            self._audit_generation(AuthorityAuditReason.GENERATION_START_FAILED, active_lease)
            try:
                self.authority_store.transition_worker_lease(
                    active_lease,
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
            if handle.resource_scope is not None:
                handle.resource_scope.cleanup()
                handle.resource_scope = None
            self._try_cleanup_generation_runtime(
                owner_home,
                generation.worker_generation,
                socket_path=socket_path,
            )
            raise OwnerWorkerStartupError("owner worker relay activation failed") from exc
        with self._lock:
            self._handles[owner_key] = handle
        self._audit_generation(AuthorityAuditReason.GENERATION_ACTIVE, active_lease)
        observe_latency_stage(
            stage="owner_worker.ready.lease_activate",
            started_at=activation_started_at,
            path=readiness_path,
        )
        return handle

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
        if not canonical_unix_peer_is_absent(socket_path):
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
        self._try_cleanup_generation_runtime(owner_home, lease.worker_generation)
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
        first_error = None
        draining: list[tuple[str, OwnerWorkerHandle, OwnerWorkerAuthorityLease, OwnerWorkerClient]] = []
        for owner_key, handle in reserved:
            if handle.process.poll() is not None:
                continue
            lease = self._lease_for_handle(handle)
            client = self.client_cls(
                handle.socket_path,
                timeout=max(2.0, self.poll_interval * 2),
                control_home=self.control_home,
            )
            try:
                client.begin_drain(lease=lease)
                draining.append((owner_key, handle, lease, client))
            except Exception as exc:
                first_error = first_error or exc
        deadline = time.monotonic() + self.drain_timeout
        pending = list(draining)
        while pending and time.monotonic() < deadline:
            still_pending = []
            for item in pending:
                try:
                    if item[3].drain_status(lease=item[2]).get("active_turns", 0) > 0:
                        still_pending.append(item)
                except Exception as exc:
                    first_error = first_error or exc
                    still_pending.append(item)
            pending = still_pending
            if pending:
                time.sleep(self.poll_interval)
        for _owner_key, _handle, lease, client in pending:
            try:
                client.force_drain(lease=lease)
            except Exception as exc:
                first_error = first_error or exc
        for owner_key, handle in reserved:
            try:
                self._teardown_terminated_handle(
                    owner_key,
                    handle,
                    planned_restart=True,
                )
            except Exception as exc:
                first_error = first_error or exc
        if self.launcher is not None:
            try:
                self.launcher.close()
            except Exception as exc:
                first_error = first_error or exc
        for broker in (
            self.deployment_inference_broker,
            self.deployment_image_broker,
            self.deployment_resource_broker,
        ):
            if broker is None:
                continue
            try:
                broker.close()
            except Exception as exc:
                first_error = first_error or exc
        if first_error is not None:
            raise first_error

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

    def needs_start(self, owner: Any) -> bool:
        """Return whether the exact owner lacks a live accepting Worker."""
        owner_key = self._owner_key(owner)
        owner_home = self._owner_home(owner)
        with self._lock:
            handle = self._handles.get(owner_key)
            return not (
                handle is not None
                and handle.owner_home.resolve() == owner_home.resolve()
                and handle.accepting
                and handle.process.poll() is None
            )

    def acquire_use(self, handle: OwnerWorkerHandle) -> OwnerWorkerLease:
        """Mark a worker as actively serving an HTTP stream or WS bridge."""
        with self._lock:
            current = self._handles.get(handle.owner_key)
            if current is not handle or not handle.accepting:
                raise OwnerWorkerUnavailableError(
                    "owner worker handle is no longer active"
                )
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
        with self._start_finished:
            if handle.active_uses > 0:
                handle.active_uses -= 1
            handle.last_used_at = time.time()
            if handle.active_uses == 0 and not handle.accepting:
                self._start_finished.notify_all()

    def report_request_failure(self, handle: OwnerWorkerHandle) -> bool:
        """Fence an exact failed handle for lifecycle-only retirement."""
        with self._start_finished:
            if self._handles.get(handle.owner_key) is not handle:
                return False
            if handle.process.poll() is None:
                try:
                    self.authority_store.assert_worker_lease(
                        self._lease_for_handle(handle),
                        states=frozenset({WorkerLeaseState.ACTIVE}),
                    )
                except AuthorizationRejected:
                    return False
            handle.accepting = False
            self._start_finished.notify_all()
            return True

    def maintenance_tick(self, *, now: float | None = None) -> set[str]:
        """Perform lifecycle-only reaping, retirement, and idle cleanup."""
        observed_at = time.time() if now is None else float(now)
        reserved: list[tuple[str, OwnerWorkerHandle]] = []
        with self._lock:
            for owner_key, handle in tuple(self._handles.items()):
                should_retire = (
                    handle.process.poll() is not None
                    or not handle.accepting
                    or (
                        handle.active_uses <= 0
                        and observed_at - handle.last_used_at >= self.idle_timeout
                    )
                )
                if (
                    should_retire
                    and handle.active_uses <= 0
                    and self._reserve_termination_locked(owner_key, handle)
                ):
                    reserved.append((owner_key, handle))
        for owner_key, handle in reserved:
            self._drain_and_teardown_reserved_handle(owner_key, handle)
        return {owner_key for owner_key, _handle in reserved}

    def next_maintenance_delay(self, *, now: float | None = None) -> float:
        observed_at = time.time() if now is None else float(now)
        with self._lock:
            if any(not handle.accepting for handle in self._handles.values()):
                return max(0.01, self.poll_interval)
            deadlines = [
                max(0.0, handle.last_used_at + self.idle_timeout - observed_at)
                for handle in self._handles.values()
                if handle.active_uses <= 0
            ]
        return min(deadlines, default=self.idle_timeout)

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

    def _cleanup_generation_runtime(
        self,
        owner_home: Path,
        worker_generation: int,
        *,
        socket_path: Path | None = None,
    ) -> None:
        runtime_paths = owner_worker_runtime_paths(
            owner_home=owner_home,
            worker_generation=worker_generation,
        )
        if socket_path is not None and socket_path.resolve(strict=False) != runtime_paths.worker_socket.resolve(
            strict=False
        ):
            raise RuntimeError("owner worker socket does not match worker generation")
        controlled_roots = self._controlled_roots_for(runtime_paths)
        try:
            relative_runtime = runtime_paths.worker_runtime_dir.relative_to(runtime_paths.owner_home)
            controlled_roots.remove_tree_for_cleanup(
                RootKind.OWNER_WRITABLE,
                relative_runtime.as_posix(),
            )
        finally:
            controlled_roots.close()

    def _try_cleanup_generation_runtime(
        self,
        owner_home: Path,
        worker_generation: int,
        *,
        socket_path: Path | None = None,
    ) -> None:
        try:
            self._cleanup_generation_runtime(
                owner_home,
                worker_generation,
                socket_path=socket_path,
            )
        except Exception:
            _log.warning(
                "Owner worker generation runtime cleanup failed",
                extra={"worker_generation": worker_generation},
                exc_info=True,
            )

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

    def _terminate_handle(
        self,
        owner_key: str,
        handle: OwnerWorkerHandle,
        *,
        readiness_path: str | None = None,
        control_timeout: float | None = None,
    ) -> None:
        """Retire one exact local handle without holding the supervisor lock."""
        with self._lock:
            if not self._reserve_termination_locked(owner_key, handle):
                return
        self._drain_and_teardown_reserved_handle(
            owner_key,
            handle,
            readiness_path=readiness_path,
            control_timeout=control_timeout,
        )

    def _drain_and_teardown_reserved_handle(
        self,
        owner_key: str,
        handle: OwnerWorkerHandle,
        *,
        planned_restart: bool = False,
        readiness_path: str | None = None,
        control_timeout: float | None = None,
    ) -> None:
        """Drain one reserved live Worker before ordered generation teardown."""
        drain_stage = (
            observed_latency_stage(
                stage="owner_worker.ready.replacement_drain",
                path=readiness_path,
            )
            if readiness_path is not None
            else nullcontext()
        )
        try:
            with drain_stage:
                if handle.process.poll() is None:
                    lease = self._lease_for_handle(handle)
                    client = self.client_cls(
                        handle.socket_path,
                        timeout=(
                            _LOCAL_CONTROL_TIMEOUT
                            if control_timeout is None
                            else control_timeout
                        ),
                        control_home=self.control_home,
                    )
                    status = client.begin_drain(lease=lease)
                    deadline = time.monotonic() + self.drain_timeout
                    while status.get("active_turns", 0) > 0 and time.monotonic() < deadline:
                        time.sleep(self.poll_interval)
                        status = client.drain_status(lease=lease)
                    if status.get("active_turns", 0) > 0:
                        client.force_drain(lease=lease)
        except Exception:
            # Ordered teardown still fences exact capability and process state.
            # Worker lifespan performs a final resumable force-drain on exit.
            pass
        teardown_stage = (
            observed_latency_stage(
                stage="owner_worker.ready.replacement_teardown",
                path=readiness_path,
            )
            if readiness_path is not None
            else nullcontext()
        )
        with teardown_stage:
            self._teardown_terminated_handle(
                owner_key,
                handle,
                planned_restart=planned_restart,
            )

    def _teardown_terminated_handle(
        self,
        owner_key: str,
        handle: OwnerWorkerHandle,
        *,
        planned_restart: bool = False,
    ) -> None:
        """Run ordered external teardown for a previously reserved handle.

        This method deliberately never holds ``_lock`` while calling the bridge
        revoker. The revoker synchronously waits for bridge close, which releases
        an owner-use lease back through ``release_use()`` and must acquire it.
        """
        first_error = None
        draining = self._drain_handle(handle)
        if draining is not None:
            self._audit_generation(AuthorityAuditReason.GENERATION_DRAINING, draining)
        retired_lease = draining or self._lease_for_handle(handle)

        def _cleanup(callback: Callable[[], None]) -> None:
            nonlocal first_error
            try:
                callback()
            except Exception as exc:
                first_error = first_error or exc

        try:
            # Fence exact capability/bootstrap admission before closing bridge or
            # touching the process. Cleanup continues after individual failures so
            # no broker endpoint, process, or cgroup reservation is leaked.
            for broker in (
                self.deployment_inference_broker,
                self.deployment_image_broker,
                self.deployment_resource_broker,
            ):
                if broker is not None:
                    _cleanup(lambda broker=broker: broker.revoke(retired_lease))
            if self.generation_bridge_revoker is not None:
                _cleanup(
                    lambda: self.generation_bridge_revoker(
                        retired_lease,
                        planned_restart=planned_restart,
                    )
                )

            process_exited = handle.process.poll() is not None
            if process_exited:
                _cleanup(handle.process.wait)
            else:
                try:
                    handle.process.terminate()
                    handle.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    handle.process.kill()
                    try:
                        handle.process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        pass
                except Exception as exc:
                    first_error = first_error or exc
                process_exited = handle.process.poll() is not None
            if handle.resource_scope is not None:
                scope = handle.resource_scope
                _cleanup(scope.cleanup)
                if getattr(scope, "released", False):
                    handle.resource_scope = None
            if process_exited:
                self._try_cleanup_generation_runtime(
                    handle.owner_home,
                    handle.worker_generation,
                    socket_path=handle.socket_path,
                )
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
            if first_error is not None:
                raise first_error
        finally:
            with self._start_finished:
                if self._terminating_handles.get(owner_key) is handle:
                    self._terminating_handles.pop(owner_key, None)
                self._start_finished.notify_all()

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
            self._drain_and_teardown_reserved_handle(*reserved)

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
                    if self.deployment_inference_policy is not None else None
                ),
                deployment_image_descriptor=(
                    self.deployment_image_policy.descriptor()
                    if self.deployment_image_policy is not None else None
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
