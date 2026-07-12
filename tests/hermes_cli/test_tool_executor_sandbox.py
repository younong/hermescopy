from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli.dashboard_auth.authority import OwnerWorkerAuthorityLease, WorkerLeaseState
from hermes_cli.owner_worker.executor_identity import ExecutorIdentity
from hermes_cli.owner_worker.tool_executor_sandbox import (
    ExecutorIsolationUnavailable,
    SandboxLaunchBinding,
    build_bubblewrap_launch_spec,
)


def _environment():
    return {
        "HOME": "/executor",
        "TMPDIR": "/executor/tmp",
        "HERMES_EXECUTOR_RUNTIME": "1",
        "HERMES_EXECUTOR_WORKSPACE_FD": "10",
    }


def _inputs(tmp_path):
    owner = tmp_path / "owner"
    runtime = owner / "runtime" / "executors" / "executor-a" / "gen-1"
    workspace = tmp_path / "workspace"
    dependency = tmp_path / "python-runtime"
    for directory in (runtime, workspace, dependency):
        directory.mkdir(parents=True, exist_ok=True)
    bwrap = tmp_path / "bwrap"
    bwrap.write_text("#!/bin/sh\n")
    bwrap.chmod(0o755)
    return owner, runtime, workspace, dependency, bwrap


def _probe(*args, **kwargs):
    del args, kwargs
    return SimpleNamespace(returncode=0, stdout="  --bind-fd FD DEST\n", stderr="")


def _identity(owner_key="ok1_owner", *, executor_generation=1):
    lease = OwnerWorkerAuthorityLease(owner_key, 1, "worker-a", WorkerLeaseState.ACTIVE, 1, 0)
    return ExecutorIdentity.for_task(
        lease,
        workspace_prefix="default",
        task_id="task-a",
        session_id="session-a",
        executor_id="executor-a",
        executor_generation=executor_generation,
    )


def _spec(tmp_path, **overrides):
    owner, runtime, workspace, dependency, bwrap = _inputs(tmp_path)
    kwargs = {
        "environment": _environment(),
        "workspace_fd": 10,
        "runtime_home": runtime,
        "owner_home": owner,
        "workspace_root": workspace,
        "runtime_dependency_roots": (dependency,),
        "bubblewrap_binary": bwrap,
        "platform_name": "Linux",
        "runner": _probe,
    }
    kwargs.update(overrides)
    return build_bubblewrap_launch_spec(**kwargs)


def test_linux_spec_uses_private_namespaces_minimal_mounts_and_exact_environment(tmp_path):
    spec = _spec(tmp_path)
    argv = list(spec.argv)

    for argument in ("--unshare-user", "--unshare-pid", "--unshare-ipc", "--unshare-net", "--die-with-parent"):
        assert argument in argv
    assert ["--tmpfs", "/"] == argv[argv.index("--tmpfs"):argv.index("--tmpfs") + 2]
    assert ["--proc", "/proc"] == argv[argv.index("--proc"):argv.index("--proc") + 2]
    assert ["--dev", "/dev"] == argv[argv.index("--dev"):argv.index("--dev") + 2]
    assert ["--bind-fd", "10", "/workspace"] == argv[argv.index("--bind-fd"):argv.index("--bind-fd") + 3]
    assert "--clearenv" in argv
    assert "--share-net" not in argv
    assert "--" in argv
    assert all(value not in argv for value in ("/sys", "/run", "/var/run", "/var/run/docker.sock"))
    assert "/workspace" in argv
    assert str(tmp_path / "workspace") not in argv
    assert ["--bind", str(tmp_path / "owner" / "runtime" / "executors" / "executor-a" / "gen-1"), "/executor"] == argv[argv.index("--bind"):argv.index("--bind") + 3]
    assert ["--chdir", "/workspace"] == argv[argv.index("--chdir"):argv.index("--chdir") + 2]
    triples = list(zip(argv, argv[1:], argv[2:]))
    for key, value in _environment().items():
        assert ("--setenv", key, value) in triples


def test_non_linux_or_missing_or_unsupported_bubblewrap_fails_before_launch_spec(tmp_path):
    with pytest.raises(ExecutorIsolationUnavailable, match="requires Linux"):
        _spec(tmp_path, platform_name="Darwin")
    with pytest.raises(ExecutorIsolationUnavailable, match="required"):
        _spec(tmp_path, bubblewrap_binary=None, which=lambda _: None)
    with pytest.raises(ExecutorIsolationUnavailable, match="--bind-fd"):
        _spec(tmp_path, runner=lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""))


def test_dependency_roots_cannot_overlap_owner_workspace_or_sensitive_host_trees(tmp_path):
    owner, runtime, workspace, dependency, bwrap = _inputs(tmp_path)
    common = dict(
        environment=_environment(), workspace_fd=10, runtime_home=runtime, owner_home=owner,
        workspace_root=workspace, bubblewrap_binary=bwrap, platform_name="Linux", runner=_probe,
    )
    for unsafe in (owner, workspace, Path("/"), Path("/proc"), Path("/run")):
        with pytest.raises(ExecutorIsolationUnavailable):
            build_bubblewrap_launch_spec(**common, runtime_dependency_roots=(unsafe,))


def test_workspace_descriptor_must_be_nonnegative(tmp_path):
    with pytest.raises(ExecutorIsolationUnavailable, match="descriptor"):
        _spec(tmp_path, workspace_fd=-1)


def test_sandbox_binding_is_immutable_and_owner_generation_bound(tmp_path):
    owner, runtime, _workspace, _dependency, _bwrap = _inputs(tmp_path)
    first = SandboxLaunchBinding(_identity(), "sandbox-a", owner, runtime)
    second = SandboxLaunchBinding(_identity("ok1_other"), "sandbox-b", owner, runtime)

    assert first.mount_view_id != second.mount_view_id
    assert first.tmpfs_id != second.tmpfs_id
    assert first.security_subject_id != second.security_subject_id
    with pytest.raises(Exception):
        first.sandbox_id = "reused"


def test_sandbox_binding_rejects_mismatched_owner_or_executor_generation(tmp_path):
    owner, runtime, _workspace, _dependency, _bwrap = _inputs(tmp_path)
    with pytest.raises(ExecutorIsolationUnavailable, match="does not match"):
        SandboxLaunchBinding(_identity(), "sandbox-a", owner, owner / "runtime" / "executors" / "other" / "gen-1")
    with pytest.raises(ExecutorIsolationUnavailable, match="does not match"):
        SandboxLaunchBinding(_identity(executor_generation=2), "sandbox-a", owner, runtime)


def test_bubblewrap_spec_rejects_paths_that_disagree_with_binding(tmp_path):
    owner, runtime, workspace, dependency, bwrap = _inputs(tmp_path)
    binding = SandboxLaunchBinding(_identity(), "sandbox-a", owner, runtime)
    other_owner = tmp_path / "other-owner"
    other_owner.mkdir()
    with pytest.raises(ExecutorIsolationUnavailable, match="does not match"):
        build_bubblewrap_launch_spec(
            environment=_environment(), workspace_fd=10,
            runtime_home=runtime, owner_home=other_owner, workspace_root=workspace,
            binding=binding, runtime_dependency_roots=(dependency,), bubblewrap_binary=bwrap,
            platform_name="Linux", runner=_probe,
        )
