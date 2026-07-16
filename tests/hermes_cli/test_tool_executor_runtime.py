from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import pytest

from hermes_cli.dashboard_auth.authority import OwnerWorkerAuthorityLease, WorkerLeaseState
from hermes_cli.owner_worker.executor_identity import (
    EgressProfile,
    ExecutorIdentity,
    ExecutorIdentityInvalid,
    ExecutorInvocation,
    ExecutorResourceDecision,
    ExecutorResourceQuota,
    parse_egress_profile,
)
from hermes_cli.tool_executor_runtime.entrypoint import (
    ExecutorRuntimeInvalid,
    _admit_workspace_mount,
    _require_matching_egress_profile,
    invocation_from_payload,
)
from hermes_cli.tool_executor_runtime.env import (
    EXECUTOR_BOOTSTRAP_FD,
    EXECUTOR_HOME,
    ExecutorEnvironmentInvalid,
    build_executor_environment,
    validate_executor_environment,
)


def _identity():
    lease = OwnerWorkerAuthorityLease("ok1_owner", 3, "worker-3", WorkerLeaseState.ACTIVE, 2, 1)
    return ExecutorIdentity.for_task(
        lease, workspace_prefix="default", task_id="task-a", session_id="session-a", executor_id="executor-a"
    )


def test_executor_environment_is_fresh_allowlist_and_binds_runtime_dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_CONTROL_HOME", "/control")
    monkeypatch.setenv("HERMES_OWNER_WORKER_CAPABILITY_PUBLIC_KEY", "public")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret")
    home = tmp_path / "owner" / "runtime" / "executors" / "executor-a" / "gen-1"
    tmp = home / "tmp"
    tmp.mkdir(parents=True)

    environment = build_executor_environment(
        _identity(), runtime_home=home, workspace_fd=11, bootstrap_fd=12, response_fd=13, egress_profile="tool-none"
    )

    assert environment["HOME"] == "/executor"
    assert environment["TMPDIR"] == "/executor/tmp"
    assert environment[EXECUTOR_HOME] == "/executor"
    assert str(home.resolve()) not in environment.values()
    assert str(tmp.resolve()) not in environment.values()
    assert environment[EXECUTOR_BOOTSTRAP_FD] == "12"
    assert environment["HERMES_EXECUTOR_RESPONSE_FD"] == "13"
    assert "HERMES_CONTROL_HOME" not in environment
    assert "HERMES_OWNER_WORKER_CAPABILITY_PUBLIC_KEY" not in environment
    assert "ANTHROPIC_API_KEY" not in environment
    assert set(environment) <= {
        "HOME", "TMPDIR", "PATH", "LANG", "LC_ALL", "LC_CTYPE", "__CF_USER_TEXT_ENCODING", "PYTHONUNBUFFERED", "PYTHONNOUSERSITE",
        "HERMES_EXECUTOR_RUNTIME", "HERMES_EXECUTOR_HOME", "HERMES_EXECUTOR_TMP", "HERMES_EXECUTOR_WORKSPACE_FD",
        "HERMES_EXECUTOR_BOOTSTRAP_FD", "HERMES_EXECUTOR_RESPONSE_FD", "HERMES_EXECUTOR_GENERATION", "HERMES_EXECUTOR_EGRESS_PROFILE",
    }


def test_executor_environment_rejects_parent_authority_and_unknown_keys(tmp_path):
    home = tmp_path / "runtime"
    tmp = home / "tmp"
    tmp.mkdir(parents=True)
    environment = build_executor_environment(
        _identity(), runtime_home=home, workspace_fd=11, bootstrap_fd=12, response_fd=13, egress_profile="tool-none"
    )

    poisoned = dict(environment, HERMES_CONTROL_HOME="/control")
    with pytest.raises(ExecutorEnvironmentInvalid, match="unallowed|forbidden"):
        validate_executor_environment(poisoned)
    poisoned = dict(environment, ANTHROPIC_API_KEY="secret")
    with pytest.raises(ExecutorEnvironmentInvalid, match="unallowed|forbidden"):
        validate_executor_environment(poisoned)


def test_executor_environment_rejects_duplicate_stdio_descriptors_and_missing_egress(tmp_path):
    home = tmp_path / "runtime"
    tmp = home / "tmp"
    tmp.mkdir(parents=True)
    environment = build_executor_environment(
        _identity(), runtime_home=home, workspace_fd=11, bootstrap_fd=12, response_fd=13, egress_profile="tool-none"
    )
    for key, value in (
        ("HERMES_EXECUTOR_BOOTSTRAP_FD", "11"),
        ("HERMES_EXECUTOR_RESPONSE_FD", "1"),
        ("HERMES_EXECUTOR_EGRESS_PROFILE", ""),
    ):
        with pytest.raises(ExecutorEnvironmentInvalid):
            validate_executor_environment(dict(environment, **{key: value}))


@pytest.mark.parametrize("profile", [
    EgressProfile.CONTROL_ONLY, EgressProfile.OWNER_PUBLIC, EgressProfile.TOOL_NONE,
    EgressProfile.TOOL_PUBLIC, EgressProfile.PROTECTED_TARGET,
])
def test_egress_profile_vocabulary_is_closed_and_invocation_serializes_canonical_value(profile):
    if profile in {EgressProfile.CONTROL_ONLY, EgressProfile.OWNER_PUBLIC}:
        with pytest.raises(ExecutorIdentityInvalid, match="not allowed"):
            ExecutorInvocation(_identity(), "read_file", {}, "call-a", "turn-a", "request-a", "invoke-a", profile)
    else:
        invocation = ExecutorInvocation(_identity(), "read_file", {}, "call-a", "turn-a", "request-a", "invoke-a", profile)
        assert invocation.egress_profile is profile
        assert invocation.to_payload()["egress_profile"] == profile.value


@pytest.mark.parametrize("value", ["", "tool-none ", "unknown", None, 1])
def test_egress_profile_rejects_noncanonical_values(value):
    with pytest.raises(ExecutorIdentityInvalid):
        parse_egress_profile(value)


@pytest.mark.parametrize("profile", ["tool-none", "tool-public", "protected-target"])
def test_executor_environment_accepts_only_executor_profiles(tmp_path, profile):
    environment = build_executor_environment(
        _identity(), runtime_home=tmp_path, workspace_fd=11, bootstrap_fd=12, response_fd=13, egress_profile=profile
    )
    assert environment["HERMES_EXECUTOR_EGRESS_PROFILE"] == profile


@pytest.mark.parametrize("profile", ["control-only", "owner-public", "unknown", ""])
def test_executor_environment_rejects_non_tool_egress_profiles(tmp_path, profile):
    with pytest.raises(ExecutorEnvironmentInvalid, match="egress profile"):
        build_executor_environment(
            _identity(), runtime_home=tmp_path, workspace_fd=11, bootstrap_fd=12, response_fd=13, egress_profile=profile
        )


def test_bootstrap_egress_profile_must_match_minimal_environment():
    invocation = ExecutorInvocation(
        _identity(), "read_file", {"path": "secret-host-path"}, "call-a", "turn-a", "request-a", "invoke-a", "tool-none"
    )
    _require_matching_egress_profile(invocation, {"HERMES_EXECUTOR_EGRESS_PROFILE": "tool-none"})
    with pytest.raises(ExecutorRuntimeInvalid, match="egress profile"):
        _require_matching_egress_profile(invocation, {"HERMES_EXECUTOR_EGRESS_PROFILE": "tool-public"})


def test_bootstrap_requires_explicit_canonical_egress_profile():
    invocation = ExecutorInvocation(_identity(), "read_file", {}, "call-a", "turn-a", "request-a", "invoke-a")
    payload = invocation.to_payload()
    del payload["egress_profile"]
    with pytest.raises(ExecutorRuntimeInvalid, match="invocation"):
        invocation_from_payload(payload)
    for profile in ("control-only", "owner-public", "unknown"):
        payload = invocation.to_payload()
        payload["egress_profile"] = profile
        with pytest.raises(ExecutorRuntimeInvalid, match="invocation"):
            invocation_from_payload(payload)


def test_executor_bootstrap_requires_matching_resource_decision():
    invocation = ExecutorInvocation(_identity(), "read_file", {}, "call-a", "turn-a", "request-a", "invoke-a")
    payload = invocation.to_payload()
    assert invocation_from_payload(payload).resource_decision == invocation.resource_decision

    missing = invocation.to_payload()
    del missing["resource_decision"]
    with pytest.raises(ExecutorRuntimeInvalid, match="invocation"):
        invocation_from_payload(missing)

    altered = invocation.to_payload()
    altered["resource_decision"] = dict(altered["resource_decision"], policy_id="resource:" + "0" * 64)
    with pytest.raises(ExecutorRuntimeInvalid, match="invocation"):
        invocation_from_payload(altered)


def test_resource_decision_rejects_foreign_identity_and_invalid_quota():
    identity = _identity()
    quota = ExecutorResourceQuota(**ExecutorInvocation(identity, "read_file", {}, "call-a", "turn-a", "request-a", "invoke-a").resource_decision.quota.to_payload())
    foreign = ExecutorIdentity.for_task(
        OwnerWorkerAuthorityLease("ok1_other", 3, "worker-3", WorkerLeaseState.ACTIVE, 2, 1),
        workspace_prefix="default", task_id="task-a", session_id="session-a", executor_id="executor-a",
    )
    decision = ExecutorResourceDecision(identity, quota)
    with pytest.raises(ExecutorIdentityInvalid, match="does not match"):
        decision.require_identity(foreign)
    with pytest.raises(ExecutorIdentityInvalid, match="policy id"):
        replace(decision, policy_id="resource:" + "0" * 64)
    with pytest.raises(ExecutorIdentityInvalid, match="output_bytes"):
        ExecutorResourceQuota(**dict(quota.to_payload(), output_bytes=0))


def test_workspace_descriptor_must_match_the_fixed_sandbox_mount(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    other = tmp_path / "other"
    workspace.mkdir()
    other.mkdir()
    descriptor = os.open(workspace, os.O_RDONLY)
    other_stat = os.stat(other)
    try:
        monkeypatch.setattr("hermes_cli.tool_executor_runtime.entrypoint.os.stat", lambda _: other_stat)
        with pytest.raises(ExecutorRuntimeInvalid, match="does not match"):
            _admit_workspace_mount(descriptor)
    finally:
        os.close(descriptor)


def test_workspace_descriptor_admission_changes_to_verified_workspace_and_closes_fd(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    descriptor = os.open(workspace, os.O_RDONLY)
    descriptor_stat = os.fstat(descriptor)
    seen = []
    monkeypatch.setattr("hermes_cli.tool_executor_runtime.entrypoint.os.stat", lambda _: descriptor_stat)
    monkeypatch.setattr("hermes_cli.tool_executor_runtime.entrypoint.os.chdir", seen.append)

    _admit_workspace_mount(descriptor)

    assert seen == ["/workspace"]
    with pytest.raises(OSError):
        os.fstat(descriptor)


def test_executor_environment_keeps_tmp_internal_without_host_tmp_input(tmp_path):
    home = tmp_path / "runtime"
    home.mkdir()
    environment = build_executor_environment(
        _identity(), runtime_home=home, workspace_fd=11, bootstrap_fd=12, response_fd=13, egress_profile="tool-none",
    )
    assert environment["TMPDIR"] == "/executor/tmp"
    assert str(home) not in environment.values()
