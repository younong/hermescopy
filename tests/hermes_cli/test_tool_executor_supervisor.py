from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from hermes_cli.authenticated_file_context import AuthenticatedWorkspaceContext
from hermes_cli.controlled_roots import ControlledRoot, ControlledRoots, RootKind
from hermes_cli.dashboard_auth.authority import OwnerWorkerAuthorityLease, WorkerLeaseState
from hermes_cli.owner_worker.tool_executor_sandbox import (
    BubblewrapLaunchSpec,
    ExecutorIsolationUnavailable,
    SandboxSecurityPolicy,
    SandboxSyscallFilter,
    SandboxVerificationPolicy,
    SandboxVerificationRecord,
)
from hermes_cli.dashboard_auth.audit import AuthorityAuditEvent, AuthorityAuditReason
from hermes_cli.owner_worker.executor_identity import ExecutorInvocation, default_executor_resource_decision
from hermes_cli.owner_worker.executor_tokens import AUD_PROCESS_REGISTRY, ExecutorCapabilityInvalid
from hermes_cli.owner_worker.tool_executor_supervisor import ExecutorEgressPolicy, ToolExecutorSupervisor


class _FakeProcess:
    pid = 4242

    def wait(self, timeout=None):
        del timeout
        return 0


def _roots(tmp_path):
    locations = {
        RootKind.GLOBAL_READONLY: tmp_path / "global",
        RootKind.OWNER_WRITABLE: tmp_path / "owner",
        RootKind.WORKSPACE: tmp_path / "workspace",
        RootKind.TEMPORARY: tmp_path / "tmp",
    }
    roots = {}
    for kind, path in locations.items():
        path.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_RDONLY)
        roots[kind] = ControlledRoot(kind, descriptor, kind is not RootKind.GLOBAL_READONLY, path.resolve())
    (locations[RootKind.WORKSPACE] / "default").mkdir()
    return ControlledRoots(roots), locations


def _launch_spec(**kwargs):
    environment = kwargs["environment"]
    return BubblewrapLaunchSpec(
        ("/trusted/bwrap", "--unshare-pid", "--bind-fd", str(kwargs["workspace_fd"]), "/workspace", "--", "python"),
        "/trusted/bwrap",
        Path(environment["HERMES_EXECUTOR_HOME"]),
    )


_SECURITY_POLICY = SandboxSecurityPolicy(
    "executor-bwrap-v1", "bubblewrap-seccomp-v1", 1000, 1000, "executor-default-v1", "sha256:" + "b" * 64
)
_POLICY = SandboxVerificationPolicy("sha256:" + "a" * 64, "isolated-tool-network", _SECURITY_POLICY)


def _syscall_filter(binding, policy):
    del binding
    fd = os.open(os.devnull, os.O_RDONLY)
    return SandboxSyscallFilter(fd, policy.syscall_policy_id, policy.syscall_policy_digest)


def _record(binding, mount_policy, invocation, *, observed_at=90, expires_at=110, **overrides):
    identity = binding.identity
    values = {
        "schema_version": 1, "verifier": "host-supervisor", "observed_at": observed_at, "expires_at": expires_at,
        "image_digest": _POLICY.image_digest, "profile": _SECURITY_POLICY.profile,
        "security_backend": _SECURITY_POLICY.backend, "syscall_policy_id": _SECURITY_POLICY.syscall_policy_id,
        "syscall_policy_digest": _SECURITY_POLICY.syscall_policy_digest,
        "owner_key": identity.owner_key, "worker_id": identity.worker_id,
        "worker_generation": identity.worker_generation, "lease_version": identity.lease_version,
        "recovery_generation": identity.recovery_generation, "executor_id": identity.executor_id,
        "executor_generation": identity.executor_generation, "sandbox_id": binding.sandbox_id,
        "uid": _SECURITY_POLICY.uid, "gid": _SECURITY_POLICY.gid, "mount_view_id": binding.mount_view_id,
        "mount_policy_id": mount_policy.mount_policy_id, "tmpfs_id": binding.tmpfs_id,
        "security_subject_id": binding.security_subject_id,
        "network_mode": _POLICY.network_mode, "egress_profile": invocation.egress_profile,
        "rootfs_readonly": True, "no_new_privileges": True, "capabilities_dropped": True,
        "namespaces": ("user", "pid", "ipc", "net"),
    }
    values.update(overrides)
    return SandboxVerificationRecord(**values)


def _supervisor(
    tmp_path, process_factory, *, sandbox_builder=_launch_spec, verification_source=_record,
    egress_policy=None, audit_reporter=None, clock=lambda: 100,
):
    roots, locations = _roots(tmp_path)
    lease = OwnerWorkerAuthorityLease("ok1_owner", 1, "worker-a", WorkerLeaseState.ACTIVE, 1, 0)
    return roots, ToolExecutorSupervisor(
        owner_home=locations[RootKind.OWNER_WRITABLE],
        workspace_context=AuthenticatedWorkspaceContext(roots),
        lease=lease,
        process_factory=process_factory,
        sandbox_builder=sandbox_builder,
        sandbox_verification_source=verification_source,
        sandbox_verification_policy=_POLICY,
        sandbox_syscall_filter_source=_syscall_filter,
        egress_policy=egress_policy,
        audit_reporter=audit_reporter,
        clock=clock,
    )


def test_executor_rejection_audit_is_pre_spawn_and_deidentified(tmp_path):
    spawned = []
    audit_events = []
    roots, supervisor = _supervisor(
        tmp_path,
        lambda *args, **kwargs: spawned.append((args, kwargs)),
        audit_reporter=lambda event, reason, identity: audit_events.append((event, reason)),
    )
    workspace_fd_calls = []
    try:
        supervisor._workspace_fd = lambda: workspace_fd_calls.append(True)  # type: ignore[method-assign]
        supervisor.resource_decision_source = lambda identity: None
        with pytest.raises(Exception, match="resource decision"):
            supervisor.dispatch(
                function_name="tool-argument-sentinel",
                function_args={"path": "/owner/home/path-sentinel", "capability": "capability-sentinel"},
                task_id="task-a", session_id="session-a", tool_call_id="call-a", turn_id="turn-a", api_request_id="request-a",
            )
    finally:
        roots.close()
    assert spawned == []
    assert workspace_fd_calls == []
    assert audit_events == [
        (AuthorityAuditEvent.RESOURCE_REJECTED, AuthorityAuditReason.RESOURCE_DECISION_INVALID),
    ]
    serialized = repr(audit_events)
    for forbidden in ("tool-argument-sentinel", "/owner/home/path-sentinel", "capability-sentinel", "ok1_owner"):
        assert forbidden not in serialized


def test_supervisor_rejects_missing_or_foreign_resource_decision_before_launch_preparation(tmp_path):
    spawned = []
    roots, supervisor = _supervisor(tmp_path, lambda *args, **kwargs: spawned.append((args, kwargs)))
    workspace_fd_calls = []
    try:
        supervisor._workspace_fd = lambda: workspace_fd_calls.append(True)  # type: ignore[method-assign]
        supervisor.resource_decision_source = lambda identity: None
        with pytest.raises(Exception, match="resource decision"):
            supervisor.dispatch(
                function_name="read_file", function_args={}, task_id="task-a", session_id="session-a",
                tool_call_id="call-a", turn_id="turn-a", api_request_id="request-a",
            )

        def foreign(identity):
            other = supervisor.identity_for(task_id="task-b", session_id="session-b")
            return default_executor_resource_decision(other)

        supervisor.resource_decision_source = foreign
        with pytest.raises(Exception, match="resource decision"):
            supervisor.dispatch(
                function_name="read_file", function_args={}, task_id="task-a", session_id="session-a",
                tool_call_id="call-a", turn_id="turn-a", api_request_id="request-a",
            )
    finally:
        roots.close()
    assert spawned == []
    assert workspace_fd_calls == []


def test_supervisor_uses_sandbox_fd_bootstrap_and_no_preexec_cwd(tmp_path):
    spawned = []
    received = []

    def fake_process_factory(*args, **kwargs):
        spawned.append((args, kwargs))
        request_fd = os.dup(int(kwargs["env"]["HERMES_EXECUTOR_BOOTSTRAP_FD"]))
        response_fd = os.dup(int(kwargs["env"]["HERMES_EXECUTOR_RESPONSE_FD"]))

        def respond():
            raw = os.read(request_fd, 1 << 20)
            payload = json.loads(raw.decode("utf-8"))
            received.append(payload)
            os.write(response_fd, json.dumps({"result": json.dumps({"ok": payload["tool_name"]})}).encode("utf-8"))
            os.close(request_fd)
            os.close(response_fd)

        threading.Thread(target=respond, daemon=True).start()
        return _FakeProcess()

    roots, supervisor = _supervisor(tmp_path, fake_process_factory)
    try:
        result = supervisor.dispatch(
            function_name="read_file", function_args={"path": "/host/owner-a/secret.txt", "token": "task-only-sentinel"}, task_id="task-a", session_id="session-a",
            tool_call_id="call-a", turn_id="turn-a", api_request_id="request-a",
        )
    finally:
        roots.close()

    assert json.loads(result) == {"ok": "read_file"}
    kwargs = spawned[0][1]
    assert spawned[0][0][0][0] == "/trusted/bwrap"
    assert kwargs["close_fds"] is True
    assert kwargs["stdin"] is not None
    assert kwargs["stdout"] is not None
    assert kwargs["stderr"] is not None
    assert len(kwargs["pass_fds"]) == 4
    assert len(set(kwargs["pass_fds"])) == 4
    assert "cwd" not in kwargs
    assert "preexec_fn" not in kwargs
    assert kwargs["env"]["HERMES_EXECUTOR_RUNTIME"] == "1"
    assert "HERMES_OWNER_KEY" not in kwargs["env"]
    assert "HERMES_CONTROL_HOME" not in kwargs["env"]
    assert kwargs["env"]["HOME"] == "/executor"
    assert kwargs["env"]["TMPDIR"] == "/executor/tmp"
    spawn_text = json.dumps({"argv": spawned[0][0][0], "env": kwargs["env"]})
    assert "/host/owner-a/secret.txt" not in spawn_text
    assert "task-only-sentinel" not in spawn_text
    assert received[0]["arguments"] == {"path": "/host/owner-a/secret.txt", "token": "task-only-sentinel"}
    assert supervisor._live == {}


@pytest.mark.parametrize("profile", ["tool-none", "tool-public", "protected-target"])
def test_supervisor_selects_only_trusted_tool_egress_profiles(tmp_path, profile):
    selected = []

    def capture_record(binding, mount_policy, invocation):
        selected.append(invocation.egress_profile)
        return _record(binding, mount_policy, invocation)

    def fake_process_factory(*args, **kwargs):
        request_fd = os.dup(int(kwargs["env"]["HERMES_EXECUTOR_BOOTSTRAP_FD"]))
        response_fd = os.dup(int(kwargs["env"]["HERMES_EXECUTOR_RESPONSE_FD"]))
        def respond():
            os.read(request_fd, 1 << 20)
            os.write(response_fd, b'{"result":"ok"}')
            os.close(request_fd)
            os.close(response_fd)
        threading.Thread(target=respond, daemon=True).start()
        return _FakeProcess()

    roots, supervisor = _supervisor(
        tmp_path, fake_process_factory, verification_source=capture_record,
        egress_policy=ExecutorEgressPolicy({"read_file": profile}),
    )
    try:
        assert supervisor.dispatch(
            function_name="read_file", function_args={}, task_id="task-a", session_id="session-a",
            tool_call_id="call-a", turn_id="turn-a", api_request_id="request-a",
        ) == "ok"
    finally:
        roots.close()
    assert [value.value for value in selected] == [profile]


def test_egress_policy_copies_source_mapping_and_defaults_unmapped_tools():
    source = {"read_file": "tool-public"}
    policy = ExecutorEgressPolicy(source)
    source["read_file"] = "protected-target"
    source["write_file"] = "tool-public"

    assert policy.select("read_file").value == "tool-public"
    assert policy.select("write_file").value == "tool-none"
    with pytest.raises(TypeError):
        policy.by_tool_name["write_file"] = "tool-public"


def test_dispatch_has_no_caller_selected_egress_profile_argument(tmp_path):
    spawned = []
    roots, supervisor = _supervisor(tmp_path, lambda *args, **kwargs: spawned.append((args, kwargs)))
    try:
        with pytest.raises(TypeError, match="egress_profile"):
            supervisor.dispatch(
                function_name="read_file", function_args={}, task_id="task-a", session_id="session-a",
                tool_call_id="call-a", turn_id="turn-a", api_request_id="request-a", egress_profile="tool-public",
            )
    finally:
        roots.close()
    assert spawned == []


@pytest.mark.parametrize("profile", ["control-only", "owner-public"])
def test_supervisor_rejects_non_tool_egress_profile_before_launch_preparation(tmp_path, profile):
    spawned = []
    roots, supervisor = _supervisor(
        tmp_path, lambda *args, **kwargs: spawned.append((args, kwargs)),
        egress_policy=ExecutorEgressPolicy({"read_file": profile}),
    )
    workspace_fd_calls = []
    try:
        supervisor._workspace_fd = lambda: workspace_fd_calls.append(True)  # type: ignore[method-assign]
        with pytest.raises(Exception, match="not allowed"):
            supervisor.dispatch(
                function_name="read_file", function_args={}, task_id="task-a", session_id="session-a",
                tool_call_id="call-a", turn_id="turn-a", api_request_id="request-a",
            )
        identity = supervisor.identity_for(task_id="task-a", session_id="session-a")
        assert not (supervisor.owner_home / "runtime" / "executors" / identity.executor_id).exists()
    finally:
        roots.close()
    assert spawned == []
    assert workspace_fd_calls == []



@pytest.mark.parametrize("verification_source", [
    None, lambda binding, mount_policy, invocation: None,
    lambda binding, mount_policy, invocation: (_ for _ in ()).throw(RuntimeError("offline")),
])
def test_supervisor_requires_trusted_verification_before_launch_preparation(tmp_path, verification_source):
    spawned = []
    roots, supervisor = _supervisor(tmp_path, lambda *args, **kwargs: spawned.append((args, kwargs)), verification_source=verification_source)
    workspace_fd_calls = []
    try:
        supervisor._workspace_fd = lambda: workspace_fd_calls.append(True)  # type: ignore[method-assign]
        with pytest.raises(Exception, match="verification"):
            supervisor.dispatch(
                function_name="read_file", function_args={}, task_id="task-a", session_id="session-a",
                tool_call_id="call-a", turn_id="turn-a", api_request_id="request-a",
            )
    finally:
        roots.close()
    assert spawned == []
    assert workspace_fd_calls == []


def test_supervisor_rejects_missing_syscall_filter_before_launch_preparation(tmp_path):
    spawned = []
    roots, supervisor = _supervisor(tmp_path, lambda *args, **kwargs: spawned.append((args, kwargs)))
    workspace_fd_calls = []
    try:
        supervisor.sandbox_syscall_filter_source = None
        supervisor._workspace_fd = lambda: workspace_fd_calls.append(True)  # type: ignore[method-assign]
        with pytest.raises(Exception, match="syscall filter"):
            supervisor.dispatch(
                function_name="read_file", function_args={}, task_id="task-a", session_id="session-a",
                tool_call_id="call-a", turn_id="turn-a", api_request_id="request-a",
            )
        identity = supervisor.identity_for(task_id="task-a", session_id="session-a")
        assert not (supervisor.owner_home / "runtime" / "executors" / identity.executor_id).exists()
    finally:
        roots.close()
    assert spawned == []
    assert workspace_fd_calls == []


def test_supervisor_rejects_wrong_mount_policy_evidence_before_launch_preparation(tmp_path):
    spawned = []

    def wrong_mount_policy(binding, mount_policy, invocation):
        return _record(binding, mount_policy, invocation, mount_policy_id="wrong-policy")

    roots, supervisor = _supervisor(tmp_path, lambda *args, **kwargs: spawned.append((args, kwargs)), verification_source=wrong_mount_policy)
    workspace_fd_calls = []
    try:
        supervisor._workspace_fd = lambda: workspace_fd_calls.append(True)  # type: ignore[method-assign]
        with pytest.raises(Exception, match="does not match"):
            supervisor.dispatch(
                function_name="read_file", function_args={}, task_id="task-a", session_id="session-a",
                tool_call_id="call-a", turn_id="turn-a", api_request_id="request-a",
            )
        identity = supervisor.identity_for(task_id="task-a", session_id="session-a")
        assert not (supervisor.owner_home / "runtime" / "executors" / identity.executor_id).exists()
    finally:
        roots.close()
    assert spawned == []
    assert workspace_fd_calls == []


def test_supervisor_fails_closed_without_sandbox_before_spawning(tmp_path):
    spawned = []

    def unavailable(**kwargs):
        del kwargs
        raise ExecutorIsolationUnavailable("Bubblewrap is required")

    roots, supervisor = _supervisor(tmp_path, lambda *args, **kwargs: spawned.append((args, kwargs)), sandbox_builder=unavailable)
    try:
        with pytest.raises(ExecutorIsolationUnavailable, match="Bubblewrap"):
            supervisor.dispatch(
                function_name="read_file", function_args={}, task_id="task-a", session_id="session-a",
                tool_call_id="call-a", turn_id="turn-a", api_request_id="request-a",
            )
    finally:
        roots.close()
    assert spawned == []


def test_supervisor_uses_distinct_sandbox_bindings_for_cached_executor_identity(tmp_path):
    bindings = []

    def capture_binding(**kwargs):
        bindings.append(kwargs["binding"])
        return _launch_spec(**kwargs)

    def fake_process_factory(*args, **kwargs):
        request_fd = os.dup(int(kwargs["env"]["HERMES_EXECUTOR_BOOTSTRAP_FD"]))
        response_fd = os.dup(int(kwargs["env"]["HERMES_EXECUTOR_RESPONSE_FD"]))

        def respond():
            os.read(request_fd, 1 << 20)
            os.write(response_fd, b'{"result":"ok"}')
            os.close(request_fd)
            os.close(response_fd)

        threading.Thread(target=respond, daemon=True).start()
        return _FakeProcess()

    roots, supervisor = _supervisor(tmp_path, fake_process_factory, sandbox_builder=capture_binding)
    try:
        assert supervisor.dispatch(function_name="read_file", function_args={}, task_id="task-a", session_id="session-a", tool_call_id="call-a", turn_id="turn-a", api_request_id="request-a") == "ok"
        assert supervisor.dispatch(function_name="read_file", function_args={}, task_id="task-a", session_id="session-a", tool_call_id="call-b", turn_id="turn-b", api_request_id="request-b") == "ok"
    finally:
        roots.close()

    assert len(bindings) == 2
    assert bindings[0].identity == bindings[1].identity
    assert bindings[0].sandbox_id != bindings[1].sandbox_id
    assert bindings[0].mount_view_id != bindings[1].mount_view_id
    assert bindings[0].tmpfs_id != bindings[1].tmpfs_id
    assert bindings[0].security_subject_id != bindings[1].security_subject_id


@pytest.mark.parametrize("field,value", [
    ("owner_key", "ok1_other"),
    ("worker_id", "worker-b"),
    ("worker_generation", 2),
    ("lease_version", 2),
    ("recovery_generation", 1),
])
def test_supervisor_rejects_identity_that_does_not_match_active_lease_before_spawning(tmp_path, field, value):
    spawned = []
    roots, supervisor = _supervisor(tmp_path, lambda *args, **kwargs: spawned.append((args, kwargs)))
    try:
        identity = supervisor.identity_for(task_id="task-a", session_id="session-a")
        payload = identity.to_payload()
        payload[field] = value
        forged = type(identity).from_payload(payload)
        invocation = ExecutorInvocation(
            identity=forged, tool_name="read_file", arguments={}, tool_call_id="call-a", turn_id="turn-a",
            api_request_id="request-a", invocation_id="invocation-a", egress_profile="tool-none",
        )
        with pytest.raises(PermissionError, match="active owner-worker lease"):
            supervisor._dispatch_invocation(invocation)
    finally:
        roots.close()
    assert spawned == []


def test_invalid_executor_response_terminates_and_removes_live_process(tmp_path):
    def fake_process_factory(*args, **kwargs):
        request_fd = os.dup(int(kwargs["env"]["HERMES_EXECUTOR_BOOTSTRAP_FD"]))
        response_fd = os.dup(int(kwargs["env"]["HERMES_EXECUTOR_RESPONSE_FD"]))

        def respond():
            os.read(request_fd, 1 << 20)
            os.write(response_fd, b"not-json")
            os.close(request_fd)
            os.close(response_fd)

        threading.Thread(target=respond, daemon=True).start()
        return _FakeProcess()

    roots, supervisor = _supervisor(tmp_path, fake_process_factory)
    terminated = []
    try:
        with patch.object(supervisor, "_terminate", side_effect=lambda process: terminated.append(process)):
            with pytest.raises(RuntimeError, match="invalid response"):
                supervisor.dispatch(
                    function_name="read_file", function_args={}, task_id="task-a", session_id="session-a",
                    tool_call_id="call-a", turn_id="turn-a", api_request_id="request-a",
                )
        assert len(terminated) == 1
        assert supervisor._live == {}
    finally:
        roots.close()


def test_revoke_executor_and_generation_stop_only_terminate_matching_live_processes(tmp_path):
    roots, supervisor = _supervisor(tmp_path, lambda *args, **kwargs: _FakeProcess())
    reaped = []
    try:
        identity = supervisor.identity_for(task_id="task-a", session_id="session-a")
        other = supervisor.identity_for(task_id="task-b", session_id="session-b")
        first_grant = supervisor.credential_broker.issue(
            identity, audience=AUD_PROCESS_REGISTRY, operation="process.read", scope="proc-a"
        )
        second_grant = supervisor.credential_broker.issue(
            other, audience=AUD_PROCESS_REGISTRY, operation="process.read", scope="proc-b"
        )
        supervisor._live[(identity.stable_key, "one")] = type("Live", (), {"identity": identity, "invocation_id": "one", "process": _FakeProcess()})()
        supervisor._live[(other.stable_key, "two")] = type("Live", (), {"identity": other, "invocation_id": "two", "process": _FakeProcess()})()
        terminated = []
        with patch.object(supervisor, "_terminate", side_effect=lambda process: terminated.append(process)), \
             patch.object(supervisor, "_reap_registry_descendants", side_effect=reaped.append):
            assert supervisor.revoke_executor(identity) == 1
            assert list(supervisor._live) == [(other.stable_key, "two")]
            with pytest.raises(ExecutorCapabilityInvalid, match="revoked_or_unknown"):
                supervisor.credential_broker.validate(
                    first_grant.capability, identity, audience=AUD_PROCESS_REGISTRY, operation="process.read", scope="proc-a"
                )
            assert supervisor.stop_generation() == 1
        assert len(terminated) == 2
        assert reaped == [identity, identity, other]
        assert supervisor.credential_broker.active_grant_count == 0
        with pytest.raises(ExecutorCapabilityInvalid, match="revoked_or_unknown"):
            supervisor.credential_broker.validate(
                second_grant.capability, other, audience=AUD_PROCESS_REGISTRY, operation="process.read", scope="proc-b"
            )
        with pytest.raises(PermissionError, match="revoked"):
            supervisor.identity_for(task_id="task-a", session_id="session-a")
    finally:
        roots.close()
