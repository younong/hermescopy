from __future__ import annotations

import os
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from hermes_cli.dashboard_auth.authority import OwnerWorkerAuthorityLease, WorkerLeaseState
from hermes_cli.owner_worker.executor_identity import EgressProfile, ExecutorIdentity
from hermes_cli.owner_worker.tool_executor_sandbox import (
    ExecutorIsolationUnavailable,
    SandboxDeploymentPolicy,
    SandboxLaunchBinding,
    SandboxMountPolicy,
    SandboxReadonlyMount,
    SandboxSecurityPolicy,
    SandboxSyscallFilter,
    SandboxVerificationInvalid,
    SandboxVerificationPolicy,
    SandboxVerificationRecord,
    build_bubblewrap_launch_spec,
    load_sandbox_deployment_policy,
    validate_sandbox_verification_record,
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
    return SimpleNamespace(
        returncode=0,
        stdout="  --bind-fd FD DEST\n  --ro-bind-fd FD DEST\n  --size BYTES\n  --uid UID\n  --gid GID\n"
        "  --cap-drop CAP\n  --seccomp FD\n  --remount-ro DEST\n  --info-fd FD\n",
        stderr="",
    )


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


def _mount_policy(owner, runtime, workspace, dependency):
    return SandboxMountPolicy(
        SandboxLaunchBinding(_identity(), "sandbox-a", owner, runtime), (dependency,), workspace, None
    )


def _security_policy():
    return SandboxSecurityPolicy(
        "executor-bwrap-v1", "bubblewrap-seccomp-v1", 1000, 1000, "executor-default-v1", "sha256:" + "b" * 64
    )


def _syscall_filter():
    fd = os.open(os.devnull, os.O_RDONLY)
    policy = _security_policy()
    return SandboxSyscallFilter(fd, policy.syscall_policy_id, policy.syscall_policy_digest)


def _spec(tmp_path, **overrides):
    owner, runtime, workspace, dependency, bwrap = _inputs(tmp_path)
    mount_policy = _mount_policy(owner, runtime, workspace, dependency)
    kwargs = {
        "environment": _environment(),
        "workspace_fd": 10,
        "binding": mount_policy.binding,
        "mount_policy": mount_policy,
        "security_policy": _security_policy(),
        "syscall_filter": _syscall_filter(),
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
    assert ["--uid", "1000"] == argv[argv.index("--uid"):argv.index("--uid") + 2]
    assert ["--gid", "1000"] == argv[argv.index("--gid"):argv.index("--gid") + 2]
    assert ["--cap-drop", "ALL"] == argv[argv.index("--cap-drop"):argv.index("--cap-drop") + 2]
    assert ["--seccomp", str(spec.inherited_security_fds[0])] == argv[argv.index("--seccomp"):argv.index("--seccomp") + 2]
    assert ["--size", str(64 << 20), "--tmpfs", "/"] == argv[argv.index("--size"):argv.index("--size") + 4]
    second_size = len(argv) - 1 - argv[::-1].index("--size")
    assert ["--size", str(32 << 20), "--tmpfs", "/executor/tmp"] == argv[second_size:second_size + 4]
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
    assert ["--remount-ro", "/"] == argv[argv.index("--remount-ro"):argv.index("--remount-ro") + 2]
    assert argv.index("--remount-ro") < argv.index("--bind-fd") < argv.index("--bind")
    assert ["--chdir", "/workspace"] == argv[argv.index("--chdir"):argv.index("--chdir") + 2]
    triples = list(zip(argv, argv[1:], argv[2:]))
    for key, value in _environment().items():
        assert ("--setenv", key, value) in triples


def test_linux_spec_wires_bubblewrap_info_descriptor(tmp_path):
    spec = _spec(tmp_path, info_fd=17)
    argv = list(spec.argv)

    assert ["--info-fd", "17"] == argv[argv.index("--info-fd"):argv.index("--info-fd") + 2]


def test_explicit_readonly_mounts_use_exact_destinations_and_policy_python(tmp_path):
    owner, runtime, workspace, dependency, bwrap = _inputs(tmp_path)
    destination = Path("/opt/hermes/runtime")
    python = destination / "bin/python3"
    binding = SandboxLaunchBinding(_identity(), "sandbox-a", owner, runtime)
    mount_policy = SandboxMountPolicy(
        binding, (), workspace, None,
        readonly_mounts=(SandboxReadonlyMount(dependency, destination),),
        python_executable=python,
    )
    spec = build_bubblewrap_launch_spec(
        environment=_environment(), workspace_fd=10, binding=binding,
        mount_policy=mount_policy, security_policy=_security_policy(),
        syscall_filter=_syscall_filter(), bubblewrap_binary=bwrap,
        platform_name="Linux", runner=_probe,
    )
    argv = list(spec.argv)

    mount_index = argv.index("--ro-bind-fd")
    mount_fd = int(argv[mount_index + 1])
    try:
        assert argv[mount_index + 2] == str(destination)
        assert mount_fd in spec.inherited_security_fds
        descriptor = os.fstat(mount_fd)
        source = dependency.stat()
        assert (descriptor.st_dev, descriptor.st_ino) == (source.st_dev, source.st_ino)
        assert argv[argv.index("--") + 1] == str(python)
        assert str(dependency) not in argv
        assert str(dependency / "bin/python3") not in argv
    finally:
        for fd in spec.inherited_security_fds:
            os.close(fd)


def test_readonly_mount_destinations_must_be_unique_reserved_and_non_overlapping(tmp_path):
    owner, runtime, workspace, dependency, _bwrap = _inputs(tmp_path)
    other = tmp_path / "other-runtime"
    other.mkdir()
    binding = SandboxLaunchBinding(_identity(), "sandbox-a", owner, runtime)

    for mounts in (
        (SandboxReadonlyMount(dependency, "/opt/runtime"), SandboxReadonlyMount(other, "/opt/runtime/lib")),
        (SandboxReadonlyMount(dependency, "/workspace/runtime"),),
    ):
        with pytest.raises(ExecutorIsolationUnavailable):
            SandboxMountPolicy(
                binding, (), workspace, None, readonly_mounts=mounts,
                python_executable=Path(str(mounts[0].destination)) / "bin/python3",
            )


def test_non_linux_or_missing_or_unsupported_bubblewrap_fails_before_launch_spec(tmp_path):
    with pytest.raises(ExecutorIsolationUnavailable, match="requires Linux"):
        _spec(tmp_path, platform_name="Darwin")
    with pytest.raises(ExecutorIsolationUnavailable, match="required"):
        _spec(tmp_path, bubblewrap_binary=None, which=lambda _: None)
    with pytest.raises(ExecutorIsolationUnavailable, match="--bind-fd"):
        _spec(tmp_path, runner=lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""))


def test_mount_policy_rejects_owner_workspace_and_sensitive_global_roots(tmp_path):
    owner, runtime, workspace, _dependency, _bwrap = _inputs(tmp_path)
    binding = SandboxLaunchBinding(_identity(), "sandbox-a", owner, runtime)
    sibling = tmp_path / "other-owner"
    sibling.mkdir()
    for unsafe in (owner, workspace, Path("/"), Path("/proc"), Path("/sys"), Path("/dev"), Path("/run"), Path("/var/run")):
        with pytest.raises(ExecutorIsolationUnavailable):
            SandboxMountPolicy(binding, (unsafe,), workspace, None)
    with pytest.raises(ExecutorIsolationUnavailable):
        SandboxMountPolicy(binding, (sibling,), workspace, sibling)


def test_deployment_policy_rejects_owner_root_and_sibling_mounts(tmp_path):
    owner, runtime, workspace, dependency, _bwrap = _inputs(tmp_path)
    owner_root = owner.parent
    sibling = owner_root / "other-owner"
    sibling.mkdir()

    with pytest.raises(ExecutorIsolationUnavailable, match="overlaps owner root"):
        SandboxDeploymentPolicy(
            _verification_policy(), lambda *_args: None, lambda *_args: None,
            (sibling,), owner_root,
        )

    global_dependency = tmp_path.parent / f"{tmp_path.name}-global-dependency"
    global_dependency.mkdir()
    policy = SandboxDeploymentPolicy(
        _verification_policy(), lambda *_args: None, lambda *_args: None,
        (global_dependency,), owner_root,
    )
    binding = SandboxLaunchBinding(_identity(), "sandbox-a", owner, runtime)
    with pytest.raises(ExecutorIsolationUnavailable, match="overlaps protected owner data"):
        SandboxMountPolicy(binding, (owner_root,), workspace, None, owner_root)
    assert policy.owner_root == owner_root.resolve()


def test_deployment_policy_loader_requires_explicit_valid_operator_factory(tmp_path, monkeypatch):
    owner, _runtime, _workspace, _dependency, _bwrap = _inputs(tmp_path)
    global_dependency = tmp_path.parent / f"{tmp_path.name}-operator-global"
    global_dependency.mkdir()
    policy = SandboxDeploymentPolicy(
        _verification_policy(), lambda *_args: None, lambda *_args: None,
        (global_dependency,), owner.parent,
    )
    module = ModuleType("test_sandbox_operator")
    module.make_policy = lambda: policy
    monkeypatch.setitem(sys.modules, module.__name__, module)

    assert load_sandbox_deployment_policy("test_sandbox_operator:make_policy") is policy
    for spec in ("", "test_sandbox_operator:missing", "test_sandbox_operator:make_policy.extra"):
        with pytest.raises(SandboxVerificationInvalid):
            load_sandbox_deployment_policy(spec)


def test_mount_policy_rejects_unbounded_tmpfs(tmp_path):
    owner, runtime, workspace, dependency, _bwrap = _inputs(tmp_path)
    binding = SandboxLaunchBinding(_identity(), "sandbox-a", owner, runtime)
    for size in (0, -1, (1 << 30) + 1):
        with pytest.raises(ExecutorIsolationUnavailable, match="tmpfs"):
            SandboxMountPolicy(binding, (dependency,), workspace, None, root_tmpfs_bytes=size)


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


def _verification_policy():
    return SandboxVerificationPolicy("sha256:" + "a" * 64, "isolated-tool-network", _security_policy())


def _verification_record(binding, mount_policy, *, egress_profile="tool-none", observed_at=90, expires_at=110, **overrides):
    identity = binding.identity
    policy = _verification_policy()
    values = {
        "schema_version": 1, "verifier": "host-supervisor", "observed_at": observed_at, "expires_at": expires_at,
        "image_digest": policy.image_digest, "profile": policy.security_policy.profile,
        "security_backend": policy.security_policy.backend, "syscall_policy_id": policy.security_policy.syscall_policy_id,
        "syscall_policy_digest": policy.security_policy.syscall_policy_digest, "owner_key": identity.owner_key,
        "worker_id": identity.worker_id, "worker_generation": identity.worker_generation,
        "lease_version": identity.lease_version, "recovery_generation": identity.recovery_generation,
        "executor_id": identity.executor_id, "executor_generation": identity.executor_generation,
        "sandbox_id": binding.sandbox_id, "uid": policy.security_policy.uid, "gid": policy.security_policy.gid,
        "mount_view_id": binding.mount_view_id, "mount_policy_id": mount_policy.mount_policy_id, "tmpfs_id": binding.tmpfs_id,
        "security_subject_id": binding.security_subject_id, "network_mode": policy.network_mode,
        "egress_profile": egress_profile, "rootfs_readonly": True, "no_new_privileges": True,
        "capabilities_dropped": True, "namespaces": ("user", "pid", "ipc", "net"),
    }
    values.update(overrides)
    return SandboxVerificationRecord(**values)


@pytest.mark.parametrize("kwargs", [
    {"uid": 0}, {"gid": 0}, {"profile": ""}, {"backend": ""},
    {"syscall_policy_id": ""}, {"syscall_policy_digest": "sha256:not-a-digest"},
    {"capabilities": ("CAP_SYS_ADMIN",)}, {"no_new_privileges": False},
])
def test_security_policy_rejects_incomplete_or_privileged_controls(kwargs):
    values = {
        "profile": "executor-bwrap-v1", "backend": "bubblewrap-seccomp-v1", "uid": 1000, "gid": 1000,
        "syscall_policy_id": "executor-default-v1", "syscall_policy_digest": "sha256:" + "b" * 64,
    }
    values.update(kwargs)
    with pytest.raises(SandboxVerificationInvalid):
        SandboxSecurityPolicy(**values)


def test_syscall_filter_must_match_security_policy_and_stay_inherited(tmp_path):
    owner, runtime, workspace, dependency, bwrap = _inputs(tmp_path)
    mount_policy = _mount_policy(owner, runtime, workspace, dependency)
    syscall_filter = _syscall_filter()
    try:
        with pytest.raises(SandboxVerificationInvalid, match="does not match"):
            build_bubblewrap_launch_spec(
                environment=_environment(), workspace_fd=10, binding=mount_policy.binding, mount_policy=mount_policy,
                security_policy=SandboxSecurityPolicy(
                    "other", "bubblewrap-seccomp-v1", 1000, 1000, "other", "sha256:" + "c" * 64
                ), syscall_filter=syscall_filter, bubblewrap_binary=bwrap, platform_name="Linux", runner=_probe,
            )
    finally:
        os.close(syscall_filter.fd)


def test_bubblewrap_spec_rejects_paths_that_disagree_with_binding(tmp_path):
    owner, runtime, workspace, dependency, bwrap = _inputs(tmp_path)
    binding = SandboxLaunchBinding(_identity(), "sandbox-a", owner, runtime)
    mount_policy = SandboxMountPolicy(binding, (dependency,), workspace, None)
    other_owner = tmp_path / "other-owner"
    other_owner.mkdir()
    with pytest.raises(ExecutorIsolationUnavailable, match="does not match"):
        build_bubblewrap_launch_spec(
            environment=_environment(), workspace_fd=10, owner_home=other_owner,
            binding=binding, mount_policy=mount_policy, security_policy=_security_policy(),
            syscall_filter=_syscall_filter(), bubblewrap_binary=bwrap,
            platform_name="Linux", runner=_probe,
        )


@pytest.mark.parametrize("profile", [EgressProfile.TOOL_NONE, EgressProfile.TOOL_PUBLIC, EgressProfile.PROTECTED_TARGET])
def test_verification_record_accepts_each_executor_egress_profile(tmp_path, profile):
    owner, runtime, _workspace, dependency, _bwrap = _inputs(tmp_path)
    binding = SandboxLaunchBinding(_identity(), "sandbox-a", owner, runtime)
    mount_policy = SandboxMountPolicy(binding, (dependency,), None, None)
    record = _verification_record(binding, mount_policy, egress_profile=profile)
    assert validate_sandbox_verification_record(
        record, binding=binding, mount_policy=mount_policy, egress_profile=profile, policy=_verification_policy(), now=100
    ) is record


@pytest.mark.parametrize("profile", ["control-only", "owner-public", "unknown"])
def test_verification_record_rejects_non_tool_egress_profile(tmp_path, profile):
    owner, runtime, _workspace, dependency, _bwrap = _inputs(tmp_path)
    binding = SandboxLaunchBinding(_identity(), "sandbox-a", owner, runtime)
    mount_policy = SandboxMountPolicy(binding, (dependency,), None, None)
    with pytest.raises(SandboxVerificationInvalid, match="egress profile"):
        _verification_record(binding, mount_policy, egress_profile=profile)


def test_verification_record_is_immutable_and_exactly_matches_binding(tmp_path):
    owner, runtime, _workspace, _dependency, _bwrap = _inputs(tmp_path)
    binding = SandboxLaunchBinding(_identity(), "sandbox-a", owner, runtime)
    mount_policy = SandboxMountPolicy(binding, (_dependency,), None, None)
    record = _verification_record(binding, mount_policy)

    assert validate_sandbox_verification_record(
        record, binding=binding, mount_policy=mount_policy, egress_profile="tool-none", policy=_verification_policy(), now=100
    ) is record
    with pytest.raises(Exception):
        record.profile = "changed"


@pytest.mark.parametrize("field,value", [
    ("image_digest", "sha256:" + "b" * 64), ("profile", "other"),
    ("security_backend", "other-backend"), ("syscall_policy_id", "other-policy"),
    ("syscall_policy_digest", "sha256:" + "c" * 64), ("owner_key", "ok1_other"),
    ("worker_generation", 2), ("lease_version", 2), ("recovery_generation", 1),
    ("executor_generation", 2), ("uid", 1001), ("mount_view_id", "mount:other"),
    ("network_mode", "host"), ("egress_profile", "tool-public"), ("rootfs_readonly", False),
    ("no_new_privileges", False), ("capabilities_dropped", False), ("namespaces", ("user", "pid")),
])
def test_verification_record_rejects_any_binding_or_policy_mismatch(tmp_path, field, value):
    owner, runtime, _workspace, _dependency, _bwrap = _inputs(tmp_path)
    binding = SandboxLaunchBinding(_identity(), "sandbox-a", owner, runtime)
    mount_policy = SandboxMountPolicy(binding, (_dependency,), None, None)
    if field in {"rootfs_readonly", "no_new_privileges", "capabilities_dropped", "namespaces"}:
        with pytest.raises(SandboxVerificationInvalid):
            _verification_record(binding, mount_policy, **{field: value})
    else:
        record = _verification_record(binding, mount_policy, **{field: value})
        with pytest.raises(SandboxVerificationInvalid, match="does not match"):
            validate_sandbox_verification_record(
                record, binding=binding, mount_policy=mount_policy, egress_profile="tool-none", policy=_verification_policy(), now=100
            )


def test_verification_record_rejects_missing_stale_and_future_evidence(tmp_path):
    owner, runtime, _workspace, _dependency, _bwrap = _inputs(tmp_path)
    binding = SandboxLaunchBinding(_identity(), "sandbox-a", owner, runtime)
    mount_policy = SandboxMountPolicy(binding, (_dependency,), None, None)
    with pytest.raises(SandboxVerificationInvalid, match="required"):
        validate_sandbox_verification_record(
            None, binding=binding, mount_policy=mount_policy, egress_profile="tool-none", policy=_verification_policy(), now=100
        )
    for record, now in ((_verification_record(binding, mount_policy, expires_at=100), 100), (_verification_record(binding, mount_policy, observed_at=101, expires_at=110), 100)):
        with pytest.raises(SandboxVerificationInvalid, match="stale"):
            validate_sandbox_verification_record(
                record, binding=binding, mount_policy=mount_policy, egress_profile="tool-none", policy=_verification_policy(), now=now
            )
