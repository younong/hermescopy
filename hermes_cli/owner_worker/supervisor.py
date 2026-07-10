"""Supervisor for per-owner Hermes worker processes."""
from __future__ import annotations

import os
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from hermes_constants import get_hermes_home
from hermes_cli.owner_runtime import ensure_owner_runtime_dirs, owner_worker_env_for

from .client import OwnerWorkerClient, OwnerWorkerHealthError


@dataclass
class OwnerWorkerHandle:
    owner_key: str
    owner_home: Path
    socket_path: Path
    process: subprocess.Popen[Any]
    pid: int
    started_at: float = field(default_factory=time.time)
    last_used_at: float = field(default_factory=time.time)
    last_health: dict[str, Any] = field(default_factory=dict)
    active_uses: int = 0


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
    "HERMES_OWNER_WORKER_TOKEN_SECRET",
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
        control_ws_base: str | None = None,
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
        self.control_ws_base = (control_ws_base or os.environ.get("HERMES_OWNER_WORKER_CONTROL_WS_BASE", "")).strip()
        self._handles: dict[str, OwnerWorkerHandle] = {}
        self._last_start_attempt: dict[str, float] = {}
        self._lock = threading.RLock()

    def get_or_start(self, owner: Any) -> OwnerWorkerHandle:
        with self._lock:
            owner_key = self._owner_key(owner)
            owner_home = self._owner_home(owner)
            socket_path = self.socket_path_for(owner)
            now = time.time()
            self._reap_exited()
            self._stop_idle(now=now)

            existing = self._handles.get(owner_key)
            if existing is not None:
                if existing.owner_home.resolve() != owner_home.resolve():
                    raise RuntimeError("owner worker exact owner_home mismatch for owner_key")
                existing.last_used_at = time.time()
                if existing.process.poll() is None:
                    health = self.client_cls(existing.socket_path, control_home=self.control_home).verify_health(
                        owner_key=owner_key,
                        owner_home=owner_home,
                    )
                    if int(health["pid"]) != existing.pid:
                        raise RuntimeError("owner worker pid mismatch")
                    existing.last_health = health
                    return existing
                self._handles.pop(owner_key, None)

            last_attempt = self._last_start_attempt.get(owner_key, 0.0)
            if self.startup_cooldown and now - last_attempt < self.startup_cooldown:
                raise RuntimeError("owner worker startup throttled")
            if owner_key not in self._handles and len(self._handles) >= self.max_workers:
                self._evict_oldest_idle(now=now)
            if owner_key not in self._handles and len(self._handles) >= self.max_workers:
                raise RuntimeError("owner worker limit reached")

            self._last_start_attempt[owner_key] = now
            ensure_owner_runtime_dirs(owner_home)

            try:
                socket_path.unlink()
            except FileNotFoundError:
                pass

            env = self._env_for(owner)
            cwd = (Path(env["HERMES_WORKSPACE_ROOT"]) / "default").resolve()
            stdout_path = owner_home / "runtime" / "logs" / "owner-worker.stdout.log"
            stderr_path = owner_home / "runtime" / "logs" / "owner-worker.stderr.log"
            stdout_handle = None
            stderr_handle = None
            try:
                stdout_handle = stdout_path.open("ab")
                stderr_handle = stderr_path.open("ab")
                self._chmod_private_file(stdout_path)
                self._chmod_private_file(stderr_path)
                process = self.process_factory(
                    self._argv_for(owner, socket_path),
                    env=env,
                    cwd=str(cwd),
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_handle,
                    stderr=stderr_handle,
                    close_fds=True,
                )
            finally:
                if stdout_handle is not None:
                    stdout_handle.close()
                if stderr_handle is not None:
                    stderr_handle.close()
            try:
                health = self._wait_until_healthy(
                    process=process,
                    socket_path=socket_path,
                    owner_key=owner_key,
                    owner_home=owner_home,
                )
                self._chmod_private_file(socket_path)
                self._verify_socket_path(socket_path, owner_home)
            except Exception:
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
                raise

            handle = OwnerWorkerHandle(
                owner_key=owner_key,
                owner_home=owner_home,
                socket_path=socket_path,
                process=process,
                pid=int(health["pid"]),
                last_health=health,
            )
            self._handles[owner_key] = handle
            return handle

    def acquire_use(self, handle: OwnerWorkerHandle) -> OwnerWorkerLease:
        """Mark a worker as actively serving an HTTP stream or WS bridge."""
        with self._lock:
            current = self._handles.get(handle.owner_key)
            if current is not handle:
                raise RuntimeError("owner worker handle is no longer active")
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
    def _chmod_private_file(path: Path) -> None:
        if os.name == "nt":
            return
        try:
            path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass

    @staticmethod
    def _verify_socket_path(socket_path: Path, owner_home: Path) -> None:
        runtime_dir = (owner_home / "runtime").resolve()
        resolved = socket_path.resolve()
        try:
            resolved.relative_to(runtime_dir)
        except ValueError as exc:
            raise RuntimeError("owner worker socket escaped owner runtime directory") from exc
        if os.name != "nt":
            mode = resolved.stat().st_mode
            if mode & (stat.S_IRWXG | stat.S_IRWXO):
                raise RuntimeError("owner worker socket is group/world accessible")

    def _terminate_handle(self, owner_key: str, handle: OwnerWorkerHandle) -> None:
        self._handles.pop(owner_key, None)
        if handle.process.poll() is None:
            handle.process.terminate()
            try:
                handle.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                handle.process.kill()
                try:
                    handle.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass

    def _reap_exited(self) -> None:
        for owner_key, handle in list(self._handles.items()):
            if handle.process.poll() is not None:
                self._handles.pop(owner_key, None)

    def _stop_idle(self, *, now: float) -> None:
        for owner_key, handle in list(self._handles.items()):
            if handle.process.poll() is not None:
                self._handles.pop(owner_key, None)
                continue
            if handle.active_uses > 0:
                continue
            if now - handle.last_used_at >= self.idle_timeout:
                self._terminate_handle(owner_key, handle)

    def _evict_oldest_idle(self, *, now: float) -> None:
        live = [
            (owner_key, handle)
            for owner_key, handle in self._handles.items()
            if handle.process.poll() is None and handle.active_uses <= 0
        ]
        if not live:
            return
        owner_key, handle = min(live, key=lambda item: item[1].last_used_at)
        if now - handle.last_used_at >= self.idle_timeout:
            self._terminate_handle(owner_key, handle)

    def socket_path_for(self, owner: Any) -> Path:
        return self._owner_home(owner) / "runtime" / "worker.sock"

    def _wait_until_healthy(
        self,
        *,
        process: subprocess.Popen[Any],
        socket_path: Path,
        owner_key: str,
        owner_home: Path,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + self.startup_timeout
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"owner worker exited during startup with code {process.returncode}")
            if socket_path.exists():
                try:
                    return self.client_cls(socket_path, control_home=self.control_home).verify_health(
                        owner_key=owner_key,
                        owner_home=owner_home,
                    )
                except OwnerWorkerHealthError as exc:
                    last_error = exc
            time.sleep(self.poll_interval)
        if last_error is not None:
            raise RuntimeError(f"owner worker failed health verification: {last_error}") from last_error
        raise TimeoutError("timed out waiting for owner worker socket")

    def _argv_for(self, owner: Any, socket_path: Path) -> list[str]:
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

    def _env_for(self, owner: Any) -> dict[str, str]:
        keep = _OWNER_WORKER_ENV_ALLOW | _OWNER_WORKER_ENV_EXPLICIT_KEEP | _configured_env_allowlist()
        env = {key: value for key, value in os.environ.items() if key in keep}
        env.update(
            owner_worker_env_for(
                owner_key=self._owner_key(owner),
                owner_home=self._owner_home(owner),
                tenant_id=str(self._get_attr(owner, "tenant_id", "") or ""),
                owner_user_id=str(self._get_attr(owner, "owner_user_id", "") or ""),
                auth_provider=str(self._get_attr(owner, "auth_provider", "") or ""),
                control_home=self.control_home,
            )
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
