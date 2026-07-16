from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli.dashboard_auth.authority import OwnerWorkerAuthorityLease, WorkerLeaseState
from hermes_cli.owner_worker.executor_identity import ExecutorIdentity
from hermes_cli.owner_worker.host_sandbox_attestation import attest_host_bubblewrap_process
from hermes_cli.owner_worker.tool_executor_sandbox import (
    SandboxLaunchBinding,
    SandboxMountPolicy,
    SandboxReadonlyMount,
    SandboxSecurityPolicy,
    SandboxVerificationInvalid,
)


def _inputs(tmp_path: Path):
    owner = tmp_path / "owners" / "owner-a"
    runtime = owner / "runtime" / "executors" / "executor-a" / "gen-1"
    workspace = owner / "workspaces" / "default"
    dependency = tmp_path / "runtime"
    for path in (runtime, workspace, dependency):
        path.mkdir(parents=True)
    lease = OwnerWorkerAuthorityLease("ok1_owner", 1, "worker-a", WorkerLeaseState.ACTIVE, 1, 0)
    identity = ExecutorIdentity.for_task(
        lease, workspace_prefix="default", task_id="task", session_id="session",
        executor_id="executor-a", executor_generation=1,
    )
    binding = SandboxLaunchBinding(identity, "sandbox-a", owner, runtime)
    mount_policy = SandboxMountPolicy(
        binding, (), workspace, None, owner.parent,
        readonly_mounts=(SandboxReadonlyMount(dependency, "/opt/hermes/runtime"),),
        python_executable="/opt/hermes/runtime/bin/python3",
    )
    security = SandboxSecurityPolicy(
        "executor-bwrap-v1", "host-bwrap-seccomp-v1", 65532, 65532,
        "executor-default-v1", "sha256:" + "a" * 64,
    )
    return mount_policy, security


def _status(*, uid=65532, gid=65532, nnp=1, seccomp=2, cap_eff="0", name="python3"):
    return "\n".join((
        f"Name:\t{name}",
        f"Uid:\t{uid}\t{uid}\t{uid}\t{uid}",
        f"Gid:\t{gid}\t{gid}\t{gid}\t{gid}",
        f"NoNewPrivs:\t{nnp}", f"Seccomp:\t{seccomp}",
        "CapInh:\t0000000000000000", "CapPrm:\t0000000000000000",
        f"CapEff:\t{cap_eff}", "CapBnd:\t0000000000000000", "CapAmb:\t0000000000000000",
    ))


def _mountinfo():
    return "\n".join((
        "1 0 0:1 / / ro,relatime - tmpfs tmpfs rw",
        "2 1 0:2 / /workspace rw,relatime - ext4 /dev/test rw",
        "3 1 0:3 / /executor rw,relatime - ext4 /dev/test rw",
        "4 3 0:4 / /executor/tmp rw,relatime - tmpfs tmpfs rw",
        "5 1 0:5 / /opt/hermes/runtime ro,relatime - ext4 /dev/test ro",
    ))


def _reader(status: str, mountinfo: str):
    def read(path: Path) -> str:
        return mountinfo if path.name == "mountinfo" else status
    return read


def _stats(path: Path):
    if path.parent.name == "self":
        inode = 1
    elif path.name == "root":
        inode = 2
    elif path.name in {"runtime", "python-runtime"}:
        inode = 3
    else:
        inode = 4
    return SimpleNamespace(st_dev=1, st_ino=inode)


def _links(path: Path) -> str:
    if path.name == "root":
        return "/"
    if path.parent.parent.name == "self":
        return f"{path.name}:[1]"
    return f"{path.name}:[2]"


def test_host_attestation_accepts_exact_post_spawn_kernel_state(tmp_path):
    mount_policy, security = _inputs(tmp_path)

    attest_host_bubblewrap_process(
        4243, mount_policy=mount_policy, security_policy=security,
        proc_root=tmp_path / "proc", read_text=_reader(_status(), _mountinfo()),
        read_link=_links, stat_path=_stats,
    )


def test_host_attestation_waits_for_bubblewrap_final_security_state(tmp_path):
    mount_policy, security = _inputs(tmp_path)
    statuses = iter((
        _status(nnp=1, seccomp=0, cap_eff="1", name="bwrap"),
        _status(nnp=1, seccomp=0, cap_eff="1", name="bwrap"),
        _status(),
    ))
    sleeps = []

    def read(path: Path) -> str:
        return _mountinfo() if path.name == "mountinfo" else next(statuses)

    attest_host_bubblewrap_process(
        4243, mount_policy=mount_policy, security_policy=security,
        proc_root=tmp_path / "proc", read_text=read,
        read_link=_links, stat_path=_stats,
        clock=lambda: 0.0, sleep=sleeps.append,
    )

    assert sleeps == [0.001, 0.001]


def test_host_attestation_rejects_bubblewrap_that_never_applies_security(tmp_path):
    mount_policy, security = _inputs(tmp_path)
    now = iter((0.0, 0.0, 5.0))

    with pytest.raises(SandboxVerificationInvalid, match="seccomp"):
        attest_host_bubblewrap_process(
            4243, mount_policy=mount_policy, security_policy=security,
            proc_root=tmp_path / "proc",
            read_text=_reader(
                _status(nnp=1, seccomp=0, cap_eff="1", name="bwrap"),
                _mountinfo(),
            ),
            read_link=_links, stat_path=_stats,
            clock=lambda: next(now), sleep=lambda _seconds: None,
        )


@pytest.mark.parametrize("status", [
    _status(uid=0), _status(gid=0), _status(nnp=0), _status(seccomp=0), _status(cap_eff="1"),
])
def test_host_attestation_rejects_privileged_or_unfiltered_process(tmp_path, status):
    mount_policy, security = _inputs(tmp_path)
    with pytest.raises(SandboxVerificationInvalid):
        attest_host_bubblewrap_process(
            4243, mount_policy=mount_policy, security_policy=security,
            proc_root=tmp_path / "proc", read_text=_reader(status, _mountinfo()),
            read_link=_links, stat_path=_stats,
        )


def test_host_attestation_rejects_shared_namespace_and_wrong_mount_mode(tmp_path):
    mount_policy, security = _inputs(tmp_path)

    def shared(path: Path) -> str:
        if path.name == "root":
            return "/"
        return f"{path.name}:[1]"

    with pytest.raises(SandboxVerificationInvalid, match="namespace"):
        attest_host_bubblewrap_process(
            4243, mount_policy=mount_policy, security_policy=security,
            proc_root=tmp_path / "proc", read_text=_reader(_status(), _mountinfo()), read_link=shared, stat_path=_stats,
        )
    with pytest.raises(SandboxVerificationInvalid, match="mount"):
        attest_host_bubblewrap_process(
            4243, mount_policy=mount_policy, security_policy=security,
            proc_root=tmp_path / "proc",
            read_text=_reader(_status(), _mountinfo().replace("/workspace rw", "/workspace ro")),
            read_link=_links, stat_path=_stats,
        )


def test_host_attestation_rejects_forbidden_authority_mount(tmp_path):
    mount_policy, security = _inputs(tmp_path)
    mountinfo = _mountinfo() + "\n6 1 0:6 / /.env ro - ext4 /dev/test ro"
    with pytest.raises(SandboxVerificationInvalid, match="forbidden"):
        attest_host_bubblewrap_process(
            4243, mount_policy=mount_policy, security_policy=security,
            proc_root=tmp_path / "proc", read_text=_reader(_status(), mountinfo),
            read_link=_links, stat_path=_stats,
        )


def test_host_attestation_rejects_wrong_readonly_mount_source(tmp_path):
    mount_policy, security = _inputs(tmp_path)

    def wrong_source(path: Path):
        value = _stats(path)
        if path.name == "runtime" and "proc" in path.parts:
            return SimpleNamespace(st_dev=value.st_dev, st_ino=99)
        return value

    with pytest.raises(SandboxVerificationInvalid, match="source"):
        attest_host_bubblewrap_process(
            4243, mount_policy=mount_policy, security_policy=security,
            proc_root=tmp_path / "proc", read_text=_reader(_status(), _mountinfo()),
            read_link=_links, stat_path=wrong_source,
        )
