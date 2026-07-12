from __future__ import annotations

import json
import os
from dataclasses import replace
from unittest.mock import MagicMock, patch

import pytest

from hermes_cli.dashboard_auth.authority import OwnerWorkerAuthorityLease, WorkerLeaseState
from hermes_cli.owner_worker.executor_identity import ExecutorIdentity
from tools.process_registry import ProcessRegistry, ProcessSession


def _identity(*, owner="ok1_owner_a", generation=1):
    lease = OwnerWorkerAuthorityLease(owner, generation, "worker-a", WorkerLeaseState.ACTIVE, 1, 0)
    return ExecutorIdentity.for_task(
        lease, workspace_prefix="default", task_id="task-a", session_id="session-a", executor_id="executor-a"
    )


def _session(identity):
    return ProcessSession(
        id="proc-auth", command="echo hello", task_id=identity.task_id, session_key=identity.session_id,
        authenticated_executor=True, executor_owner_digest=identity.owner_digest,
        executor_workspace_prefix=identity.workspace_prefix, executor_worker_id=identity.worker_id,
        executor_worker_generation=identity.worker_generation, executor_id=identity.executor_id,
        executor_generation=identity.executor_generation, executor_identity=identity,
        output_buffer="owner output",
    )


def test_authenticated_output_and_control_require_exact_executor_identity():
    registry = ProcessRegistry()
    identity = _identity()
    session = _session(identity)
    registry._running[session.id] = session
    other = _identity(owner="ok1_owner_b")

    assert registry.poll(session.id)["status"] == "not_found"
    assert registry.read_log(session.id, executor_identity=other)["status"] == "not_found"
    assert registry.wait(session.id, timeout=1, executor_identity=other)["status"] == "not_found"
    assert registry.kill_process(session.id, executor_identity=other)["status"] == "not_found"
    assert registry.poll(session.id, executor_identity=identity)["output_preview"] == "owner output"


@pytest.mark.parametrize(
    "foreign",
    [
        lambda identity: replace(identity, lease_version=identity.lease_version + 1),
        lambda identity: replace(identity, recovery_generation=identity.recovery_generation + 1),
        lambda identity: replace(identity, worker_generation=identity.worker_generation + 1),
        lambda identity: replace(identity, executor_generation=identity.executor_generation + 1),
    ],
)
def test_authenticated_stdin_and_terminal_controls_require_exact_identity(foreign):
    registry = ProcessRegistry()
    identity = _identity()
    session = _session(identity)
    pty = MagicMock()
    session._pty = pty
    registry._running[session.id] = session
    closed = []
    registry.on_close = lambda candidate, process_id: closed.append((candidate, process_id))

    other = foreign(identity)
    for action in (
        lambda: registry.write_stdin(session.id, "secret", executor_identity=other),
        lambda: registry.submit_stdin(session.id, "secret", executor_identity=other),
        lambda: registry.close_stdin(session.id, executor_identity=other),
        lambda: registry.request_close_terminal(session.id, executor_identity=other),
    ):
        assert action()["status"] == "not_found"
    assert not pty.write.called
    assert not pty.sendeof.called
    assert closed == []

    assert registry.write_stdin(session.id, "ok", executor_identity=identity)["status"] == "ok"
    assert registry.close_stdin(session.id, executor_identity=identity)["status"] == "ok"
    assert registry.request_close_terminal(session.id, executor_identity=identity)["status"] == "ok"
    assert closed == [(session, session.id)]


def test_authenticated_checkpoint_identity_round_trip_and_invalid_records_fail_closed(tmp_path, monkeypatch):
    import tools.process_registry as pr_mod

    identity = _identity()
    registry = ProcessRegistry()
    session = _session(identity)
    session.pid = os.getpid()
    session.host_start_time = registry._safe_host_start_time(session.pid)
    registry._running[session.id] = session
    checkpoint = tmp_path / "processes.json"
    monkeypatch.setattr(pr_mod, "CHECKPOINT_PATH", checkpoint)
    registry._write_checkpoint()

    recovered = ProcessRegistry()
    assert recovered.recover_from_checkpoint() == 1
    restored = recovered.get(session.id)
    assert restored is not None
    assert restored.executor_identity == identity
    assert recovered.poll(session.id, executor_identity=replace(identity, lease_version=2))["status"] == "not_found"

    entry = json.loads(checkpoint.read_text())[0]
    entry["executor_identity"] = {"owner_key": "broken"}
    checkpoint.write_text(json.dumps([entry]))
    malformed = ProcessRegistry()
    assert malformed.recover_from_checkpoint() == 0
    assert malformed.get(session.id) is None
    assert malformed.pending_watchers == []


def test_kill_executor_generation_targets_only_exact_fence(monkeypatch):
    registry = ProcessRegistry()
    identity = _identity()
    same = _session(identity)
    same.id = "proc-same"
    other = _session(_identity(generation=2))
    other.id = "proc-other"
    registry._running = {same.id: same, other.id: other}
    killed = []

    def fake_kill(session_id, **kwargs):
        killed.append((session_id, kwargs["executor_identity"]))
        return {"status": "killed"}

    monkeypatch.setattr(registry, "kill_process", fake_kill)
    assert registry.kill_executor_generation(identity) == 1
    assert killed == [("proc-same", identity)]


def test_authenticated_spawn_uses_explicit_env_and_close_fds(tmp_path, monkeypatch):
    registry = ProcessRegistry()
    identity = _identity()
    captured = {}
    process = MagicMock(pid=12345)
    monkeypatch.setattr(registry, "_safe_host_start_time", lambda pid: 1)
    monkeypatch.setattr("tools.process_registry.threading.Thread", lambda *args, **kwargs: MagicMock(start=lambda: None))
    monkeypatch.setattr("tools.process_registry.subprocess.Popen", lambda *args, **kwargs: captured.update(kwargs) or process)

    session = registry.spawn_authenticated(
        "echo hello", cwd=str(tmp_path), task_id="task-a", session_key="session-a", executor_identity=identity,
        env_vars={"PATH": "/usr/bin", "HOME": str(tmp_path)},
    )

    assert session.authenticated_executor is True
    assert session.executor_owner_digest == identity.owner_digest
    assert captured["close_fds"] is True
    assert captured["env"] == {
        "HOME": "/executor", "TMPDIR": "/executor/tmp", "PATH": "/usr/bin",
        "LANG": "C.UTF-8", "PYTHONUNBUFFERED": "1", "PYTHONNOUSERSITE": "1",
    }
    assert "pass_fds" not in captured
    assert captured["stdin"] is not None


def test_authenticated_spawn_rejects_authority_socket_and_unknown_environment(tmp_path, monkeypatch):
    registry = ProcessRegistry()
    identity = _identity()
    spawned = []
    monkeypatch.setattr("tools.process_registry.subprocess.Popen", lambda *args, **kwargs: spawned.append((args, kwargs)))
    forbidden = (
        "HERMES_CONTROL_HOME", "HERMES_OWNER_WORKER_CAPABILITY_PUBLIC_KEY", "HERMES_DASHBOARD_SESSION_TOKEN",
        "HERMES_REPLAY_STATE", "HERMES_SUPERVISOR_METADATA", "SSH_AUTH_SOCK", "DOCKER_HOST",
        "HERMES_EXECUTOR_BOOTSTRAP_FD", "ARBITRARY_INHERITED_VALUE",
    )

    for key in forbidden:
        with pytest.raises(ValueError, match="environment"):
            registry.spawn_authenticated(
                "echo hello", cwd=str(tmp_path), task_id="task-a", session_key="session-a", executor_identity=identity,
                env_vars={key: "forbidden"},
            )
    assert spawned == []
