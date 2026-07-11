from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

from hermes_cli.dashboard_auth.authority import AuthorityStore, WorkerGenerationState, WorkerLeaseState
from hermes_cli.dashboard_auth.base import Session
from hermes_cli.dashboard_auth.owner_context import owner_context_from_session
from hermes_cli.owner_runtime import REQUIRED_OWNER_DIRS, ensure_owner_runtime_dirs, owner_worker_socket_path
from hermes_cli.owner_worker import OwnerWorkerClient, OwnerWorkerSupervisor
from hermes_cli.owner_worker.tokens import (
    AUD_OWNER_WORKER_HTTP,
    AUD_OWNER_WORKER_WS,
    SCOPE_OWNER_WORKER_HTTP,
    SCOPE_OWNER_WORKER_WS,
    OwnerWorkerCapabilityInvalid,
    child_token_ttl_seconds,
    admit_owner_worker_bootstrap,
    mint_owner_worker_bootstrap,
    mint_owner_worker_capability,
    owner_worker_capability_public_config,
    owp1_data,
    parse_owp1_data,
    parse_owner_worker_bootstrap,
    verify_owner_worker_capability,
)


@dataclass(frozen=True)
class _Owner:
    owner_key: str
    owner_home: Path
    tenant_id: str = "tenant-1"
    owner_user_id: str = "user-1"
    auth_provider: str = "test"


class _FakeProcess:
    returncode = None

    def __init__(self, pid: int = 4321) -> None:
        self.pid = pid
        self.terminated = False
        self.killed = False
        self.wait_calls = 0

    def poll(self):
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 143

    def kill(self) -> None:
        self.killed = True
        self.returncode = 137

    def wait(self, timeout=None):
        self.wait_calls += 1
        return self.returncode


class _FakeClient:
    def __init__(self, socket_path, *, control_home=None, timeout=2.0) -> None:
        self.socket_path = Path(socket_path)
        self.control_home = control_home

    def verify_health(self, *, owner_key: str, owner_home, worker_generation=None, worker_id=None, **_kwargs):
        return {
            "ready": True,
            "owner_key": owner_key,
            "owner_home": str(Path(owner_home).resolve()),
            "worker_generation": worker_generation,
            "worker_id": worker_id,
            "pid": 4321,
            "hermes_home": str(Path(owner_home).resolve()),
        }


def _capability_for(
    app,
    path: str,
    *,
    audience=AUD_OWNER_WORKER_HTTP,
    scope=SCOPE_OWNER_WORKER_HTTP,
    control_home=None,
) -> str:
    return mint_owner_worker_capability(
        app.state.owner_worker_lease,
        audience=audience,
        scope=scope,
        path=path,
        control_home=control_home or app.state.owner_worker_control_home,
    )


def _active_lease(tmp_path, *, owner_key="ok1_owner_a", worker_id="worker-a"):
    store = AuthorityStore(tmp_path / "control")
    claim = store.claim_worker_start(owner_key, worker_id=worker_id)
    active = store.transition_worker_lease(
        claim.lease,
        state=WorkerLeaseState.ACTIVE,
        generation_state=WorkerGenerationState.ACTIVE,
    )
    return store, active


def test_capability_is_exact_owner_generation_lease_scope_and_path_bound(tmp_path):
    store, lease = _active_lease(tmp_path)
    token = mint_owner_worker_capability(
        lease,
        audience=AUD_OWNER_WORKER_WS,
        scope=SCOPE_OWNER_WORKER_WS,
        path="/api/ws",
        ttl_seconds=2,
        control_home=tmp_path / "control",
    )
    verifier = owner_worker_capability_public_config(tmp_path / "control")
    kwargs = {
        "authority_store": store,
        "public_key": verifier["HERMES_OWNER_WORKER_CAPABILITY_PUBLIC_KEY"],
        "issuer_key_version": verifier["HERMES_OWNER_WORKER_CAPABILITY_ISSUER"],
    }
    claims = verify_owner_worker_capability(
        token, expected_lease=lease, audience=AUD_OWNER_WORKER_WS,
        scope=SCOPE_OWNER_WORKER_WS, path="/api/ws", **kwargs,
    )
    assert claims.owner_key == lease.owner_key
    assert claims.worker_generation == lease.worker_generation
    assert claims.worker_id == lease.worker_id

    for audience, scope, path in (
        (AUD_OWNER_WORKER_HTTP, SCOPE_OWNER_WORKER_HTTP, "/api/ws"),
        (AUD_OWNER_WORKER_WS, SCOPE_OWNER_WORKER_HTTP, "/api/ws"),
        (AUD_OWNER_WORKER_WS, SCOPE_OWNER_WORKER_WS, "/api/pub"),
    ):
        with pytest.raises(OwnerWorkerCapabilityInvalid, match="binding_mismatch"):
            verify_owner_worker_capability(token, expected_lease=lease, audience=audience, scope=scope, path=path, **kwargs)


def test_capability_rejects_legacy_ow2_and_stale_replacement(tmp_path):
    store, lease = _active_lease(tmp_path)
    token = mint_owner_worker_capability(
        lease, audience=AUD_OWNER_WORKER_HTTP, scope=SCOPE_OWNER_WORKER_HTTP,
        path="/internal/health", control_home=tmp_path / "control",
    )
    verifier = owner_worker_capability_public_config(tmp_path / "control")
    kwargs = {
        "authority_store": store,
        "public_key": verifier["HERMES_OWNER_WORKER_CAPABILITY_PUBLIC_KEY"],
        "issuer_key_version": verifier["HERMES_OWNER_WORKER_CAPABILITY_ISSUER"],
    }
    with pytest.raises(OwnerWorkerCapabilityInvalid):
        verify_owner_worker_capability("ow2.invalid.signature", expected_lease=lease, audience=AUD_OWNER_WORKER_HTTP, scope=SCOPE_OWNER_WORKER_HTTP, path="/internal/health", **kwargs)
    store.invalidate_outstanding_credentials(reason="replace")
    store.claim_worker_start(lease.owner_key, worker_id="worker-b")
    with pytest.raises(OwnerWorkerCapabilityInvalid, match="lease_invalid"):
        verify_owner_worker_capability(token, expected_lease=lease, audience=AUD_OWNER_WORKER_HTTP, scope=SCOPE_OWNER_WORKER_HTTP, path="/internal/health", **kwargs)


def test_bootstrap_is_exact_lease_bound_and_consumed_once(tmp_path):
    store, lease = _active_lease(tmp_path)
    control_home = tmp_path / "control"
    verifier = owner_worker_capability_public_config(control_home)
    token = mint_owner_worker_bootstrap(
        lease,
        path="/api/ws",
        connection_id="connection_identifier_1234",
        nonce="control_nonce_identifier_1234",
        control_home=control_home,
    )
    kwargs = {
        "expected_lease": lease,
        "path": "/api/ws",
        "authority_store": store,
        "public_key": verifier["HERMES_OWNER_WORKER_CAPABILITY_PUBLIC_KEY"],
        "issuer_key_version": verifier["HERMES_OWNER_WORKER_CAPABILITY_ISSUER"],
    }
    claims = admit_owner_worker_bootstrap(token, **kwargs)
    assert claims.connection_id == "connection_identifier_1234"
    assert claims.nonce == "control_nonce_identifier_1234"
    with pytest.raises(OwnerWorkerCapabilityInvalid, match="replay"):
        admit_owner_worker_bootstrap(token, **kwargs)
    with pytest.raises(OwnerWorkerCapabilityInvalid, match="binding_mismatch"):
        parse_owner_worker_bootstrap(
            token,
            expected_lease=lease,
            path="/api/pub",
            public_key=kwargs["public_key"],
            issuer_key_version=kwargs["issuer_key_version"],
        )


def test_owp1_data_requires_exact_peer_and_monotonic_sequence(tmp_path):
    _store, lease = _active_lease(tmp_path)
    verifier = owner_worker_capability_public_config(tmp_path / "control")
    claims = parse_owner_worker_bootstrap(
        mint_owner_worker_bootstrap(
            lease,
            path="/api/ws",
            connection_id="connection_identifier_1234",
            nonce="control_nonce_identifier_1234",
            control_home=tmp_path / "control",
        ),
        expected_lease=lease,
        path="/api/ws",
        public_key=verifier["HERMES_OWNER_WORKER_CAPABILITY_PUBLIC_KEY"],
        issuer_key_version=verifier["HERMES_OWNER_WORKER_CAPABILITY_ISSUER"],
    )
    envelope = owp1_data(
        claims, direction="control-to-worker", sequence=1, text="hello",
    )
    assert parse_owp1_data(
        envelope, claims, direction="control-to-worker", expected_sequence=1,
    ) == ("text", "hello")
    with pytest.raises(OwnerWorkerCapabilityInvalid, match="data_mismatch"):
        parse_owp1_data(envelope, claims, direction="control-to-worker", expected_sequence=2)
    with pytest.raises(OwnerWorkerCapabilityInvalid, match="data_mismatch"):
        parse_owp1_data(envelope, claims, direction="worker-to-control", expected_sequence=1)


def test_capability_ttl_is_bounded(tmp_path):
    _store, lease = _active_lease(tmp_path)
    with pytest.raises(ValueError, match="ttl"):
        mint_owner_worker_capability(
            lease, audience=AUD_OWNER_WORKER_HTTP, scope=SCOPE_OWNER_WORKER_HTTP,
            path="/internal/health", ttl_seconds=0, control_home=tmp_path / "control",
        )


def test_capability_accepts_only_configured_active_or_retained_verifier(tmp_path):
    store, lease = _active_lease(tmp_path)
    token = mint_owner_worker_capability(
        lease,
        audience=AUD_OWNER_WORKER_HTTP,
        scope=SCOPE_OWNER_WORKER_HTTP,
        path="/internal/health",
        control_home=tmp_path / "control",
    )
    verifier = owner_worker_capability_public_config(tmp_path / "control")
    claims = verify_owner_worker_capability(
        token,
        expected_lease=lease,
        audience=AUD_OWNER_WORKER_HTTP,
        scope=SCOPE_OWNER_WORKER_HTTP,
        path="/internal/health",
        authority_store=store,
        public_key=verifier["HERMES_OWNER_WORKER_CAPABILITY_PUBLIC_KEY"],
        issuer_key_version="replacement-key",
        retained_public_keys={
            "owc1-1": verifier["HERMES_OWNER_WORKER_CAPABILITY_PUBLIC_KEY"],
        },
    )
    assert claims.issuer_key_version == "owc1-1"
    with pytest.raises(OwnerWorkerCapabilityInvalid, match="issuer_mismatch"):
        verify_owner_worker_capability(
            token,
            expected_lease=lease,
            audience=AUD_OWNER_WORKER_HTTP,
            scope=SCOPE_OWNER_WORKER_HTTP,
            path="/internal/health",
            authority_store=store,
            public_key=verifier["HERMES_OWNER_WORKER_CAPABILITY_PUBLIC_KEY"],
            issuer_key_version="replacement-key",
        )


def test_child_token_ttl_is_bounded(monkeypatch):
    monkeypatch.setenv("HERMES_OWNER_WORKER_CHILD_TOKEN_TTL_SECONDS", str(99 * 60 * 60))

    assert child_token_ttl_seconds() == 24 * 60 * 60


def test_supervisor_spawns_the_canonical_session_owner_context(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "global"))
    monkeypatch.setenv("HERMES_OWNER_SECRET", "owner-secret")
    session = Session(
        user_id="user-a",
        email="a@example.test",
        display_name="A",
        org_id="org-a",
        provider="stub",
        expires_at=9999999999,
        access_token="a",
        refresh_token="r",
    )
    owner = owner_context_from_session(session)
    spawned: list[dict] = []

    def fake_process_factory(*args, **kwargs):
        spawned.append({"args": args, "kwargs": kwargs})
        argv = args[0]
        Path(argv[argv.index("--socket") + 1]).touch()
        return _FakeProcess()

    supervisor = OwnerWorkerSupervisor(
        control_home=tmp_path / "control",
        client_cls=_FakeClient,
        process_factory=fake_process_factory,
        startup_timeout=0.1,
    )

    supervisor.get_or_start(owner)

    argv = spawned[0]["args"][0]
    child_env = spawned[0]["kwargs"]["env"]
    assert argv[argv.index("--owner-key") + 1] == owner.owner_key
    assert argv[argv.index("--owner-home") + 1] == str(owner.owner_home)
    assert argv[argv.index("--tenant-id") + 1] == owner.tenant_id
    assert argv[argv.index("--owner-user-id") + 1] == owner.owner_user_id
    assert argv[argv.index("--auth-provider") + 1] == owner.auth_provider
    assert child_env["HERMES_OWNER_KEY"] == owner.owner_key
    assert child_env["HERMES_HOME"] == str(owner.owner_home)
    assert child_env["HERMES_WORKER_GENERATION"] == "1"
    assert child_env["HERMES_WORKER_ID"]
    assert argv[argv.index("--worker-generation") + 1] == "1"
    assert argv[argv.index("--worker-id") + 1] == child_env["HERMES_WORKER_ID"]


def test_supervisor_restart_uses_new_generation_and_socket_with_reused_pid(tmp_path):
    owner = _Owner("ok1_restart", tmp_path / "owner")
    spawned: list[dict] = []

    def fake_process_factory(*args, **kwargs):
        spawned.append({"args": args, "kwargs": kwargs})
        argv = args[0]
        Path(argv[argv.index("--socket") + 1]).touch()
        return _FakeProcess(pid=4321)

    supervisor = OwnerWorkerSupervisor(
        control_home=tmp_path / "control",
        client_cls=_FakeClient,
        process_factory=fake_process_factory,
        startup_timeout=0.1,
        startup_cooldown=0,
    )
    first = supervisor.get_or_start(owner)
    supervisor._terminate_handle(owner.owner_key, first)
    second = supervisor.get_or_start(owner)

    assert first.pid == second.pid == 4321
    assert (first.worker_generation, second.worker_generation) == (1, 2)
    assert first.worker_id != second.worker_id
    assert first.socket_path != second.socket_path
    assert supervisor.authority_store.read_worker_generation(owner.owner_key, 1).state.value == "terminated"
    assert supervisor.authority_store.read_worker_generation(owner.owner_key, 2).state.value == "active"


def test_supervisor_rejects_same_key_different_owner_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "control-home"))
    monkeypatch.setenv("HERMES_PROFILE", "control-profile")
    monkeypatch.setenv("HERMES_SESSION_PROFILE", "control-session-profile")
    monkeypatch.setenv("HERMES_CONFIG", str(tmp_path / "control-config"))
    monkeypatch.setenv("HERMES_ENV", "control-env")
    monkeypatch.setenv("HERMES_WORKSPACE_ROOT", str(tmp_path / "control-workspaces"))
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path / "control-cwd"))
    monkeypatch.setenv("HERMES_OWNER_KEY", "ok1_control")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "global-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "global-secret")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example")
    monkeypatch.setenv("HERMES_OWNER_WORKER_ENV_ALLOWLIST", "HTTPS_PROXY")
    owner_a = _Owner("ok1_same", tmp_path / "a")
    owner_b = _Owner("ok1_same", tmp_path / "b")
    spawned: list[dict] = []

    def fake_process_factory(*args, **kwargs):
        spawned.append({"args": args, "kwargs": kwargs})
        argv = args[0]
        socket_path = Path(argv[argv.index("--socket") + 1])
        socket_path.touch()
        return _FakeProcess()

    supervisor = OwnerWorkerSupervisor(
        control_home=tmp_path / "control",
        client_cls=_FakeClient,
        process_factory=fake_process_factory,
        startup_timeout=0.1,
    )

    handle = supervisor.get_or_start(owner_a)
    assert handle.owner_key == "ok1_same"
    assert handle.owner_home == owner_a.owner_home.resolve()
    child_env = spawned[0]["kwargs"]["env"]
    assert child_env["HERMES_HOME"] == str(owner_a.owner_home.resolve())
    assert child_env["HERMES_OWNER_KEY"] == "ok1_same"
    assert child_env["HERMES_WORKSPACE_ROOT"] == str(owner_a.owner_home.resolve() / "workspaces")
    assert child_env.get("HERMES_PROFILE") is None
    assert child_env.get("HERMES_SESSION_PROFILE") is None
    assert child_env.get("HERMES_CONFIG") is None
    assert child_env.get("HERMES_ENV") is None
    assert child_env.get("TERMINAL_CWD") is None
    assert child_env.get("HERMES_OWNER_WORKER_ENV_ALLOWLIST") is None
    assert child_env.get("ANTHROPIC_API_KEY") is None
    assert child_env.get("OPENAI_API_KEY") is None
    assert child_env.get("HTTPS_PROXY") == "http://proxy.example"
    assert child_env.get("PATH") == os.environ.get("PATH")
    assert spawned[0]["kwargs"]["stdout"] is not subprocess.DEVNULL
    assert spawned[0]["kwargs"]["stderr"] is not subprocess.DEVNULL
    assert (owner_a.owner_home / "runtime" / "logs" / "owner-worker.stdout.log").exists()
    assert (owner_a.owner_home / "runtime" / "logs" / "owner-worker.stderr.log").exists()
    expected_cwd = (owner_a.owner_home / "workspaces" / "default").resolve()
    assert spawned[0]["kwargs"]["cwd"] == str(expected_cwd)
    assert expected_cwd.relative_to(owner_a.owner_home.resolve())
    for rel in REQUIRED_OWNER_DIRS:
        assert (owner_a.owner_home / rel).is_dir()
    assert not (owner_a.owner_home / "memory").exists()

    with pytest.raises(RuntimeError, match="owner_home mismatch"):
        supervisor.get_or_start(owner_b)

    supervisor._terminate_handle("ok1_same", handle)
    assert handle.process.wait_calls >= 1


def test_supervisor_closes_log_handles_when_spawn_fails(tmp_path):
    owner = _Owner("ok1_spawn_fail", tmp_path / "owner")
    captured: dict[str, object] = {}

    def fake_process_factory(*args, **kwargs):
        captured.update(kwargs)
        raise RuntimeError("spawn failed")

    supervisor = OwnerWorkerSupervisor(
        control_home=tmp_path / "control",
        client_cls=_FakeClient,
        process_factory=fake_process_factory,
        startup_timeout=0.1,
        startup_cooldown=0,
    )

    with pytest.raises(RuntimeError, match="spawn failed"):
        supervisor.get_or_start(owner)

    assert getattr(captured["stdout"], "closed") is True
    assert getattr(captured["stderr"], "closed") is True


def test_supervisor_serializes_concurrent_start_for_same_owner(tmp_path):
    owner = _Owner("ok1_concurrent", tmp_path / "owner")
    spawned: list[dict] = []
    barrier = threading.Barrier(3)

    def fake_process_factory(*args, **kwargs):
        time.sleep(0.05)
        spawned.append({"args": args, "kwargs": kwargs})
        argv = args[0]
        socket_path = Path(argv[argv.index("--socket") + 1])
        socket_path.touch()
        return _FakeProcess()

    supervisor = OwnerWorkerSupervisor(
        control_home=tmp_path / "control",
        client_cls=_FakeClient,
        process_factory=fake_process_factory,
        startup_timeout=0.1,
        startup_cooldown=0,
    )
    results: list[OwnerWorkerSupervisor | object] = []
    errors: list[BaseException] = []

    def worker():
        try:
            barrier.wait(timeout=5)
            results.append(supervisor.get_or_start(owner))
        except BaseException as exc:  # pragma: no cover - makes thread errors visible
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert errors == []
    assert len(results) == 3
    assert len(spawned) == 1
    assert len({id(result) for result in results}) == 1


def test_competing_supervisors_admit_one_fenced_worker(tmp_path):
    owner = _Owner("ok1_shared", tmp_path / "owner")
    spawned: list[dict] = []
    barrier = threading.Barrier(3)
    results: list[object] = []
    errors: list[BaseException] = []

    def fake_process_factory(*args, **kwargs):
        spawned.append({"args": args, "kwargs": kwargs})
        argv = args[0]
        Path(argv[argv.index("--socket") + 1]).touch()
        return _FakeProcess()

    supervisors = [
        OwnerWorkerSupervisor(
            control_home=tmp_path / "control",
            client_cls=_FakeClient,
            process_factory=fake_process_factory,
            startup_timeout=0.1,
            startup_cooldown=0,
        )
        for _ in range(2)
    ]

    def start(supervisor):
        try:
            barrier.wait(timeout=5)
            results.append(supervisor.get_or_start(owner))
        except BaseException as exc:  # pragma: no cover - makes thread errors visible
            errors.append(exc)

    threads = [threading.Thread(target=start, args=(supervisor,)) for supervisor in supervisors]
    for thread in threads:
        thread.start()
    barrier.wait(timeout=5)
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()

    assert len(spawned) == 1
    assert len(results) == 1
    assert len(errors) == 1
    assert "already_owned" in str(errors[0])


def test_stale_supervisor_handle_cannot_fail_replacement(tmp_path):
    owner = _Owner("ok1_replaced", tmp_path / "owner")

    def fake_process_factory(*args, **kwargs):
        argv = args[0]
        Path(argv[argv.index("--socket") + 1]).touch()
        return _FakeProcess()

    first_supervisor = OwnerWorkerSupervisor(
        control_home=tmp_path / "control",
        client_cls=_FakeClient,
        process_factory=fake_process_factory,
        startup_timeout=0.1,
        startup_cooldown=0,
    )
    stale = first_supervisor.get_or_start(owner)
    first_supervisor.authority_store.invalidate_outstanding_credentials(reason="replacement")
    second_supervisor = OwnerWorkerSupervisor(
        control_home=tmp_path / "control",
        client_cls=_FakeClient,
        process_factory=fake_process_factory,
        startup_timeout=0.1,
        startup_cooldown=0,
    )
    replacement = second_supervisor.get_or_start(owner)

    first_supervisor._mark_handle_failed(stale)

    current = second_supervisor.authority_store.read_owner_worker_lease(owner.owner_key)
    assert current is not None
    assert current.worker_generation == replacement.worker_generation
    assert current.worker_id == replacement.worker_id
    assert current.state.value == "active"


def test_supervisor_fences_and_closes_bridges_before_terminating_exact_generation(tmp_path):
    owner = _Owner("ok1_revoked", tmp_path / "owner")
    events = []

    class _OrderedProcess(_FakeProcess):
        def terminate(self):
            events.append("terminate")
            super().terminate()

    def fake_process_factory(*args, **kwargs):
        argv = args[0]
        Path(argv[argv.index("--socket") + 1]).touch()
        return _OrderedProcess()

    def revoke_bridges(lease):
        events.append("bridges")
        assert lease.state is WorkerLeaseState.DRAINING
        with pytest.raises(Exception):
            supervisor.authority_store.assert_worker_lease(
                lease,
                states=frozenset({WorkerLeaseState.ACTIVE}),
            )

    supervisor = OwnerWorkerSupervisor(
        control_home=tmp_path / "control",
        client_cls=_FakeClient,
        process_factory=fake_process_factory,
        startup_timeout=0.1,
        startup_cooldown=0,
        generation_bridge_revoker=revoke_bridges,
    )
    handle = supervisor.get_or_start(owner)
    socket_path = handle.socket_path

    supervisor._terminate_handle(owner.owner_key, handle)

    assert events == ["bridges", "terminate"]
    assert not socket_path.exists()
    assert not socket_path.parent.exists()
    assert supervisor.authority_store.read_worker_generation(owner.owner_key, 1).state is WorkerGenerationState.TERMINATED
    assert supervisor.authority_store.read_owner_worker_lease(owner.owner_key).state is WorkerLeaseState.REVOKED


def test_supervisor_does_not_mark_unconfirmed_process_exit_terminated(tmp_path):
    owner = _Owner("ok1_hung", tmp_path / "owner")

    class _HungProcess(_FakeProcess):
        def poll(self):
            return None

        def wait(self, timeout=None):
            self.wait_calls += 1
            raise subprocess.TimeoutExpired("owner-worker", timeout)

    def fake_process_factory(*args, **kwargs):
        argv = args[0]
        Path(argv[argv.index("--socket") + 1]).touch()
        return _HungProcess()

    supervisor = OwnerWorkerSupervisor(
        control_home=tmp_path / "control",
        client_cls=_FakeClient,
        process_factory=fake_process_factory,
        startup_timeout=0.1,
        startup_cooldown=0,
    )
    handle = supervisor.get_or_start(owner)

    supervisor._terminate_handle(owner.owner_key, handle)

    assert handle.process.terminated and handle.process.killed
    assert handle.socket_path.exists()
    assert supervisor.authority_store.read_worker_generation(owner.owner_key, 1).state is WorkerGenerationState.REVOKED
    assert supervisor.authority_store.read_owner_worker_lease(owner.owner_key).state is WorkerLeaseState.REVOKED


def test_supervisor_active_use_skips_idle_stop_until_released(tmp_path):
    owner = _Owner("ok1_active", tmp_path / "owner")

    def fake_process_factory(*args, **kwargs):
        argv = args[0]
        socket_path = Path(argv[argv.index("--socket") + 1])
        socket_path.touch()
        return _FakeProcess()

    supervisor = OwnerWorkerSupervisor(
        control_home=tmp_path / "control",
        client_cls=_FakeClient,
        process_factory=fake_process_factory,
        startup_timeout=0.1,
        startup_cooldown=0,
        idle_timeout=1,
    )
    handle = supervisor.get_or_start(owner)
    lease = supervisor.acquire_use(handle)
    handle.last_used_at = 0

    supervisor._stop_idle(now=10)

    assert not handle.process.terminated
    assert handle.active_uses == 1

    lease.release()
    handle.last_used_at = 0
    supervisor._stop_idle(now=10)

    assert handle.process.terminated
    assert supervisor.authority_store.read_worker_generation(owner.owner_key, 1).state.value == "terminated"
    assert supervisor.authority_store.read_owner_worker_lease(owner.owner_key).state.value == "revoked"


def test_supervisor_evicts_only_idle_workers(tmp_path):
    owners = [_Owner("ok1_active", tmp_path / "active"), _Owner("ok1_idle", tmp_path / "idle")]

    def fake_process_factory(*args, **kwargs):
        argv = args[0]
        socket_path = Path(argv[argv.index("--socket") + 1])
        socket_path.touch()
        return _FakeProcess()

    supervisor = OwnerWorkerSupervisor(
        control_home=tmp_path / "control",
        client_cls=_FakeClient,
        process_factory=fake_process_factory,
        startup_timeout=0.1,
        startup_cooldown=0,
        idle_timeout=1,
    )
    active = supervisor.get_or_start(owners[0])
    idle = supervisor.get_or_start(owners[1])
    lease = supervisor.acquire_use(active)
    active.last_used_at = 0
    idle.last_used_at = 0

    supervisor._evict_oldest_idle(now=10)

    assert not active.process.terminated
    assert idle.process.terminated
    lease.release()


def test_worker_health_over_unix_socket_reports_owner_env(tmp_path):
    # macOS AF_UNIX paths are capped at 104 bytes. pytest's default temporary
    # root can exceed that before the owner runtime suffix is appended, so keep
    # this real-subprocess socket under a short, per-process temporary root.
    socket_root = Path("/tmp") / f"h{os.getpid():x}"
    socket_root.mkdir(mode=0o700, exist_ok=True)
    owner_home = socket_root / "u"
    control_home = socket_root / "c"
    socket_path = owner_worker_socket_path(owner_home, 1)
    owner_home.mkdir(parents=True)
    control_home.mkdir(parents=True)
    store = AuthorityStore(control_home)
    claim = store.claim_worker_start("ok1_worker", worker_id="subprocess-test-worker")
    verifier = owner_worker_capability_public_config(control_home)
    worker_env = {
        **os.environ,
        "HERMES_HOME": str(owner_home),
        "HERMES_OWNER_KEY": "ok1_worker",
        "HERMES_WORKSPACE_ROOT": str(owner_home / "workspaces"),
        "HERMES_CONTROL_HOME": str(control_home),
        "HERMES_WORKER_GENERATION": str(claim.lease.worker_generation),
        "HERMES_WORKER_ID": claim.lease.worker_id,
        "HERMES_WORKER_LEASE_VERSION": str(claim.lease.lease_version),
        "HERMES_WORKER_RECOVERY_GENERATION": str(claim.lease.recovery_generation),
        **verifier,
    }

    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "hermes_cli.owner_worker.entrypoint",
            "--owner-key",
            "ok1_worker",
            "--owner-home",
            str(owner_home),
            "--socket",
            str(socket_path),
            "--tenant-id",
            "tenant-1",
            "--owner-user-id",
            "user-1",
            "--auth-provider",
            "test",
            "--control-home",
            str(control_home),
            "--worker-generation",
            "1",
            "--worker-id",
            "subprocess-test-worker",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=worker_env,
    )
    try:
        deadline = time.monotonic() + 10
        last_error: Exception | None = None
        health = None
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                stdout, stderr = proc.communicate(timeout=1)
                raise AssertionError(f"worker exited early: {proc.returncode}\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}")
            if socket_path.exists():
                try:
                    store = AuthorityStore(control_home)
                    lease = store.read_owner_worker_lease("ok1_worker")
                    assert lease is not None
                    health = OwnerWorkerClient(socket_path, control_home=control_home).verify_health(
                        owner_key="ok1_worker",
                        owner_home=owner_home,
                        worker_generation=1,
                        worker_id="subprocess-test-worker",
                        lease_version=lease.lease_version,
                        recovery_generation=lease.recovery_generation,
                        lease=lease,
                    )
                    break
                except Exception as exc:
                    last_error = exc
            time.sleep(0.05)
        if health is None:
            raise AssertionError(f"worker did not become healthy: {last_error}")

        assert health["ready"] is True
        assert health["owner_key"] == "ok1_worker"
        assert Path(health["owner_home"]).resolve() == owner_home.resolve()
        assert Path(health["hermes_home"]).resolve() == owner_home.resolve()
        assert health["worker_generation"] == 1
        assert health["worker_id"] == "subprocess-test-worker"
        assert health["pid"] == proc.pid
        assert health["workspace_root"] == str((owner_home / "workspaces").resolve())
        assert health["forbidden_env_present"] == []

        transport = httpx.HTTPTransport(uds=str(socket_path))
        with httpx.Client(transport=transport, base_url="http://owner-worker") as client:
            response = client.get("/internal/health")
        assert response.status_code == 401
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        if socket_root.exists():
            import shutil

            shutil.rmtree(socket_root)


def test_worker_client_rejects_generation_or_identity_mismatch(tmp_path):
    class _MismatchClient(OwnerWorkerClient):
        def health(self, *, lease=None):
            owner_key = lease.owner_key if lease is not None else None
            return {
                "ready": True, "owner_key": owner_key, "owner_home": str(tmp_path / "owner"),
                "hermes_home": str(tmp_path / "owner"), "workspace_root": str(tmp_path / "owner" / "workspaces"),
                "worker_generation": 2, "worker_id": "wrong", "pid": 1, "forbidden_env_present": [],
            }

    with pytest.raises(Exception, match="generation mismatch"):
        _MismatchClient(tmp_path / "worker.sock").verify_health(
            owner_key="ok1_worker", owner_home=tmp_path / "owner", worker_generation=1, worker_id="expected",
            lease=type("Lease", (), {"owner_key": "ok1_worker"})(),
        )


def test_worker_create_app_fails_when_startup_self_check_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "wrong-owner"))
    monkeypatch.setenv("HERMES_OWNER_KEY", "ok1_worker_routes")
    monkeypatch.setenv("HERMES_CONTROL_HOME", str(tmp_path / "control"))
    from hermes_cli.owner_worker.entrypoint import create_app

    owner_home = ensure_owner_runtime_dirs(tmp_path / "owner")

    with pytest.raises(RuntimeError, match="startup self-check failed"):
        create_app("ok1_worker_routes", owner_home)


def test_worker_create_app_rejects_forbidden_environment_before_route_registration(tmp_path, monkeypatch):
    owner_home = ensure_owner_runtime_dirs(tmp_path / "owner")
    monkeypatch.setenv("HERMES_HOME", str(owner_home))
    monkeypatch.setenv("HERMES_OWNER_KEY", "ok1_worker_routes")
    monkeypatch.setenv("HERMES_CONTROL_HOME", str(tmp_path / "control"))
    monkeypatch.setenv("HERMES_CONFIG", str(tmp_path / "poisoned-config"))
    from hermes_cli.owner_worker.entrypoint import create_app

    with pytest.raises(RuntimeError, match="forbidden"):
        create_app("ok1_worker_routes", owner_home)


def test_worker_session_routes_require_owner_token(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "owner"))
    monkeypatch.setenv("HERMES_OWNER_KEY", "ok1_worker_routes")
    monkeypatch.setenv("HERMES_CONTROL_HOME", str(tmp_path / "control"))
    from hermes_cli.owner_worker.entrypoint import create_app

    owner_home = ensure_owner_runtime_dirs(tmp_path / "owner")
    app = create_app("ok1_worker_routes", owner_home)
    client = TestClient(app)

    assert client.get("/api/sessions").status_code == 401

    token = _capability_for(app, audience=AUD_OWNER_WORKER_HTTP, path="/api/sessions", control_home=tmp_path / "control")
    response = client.get("/api/sessions", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.json()["sessions"] == []

    other_store, other_lease = _active_lease(tmp_path, owner_key="ok1_other", worker_id="worker-other")
    del other_store
    wrong = mint_owner_worker_capability(
        other_lease,
        audience=AUD_OWNER_WORKER_HTTP,
        scope=SCOPE_OWNER_WORKER_HTTP,
        path="/api/sessions",
        control_home=tmp_path / "control",
    )
    assert client.get("/api/sessions", headers={"Authorization": f"Bearer {wrong}"}).status_code == 401


def test_worker_http_token_validation_uses_stored_control_home(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    control_a = tmp_path / "control-a"
    control_b = tmp_path / "control-b"
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "owner"))
    monkeypatch.setenv("HERMES_OWNER_KEY", "ok1_worker_routes")
    monkeypatch.setenv("HERMES_CONTROL_HOME", str(control_a))
    from hermes_cli.owner_worker.entrypoint import create_app

    owner_home = ensure_owner_runtime_dirs(tmp_path / "owner")
    app = create_app("ok1_worker_routes", owner_home)
    client = TestClient(app)
    monkeypatch.setenv("HERMES_CONTROL_HOME", str(control_b))

    good = _capability_for(app, audience=AUD_OWNER_WORKER_HTTP, path="/internal/health", control_home=control_a)
    wrong_secret = _capability_for(app, audience=AUD_OWNER_WORKER_HTTP, path="/internal/health", control_home=control_b)

    assert client.get("/internal/health", headers={"Authorization": f"Bearer {good}"}).status_code == 200
    assert client.get("/internal/health", headers={"Authorization": f"Bearer {wrong_secret}"}).status_code == 401


def test_worker_http_token_rejects_wrong_path(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "owner"))
    monkeypatch.setenv("HERMES_OWNER_KEY", "ok1_worker_routes")
    monkeypatch.setenv("HERMES_CONTROL_HOME", str(tmp_path / "control"))
    from hermes_cli.owner_worker.entrypoint import create_app

    owner_home = ensure_owner_runtime_dirs(tmp_path / "owner")
    app = create_app("ok1_worker_routes", owner_home)
    client = TestClient(app)
    wrong_path = _capability_for(app, audience=AUD_OWNER_WORKER_HTTP, path="/api/sessions", control_home=tmp_path / "control")

    assert client.get("/internal/health", headers={"Authorization": f"Bearer {wrong_path}"}).status_code == 401


def test_worker_routes_reject_external_owner_selector(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "owner"))
    monkeypatch.setenv("HERMES_OWNER_KEY", "ok1_worker_routes")
    monkeypatch.setenv("HERMES_CONTROL_HOME", str(tmp_path / "control"))
    from hermes_cli.owner_worker.entrypoint import create_app

    owner_home = ensure_owner_runtime_dirs(tmp_path / "owner")
    app = create_app("ok1_worker_routes", owner_home)
    client = TestClient(app)
    token = _capability_for(
        app,
        audience=AUD_OWNER_WORKER_HTTP,
        path="/api/sessions",
        control_home=tmp_path / "control",
    )

    response = client.get("/api/sessions?owner_key=ok1_other", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 400
    assert response.json()["detail"] == "owner selection is not available in authenticated mode"


def test_worker_session_routes_reject_legacy_profile_selection(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "owner"))
    monkeypatch.setenv("HERMES_OWNER_KEY", "ok1_worker_routes")
    monkeypatch.setenv("HERMES_CONTROL_HOME", str(tmp_path / "control"))
    from hermes_cli.owner_worker.entrypoint import create_app

    owner_home = ensure_owner_runtime_dirs(tmp_path / "owner")
    app = create_app("ok1_worker_routes", owner_home)
    client = TestClient(app)
    token = _capability_for(app, audience=AUD_OWNER_WORKER_HTTP, path="/api/sessions", control_home=tmp_path / "control")

    response = client.get("/api/sessions?profile=legacy", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 400


@pytest.mark.parametrize(
    ("request_path", "capability_path"),
    [
        ("/api/sessions?limit=20&min_messages=0", "/api/sessions"),
        ("/api/sessions/search?q=owner", "/api/sessions/search"),
        ("/api/sessions/owner-visible", "/api/sessions/owner-visible"),
        ("/api/sessions/owner-visible/messages", "/api/sessions/owner-visible/messages"),
        ("/api/sessions/stats", "/api/sessions/stats"),
        ("/api/sessions/owner-visible/export", "/api/sessions/owner-visible/export"),
        (
            "/api/sessions/owner-visible/latest-descendant",
            "/api/sessions/owner-visible/latest-descendant",
        ),
    ],
)
def test_worker_session_read_routes_use_owner_db_not_global_sentinel(
    tmp_path, monkeypatch, request_path, capability_path
):
    from fastapi.testclient import TestClient
    from hermes_state import SessionDB

    global_home = tmp_path / "global"
    owner_home = tmp_path / "users" / "ok1_worker_routes"
    control_home = tmp_path / "control"
    global_home.mkdir(parents=True)
    owner_home.mkdir(parents=True)

    global_db = SessionDB(db_path=global_home / "state.db")
    try:
        global_db.create_session("global-sentinel", "cli")
        global_db.append_message("global-sentinel", "user", "must not leak")
    finally:
        global_db.close()

    monkeypatch.setenv("HERMES_HOME", str(owner_home))
    monkeypatch.setenv("HERMES_OWNER_KEY", "ok1_worker_routes")
    monkeypatch.setenv("HERMES_CONTROL_HOME", str(control_home))
    owner_db = SessionDB()
    try:
        owner_db.create_session("owner-visible", "cli")
        owner_db.append_message("owner-visible", "user", "owner only")
    finally:
        owner_db.close()

    from hermes_cli.owner_worker.entrypoint import create_app

    ensure_owner_runtime_dirs(owner_home)
    app = create_app("ok1_worker_routes", owner_home)
    client = TestClient(app)
    token = _capability_for(
        app,
        audience=AUD_OWNER_WORKER_HTTP,
        path=capability_path,
        control_home=control_home,
    )

    response = client.get(request_path, headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    payload = response.json()
    assert "global-sentinel" not in str(payload)
    if request_path == "/api/sessions/stats":
        assert payload["total"] == 1
        assert payload["messages"] == 1
    else:
        assert "owner-visible" in str(payload)


@pytest.mark.parametrize(
    ("method", "request_path", "payload", "capability_path"),
    [
        ("PATCH", "/api/sessions/owner-visible", {"title": "Updated"}, "/api/sessions/owner-visible"),
        ("DELETE", "/api/sessions/owner-visible", None, "/api/sessions/owner-visible"),
        ("POST", "/api/sessions/prune", {"older_than_days": 1}, "/api/sessions/prune"),
        ("POST", "/api/sessions/bulk-delete", {"ids": ["owner-visible"]}, "/api/sessions/bulk-delete"),
        ("DELETE", "/api/sessions/empty", None, "/api/sessions/empty"),
    ],
)
def test_worker_session_write_routes_mutate_only_owner_db(
    tmp_path, monkeypatch, method, request_path, payload, capability_path
):
    from fastapi.testclient import TestClient
    from hermes_state import SessionDB

    global_home = tmp_path / "global"
    owner_home = tmp_path / "users" / "ok1_worker_routes"
    control_home = tmp_path / "control"
    global_home.mkdir(parents=True)
    owner_home.mkdir(parents=True)

    global_db = SessionDB(db_path=global_home / "state.db")
    try:
        global_db.create_session("global-sentinel", "cli")
        global_db.append_message("global-sentinel", "user", "must not mutate")
    finally:
        global_db.close()

    monkeypatch.setenv("HERMES_HOME", str(owner_home))
    monkeypatch.setenv("HERMES_OWNER_KEY", "ok1_worker_routes")
    monkeypatch.setenv("HERMES_CONTROL_HOME", str(control_home))
    owner_db = SessionDB()
    try:
        owner_db.create_session("owner-visible", "cli")
        owner_db.append_message("owner-visible", "user", "owner only")
        if request_path == "/api/sessions/empty":
            owner_db.create_session("empty-ended", "cli")
            owner_db.end_session("empty-ended", "completed")
        if request_path == "/api/sessions/prune":
            owner_db.end_session("owner-visible", "completed")
            owner_db._conn.execute("UPDATE sessions SET ended_at = 0 WHERE id = ?", ("owner-visible",))
            owner_db._conn.commit()
    finally:
        owner_db.close()

    from hermes_cli.owner_worker.entrypoint import create_app

    ensure_owner_runtime_dirs(owner_home)
    app = create_app("ok1_worker_routes", owner_home)
    client = TestClient(app)
    token = _capability_for(
        app,
        audience=AUD_OWNER_WORKER_HTTP,
        path=capability_path,
        control_home=control_home,
    )

    response = client.request(
        method,
        request_path,
        json=payload,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )

    assert response.status_code == 200, response.text
    global_db = SessionDB(db_path=global_home / "state.db")
    try:
        assert global_db.get_session("global-sentinel") is not None
    finally:
        global_db.close()
    owner_db = SessionDB()
    try:
        if method == "PATCH":
            assert owner_db.get_session_title("owner-visible") == "Updated"
        elif request_path == "/api/sessions/empty":
            assert owner_db.get_session("empty-ended") is None
        elif request_path == "/api/sessions/prune":
            assert owner_db.get_session("owner-visible") is not None
            assert response.json()["removed"] == 0
        else:
            assert owner_db.get_session("owner-visible") is None
    finally:
        owner_db.close()


def test_worker_session_identifiers_are_resolved_only_within_owner_scope(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from hermes_state import SessionDB

    owner_a_home = tmp_path / "users" / "ok1_owner_a"
    owner_b_home = tmp_path / "users" / "ok1_owner_b"
    control_home = tmp_path / "control"
    owner_a_home.mkdir(parents=True)
    owner_b_home.mkdir(parents=True)

    owner_b_db = SessionDB(db_path=owner_b_home / "state.db")
    try:
        owner_b_db.create_session("b-session-unique", "cli")
        owner_b_db.append_message("b-session-unique", "user", "owner B only")
        owner_b_db.set_session_title("b-session-unique", "B private title")
    finally:
        owner_b_db.close()

    monkeypatch.setenv("HERMES_HOME", str(owner_a_home))
    monkeypatch.setenv("HERMES_OWNER_KEY", "ok1_owner_a")
    monkeypatch.setenv("HERMES_CONTROL_HOME", str(control_home))
    owner_a_db = SessionDB()
    try:
        owner_a_db.create_session("a-session-unique", "cli")
        owner_a_db.append_message("a-session-unique", "user", "owner A only")
        owner_a_db.set_session_title("a-session-unique", "A visible title")
    finally:
        owner_a_db.close()

    from hermes_cli.owner_worker.entrypoint import create_app

    ensure_owner_runtime_dirs(owner_a_home)
    app = create_app("ok1_owner_a", owner_a_home)
    client = TestClient(app)

    for path in (
        "/api/sessions/b-session",
        "/api/sessions/b-session/messages",
        "/api/sessions/b-session/export",
        "/api/sessions/b-session/latest-descendant",
    ):
        token = _capability_for(app, path=path, control_home=control_home)
        response = client.get(path, headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 404

    token = _capability_for(app, path="/api/sessions/search", control_home=control_home)
    response = client.get("/api/sessions/search?q=B+private+title", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.json() == {"results": []}

    token = _capability_for(app, path="/api/sessions/a-session", control_home=control_home)
    response = client.get("/api/sessions/a-session", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.json()["id"] == "a-session-unique"


def test_worker_analytics_and_model_info_routes_require_owner_token(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "owner"))
    monkeypatch.setenv("HERMES_OWNER_KEY", "ok1_worker_routes")
    monkeypatch.setenv("HERMES_CONTROL_HOME", str(tmp_path / "control"))
    from hermes_cli.owner_worker.entrypoint import create_app

    owner_home = ensure_owner_runtime_dirs(tmp_path / "owner")
    app = create_app("ok1_worker_routes", owner_home)
    client = TestClient(app)

    assert client.get("/api/analytics/usage").status_code == 401
    assert client.get("/api/analytics/models").status_code == 401
    assert client.get("/api/model/info").status_code == 401


def test_worker_analytics_routes_return_owner_local_data(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "owner"))
    monkeypatch.setenv("HERMES_OWNER_KEY", "ok1_worker_routes")
    monkeypatch.setenv("HERMES_CONTROL_HOME", str(tmp_path / "control"))
    from hermes_cli.owner_worker.entrypoint import create_app
    from hermes_state import SessionDB

    owner_home = ensure_owner_runtime_dirs(tmp_path / "owner")
    db = SessionDB()
    try:
        db.create_session("analytics-session", "cli", model="claude-test")
        db.append_message("analytics-session", "user", "hello")
    finally:
        db.close()

    app = create_app("ok1_worker_routes", owner_home)
    client = TestClient(app)
    usage_token = _capability_for(app, audience=AUD_OWNER_WORKER_HTTP, path="/api/analytics/usage", control_home=tmp_path / "control")
    models_token = _capability_for(app, audience=AUD_OWNER_WORKER_HTTP, path="/api/analytics/models", control_home=tmp_path / "control")

    usage = client.get("/api/analytics/usage?days=36500", headers={"Authorization": f"Bearer {usage_token}"})
    models = client.get("/api/analytics/models?days=36500", headers={"Authorization": f"Bearer {models_token}"})

    assert usage.status_code == 200
    assert usage.json()["period_days"] == 36500
    assert models.status_code == 200
    assert models.json()["period_days"] == 36500


def test_worker_model_info_returns_owner_local_config(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    owner_home = tmp_path / "owner"
    monkeypatch.setenv("HERMES_HOME", str(owner_home))
    monkeypatch.setenv("HERMES_OWNER_KEY", "ok1_worker_routes")
    monkeypatch.setenv("HERMES_CONTROL_HOME", str(tmp_path / "control"))
    from hermes_cli.config import save_config
    from hermes_cli.owner_worker.entrypoint import create_app

    ensure_owner_runtime_dirs(owner_home)
    save_config({"model": {"default": "claude-test-owner", "provider": "anthropic", "context_length": 12345}})
    app = create_app("ok1_worker_routes", owner_home)
    client = TestClient(app)
    token = _capability_for(app, audience=AUD_OWNER_WORKER_HTTP, path="/api/model/info", control_home=tmp_path / "control")

    response = client.get("/api/model/info", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    body = response.json()
    assert body["model"] == "claude-test-owner"
    assert body["provider"] == "anthropic"
    assert body["config_context_length"] == 12345


def test_worker_analytics_routes_reject_legacy_profile_selection(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "owner"))
    monkeypatch.setenv("HERMES_OWNER_KEY", "ok1_worker_routes")
    monkeypatch.setenv("HERMES_CONTROL_HOME", str(tmp_path / "control"))
    from hermes_cli.owner_worker.entrypoint import create_app

    owner_home = ensure_owner_runtime_dirs(tmp_path / "owner")
    app = create_app("ok1_worker_routes", owner_home)
    client = TestClient(app)
    token = _capability_for(app, audience=AUD_OWNER_WORKER_HTTP, path="/api/analytics/usage", control_home=tmp_path / "control")

    response = client.get("/api/analytics/usage?profile=legacy", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 400


def test_create_app_does_not_mutate_web_server_global_state(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "owner"))
    monkeypatch.setenv("HERMES_OWNER_KEY", "ok1_worker_routes")
    monkeypatch.setenv("HERMES_CONTROL_HOME", str(tmp_path / "control"))
    from hermes_cli import web_server
    from hermes_cli.owner_worker.entrypoint import create_app

    monkeypatch.setattr(web_server.app.state, "auth_required", True, raising=False)
    monkeypatch.setattr(web_server.app.state, "bound_host", "control.example", raising=False)
    monkeypatch.setattr(web_server.app.state, "bound_port", 443, raising=False)

    owner_home = ensure_owner_runtime_dirs(tmp_path / "owner")
    create_app("ok1_worker_routes", owner_home)

    assert web_server.app.state.auth_required is True
    assert web_server.app.state.bound_host == "control.example"
    assert web_server.app.state.bound_port == 443
