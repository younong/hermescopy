from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli.dashboard_auth.authority import OwnerWorkerAuthorityLease, WorkerLeaseState
from hermes_cli.owner_worker.cgroup_v2 import (
    CgroupAdmissionRejected,
    CgroupCleanupFailed,
    CgroupV2Manager,
    CgroupV2Unavailable,
)
from hermes_cli.owner_worker.executor_identity import ExecutorIdentity
from hermes_cli.owner_worker.tool_executor_sandbox import (
    SandboxResourceLimits,
    SandboxResourcePolicy,
)


class FakeCgroupV2IO:
    """Kernel-semantics fake; it does not rely on privileged Linux cgroupfs."""

    def __init__(
        self,
        root: Path,
        *,
        controllers=("cpu", "memory", "pids"),
        unified=True,
        cgroup_kill=True,
        cgroup_freeze=True,
    ):
        self.root = root
        self.unified = unified
        self.cgroup_kill = cgroup_kill
        self.cgroup_freeze = cgroup_freeze
        self.nodes: dict[tuple[str, ...], dict[str, str]] = {
            (): self._node(tuple(controllers))
        }
        self.killed: list[int] = []
        self.ignore_membership_moves = False
        self.ignore_limit_writes: set[str] = set()
        self.kill_cgroup_succeeds = True
        self.freeze_succeeds = True
        self.process_kill_succeeds = True

    def _node(self, controllers=("cpu", "memory", "pids")):
        return {
            "cgroup.controllers": " ".join(controllers),
            "cgroup.subtree_control": "",
            "cgroup.procs": "",
            "cgroup.events": "populated 0\nfrozen 0\n",
            "cpu.max": "max 100000",
            "memory.max": "max",
            "memory.swap.max": "max",
            "pids.max": "max",
            "memory.oom.group": "0",
            "cpu.stat": "usage_usec 0\nnr_periods 0\n",
            "memory.events": "low 0\nhigh 0\nmax 0\noom 0\noom_kill 0\n",
            "pids.events": "max 0\n",
            "cgroup.freeze": "0",
            "cgroup.kill": "",
        }

    def validate_unified_v2(self):
        if not self.unified:
            raise CgroupV2Unavailable("not unified")

    def mkdir(self, relative):
        assert relative not in self.nodes
        parent = relative[:-1]
        controllers = tuple(self.nodes[parent]["cgroup.controllers"].split())
        self.nodes[relative] = self._node(controllers)

    def list_dirs(self, relative):
        depth = len(relative) + 1
        return tuple(sorted(path[-1] for path in self.nodes if len(path) == depth and path[:-1] == relative))

    def read_text(self, relative, name):
        try:
            return self.nodes[relative][name]
        except KeyError as exc:
            raise CgroupV2Unavailable("missing fake control") from exc

    def write_text(self, relative, name, value):
        node = self.nodes[relative]
        if name in self.ignore_limit_writes:
            return
        if name == "cgroup.subtree_control":
            enabled = {part.removeprefix("+") for part in value.split()}
            available = set(node["cgroup.controllers"].split())
            node[name] = " ".join(sorted(enabled & available))
            return
        if name == "cgroup.kill":
            if not self.cgroup_kill or not self.kill_cgroup_succeeds:
                raise CgroupV2Unavailable("kill unavailable")
            for scope in self._descendants(relative):
                self._set_pids(scope, ())
            return
        if name == "cgroup.freeze":
            if not self.cgroup_freeze:
                raise CgroupV2Unavailable("freeze unavailable")
            if self.freeze_succeeds:
                node[name] = value
                self._refresh_events(relative, frozen=int(value))
            return
        node[name] = value

    def exists(self, relative, name):
        if name == "cgroup.kill":
            return self.cgroup_kill
        if name == "cgroup.freeze":
            return self.cgroup_freeze
        return name in self.nodes[relative]

    def move_process(self, relative, pid):
        if self.ignore_membership_moves:
            return
        for scope in tuple(self.nodes):
            pids = set(self._pids(scope))
            if pid in pids:
                pids.remove(pid)
                self._set_pids(scope, pids)
        pids = set(self._pids(relative))
        pids.add(pid)
        self._set_pids(relative, pids)

    def remove_dir(self, relative):
        if self.list_dirs(relative) or self._pids(relative):
            raise CgroupCleanupFailed("fake cgroup is not empty")
        del self.nodes[relative]

    def kill_process(self, pid):
        self.killed.append(pid)
        if not self.process_kill_succeeds:
            return
        for scope in tuple(self.nodes):
            pids = set(self._pids(scope))
            if pid in pids:
                pids.remove(pid)
                self._set_pids(scope, pids)

    def add_unmanaged_child(self, parent, name):
        self.nodes[parent + (name,)] = self._node()

    def _descendants(self, relative):
        return tuple(path for path in self.nodes if path[:len(relative)] == relative)

    def _pids(self, relative):
        return tuple(int(value) for value in self.nodes[relative]["cgroup.procs"].split())

    def _set_pids(self, relative, pids):
        self.nodes[relative]["cgroup.procs"] = "\n".join(str(pid) for pid in sorted(pids))
        for depth in range(len(relative) + 1):
            ancestor = relative[:depth]
            populated = any(
                self._pids(path)
                for path in self.nodes
                if path[:len(ancestor)] == ancestor
            )
            self._refresh_events(ancestor, populated=int(populated))

    def _refresh_events(self, relative, *, populated=None, frozen=None):
        values = dict(
            line.split() for line in self.nodes[relative]["cgroup.events"].splitlines()
        )
        if populated is not None:
            values["populated"] = str(populated)
        if frozen is not None:
            values["frozen"] = str(frozen)
        self.nodes[relative]["cgroup.events"] = (
            f"populated {values['populated']}\nfrozen {values['frozen']}\n"
        )


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def _policy(tmp_path, *, global_workers=2, global_executors=3, owner_executors=2, kill_required=False):
    return SandboxResourcePolicy(
        cgroup_root=tmp_path / "cgroup" / "hermes-dashboard.service",
        required_controllers=("cpu", "memory", "pids"),
        global_limits=SandboxResourceLimits(
            3000, 3 << 30, 512, global_executors, max_owner_workers=global_workers,
        ),
        owner_limits=SandboxResourceLimits(2000, 2 << 30, 256, owner_executors),
        executor_limits=SandboxResourceLimits(
            1000, 1 << 30, 64, 1, swap_bytes=0, file_descriptors=64,
            duration_seconds=120, output_bytes=200_000,
        ),
        cleanup_grace_seconds=1,
        cleanup_timeout_seconds=2,
        cgroup_kill_required=kill_required,
    )


def _manager(tmp_path, **policy_args):
    policy = _policy(tmp_path, **policy_args)
    io = FakeCgroupV2IO(policy.cgroup_root)
    clock = FakeClock()
    return CgroupV2Manager(policy, io=io, clock=clock, sleeper=clock.sleep), io


def _lease(owner="ok1_owner", worker="worker-a", generation=1):
    return OwnerWorkerAuthorityLease(owner, generation, worker, WorkerLeaseState.ACTIVE, 1, 0)


def _identity(owner="ok1_owner", worker="worker-a", generation=1, executor="executor-a"):
    return ExecutorIdentity.for_task(
        _lease(owner, worker, generation), workspace_prefix="default", task_id="task-a",
        session_id="session-a", executor_id=executor,
    )


def test_initializes_unified_empty_pool_with_exact_limits_and_controller_readback(tmp_path):
    manager, io = _manager(tmp_path)

    pool = ("pool-v1",)
    assert io.nodes[()]["cgroup.subtree_control"].split() == ["cpu", "memory", "pids"]
    assert io.nodes[pool]["cgroup.subtree_control"].split() == ["cpu", "memory", "pids"]
    assert manager.read_pool_events().populated is False
    assert io.nodes[pool]["cpu.max"] == "300000 100000"
    assert io.nodes[pool]["memory.max"] == str(3 << 30)
    assert io.nodes[pool]["memory.swap.max"] == "0"
    assert io.nodes[pool]["pids.max"] == "512"
    assert io.nodes[pool]["memory.oom.group"] == "1"


@pytest.mark.parametrize(
    "mutate, match",
    [
        (lambda io: setattr(io, "unified", False), "not unified"),
        (lambda io: io.nodes[()].update({"cgroup.controllers": "cpu memory"}), "controllers"),
        (lambda io: io._set_pids((), {41}), "no processes"),
        (lambda io: io.ignore_limit_writes.add("memory.max"), "numeric limit|readback"),
    ],
)
def test_fails_closed_when_v2_controller_topology_or_limit_proof_is_invalid(tmp_path, mutate, match):
    policy = _policy(tmp_path)
    io = FakeCgroupV2IO(policy.cgroup_root)
    mutate(io)

    with pytest.raises(CgroupV2Unavailable, match=match):
        CgroupV2Manager(policy, io=io)


def test_admits_generated_owner_worker_and_per_invocation_executor_leaves(tmp_path):
    manager, io = _manager(tmp_path)
    worker = manager.admit_worker(_lease(owner="../../raw-owner", worker="../raw-worker"))
    executor = manager.admit_executor(
        _identity(owner="../../raw-owner", worker="../raw-worker", executor="../raw-executor"),
        "../raw-invocation",
    )

    assert worker.path.parent == executor.path.parent
    assert worker.path.name.startswith("worker-") and len(worker.path.name) == 71
    assert executor.path.name.startswith("executor-") and len(executor.path.name) == 73
    assert worker.path.parent.name.startswith("owner-") and len(worker.path.parent.name) == 70
    assert "raw" not in str(worker.path) and ".." not in str(worker.path)
    assert io.nodes[worker._relative]["memory.oom.group"] == "1"
    assert executor.read_limits().memory_swap_max == 0


def test_enforces_exact_global_worker_and_global_owner_executor_admission(tmp_path):
    manager, _io = _manager(tmp_path, global_workers=1, global_executors=2, owner_executors=1)
    manager.admit_worker(_lease("owner-a", "worker-a"))
    with pytest.raises(CgroupAdmissionRejected, match="worker"):
        manager.admit_worker(_lease("owner-b", "worker-b"))

    manager.admit_executor(_identity("owner-a", executor="executor-a"), "inv-a")
    with pytest.raises(CgroupAdmissionRejected, match="owner executor"):
        manager.admit_executor(_identity("owner-a", executor="executor-b"), "inv-b")
    manager.admit_executor(_identity("owner-b", executor="executor-c"), "inv-c")
    with pytest.raises(CgroupAdmissionRejected, match="global executor"):
        manager.admit_executor(_identity("owner-c", executor="executor-d"), "inv-d")


def test_membership_attach_is_verified_across_all_managed_leaves(tmp_path):
    manager, io = _manager(tmp_path)
    first = manager.admit_executor(_identity(executor="executor-a"), "inv-a")
    second = manager.admit_executor(_identity(executor="executor-b"), "inv-b")

    first.attach(101)
    assert first.verify_membership(101)
    assert not second.verify_membership(101)
    second.attach(101)
    assert second.verify_membership(101)
    assert not first.verify_membership(101)

    io.ignore_membership_moves = True
    with pytest.raises(CgroupAdmissionRejected, match="membership"):
        first.attach(202)


def test_reads_deterministic_resource_events(tmp_path):
    manager, io = _manager(tmp_path)
    scope = manager.admit_executor(_identity(), "inv-a")
    io.nodes[scope._relative]["cpu.stat"] = "usage_usec 17\nnr_periods 3\n"
    io.nodes[scope._relative]["memory.events"] = "low 1\nhigh 2\nmax 3\noom 4\noom_kill 5\n"
    io.nodes[scope._relative]["pids.events"] = "max 6\n"

    events = scope.read_events()

    assert dict(events.cpu) == {"usage_usec": 17, "nr_periods": 3}
    assert dict(events.memory)["oom_kill"] == 5
    assert dict(events.pids) == {"max": 6}
    with pytest.raises(TypeError):
        events.cpu["usage_usec"] = 99


def test_cgroup_wide_cleanup_prefers_kill_waits_for_populated_zero_and_releases(tmp_path):
    manager, io = _manager(tmp_path)
    scope = manager.admit_executor(_identity(), "inv-a")
    scope.attach(303)

    scope.cleanup()

    assert scope.released
    assert scope._relative not in io.nodes
    assert io.killed == []


def test_cleanup_uses_verified_freeze_and_recursive_sigkill_only_when_policy_allows(tmp_path):
    policy = _policy(tmp_path, kill_required=False)
    io = FakeCgroupV2IO(policy.cgroup_root, cgroup_kill=False, cgroup_freeze=True)
    clock = FakeClock()
    manager = CgroupV2Manager(policy, io=io, clock=clock, sleeper=clock.sleep)
    scope = manager.admit_executor(_identity(), "inv-a")
    scope.attach(404)

    scope.cleanup()

    assert io.killed == [404]
    assert scope.released


def test_cleanup_retains_reservation_and_scope_when_empty_proof_fails(tmp_path):
    policy = _policy(tmp_path, kill_required=False)
    io = FakeCgroupV2IO(policy.cgroup_root, cgroup_kill=False, cgroup_freeze=True)
    io.process_kill_succeeds = False
    clock = FakeClock()
    manager = CgroupV2Manager(policy, io=io, clock=clock, sleeper=clock.sleep)
    scope = manager.admit_executor(_identity(), "inv-a")
    scope.attach(505)

    with pytest.raises(CgroupCleanupFailed, match="populated 0"):
        scope.cleanup()

    assert not scope.released
    assert scope._relative in io.nodes
    with pytest.raises(CgroupAdmissionRejected, match="reserved"):
        manager.admit_executor(_identity(), "inv-a")


def test_cleanup_fails_closed_when_cgroup_kill_is_required(tmp_path):
    policy = _policy(tmp_path, kill_required=True)
    io = FakeCgroupV2IO(policy.cgroup_root, cgroup_kill=False, cgroup_freeze=True)
    clock = FakeClock()
    manager = CgroupV2Manager(policy, io=io, clock=clock, sleeper=clock.sleep)
    scope = manager.admit_executor(_identity(), "inv-a")
    scope.attach(606)

    with pytest.raises(CgroupCleanupFailed, match="required cgroup.kill"):
        scope.cleanup()

    assert io.killed == []
    assert not scope.released


def test_startup_cleans_managed_stale_scopes_before_admission(tmp_path):
    policy = _policy(tmp_path)
    io = FakeCgroupV2IO(policy.cgroup_root)
    first = CgroupV2Manager(policy, io=io)
    stale = first.admit_executor(_identity(executor="stale"), "stale")
    stale.attach(707)

    replacement = CgroupV2Manager(policy, io=io)

    assert stale._relative not in io.nodes
    assert replacement.startup_cleanup_count == 2
    assert replacement.admit_executor(
        _identity(executor="replacement"), "replacement"
    )


def test_startup_rejects_unmanaged_stale_scope_names(tmp_path):
    policy = _policy(tmp_path)
    io = FakeCgroupV2IO(policy.cgroup_root)
    CgroupV2Manager(policy, io=io)
    io.add_unmanaged_child(("pool-v1",), "unmanaged")

    with pytest.raises(CgroupV2Unavailable, match="unmanaged"):
        CgroupV2Manager(policy, io=io)


def test_stale_empty_cleanup_preserves_active_reservations(tmp_path):
    manager, io = _manager(tmp_path)
    active = manager.admit_executor(_identity(executor="active"), "active")
    stale = manager.admit_executor(_identity(executor="stale"), "stale")
    manager._active.pop(stale._relative)

    assert manager.cleanup_stale_empty_scopes() == 1
    assert active._relative in io.nodes
    assert stale._relative not in io.nodes
