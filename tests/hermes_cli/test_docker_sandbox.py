from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli.dashboard_auth.authority import OwnerWorkerAuthorityLease, WorkerLeaseState
from hermes_cli.owner_worker.docker_sandbox import (
    DockerSandboxInvalid,
    DockerSandboxUnavailable,
    build_docker_sandbox_deployment_policy,
    docker_sandbox_config_from_environment,
    parse_docker_sandbox_config,
    preflight_docker_sandbox,
)
from hermes_cli.owner_worker.executor_identity import ExecutorIdentity
from hermes_cli.owner_worker.tool_executor_sandbox import SandboxLaunchBinding, SandboxMountPolicy


DIGEST = "sha256:" + "a" * 64
SYSCALL_DIGEST = "sha256:" + "b" * 64


def _probe(*_args, **_kwargs):
    return SimpleNamespace(
        returncode=0,
        stdout=" ".join(("--bind-fd", "--size", "--uid", "--gid", "--cap-drop", "--seccomp")),
        stderr="",
    )


def _config(tmp_path: Path, **overrides):
    bwrap = tmp_path / "bwrap"
    bwrap.write_text("#!/bin/sh\n")
    bwrap.chmod(0o755)
    seccomp_fd = os.open(os.devnull, os.O_RDONLY)
    values = {
        "container_id": "owner-worker-container",
        "image_digest": DIGEST,
        "owner_root": str(tmp_path / "owners"),
        "readonly_global_roots": [str(tmp_path / "runtime")],
        "uid": 10001,
        "gid": 10001,
        "network_mode": "owner-worker-isolated",
        "mounts": [
            {"source": str(tmp_path / "runtime"), "destination": "/opt/hermes-runtime", "type": "bind"},
        ],
        "bwrap_binary": str(bwrap),
        "syscall_policy_id": "executor-default-v1",
        "syscall_policy_digest": SYSCALL_DIGEST,
        "seccomp_fd": seccomp_fd,
    }
    values.update(overrides)
    for directory in (tmp_path / "owners", tmp_path / "runtime"):
        directory.mkdir(exist_ok=True)
    return parse_docker_sandbox_config(values)


def _inspection(config, **host_overrides):
    host = {
        "ReadonlyRootfs": True,
        "Privileged": False,
        "NetworkMode": config.network_mode,
        "SecurityOpt": ["no-new-privileges:true"],
        "CapAdd": None,
        "CapDrop": ["ALL"],
    }
    host.update(host_overrides)
    return {
        "Image": config.image_digest,
        "Config": {"Image": f"registry.example/hermes@{config.image_digest}", "User": f"{config.uid}:{config.gid}"},
        "HostConfig": host,
        "State": {"Running": True},
        "Mounts": [
            {"Source": str(mount.source), "Destination": str(mount.destination), "Type": mount.mount_type, "RW": False}
            for mount in config.mounts
        ],
    }


class _Inspect:
    def __init__(self, document):
        self.document = document
        self.calls = 0

    def inspect_container(self, container_id):
        assert container_id == "owner-worker-container"
        self.calls += 1
        return self.document


def _binding_and_mount(tmp_path):
    owner = tmp_path / "owners" / "owner-a"
    runtime = owner / "runtime" / "executors" / "executor-a" / "gen-1"
    workspace = tmp_path / "workspace"
    for directory in (runtime, workspace):
        directory.mkdir(parents=True, exist_ok=True)
    lease = OwnerWorkerAuthorityLease("ok1_owner", 1, "worker-a", WorkerLeaseState.ACTIVE, 1, 0)
    identity = ExecutorIdentity.for_task(
        lease, workspace_prefix="default", task_id="task-a", session_id="session-a",
        executor_id="executor-a", executor_generation=1,
    )
    binding = SandboxLaunchBinding(identity, "sandbox-a", owner, runtime)
    return binding, workspace


def test_operator_config_parser_rejects_missing_unknown_and_unsafe_mounts(tmp_path):
    config = _config(tmp_path)
    assert config.uid == 10001
    assert config.mounts[0].destination == Path("/opt/hermes-runtime")

    unsafe = {
        "container_id": "container", "image_digest": DIGEST, "owner_root": str(tmp_path / "owners"),
        "readonly_global_roots": [str(tmp_path / "runtime")], "uid": 1, "gid": 1, "network_mode": "isolated",
        "mounts": [{"source": "/dev", "destination": "/socket"}], "bwrap_binary": str(tmp_path / "bwrap"),
        "syscall_policy_id": "default", "syscall_policy_digest": SYSCALL_DIGEST, "seccomp_fd": 4,
    }
    with pytest.raises(DockerSandboxInvalid, match="sensitive"):
        parse_docker_sandbox_config(unsafe)
    unsafe["mounts"] = [{"source": "/opt/runtime", "destination": "/runtime", "type": "volume"}]
    with pytest.raises(DockerSandboxInvalid, match="must be bind"):
        parse_docker_sandbox_config(unsafe)
    unsafe["mounts"] = []
    unsafe["unexpected"] = True
    with pytest.raises(DockerSandboxInvalid, match="fields"):
        parse_docker_sandbox_config(unsafe)


def test_environment_parser_requires_complete_json(tmp_path):
    config = _config(tmp_path)
    document = {
        "container_id": config.container_id, "image_digest": config.image_digest, "owner_root": str(config.owner_root),
        "readonly_global_roots": [str(item) for item in config.readonly_global_roots], "uid": config.uid, "gid": config.gid,
        "network_mode": config.network_mode, "mounts": [{"source": str(item.source), "destination": str(item.destination)} for item in config.mounts],
        "bwrap_binary": str(config.bwrap_binary), "syscall_policy_id": config.syscall_policy_id,
        "syscall_policy_digest": config.syscall_policy_digest, "seccomp_fd": config.seccomp_fd,
    }
    loaded = docker_sandbox_config_from_environment({"HERMES_DOCKER_SANDBOX_CONFIG": json.dumps(document)})
    assert loaded.image_digest == DIGEST
    with pytest.raises(DockerSandboxInvalid, match="required"):
        docker_sandbox_config_from_environment({})


@pytest.mark.parametrize("mutate, error", [
    (lambda document: document["HostConfig"].update({"Privileged": True}), "root filesystem or privilege"),
    (lambda document: document["HostConfig"].update({"CapAdd": ["SYS_ADMIN"]}), "capability"),
    (lambda document: document["HostConfig"].update({"SecurityOpt": []}), "no-new-privileges"),
    (lambda document: document["Mounts"].__setitem__(0, {**document["Mounts"][0], "RW": True}), "read-only"),
    (lambda document: document["Config"].update({"User": "0:0"}), "non-root"),
])
def test_preflight_fails_closed_for_weakened_docker_controls(tmp_path, mutate, error):
    config = _config(tmp_path)
    document = _inspection(config)
    mutate(document)
    with pytest.raises(DockerSandboxUnavailable, match=error):
        preflight_docker_sandbox(config, inspect_client=_Inspect(document), platform_name="Linux", runner=_probe)


def test_preflight_requires_linux_bwrap_capabilities_and_immutable_digest(tmp_path):
    config = _config(tmp_path)
    client = _Inspect(_inspection(config))
    with pytest.raises(DockerSandboxUnavailable, match="Linux"):
        preflight_docker_sandbox(config, inspect_client=client, platform_name="Darwin", runner=_probe)
    with pytest.raises(DockerSandboxUnavailable, match="Bubblewrap"):
        preflight_docker_sandbox(config, inspect_client=client, platform_name="Linux", runner=lambda *_a, **_k: SimpleNamespace(returncode=0, stdout="", stderr=""))
    document = _inspection(config)
    document["Image"] = "sha256:" + "c" * 64
    document["Config"]["Image"] = "registry.example/hermes@sha256:" + "c" * 64
    with pytest.raises(DockerSandboxUnavailable, match="digest"):
        preflight_docker_sandbox(config, inspect_client=_Inspect(document), platform_name="Linux", runner=_probe)


def test_policy_rechecks_inspect_and_builds_exact_record_without_docker_daemon(tmp_path):
    config = _config(tmp_path)
    client = _Inspect(_inspection(config))
    policy = build_docker_sandbox_deployment_policy(
        config, inspect_client=client, platform_name="Linux", runner=_probe, clock=lambda: 100,
    )
    binding, workspace = _binding_and_mount(tmp_path)
    mount_policy = SandboxMountPolicy(binding, config.readonly_global_roots, workspace, None, config.owner_root)
    invocation = SimpleNamespace(egress_profile="tool-none")
    record = policy.verification_source(binding, mount_policy, invocation)

    assert client.calls == 2
    assert record.image_digest == DIGEST
    assert record.network_mode == config.network_mode
    assert record.uid == config.uid
    assert record.observed_at == 100
    assert record.expires_at == 130
    filter_fd = policy.syscall_filter_source(binding, policy.verification_policy.security_policy).fd
    assert filter_fd != config.seccomp_fd
    assert os.fstat(filter_fd).st_ino == os.fstat(config.seccomp_fd).st_ino
    os.close(filter_fd)

    client.document["HostConfig"]["NetworkMode"] = "host"
    with pytest.raises(DockerSandboxUnavailable, match="network mode"):
        policy.verification_source(binding, mount_policy, invocation)
