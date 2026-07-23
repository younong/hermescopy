"""Fail-closed cgroup v2 lifecycle management for authenticated workers.

The configured root is an operator-delegated, empty cgroup.  This module owns a
single generated subtree below it and never derives a path component from raw
owner, worker, executor, or invocation strings. Supervisors hold the exact
leases exposed here and release them during worker or invocation teardown.
"""
from __future__ import annotations

import ctypes
import hashlib
import os
import re
import signal
import stat
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Mapping, Protocol, Sequence

from hermes_cli.dashboard_auth.authority import OwnerWorkerAuthorityLease, WorkerLeaseState
from hermes_cli.owner_worker.executor_identity import ExecutorIdentity
from hermes_cli.owner_worker.tool_executor_sandbox import (
    SandboxResourceLimits,
    SandboxResourcePolicy,
)


class CgroupV2Unavailable(RuntimeError):
    """The configured hierarchy cannot prove the required cgroup v2 contract."""


class CgroupAdmissionRejected(RuntimeError):
    """A resource scope could not be admitted without exceeding policy."""


class CgroupCleanupFailed(RuntimeError):
    """A scope was retained because empty-state cleanup could not be proved."""


_REQUIRED_CONTROLLERS = ("cpu", "memory", "pids")
_POOL_NAME = "pool-v1"
_OWNER_RE = re.compile(r"owner-[0-9a-f]{64}\Z")
_WORKER_RE = re.compile(r"worker-[0-9a-f]{64}\Z")
_EXECUTOR_RE = re.compile(r"executor-[0-9a-f]{64}\Z")
_COMPONENT_RE = re.compile(r"[a-z]+(?:-[a-z0-9]+)*\Z")
_CONTROL_FILE_RE = re.compile(r"[a-z]+(?:\.[a-z]+)*\Z")
_CPU_PERIOD_US = 100_000
_CGROUP2_SUPER_MAGIC = "63677270"


class CgroupV2IO(Protocol):
    """Small secure-I/O boundary; tests can emulate kernel files in memory."""

    root: Path

    def validate_unified_v2(self) -> None: ...
    def mkdir(self, relative: tuple[str, ...]) -> None: ...
    def list_dirs(self, relative: tuple[str, ...]) -> tuple[str, ...]: ...
    def read_text(self, relative: tuple[str, ...], name: str) -> str: ...
    def write_text(self, relative: tuple[str, ...], name: str, value: str) -> None: ...
    def exists(self, relative: tuple[str, ...], name: str) -> bool: ...
    def move_process(self, relative: tuple[str, ...], pid: int) -> None: ...
    def remove_dir(self, relative: tuple[str, ...]) -> None: ...
    def kill_process(self, pid: int) -> None: ...


class DirectoryFdCgroupV2IO:
    """Linux cgroupfs adapter using no-follow, directory-relative operations."""

    def __init__(self, root: str | Path, *, mountinfo_path: str | Path = "/proc/self/mountinfo"):
        self.root = Path(root)
        self._mountinfo_path = Path(mountinfo_path)
        flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            self._root_fd = os.open(self.root, flags)
        except OSError as exc:
            raise CgroupV2Unavailable("cgroup root cannot be opened safely") from exc
        try:
            root_status = os.fstat(self._root_fd)
            if not stat.S_ISDIR(root_status.st_mode):
                raise CgroupV2Unavailable("cgroup root is not a directory")
        except Exception:
            os.close(self._root_fd)
            raise

    def close(self) -> None:
        fd = getattr(self, "_root_fd", -1)
        if fd >= 0:
            os.close(fd)
            self._root_fd = -1

    def validate_unified_v2(self) -> None:
        if sys.platform != "linux":
            raise CgroupV2Unavailable("cgroup v2 resource enforcement requires Linux")
        try:
            mountinfo = self._mountinfo_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise CgroupV2Unavailable("cgroup v2 mount information is unavailable") from exc
        root = str(self.root.resolve())
        candidates: list[str] = []
        for line in mountinfo.splitlines():
            fields = line.split()
            try:
                separator = fields.index("-")
            except ValueError:
                continue
            if separator + 1 >= len(fields) or fields[separator + 1] != "cgroup2" or len(fields) < 5:
                continue
            mountpoint = fields[4].replace("\\040", " ")
            if root == mountpoint or root.startswith(mountpoint.rstrip("/") + "/"):
                candidates.append(mountpoint)
        if not candidates:
            raise CgroupV2Unavailable("cgroup root is not on a unified cgroup v2 mount")
        class _StatFs(ctypes.Structure):
            _fields_ = [("f_type", ctypes.c_long), ("_rest", ctypes.c_byte * 248)]

        result = _StatFs()
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.fstatfs(self._root_fd, ctypes.byref(result)) != 0:
            raise CgroupV2Unavailable("cgroup filesystem type cannot be verified")
        if result.f_type != int(_CGROUP2_SUPER_MAGIC, 16):
            raise CgroupV2Unavailable("cgroup root filesystem is not cgroup v2")

    def mkdir(self, relative: tuple[str, ...]) -> None:
        parent, name = relative[:-1], relative[-1]
        fd = self._open_dir(parent)
        try:
            os.mkdir(name, mode=0o755, dir_fd=fd)
        finally:
            os.close(fd)

    def list_dirs(self, relative: tuple[str, ...]) -> tuple[str, ...]:
        fd = self._open_dir(relative)
        try:
            names = []
            for name in os.listdir(fd):
                try:
                    status = os.stat(name, dir_fd=fd, follow_symlinks=False)
                except OSError as exc:
                    raise CgroupV2Unavailable("cgroup hierarchy changed during inspection") from exc
                if stat.S_ISLNK(status.st_mode):
                    raise CgroupV2Unavailable("cgroup hierarchy contains a symbolic link")
                if stat.S_ISDIR(status.st_mode):
                    names.append(name)
            return tuple(sorted(names))
        finally:
            os.close(fd)

    def read_text(self, relative: tuple[str, ...], name: str) -> str:
        fd = self._open_control(relative, name, os.O_RDONLY)
        try:
            chunks = []
            while True:
                chunk = os.read(fd, 64 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
                if sum(map(len, chunks)) > 1 << 20:
                    raise CgroupV2Unavailable("cgroup control file is unexpectedly large")
            return b"".join(chunks).decode("ascii")
        except (OSError, UnicodeDecodeError) as exc:
            raise CgroupV2Unavailable("cgroup control file cannot be read") from exc
        finally:
            os.close(fd)

    def write_text(self, relative: tuple[str, ...], name: str, value: str) -> None:
        payload = value.encode("ascii")
        fd = self._open_control(relative, name, os.O_WRONLY)
        try:
            written = os.write(fd, payload)
            if written != len(payload):
                raise CgroupV2Unavailable("cgroup control write was incomplete")
        except OSError as exc:
            raise CgroupV2Unavailable("cgroup control file cannot be written") from exc
        finally:
            os.close(fd)

    def exists(self, relative: tuple[str, ...], name: str) -> bool:
        directory_fd = self._open_dir(relative)
        try:
            try:
                status = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                return False
            if stat.S_ISLNK(status.st_mode) or not stat.S_ISREG(status.st_mode):
                raise CgroupV2Unavailable("cgroup control file is unsafe")
            return True
        finally:
            os.close(directory_fd)

    def move_process(self, relative: tuple[str, ...], pid: int) -> None:
        self.write_text(relative, "cgroup.procs", str(pid))

    def remove_dir(self, relative: tuple[str, ...]) -> None:
        parent_fd = self._open_dir(relative[:-1])
        try:
            os.rmdir(relative[-1], dir_fd=parent_fd)
        except OSError as exc:
            raise CgroupCleanupFailed("cgroup directory could not be removed") from exc
        finally:
            os.close(parent_fd)

    def kill_process(self, pid: int) -> None:
        try:
            os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
        except ProcessLookupError:
            return
        except OSError as exc:
            raise CgroupCleanupFailed("cgroup process could not be killed") from exc

    def _open_dir(self, relative: tuple[str, ...]) -> int:
        fd = os.dup(self._root_fd)
        flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            for component in relative:
                _validate_component(component)
                next_fd = os.open(component, flags, dir_fd=fd)
                os.close(fd)
                fd = next_fd
            return fd
        except OSError as exc:
            os.close(fd)
            raise CgroupV2Unavailable("cgroup directory cannot be opened safely") from exc
        except Exception:
            os.close(fd)
            raise

    def _open_control(self, relative: tuple[str, ...], name: str, access: int) -> int:
        _validate_control_file(name)
        directory_fd = self._open_dir(relative)
        flags = access | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(name, flags, dir_fd=directory_fd)
            status = os.fstat(fd)
            if not stat.S_ISREG(status.st_mode):
                os.close(fd)
                raise CgroupV2Unavailable("cgroup control file is not regular")
            return fd
        except OSError as exc:
            raise CgroupV2Unavailable("cgroup control file cannot be opened safely") from exc
        finally:
            os.close(directory_fd)


@dataclass(frozen=True)
class CgroupLimitState:
    cpu_max: str
    memory_max: int
    memory_swap_max: int
    pids_max: int
    memory_oom_group: bool


@dataclass(frozen=True)
class CgroupResourceEvents:
    populated: bool
    frozen: bool
    cpu: Mapping[str, int]
    memory: Mapping[str, int]
    pids: Mapping[str, int]


@dataclass
class CgroupScopeLease:
    """Reservation for one process-bearing worker or executor leaf."""

    _manager: "CgroupV2Manager" = field(repr=False)
    _relative: tuple[str, ...] = field(repr=False)
    kind: str
    _released: bool = field(default=False, init=False, repr=False)

    @property
    def path(self) -> Path:
        return self._manager.policy.cgroup_root.joinpath(*self._relative)

    @property
    def released(self) -> bool:
        return self._released

    def attach(self, pid: int) -> None:
        self._require_active()
        self._manager._attach(self._relative, pid)

    def verify_membership(self, pid: int) -> bool:
        self._require_active()
        return self._manager._verify_membership(self._relative, pid)

    def read_limits(self) -> CgroupLimitState:
        self._require_active()
        return self._manager._read_limits(self._relative)

    def read_events(self) -> CgroupResourceEvents:
        self._require_active()
        return self._manager._read_events(self._relative)

    def cleanup(self) -> None:
        if self._released:
            return
        self._manager._release(self)

    def _require_active(self) -> None:
        if self._released:
            raise CgroupCleanupFailed("cgroup reservation is already released")

    def __enter__(self) -> "CgroupScopeLease":
        self._require_active()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.cleanup()


class CgroupV2Manager:
    """Own the authenticated cgroup pool and exact filesystem-backed admission."""

    def __init__(
        self,
        policy: SandboxResourcePolicy,
        *,
        io: CgroupV2IO | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if not isinstance(policy, SandboxResourcePolicy):
            raise CgroupV2Unavailable("sandbox resource policy is required")
        if policy.required_controllers != _REQUIRED_CONTROLLERS:
            raise CgroupV2Unavailable("exact cpu, memory, and pids controllers are required")
        self.policy = policy
        self._io = io or DirectoryFdCgroupV2IO(policy.cgroup_root)
        if Path(self._io.root) != policy.cgroup_root:
            raise CgroupV2Unavailable("cgroup I/O root does not match resource policy")
        self._clock = clock
        self._sleep = sleeper
        self._lock = threading.RLock()
        self._active: dict[tuple[str, ...], CgroupScopeLease] = {}
        self._pool = (_POOL_NAME,)
        self._startup_cleanup_count = 0
        self._initialize_pool()

    @property
    def startup_cleanup_count(self) -> int:
        """Return how many stale managed cgroups startup removed."""
        return self._startup_cleanup_count

    def close(self) -> None:
        close = getattr(self._io, "close", None)
        if callable(close):
            close()

    def admit_worker(self, lease: OwnerWorkerAuthorityLease) -> CgroupScopeLease:
        if not isinstance(lease, OwnerWorkerAuthorityLease) or lease.state not in {
            WorkerLeaseState.STARTING, WorkerLeaseState.ACTIVE,
        }:
            raise CgroupAdmissionRejected("active owner worker authority is required")
        owner_digest = _owner_digest(lease.owner_key)
        worker_digest = _digest_fields(
            "worker-v1", owner_digest, lease.worker_id, lease.worker_generation,
            lease.lease_version, lease.recovery_generation,
        )
        owner = self._pool + (f"owner-{owner_digest}",)
        worker = owner + (f"worker-{worker_digest}",)
        with self._lock:
            self._inspect_hierarchy()
            if self._count_workers() >= self.policy.global_limits.max_owner_workers:
                raise CgroupAdmissionRejected("global owner worker admission limit reached")
            self._ensure_owner(owner)
            return self._reserve_leaf(worker, "worker", self.policy.owner_limits)

    def admit_executor(self, identity: ExecutorIdentity, invocation_id: str) -> CgroupScopeLease:
        if not isinstance(identity, ExecutorIdentity):
            raise CgroupAdmissionRejected("authenticated executor identity is required")
        invocation = str(invocation_id or "").strip()
        if not invocation or "\x00" in invocation:
            raise CgroupAdmissionRejected("executor invocation identity is required")
        owner_digest = _owner_digest(identity.owner_key)
        executor_digest = _digest_fields(
            "executor-v1", owner_digest, identity.worker_id, identity.worker_generation,
            identity.lease_version, identity.recovery_generation, identity.task_id,
            identity.executor_id, identity.executor_generation, invocation,
        )
        owner = self._pool + (f"owner-{owner_digest}",)
        executor = owner + (f"executor-{executor_digest}",)
        with self._lock:
            self._inspect_hierarchy()
            if self._count_executors() >= self.policy.global_limits.max_concurrent_executors:
                raise CgroupAdmissionRejected("global executor admission limit reached")
            if self._count_owner_executors(owner) >= self.policy.owner_limits.max_concurrent_executors:
                raise CgroupAdmissionRejected("owner executor admission limit reached")
            self._ensure_owner(owner)
            return self._reserve_leaf(executor, "executor", self.policy.executor_limits)

    def read_pool_events(self) -> CgroupResourceEvents:
        with self._lock:
            return self._read_events(self._pool)

    def cleanup_owner(self, identity: ExecutorIdentity | OwnerWorkerAuthorityLease) -> None:
        owner_key = identity.owner_key if isinstance(identity, (ExecutorIdentity, OwnerWorkerAuthorityLease)) else None
        if not owner_key:
            raise CgroupCleanupFailed("authenticated owner identity is required")
        owner = self._pool + (f"owner-{_owner_digest(owner_key)}",)
        with self._lock:
            self._cleanup_tree(owner)
            for relative, lease in tuple(self._active.items()):
                if relative[:len(owner)] == owner:
                    lease._released = True
                    self._active.pop(relative, None)

    def cleanup_stale_empty_scopes(self) -> int:
        """Remove unreserved empty leaves and then empty owner aggregates."""
        removed = 0
        with self._lock:
            self._inspect_hierarchy()
            for owner_name in self._io.list_dirs(self._pool):
                owner = self._pool + (owner_name,)
                for leaf_name in self._io.list_dirs(owner):
                    leaf = owner + (leaf_name,)
                    if leaf in self._active:
                        continue
                    if self._is_unpopulated(leaf) and not self._io.list_dirs(leaf):
                        self._io.remove_dir(leaf)
                        removed += 1
                if not self._io.list_dirs(owner) and self._is_unpopulated(owner):
                    self._io.remove_dir(owner)
                    removed += 1
        return removed

    def cleanup_stale_scopes(self) -> int:
        """Kill and remove unreserved scopes left by a prior manager process."""
        removed = 0
        with self._lock:
            self._inspect_hierarchy()
            for owner_name in self._io.list_dirs(self._pool):
                owner = self._pool + (owner_name,)
                for leaf_name in self._io.list_dirs(owner):
                    leaf = owner + (leaf_name,)
                    if leaf in self._active:
                        continue
                    self._cleanup_tree(leaf)
                    removed += 1
                if not self._io.list_dirs(owner):
                    if not self._is_unpopulated(owner):
                        raise CgroupCleanupFailed("empty owner aggregate is unexpectedly populated")
                    self._io.remove_dir(owner)
                    removed += 1
        return removed

    def _initialize_pool(self) -> None:
        with self._lock:
            try:
                self._io.validate_unified_v2()
                controllers = tuple(sorted(self._read_words((), "cgroup.controllers")))
                if not set(_REQUIRED_CONTROLLERS).issubset(controllers):
                    raise CgroupV2Unavailable("required cgroup v2 controllers are unavailable")
                if self._read_pids(()) != ():
                    raise CgroupV2Unavailable("delegated cgroup root must contain no processes")
                self._enable_controllers(())
                roots = self._io.list_dirs(())
                if _POOL_NAME not in roots:
                    self._io.mkdir(self._pool)
                self._require_only_managed((), self._io.list_dirs(()), allow_unmanaged=False)
                if self._read_pids(self._pool) != ():
                    raise CgroupV2Unavailable("authenticated global pool must contain no processes")
                self._install_limits(self._pool, self.policy.global_limits)
                self._enable_controllers(self._pool)
                # The service unit's control-group kill normally empties this
                # subtree, but a manager restart must deterministically recover
                # any managed scopes that survived an abrupt predecessor exit.
                # Unknown names still fail closed inside cleanup_stale_scopes.
                self._startup_cleanup_count = self.cleanup_stale_scopes()
                self._inspect_hierarchy()
            except (CgroupV2Unavailable, CgroupCleanupFailed):
                raise
            except OSError as exc:
                raise CgroupV2Unavailable("cgroup v2 hierarchy initialization failed") from exc

    def _ensure_owner(self, owner: tuple[str, ...]) -> None:
        owner_name = owner[-1]
        if not _OWNER_RE.fullmatch(owner_name):
            raise CgroupV2Unavailable("generated owner cgroup name is invalid")
        created = owner_name not in self._io.list_dirs(self._pool)
        if created:
            self._io.mkdir(owner)
        try:
            if self._read_pids(owner) != ():
                raise CgroupV2Unavailable("owner aggregate cgroup contains a process")
            # Reinstall and read back policy on a pre-existing aggregate. A
            # manager restart must not trust controls left by the old process.
            self._install_limits(owner, self.policy.owner_limits)
            self._enable_controllers(owner)
        except Exception:
            # Never remove an incompletely configured scope without proving
            # that it is empty; its directory remains an admission reservation.
            if created and self._is_unpopulated(owner) and not self._io.list_dirs(owner):
                self._io.remove_dir(owner)
            raise

    def _reserve_leaf(
        self, relative: tuple[str, ...], kind: str, limits: SandboxResourceLimits,
    ) -> CgroupScopeLease:
        if relative[-1] in self._io.list_dirs(relative[:-1]) or relative in self._active:
            raise CgroupAdmissionRejected("cgroup scope is already reserved")
        self._io.mkdir(relative)
        try:
            self._install_limits(relative, limits)
            if self._read_pids(relative) != ():
                raise CgroupV2Unavailable("new cgroup scope is unexpectedly populated")
        except Exception:
            if self._is_unpopulated(relative) and not self._io.list_dirs(relative):
                self._io.remove_dir(relative)
            raise
        lease = CgroupScopeLease(self, relative, kind)
        self._active[relative] = lease
        return lease

    def _install_limits(self, relative: tuple[str, ...], limits: SandboxResourceLimits) -> None:
        values = self._limit_values(limits)
        for name, value in values.items():
            self._io.write_text(relative, name, value)
        self._verify_limits(relative, limits)

    def _verify_limits(self, relative: tuple[str, ...], limits: SandboxResourceLimits) -> None:
        values = self._limit_values(limits)
        expected = CgroupLimitState(
            cpu_max=values["cpu.max"],
            memory_max=limits.memory_bytes,
            memory_swap_max=int(values["memory.swap.max"]),
            pids_max=limits.pids,
            memory_oom_group=True,
        )
        if self._read_limits(relative) != expected:
            raise CgroupV2Unavailable("cgroup resource limit readback did not match policy")

    @staticmethod
    def _limit_values(limits: SandboxResourceLimits) -> dict[str, str]:
        return {
            "cpu.max": f"{limits.cpu_millis * (_CPU_PERIOD_US // 1000)} {_CPU_PERIOD_US}",
            "memory.max": str(limits.memory_bytes),
            "memory.swap.max": str(limits.swap_bytes if limits.swap_bytes is not None else 0),
            "pids.max": str(limits.pids),
            "memory.oom.group": "1",
        }

    def _read_limits(self, relative: tuple[str, ...]) -> CgroupLimitState:
        cpu_max = " ".join(self._io.read_text(relative, "cpu.max").split())
        cpu_parts = cpu_max.split()
        if len(cpu_parts) != 2 or any(not part.isdigit() or int(part) <= 0 for part in cpu_parts):
            raise CgroupV2Unavailable("cgroup cpu.max is invalid")
        return CgroupLimitState(
            cpu_max=cpu_max,
            memory_max=_parse_limit(self._io.read_text(relative, "memory.max")),
            memory_swap_max=_parse_limit(self._io.read_text(relative, "memory.swap.max"), allow_zero=True),
            pids_max=_parse_limit(self._io.read_text(relative, "pids.max")),
            memory_oom_group=_parse_boolean(self._io.read_text(relative, "memory.oom.group")),
        )

    def _enable_controllers(self, relative: tuple[str, ...]) -> None:
        if self._read_pids(relative):
            raise CgroupV2Unavailable("cannot enable controllers in a populated cgroup")
        available = self._read_words(relative, "cgroup.controllers")
        if not set(_REQUIRED_CONTROLLERS).issubset(available):
            raise CgroupV2Unavailable("required controllers were not delegated")
        self._io.write_text(
            relative, "cgroup.subtree_control",
            " ".join(f"+{controller}" for controller in _REQUIRED_CONTROLLERS),
        )
        enabled = self._read_words(relative, "cgroup.subtree_control")
        if not set(_REQUIRED_CONTROLLERS).issubset(enabled):
            raise CgroupV2Unavailable("required controllers could not be enabled")

    def _attach(self, relative: tuple[str, ...], pid: int) -> None:
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            raise CgroupAdmissionRejected("cgroup process id is invalid")
        with self._lock:
            self._io.move_process(relative, pid)
            if not self._verify_membership(relative, pid):
                raise CgroupAdmissionRejected("cgroup process membership could not be verified")

    def _verify_membership(self, relative: tuple[str, ...], pid: int) -> bool:
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            return False
        with self._lock:
            matches = [scope for scope in self._all_scopes(self._pool) if pid in self._read_pids(scope)]
            return matches == [relative]

    def _read_events(self, relative: tuple[str, ...]) -> CgroupResourceEvents:
        cgroup = _parse_event_file(self._io.read_text(relative, "cgroup.events"))
        if set(cgroup) != {"populated", "frozen"}:
            raise CgroupV2Unavailable("cgroup.events has unexpected fields")
        return CgroupResourceEvents(
            populated=bool(cgroup["populated"]),
            frozen=bool(cgroup["frozen"]),
            cpu=MappingProxyType(_parse_event_file(self._io.read_text(relative, "cpu.stat"))),
            memory=MappingProxyType(_parse_event_file(self._io.read_text(relative, "memory.events"))),
            pids=MappingProxyType(_parse_event_file(self._io.read_text(relative, "pids.events"))),
        )

    def _release(self, lease: CgroupScopeLease) -> None:
        with self._lock:
            current = self._active.get(lease._relative)
            if current is not lease:
                raise CgroupCleanupFailed("cgroup reservation ownership could not be proved")
            self._cleanup_tree(lease._relative)
            self._active.pop(lease._relative, None)
            lease._released = True

    def _cleanup_tree(self, relative: tuple[str, ...]) -> None:
        deadline = self._clock() + self.policy.cleanup_timeout_seconds
        grace_deadline = min(deadline, self._clock() + self.policy.cleanup_grace_seconds)
        if not self._wait_unpopulated(relative, grace_deadline):
            used_kill = False
            if self._io.exists(relative, "cgroup.kill"):
                try:
                    self._io.write_text(relative, "cgroup.kill", "1")
                    used_kill = True
                except (CgroupV2Unavailable, OSError):
                    if self.policy.cgroup_kill_required:
                        raise CgroupCleanupFailed("required cgroup.kill failed")
            elif self.policy.cgroup_kill_required:
                raise CgroupCleanupFailed("required cgroup.kill is unavailable")
            if not used_kill:
                self._freeze_and_kill(relative)
            if not self._wait_unpopulated(relative, deadline):
                raise CgroupCleanupFailed("cgroup did not reach populated 0 before timeout")
        if not self._is_unpopulated(relative):
            raise CgroupCleanupFailed("cgroup empty-state proof was lost")
        for scope in reversed(self._all_scopes(relative)):
            if not self._is_unpopulated(scope) or self._io.list_dirs(scope):
                raise CgroupCleanupFailed("cgroup recursive empty-state proof failed")
            self._io.remove_dir(scope)

    def _freeze_and_kill(self, relative: tuple[str, ...]) -> None:
        if self.policy.cgroup_kill_required:
            raise CgroupCleanupFailed("per-process cleanup fallback is forbidden")
        if not self._io.exists(relative, "cgroup.freeze"):
            raise CgroupCleanupFailed("verified cgroup freeze is unavailable")
        self._io.write_text(relative, "cgroup.freeze", "1")
        if self._io.read_text(relative, "cgroup.freeze").strip() != "1":
            raise CgroupCleanupFailed("cgroup freeze could not be verified")
        pids = sorted({pid for scope in self._all_scopes(relative) for pid in self._read_pids(scope)})
        for pid in pids:
            self._io.kill_process(pid)

    def _wait_unpopulated(self, relative: tuple[str, ...], deadline: float) -> bool:
        while True:
            if self._is_unpopulated(relative):
                return True
            if self._clock() >= deadline:
                return False
            self._sleep(min(0.02, max(0.0, deadline - self._clock())))

    def _is_unpopulated(self, relative: tuple[str, ...]) -> bool:
        events = _parse_event_file(self._io.read_text(relative, "cgroup.events"))
        return events.get("populated") == 0

    def _inspect_hierarchy(self) -> None:
        owners = self._io.list_dirs(self._pool)
        self._require_only_managed(self._pool, owners, allow_unmanaged=False)
        if self._read_pids(self._pool):
            raise CgroupV2Unavailable("authenticated global pool contains a process")
        for owner_name in owners:
            owner = self._pool + (owner_name,)
            if self._read_pids(owner):
                raise CgroupV2Unavailable("owner aggregate cgroup contains a process")
            leaves = self._io.list_dirs(owner)
            if any(not (_WORKER_RE.fullmatch(name) or _EXECUTOR_RE.fullmatch(name)) for name in leaves):
                raise CgroupV2Unavailable("authenticated owner scope contains an unmanaged cgroup")
            self._verify_limits(owner, self.policy.owner_limits)
            for leaf_name in leaves:
                leaf = owner + (leaf_name,)
                if self._io.list_dirs(leaf):
                    raise CgroupV2Unavailable("process-bearing cgroup leaf contains a child")
                self._verify_limits(
                    leaf,
                    self.policy.owner_limits if _WORKER_RE.fullmatch(leaf_name) else self.policy.executor_limits,
                )

    def _require_only_managed(
        self, relative: tuple[str, ...], names: Sequence[str], *, allow_unmanaged: bool,
    ) -> None:
        if allow_unmanaged:
            return
        if relative == () and any(name != _POOL_NAME for name in names):
            raise CgroupV2Unavailable("delegated cgroup root contains an unmanaged cgroup")
        if relative == self._pool and any(not _OWNER_RE.fullmatch(name) for name in names):
            raise CgroupV2Unavailable("authenticated pool contains an unmanaged cgroup")

    def _count_workers(self) -> int:
        return sum(
            1
            for owner_name in self._io.list_dirs(self._pool)
            for name in self._io.list_dirs(self._pool + (owner_name,))
            if _WORKER_RE.fullmatch(name)
        )

    def _count_executors(self) -> int:
        return sum(
            1
            for owner_name in self._io.list_dirs(self._pool)
            for name in self._io.list_dirs(self._pool + (owner_name,))
            if _EXECUTOR_RE.fullmatch(name)
        )

    def _count_owner_executors(self, owner: tuple[str, ...]) -> int:
        if owner[-1] not in self._io.list_dirs(self._pool):
            return 0
        return sum(1 for name in self._io.list_dirs(owner) if _EXECUTOR_RE.fullmatch(name))

    def _all_scopes(self, relative: tuple[str, ...]) -> list[tuple[str, ...]]:
        scopes: list[tuple[str, ...]] = []

        def visit(scope: tuple[str, ...]) -> None:
            scopes.append(scope)
            for name in self._io.list_dirs(scope):
                visit(scope + (name,))

        visit(relative)
        return scopes

    def _read_words(self, relative: tuple[str, ...], name: str) -> tuple[str, ...]:
        return tuple(word for word in self._io.read_text(relative, name).split() if word)

    def _read_pids(self, relative: tuple[str, ...]) -> tuple[int, ...]:
        text = self._io.read_text(relative, "cgroup.procs")
        pids = []
        for line in text.splitlines():
            value = line.strip()
            if not value:
                continue
            if not value.isdigit() or int(value) <= 0:
                raise CgroupV2Unavailable("cgroup.procs contains an invalid process id")
            pids.append(int(value))
        if len(pids) != len(set(pids)):
            raise CgroupV2Unavailable("cgroup.procs contains duplicate process ids")
        return tuple(sorted(pids))


def _owner_digest(owner_key: str) -> str:
    if not isinstance(owner_key, str) or not owner_key or "\x00" in owner_key:
        raise CgroupAdmissionRejected("authenticated owner identity is invalid")
    return hashlib.sha256(owner_key.encode("utf-8")).hexdigest()


def _digest_fields(*fields: object) -> str:
    material = "\x1f".join(str(field) for field in fields).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _validate_component(component: str) -> None:
    if not isinstance(component, str) or not _COMPONENT_RE.fullmatch(component):
        raise CgroupV2Unavailable("cgroup path component is invalid")


def _validate_control_file(name: str) -> None:
    if not isinstance(name, str) or not _CONTROL_FILE_RE.fullmatch(name):
        raise CgroupV2Unavailable("cgroup control file name is invalid")


def _parse_limit(value: str, *, allow_zero: bool = False) -> int:
    normalized = value.strip()
    if not normalized.isdigit():
        raise CgroupV2Unavailable("cgroup numeric limit is invalid")
    parsed = int(normalized)
    if parsed < 0 or (parsed == 0 and not allow_zero):
        raise CgroupV2Unavailable("cgroup numeric limit is invalid")
    return parsed


def _parse_boolean(value: str) -> bool:
    normalized = value.strip()
    if normalized not in {"0", "1"}:
        raise CgroupV2Unavailable("cgroup boolean control is invalid")
    return normalized == "1"


def _parse_event_file(value: str) -> dict[str, int]:
    events: dict[str, int] = {}
    for line in value.splitlines():
        parts = line.split()
        if len(parts) != 2 or not parts[0] or not parts[1].isdigit() or parts[0] in events:
            raise CgroupV2Unavailable("cgroup event file is invalid")
        events[parts[0]] = int(parts[1])
    if not events:
        raise CgroupV2Unavailable("cgroup event file is empty")
    return events
