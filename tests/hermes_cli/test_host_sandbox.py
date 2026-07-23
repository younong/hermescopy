from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest

from hermes_cli.dashboard_auth.authority import OwnerWorkerAuthorityLease, WorkerLeaseState
from hermes_cli.owner_worker.executor_identity import EgressProfile, ExecutorIdentity
from hermes_cli.owner_worker.host_sandbox import (
    HostSandboxInvalid,
    HostSandboxUnavailable,
    build_host_sandbox_deployment_policy,
    load_host_sandbox_config,
)
from hermes_cli.owner_worker.tool_executor_sandbox import SandboxLaunchBinding, SandboxMountPolicy


def _tree(tmp_path: Path):
    owner_root = tmp_path / "owners"
    release = tmp_path / "release"
    runtime = tmp_path / "python-runtime"
    policy_dir = tmp_path / "etc-hermes"
    seccomp = policy_dir / "executor.seccomp.bpf"
    bwrap = tmp_path / "bwrap"
    for directory in (owner_root, release, runtime, policy_dir):
        directory.mkdir(mode=(0o750 if directory == owner_root else 0o755))
    (runtime / "bin").mkdir()
    (runtime / "bin/python3").write_text("python")
    (runtime / "bin/python3").chmod(0o755)
    seccomp.write_bytes(b"compiled-seccomp-bpf")
    seccomp.chmod(0o444)
    bwrap.write_text("#!/bin/sh\n")
    bwrap.chmod(0o755)
    return owner_root, release, runtime, policy_dir, seccomp, bwrap


def _document(tmp_path: Path):
    owner_root, release, runtime, policy_dir, seccomp, bwrap = _tree(tmp_path)
    digest = "sha256:" + hashlib.sha256(seccomp.read_bytes()).hexdigest()
    return {
        "schema_version": 1,
        "architecture": "x86_64",
        "owner_root": str(owner_root),
        "uid": 65532,
        "gid": 65532,
        "bwrap_binary": str(bwrap),
        "release_root": str(release),
        "runtime_root": str(runtime),
        "python_executable": "/opt/hermes/python/bin/python3",
        "readonly_mounts": [
            {"source": str(release), "destination": "/opt/hermes/release"},
            {"source": str(runtime), "destination": "/opt/hermes/python"},
        ],
        "syscall_policy_id": "executor-default-v1",
        "syscall_policy_digest": digest,
        "seccomp_artifact": str(seccomp),
        "image_digest": "sha256:" + "a" * 64,
        "profile": "executor-bwrap-v1",
        "security_backend": "host-bwrap-seccomp-v1",
        "network_mode": "isolated-tool-network",
        "verifier": "host-sandbox-policy-v1",
        "record_ttl_seconds": 30,
        "root_tmpfs_bytes": 64 << 20,
        "executor_tmpfs_bytes": 32 << 20,
        "allowed_egress_profiles": ["tool-none"],
    }, policy_dir / "executor-sandbox.json"


def _write_policy(document: dict, path: Path) -> None:
    import json

    path.write_text(json.dumps(document))
    path.chmod(0o644)


def _config(tmp_path: Path):
    document, policy_path = _document(tmp_path)
    _write_policy(document, policy_path)
    return load_host_sandbox_config(
        policy_path, require_root_owner=False, platform_name="Linux", machine="amd64"
    ), document, policy_path


def _binding(config):
    owner = config.owner_root / "owner-a"
    runtime = owner / "runtime" / "executors" / "executor-a" / "gen-1"
    workspace = owner / "workspaces"
    runtime.mkdir(parents=True)
    workspace.mkdir(parents=True)
    lease = OwnerWorkerAuthorityLease("ok1_owner", 1, "worker-a", WorkerLeaseState.ACTIVE, 1, 0)
    identity = ExecutorIdentity.for_task(
        lease, workspace_prefix="default", task_id="task-a", session_id="session-a",
        executor_id="executor-a", executor_generation=1,
    )
    binding = SandboxLaunchBinding(identity, "sandbox-a", owner, runtime)
    policy = SandboxMountPolicy(
        binding, (), workspace, None, config.owner_root,
        readonly_mounts=config.readonly_mounts,
        python_executable=config.python_executable,
    )
    return binding, policy


def test_host_policy_accepts_packaged_usr_lib64_mount(tmp_path):
    _loaded_config, document, policy_path = _config(tmp_path)
    runtime = Path(document["runtime_root"])
    usr_lib64 = runtime / "toolchain" / "usr" / "lib64"
    usr_lib64.mkdir(parents=True)
    document["readonly_mounts"].append({
        "source": str(usr_lib64),
        "destination": "/usr/lib64",
    })
    _write_policy(document, policy_path)

    config = load_host_sandbox_config(
        policy_path, require_root_owner=False, platform_name="Linux", machine="x86_64"
    )

    assert config.readonly_mounts[-1].destination == PurePosixPath("/usr/lib64")


@pytest.mark.parametrize("destination", ["/usr/share", "/etc/fonts"])
def test_host_policy_accepts_packaged_powerpoint_data_mounts(tmp_path, destination):
    _loaded_config, document, policy_path = _config(tmp_path)
    runtime = Path(document["runtime_root"])
    source = runtime / "toolchain" / destination.lstrip("/")
    source.mkdir(parents=True)
    document["readonly_mounts"].append({
        "source": str(source),
        "destination": destination,
    })
    _write_policy(document, policy_path)

    config = load_host_sandbox_config(
        policy_path, require_root_owner=False, platform_name="Linux", machine="x86_64"
    )

    assert config.readonly_mounts[-1].destination == PurePosixPath(destination)


def test_host_policy_rejects_host_powerpoint_data_mount(tmp_path):
    _loaded_config, document, policy_path = _config(tmp_path)
    host_share = tmp_path / "host-share"
    host_share.mkdir()
    document["readonly_mounts"].append({
        "source": str(host_share),
        "destination": "/usr/share",
    })
    _write_policy(document, policy_path)

    with pytest.raises(HostSandboxInvalid, match="packaged runtime"):
        load_host_sandbox_config(
            policy_path, require_root_owner=False, platform_name="Linux", machine="x86_64"
        )


def test_load_host_policy_validates_architecture_artifacts_mounts_and_modes(tmp_path):
    config, document, _policy_path = _config(tmp_path)

    assert config.architecture == "x86_64"
    assert config.uid == config.gid == 65532
    assert config.python_executable == PurePosixPath("/opt/hermes/python/bin/python3")
    assert [(str(item.source), str(item.destination)) for item in config.readonly_mounts] == [
        (document["readonly_mounts"][0]["source"], "/opt/hermes/release"),
        (document["readonly_mounts"][1]["source"], "/opt/hermes/python"),
    ]


def test_host_policy_accepts_runtime_internal_python_symlink(tmp_path):
    _loaded_config, document, policy_path = _config(tmp_path)
    runtime = Path(document["runtime_root"])
    target = runtime / "bin/python3.11"
    (runtime / "bin/python3").unlink()
    target.write_text("python")
    target.chmod(0o755)
    (runtime / "bin/python3").symlink_to("python3.11")

    config = load_host_sandbox_config(
        policy_path, require_root_owner=False, platform_name="Linux", machine="x86_64"
    )

    assert config.python_executable == PurePosixPath("/opt/hermes/python/bin/python3")


def test_host_policy_rejects_unknown_schema_wrong_arch_root_ids_and_egress(tmp_path):
    config, document, policy_path = _config(tmp_path)
    del config
    cases = (
        {**document, "unknown": True},
        {**document, "architecture": "aarch64"},
        {**document, "uid": 0},
        {**document, "allowed_egress_profiles": ["tool-public"]},
    )
    for invalid in cases:
        _write_policy(invalid, policy_path)
        with pytest.raises(HostSandboxInvalid):
            load_host_sandbox_config(
                policy_path, require_root_owner=False, platform_name="Linux", machine="x86_64"
            )


def test_host_policy_rejects_symlink_policy_and_insecure_artifact_modes(tmp_path):
    document, policy_path = _document(tmp_path)
    _write_policy(document, policy_path)
    link = policy_path.with_name("linked-policy.json")
    link.symlink_to(policy_path)
    with pytest.raises(HostSandboxUnavailable):
        load_host_sandbox_config(link, require_root_owner=False, platform_name="Linux", machine="x86_64")

    Path(document["seccomp_artifact"]).chmod(0o644)
    with pytest.raises(HostSandboxInvalid, match="mode"):
        load_host_sandbox_config(policy_path, require_root_owner=False, platform_name="Linux", machine="x86_64")


def test_host_policy_rejects_seccomp_digest_and_non_linux(tmp_path):
    document, policy_path = _document(tmp_path)
    document["syscall_policy_digest"] = "sha256:" + "b" * 64
    _write_policy(document, policy_path)
    with pytest.raises(HostSandboxInvalid, match="digest"):
        load_host_sandbox_config(policy_path, require_root_owner=False, platform_name="Linux", machine="x86_64")
    with pytest.raises(HostSandboxUnavailable, match="Linux"):
        load_host_sandbox_config(policy_path, require_root_owner=False, platform_name="Darwin", machine="x86_64")


def test_host_policy_requires_root_owned_files_in_production(tmp_path, monkeypatch):
    document, policy_path = _document(tmp_path)
    _write_policy(document, policy_path)
    original_fstat = os.fstat

    def fake_fstat(fd):
        value = original_fstat(fd)
        return SimpleNamespace(
            st_mode=value.st_mode, st_uid=1000, st_size=value.st_size,
        )

    monkeypatch.setattr(os, "fstat", fake_fstat)
    with pytest.raises(HostSandboxInvalid, match="root-owned"):
        load_host_sandbox_config(policy_path, platform_name="Linux", machine="x86_64")


def test_host_policy_rejects_replaceable_parent_directories(tmp_path, monkeypatch):
    document, policy_path = _document(tmp_path)
    _write_policy(document, policy_path)
    original_stat = Path.stat
    original_fstat = os.fstat

    def fake_fstat(fd):
        value = original_fstat(fd)
        return SimpleNamespace(
            st_mode=value.st_mode, st_uid=0, st_size=value.st_size,
        )

    def fake_stat(path, *args, **kwargs):
        value = original_stat(path, *args, **kwargs)
        if path == policy_path.parent:
            return SimpleNamespace(st_mode=stat.S_IFDIR | 0o775, st_uid=0, st_gid=0)
        return value

    monkeypatch.setattr(os, "fstat", fake_fstat)
    monkeypatch.setattr(Path, "stat", fake_stat)
    with pytest.raises(HostSandboxInvalid, match="parent directory"):
        load_host_sandbox_config(policy_path, platform_name="Linux", machine="x86_64")


def test_seccomp_source_opens_fresh_nofollow_verified_descriptor_per_call(tmp_path, monkeypatch):
    config, _document, _policy_path = _config(tmp_path)
    policy = build_host_sandbox_deployment_policy(config)
    assert policy.allowed_egress_profiles == (EgressProfile.TOOL_NONE,)
    binding, mount_policy = _binding(config)
    opened_flags = []
    original_open = os.open

    def recording_open(path, flags, *args, **kwargs):
        opened_flags.append((Path(path), flags))
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", recording_open)
    first = policy.syscall_filter_source(binding, policy.verification_policy.security_policy)
    second = policy.syscall_filter_source(binding, policy.verification_policy.security_policy)
    try:
        assert first.fd != second.fd
        assert all(flags & os.O_NOFOLLOW for path, flags in opened_flags if path == config.seccomp_artifact)
        assert os.fstat(first.fd).st_mode & stat.S_IFREG
    finally:
        os.close(first.fd)
        os.close(second.fd)

    Path(config.seccomp_artifact).chmod(0o644)
    with pytest.raises((HostSandboxInvalid, HostSandboxUnavailable)):
        policy.syscall_filter_source(binding, policy.verification_policy.security_policy)


def test_host_verification_accepts_only_tool_none(tmp_path):
    config, _document, _policy_path = _config(tmp_path)
    policy = build_host_sandbox_deployment_policy(config, clock=lambda: 100)
    binding, mount_policy = _binding(config)

    record = policy.verification_source(binding, mount_policy, SimpleNamespace(egress_profile="tool-none"))
    assert record.egress_profile.value == "tool-none"
    assert record.observed_at == 100
    with pytest.raises(HostSandboxUnavailable, match="tool-none"):
        policy.verification_source(binding, mount_policy, SimpleNamespace(egress_profile="tool-public"))
