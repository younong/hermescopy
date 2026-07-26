"""Supervisor for lightweight per-owner Session Reader processes."""
from __future__ import annotations

import os
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
from hermes_cli.dashboard_auth.authority import (
    AuthorityStore,
    AuthorizationRejected,
    ReaderGenerationState,
    ReaderLeaseState,
    SessionReaderAuthorityLease,
)
from hermes_cli.dashboard_auth.owner_context import admit_host_owner_home
from .client import SessionReaderClient, SessionReaderHealthError, warm_http_transport
from .runtime import (
    prepare_session_reader_runtime,
    session_reader_env_for,
    session_reader_runtime_dir,
    session_reader_socket_path,
)
from .tokens import session_reader_capability_public_config
from hermes_cli.owner_worker.tokens import _signing_record


class SessionReaderUnavailableError(RuntimeError):
    """A Session Reader cannot be admitted or started."""


class SessionReaderStartupError(SessionReaderUnavailableError):
    """A Reader process exited or failed health verification during startup."""


@dataclass
class SessionReaderHandle:
    owner_key: str
    owner_home: Path
    reader_generation: int
    reader_id: str
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
    retire_pending: bool = False
    resource_scope: Any | None = field(default=None, repr=False)


class SessionReaderUse:
    def __init__(
        self,
        supervisor: "SessionReaderSupervisor",
        handle: SessionReaderHandle,
        lease: SessionReaderAuthorityLease,
    ) -> None:
        self.supervisor = supervisor
        self.handle = handle
        self.lease = lease
        self.released = False

    def release(self) -> None:
        if not self.released:
            self.released = True
            self.supervisor.release_use(self.handle)

    def __enter__(self) -> "SessionReaderUse":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.release()


_ENV_ALLOW = frozenset({
    "CONDA_DEFAULT_ENV", "CONDA_PREFIX", "CURL_CA_BUNDLE", "LANG", "LC_ALL", "LC_CTYPE",
    "PATH", "PYTHONPATH", "REQUESTS_CA_BUNDLE", "SSL_CERT_DIR", "SSL_CERT_FILE",
    "TMP", "TMPDIR", "TEMP", "VIRTUAL_ENV",
})
if os.name == "nt":
    _ENV_ALLOW |= frozenset({"COMSPEC", "PATHEXT", "SYSTEMDRIVE", "SYSTEMROOT", "WINDIR"})


class SessionReaderSupervisor:
    """Start, coalesce, health-check, and retire one Reader per owner."""

    def __init__(
        self,
        *,
        control_home: str | Path | None = None,
        global_home: str | Path | None = None,
        client_cls: type[SessionReaderClient] = SessionReaderClient,
        process_factory: Callable[..., subprocess.Popen[Any]] = subprocess.Popen,
        startup_timeout: float = 1.0,
        poll_interval: float = 0.01,
        max_readers: int = 64,
        idle_timeout: float = 1800,
        resource_manager: Any | None = None,
    ) -> None:
        self.global_home = Path(global_home).resolve() if global_home else get_hermes_home().resolve()
        self.control_home = Path(control_home).resolve() if control_home else self.global_home / "control-plane"
        self.client_cls = client_cls
        self.process_factory = process_factory
        self.startup_timeout = float(startup_timeout)
        self.poll_interval = float(poll_interval)
        self.max_readers = max(1, int(max_readers))
        self.idle_timeout = max(1.0, float(idle_timeout))
        self.resource_manager = resource_manager
        self.authority_store = AuthorityStore(self.control_home)
        self.signing_record = _signing_record(self.control_home)
        warm_http_transport()
        self._handles: dict[str, SessionReaderHandle] = {}
        self._starting: set[str] = set()
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)

    def get_or_start(self, owner: Any, *, timeout: float | None = None) -> SessionReaderHandle:
        """Compatibility alias for lifecycle callers; requests use acquire_active."""
        return self.ensure_started(owner, timeout=timeout)

    def ensure_started(
        self,
        owner: Any,
        *,
        timeout: float | None = None,
    ) -> SessionReaderHandle:
        owner_key = self._owner_key(owner)
        owner_home = self._owner_home(owner)
        startup_timeout = self.startup_timeout if timeout is None else float(timeout)
        deadline = time.monotonic() + startup_timeout
        with self._condition:
            while owner_key in self._starting:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not self._condition.wait(remaining):
                    raise TimeoutError("timed out waiting for session reader startup")
            existing = self._handles.get(owner_key)
            if existing is not None:
                if existing.owner_home != owner_home:
                    raise RuntimeError("session reader exact owner_home mismatch")
                if (
                    existing.accepting
                    and existing.process.poll() is None
                ):
                    self.authority_store.assert_reader_lease(
                        self._lease_for_handle(existing),
                        states=frozenset({ReaderLeaseState.ACTIVE}),
                    )
                    return existing
                raise SessionReaderUnavailableError(
                    "session reader is not accepting requests"
                )
            if len(self._handles) >= self.max_readers:
                raise SessionReaderUnavailableError("session reader limit reached")
            self._starting.add(owner_key)
        try:
            return self._start(owner, owner_key, owner_home, deadline=deadline)
        finally:
            with self._condition:
                self._starting.discard(owner_key)
                self._condition.notify_all()

    def acquire_active(self, owner: Any) -> SessionReaderUse:
        """Pin one already ACTIVE Reader without performing lifecycle work."""
        owner_key = self._owner_key(owner)
        owner_home = self._owner_home(owner)
        with self._lock:
            handle = self._handles.get(owner_key)
            if handle is None or not handle.accepting:
                raise SessionReaderUnavailableError("session reader is not active")
            if handle.owner_home != owner_home:
                raise SessionReaderUnavailableError("session reader owner mismatch")
            if handle.process.poll() is not None:
                raise SessionReaderUnavailableError("session reader process exited")
            try:
                lease = self.authority_store.assert_reader_lease(
                    self._lease_for_handle(handle),
                    states=frozenset({ReaderLeaseState.ACTIVE}),
                )
            except AuthorizationRejected as exc:
                raise SessionReaderUnavailableError("session reader lease is unavailable") from exc
            handle.active_uses += 1
            handle.last_used_at = time.time()
            return SessionReaderUse(self, handle, lease)

    def _start(
        self,
        owner: Any,
        owner_key: str,
        owner_home: Path,
        *,
        deadline: float,
    ) -> SessionReaderHandle:
        if not isinstance(owner, dict):
            admitted = admit_host_owner_home(owner)
            if admitted != owner_home:
                raise RuntimeError("session reader admitted owner_home mismatch")
        else:
            owner_home.mkdir(mode=0o700, parents=True, exist_ok=True)
            if os.name != "nt":
                owner_home.chmod(0o700)
        try:
            claim = self.authority_store.claim_reader_start(
                owner_key,
                reader_id=uuid.uuid4().hex,
            )
        except AuthorizationRejected as exc:
            raise SessionReaderUnavailableError(f"session reader is already owned: {exc}") from exc
        lease = claim.lease
        resource_scope = None
        if self.resource_manager is not None:
            try:
                resource_scope = self.resource_manager.admit_reader(lease)
            except Exception as exc:
                self._fail_start(lease)
                raise SessionReaderUnavailableError(
                    f"session reader resource admission failed: {exc}"
                ) from exc
        try:
            paths = prepare_session_reader_runtime(
                owner_home,
                lease.reader_generation,
            )
            socket_path = paths.reader_socket
            runtime_dir = paths.reader_runtime_dir
            socket_path.with_name("reader.ready.json").unlink(missing_ok=True)
        except Exception:
            self._fail_start(lease)
            if resource_scope is not None:
                resource_scope.cleanup()
            self._cleanup_runtime(owner_home, lease.reader_generation)
            raise
        verifier = session_reader_capability_public_config(self.control_home)
        env = {key: value for key, value in os.environ.items() if key in _ENV_ALLOW}
        env.update(session_reader_env_for(
            owner_key=owner_key,
            owner_home=owner_home,
            control_home=self.control_home,
            reader_generation=lease.reader_generation,
            reader_id=lease.reader_id,
            lease_version=lease.lease_version,
            recovery_generation=lease.recovery_generation,
            capability_issuer=verifier["HERMES_SESSION_READER_CAPABILITY_ISSUER"],
            capability_public_key=verifier["HERMES_SESSION_READER_CAPABILITY_PUBLIC_KEY"],
            capability_retained_public_keys=verifier[
                "HERMES_SESSION_READER_CAPABILITY_RETAINED_PUBLIC_KEYS"
            ],
        ))
        package_root = str(Path(__file__).resolve().parents[2])
        inherited = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = package_root if not inherited else f"{package_root}{os.pathsep}{inherited}"
        argv = [
            sys.executable, "-m", "hermes_cli.session_reader.entrypoint",
            "--owner-key", owner_key,
            "--owner-home", str(owner_home),
            "--socket", str(socket_path),
            "--control-home", str(self.control_home),
            "--reader-generation", str(lease.reader_generation),
            "--reader-id", lease.reader_id,
        ]
        log_dir = owner_home / "runtime/logs"
        stdout_fd = os.open(log_dir / "session-reader.stdout.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        stderr_fd = os.open(log_dir / "session-reader.stderr.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            process = self.process_factory(
                argv, env=env, cwd=owner_home, stdin=subprocess.DEVNULL,
                stdout=stdout_fd, stderr=stderr_fd, close_fds=True,
            )
        except Exception as exc:
            self._fail_start(lease)
            if resource_scope is not None:
                resource_scope.cleanup()
            raise SessionReaderStartupError(f"session reader process launch failed: {exc}") from exc
        finally:
            os.close(stdout_fd)
            os.close(stderr_fd)
        try:
            if resource_scope is not None:
                resource_scope.attach(process.pid)
            health = self._wait_healthy(process, socket_path, lease, owner_home, deadline)
            if os.name != "nt":
                socket_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
            active = self.authority_store.transition_reader_lease(
                lease, state=ReaderLeaseState.ACTIVE, generation_state=ReaderGenerationState.ACTIVE,
            )
        except Exception:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1)
            self._fail_start(lease)
            if resource_scope is not None:
                resource_scope.cleanup()
                resource_scope = None
            self._cleanup_runtime(owner_home, lease.reader_generation)
            raise
        handle = SessionReaderHandle(
            owner_key=owner_key,
            owner_home=owner_home,
            reader_generation=active.reader_generation,
            reader_id=active.reader_id,
            lease_version=active.lease_version,
            recovery_generation=active.recovery_generation,
            socket_path=socket_path,
            process=process,
            pid=int(health["pid"]),
            last_health=health,
            resource_scope=resource_scope,
        )
        with self._lock:
            self._handles[owner_key] = handle
        return handle

    def _wait_healthy(
        self,
        process: subprocess.Popen[Any],
        socket_path: Path,
        lease: SessionReaderAuthorityLease,
        owner_home: Path,
        deadline: float,
    ) -> dict[str, Any]:
        last_error: Exception | None = None
        ready_path = socket_path.with_name("reader.ready.json")
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise SessionReaderStartupError(
                    f"session reader exited during startup with code {process.returncode}"
                )
            if socket_path.exists() and ready_path.exists():
                try:
                    import json

                    health = json.loads(ready_path.read_text(encoding="utf-8"))
                    self.client_cls.verify_health_payload(
                        health,
                        lease=lease,
                        owner_home=owner_home,
                    )
                    return health
                except (OSError, ValueError, SessionReaderHealthError) as exc:
                    last_error = exc
            time.sleep(min(self.poll_interval, max(0.0, deadline - time.monotonic())))
        if last_error is not None:
            raise SessionReaderStartupError(f"session reader failed health verification: {last_error}")
        raise TimeoutError("timed out waiting for session reader socket")

    def acquire_use(self, handle: SessionReaderHandle) -> SessionReaderUse:
        """Compatibility helper for non-request callers holding an exact handle."""
        return self.acquire_active({
            "owner_key": handle.owner_key,
            "owner_home": handle.owner_home,
        })

    def release_use(self, handle: SessionReaderHandle) -> None:
        retire = False
        with self._lock:
            if handle.active_uses > 0:
                handle.active_uses -= 1
            handle.last_used_at = time.time()
            retire = handle.active_uses == 0 and handle.retire_pending
        if retire:
            self._retire(handle)

    def report_request_failure(
        self,
        lease: SessionReaderAuthorityLease,
    ) -> bool:
        """Fence a failed Reader generation for lifecycle-only retirement."""
        with self._lock:
            handle = self._handles.get(str(lease.owner_key))
            if handle is None or self._lease_for_handle(handle) != lease:
                return False
            handle.accepting = False
            handle.retire_pending = True
            handle.last_used_at = 0.0
            return True

    def shutdown(self) -> None:
        with self._lock:
            handles = tuple(self._handles.values())
            self._handles.clear()
            for handle in handles:
                handle.accepting = False
                handle.retire_pending = handle.active_uses > 0
        for handle in handles:
            if not handle.retire_pending:
                self._retire(handle)

    def maintenance_tick(self, *, now: float | None = None) -> None:
        """Perform lifecycle-only reaping, idle retirement, and capacity cleanup."""
        observed_at = time.time() if now is None else float(now)
        with self._lock:
            candidates = [
                (owner_key, handle)
                for owner_key, handle in self._handles.items()
                if handle.process.poll() is not None
                or (
                    handle.active_uses <= 0
                    and observed_at - handle.last_used_at >= self.idle_timeout
                )
            ]
            excess = max(0, len(self._handles) - self.max_readers)
            if excess:
                selected = {owner_key for owner_key, _handle in candidates}
                additional = sorted(
                    (
                        (handle.last_used_at, owner_key, handle)
                        for owner_key, handle in self._handles.items()
                        if owner_key not in selected
                        and handle.active_uses <= 0
                    ),
                    key=lambda item: item[0],
                )[:excess]
                candidates.extend((owner_key, handle) for _at, owner_key, handle in additional)
            retire: list[SessionReaderHandle] = []
            for owner_key, handle in candidates:
                if self._handles.get(owner_key) is not handle:
                    continue
                handle.accepting = False
                handle.retire_pending = handle.active_uses > 0
                self._handles.pop(owner_key, None)
                if not handle.retire_pending:
                    retire.append(handle)
        for handle in retire:
            self._retire(handle)

    def _reap_exited(self) -> None:
        self.maintenance_tick(now=time.time())

    def _stop_idle(self, *, now: float) -> None:
        self.maintenance_tick(now=now)

    def _retire(self, handle: SessionReaderHandle) -> None:
        with self._lock:
            handle.accepting = False
            if handle.active_uses > 0:
                handle.retire_pending = True
                return
            handle.retire_pending = False
        lease = self._lease_for_handle(handle)
        draining: SessionReaderAuthorityLease | None = None
        try:
            draining = self.authority_store.transition_reader_lease(
                lease, state=ReaderLeaseState.DRAINING,
                generation_state=ReaderGenerationState.DRAINING,
            )
        except AuthorizationRejected:
            pass
        if handle.process.poll() is None:
            handle.process.terminate()
            try:
                handle.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                handle.process.kill()
                handle.process.wait(timeout=1)
        else:
            handle.process.wait()
        if handle.resource_scope is not None:
            handle.resource_scope.cleanup()
            handle.resource_scope = None
        if draining is not None:
            try:
                self.authority_store.transition_reader_lease(
                    draining, state=ReaderLeaseState.REVOKED,
                    generation_state=ReaderGenerationState.TERMINATED,
                )
            except AuthorizationRejected:
                pass
        self._cleanup_runtime(handle.owner_home, handle.reader_generation)

    def _fail_start(self, lease: SessionReaderAuthorityLease) -> None:
        try:
            self.authority_store.transition_reader_lease(
                lease, state=ReaderLeaseState.REVOKED,
                generation_state=ReaderGenerationState.FAILED,
            )
        except AuthorizationRejected:
            pass

    @staticmethod
    def _cleanup_runtime(owner_home: Path, generation: int) -> None:
        runtime = session_reader_runtime_dir(owner_home, generation)
        socket_path = session_reader_socket_path(owner_home, generation)
        for path in (socket_path, socket_path.with_name("reader.ready.json")):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        try:
            runtime.rmdir()
        except OSError:
            pass

    @staticmethod
    def _lease_for_handle(handle: SessionReaderHandle) -> SessionReaderAuthorityLease:
        return SessionReaderAuthorityLease(
            handle.owner_key,
            handle.reader_generation,
            handle.reader_id,
            ReaderLeaseState.ACTIVE,
            handle.lease_version,
            handle.recovery_generation,
        )

    @staticmethod
    def _get(owner: Any, name: str) -> Any:
        return owner.get(name) if isinstance(owner, dict) else getattr(owner, name)

    @classmethod
    def _owner_key(cls, owner: Any) -> str:
        value = str(cls._get(owner, "owner_key") or "").strip()
        if not value:
            raise ValueError("owner.owner_key is required")
        return value

    @classmethod
    def _owner_home(cls, owner: Any) -> Path:
        return Path(cls._get(owner, "owner_home")).expanduser().resolve()
