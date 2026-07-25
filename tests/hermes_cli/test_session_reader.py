"""Isolation and real-path tests for the owner Session Reader."""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
from pathlib import Path

import pytest

from hermes_cli.dashboard_auth.authority import (
    AuthorityStore,
    AuthorizationRejected,
    ReaderGenerationState,
    ReaderLeaseState,
    WorkerGenerationState,
    WorkerLeaseState,
)
from hermes_cli.owner_runtime import (
    session_reader_env_for,
    session_reader_runtime_paths,
    session_reader_socket_path,
    validate_session_reader_runtime_environment,
)
from hermes_cli.session_reader.tokens import (
    SessionReaderCapabilityInvalid,
    mint_session_reader_capability,
    session_reader_capability_public_config,
    verify_session_reader_capability,
)


def _active_reader(store: AuthorityStore, owner_key: str = "ok1_a"):
    claim = store.claim_reader_start(owner_key, reader_id=f"reader-{owner_key}")
    return store.transition_reader_lease(
        claim.lease,
        state=ReaderLeaseState.ACTIVE,
        generation_state=ReaderGenerationState.ACTIVE,
    )


def test_reader_and_worker_authority_lanes_coexist_without_fencing(tmp_path):
    store = AuthorityStore(tmp_path / "control")
    worker_claim = store.claim_worker_start("ok1_a", worker_id="worker-a")
    worker = store.transition_worker_lease(
        worker_claim.lease,
        state=WorkerLeaseState.ACTIVE,
        generation_state=WorkerGenerationState.ACTIVE,
    )
    reader = _active_reader(store)

    assert store.read_owner_worker_lease("ok1_a") == worker
    assert store.read_session_reader_lease("ok1_a") == reader
    draining = store.transition_reader_lease(
        reader,
        state=ReaderLeaseState.DRAINING,
        generation_state=ReaderGenerationState.DRAINING,
    )
    store.transition_reader_lease(
        draining,
        state=ReaderLeaseState.REVOKED,
        generation_state=ReaderGenerationState.TERMINATED,
    )
    assert store.read_owner_worker_lease("ok1_a") == worker


def test_reader_authority_is_owner_isolated_and_stale_transition_fails(tmp_path):
    store = AuthorityStore(tmp_path / "control")
    a = _active_reader(store, "ok1_a")
    b = _active_reader(store, "ok1_b")
    assert a.reader_generation == b.reader_generation == 1
    with pytest.raises(AuthorizationRejected, match="already_owned"):
        store.claim_reader_start("ok1_a", reader_id="reader-a-2")
    store.invalidate_outstanding_credentials(reason="recovery")
    replacement = store.claim_reader_start("ok1_a", reader_id="reader-a-2")
    with pytest.raises(AuthorizationRejected, match="stale"):
        store.transition_reader_lease(
            a,
            state=ReaderLeaseState.DRAINING,
            generation_state=ReaderGenerationState.DRAINING,
        )
    assert store.read_session_reader_lease("ok1_a") == replacement.lease
    assert store.read_session_reader_lease("ok1_b") == b


def test_reader_capability_is_exact_owner_generation_lease_and_path_bound(tmp_path):
    control = tmp_path / "control"
    store = AuthorityStore(control)
    lease = _active_reader(store)
    verifier = session_reader_capability_public_config(control)
    token = mint_session_reader_capability(lease, path="/api/sessions", control_home=control, now=100)
    kwargs = {
        "authority_store": store,
        "public_key": verifier["HERMES_SESSION_READER_CAPABILITY_PUBLIC_KEY"],
        "issuer_key_version": verifier["HERMES_SESSION_READER_CAPABILITY_ISSUER"],
        "retained_public_keys": verifier["HERMES_SESSION_READER_CAPABILITY_RETAINED_PUBLIC_KEYS"],
        "now": 101,
    }
    claims = verify_session_reader_capability(
        token, expected_lease=lease, path="/api/sessions", **kwargs,
    )
    assert claims.owner_key == "ok1_a"
    with pytest.raises(SessionReaderCapabilityInvalid, match="binding_mismatch"):
        verify_session_reader_capability(
            token, expected_lease=lease, path="/internal/health", **kwargs,
        )
    other = _active_reader(store, "ok1_b")
    with pytest.raises(SessionReaderCapabilityInvalid, match="binding_mismatch"):
        verify_session_reader_capability(
            token, expected_lease=other, path="/api/sessions", **kwargs,
        )


def test_reader_runtime_contract_is_minimal_and_separate_from_worker(tmp_path):
    owner_home = tmp_path / "owner"
    control_home = tmp_path / "control"
    verifier = session_reader_capability_public_config(control_home)
    env = session_reader_env_for(
        owner_key="ok1_a",
        owner_home=owner_home,
        control_home=control_home,
        reader_generation=3,
        reader_id="reader-a",
        lease_version=2,
        recovery_generation=0,
        capability_issuer=verifier["HERMES_SESSION_READER_CAPABILITY_ISSUER"],
        capability_public_key=verifier["HERMES_SESSION_READER_CAPABILITY_PUBLIC_KEY"],
        capability_retained_public_keys=verifier[
            "HERMES_SESSION_READER_CAPABILITY_RETAINED_PUBLIC_KEYS"
        ],
    )
    paths = validate_session_reader_runtime_environment(
        owner_home=owner_home,
        owner_key="ok1_a",
        reader_generation=3,
        reader_id="reader-a",
        socket_path=session_reader_socket_path(owner_home, 3),
        source=env,
    )
    assert paths == session_reader_runtime_paths(owner_home=owner_home, reader_generation=3)
    assert paths.reader_socket == owner_home / "runtime/r/3/s"
    production_home = Path(
        "/opt/hermes/shared/.hermes/users/"
        "ok1_54b69cda14d5ddfbed684f1ca4a7e270f8ce"
    )
    production_socket = session_reader_socket_path(production_home, 999_999)
    assert len(os.fsencode(production_socket)) < 104
    assert production_socket.is_relative_to(production_home)
    assert not any(key.startswith("HERMES_WORKER_") for key in env)
    assert "HERMES_WORKSPACE_ROOT" not in env
    polluted = {**env, "HERMES_DEPLOYMENT_INFERENCE_RELAY_FD": "7"}
    with pytest.raises(RuntimeError, match="unexpected session reader"):
        validate_session_reader_runtime_environment(source=polluted)


class _FakeReaderProcess:
    def __init__(self, pid: int = 4321) -> None:
        self.pid = pid
        self.returncode = None
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def kill(self):
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


def _write_reader_ready(argv, env, process) -> None:
    socket_path = Path(argv[argv.index("--socket") + 1])
    socket_path.touch()
    health = {
        "ready": True,
        "owner_key": env["HERMES_OWNER_KEY"],
        "reader_generation": int(env["HERMES_READER_GENERATION"]),
        "reader_id": env["HERMES_READER_ID"],
        "lease_version": int(env["HERMES_READER_LEASE_VERSION"]),
        "recovery_generation": int(env["HERMES_READER_RECOVERY_GENERATION"]),
        "owner_home": str(Path(env["HERMES_HOME"]).resolve()),
        "hermes_home": str(Path(env["HERMES_HOME"]).resolve()),
        "pid": process.pid,
        "forbidden_env_present": [],
    }
    socket_path.with_name("reader.ready.json").write_text(json.dumps(health))


def test_reader_supervisor_attaches_resource_scope_before_accepting_health(tmp_path):
    from hermes_cli.session_reader.supervisor import SessionReaderSupervisor

    operations = []

    class _Scope:
        def __init__(self):
            self.cleaned = False

        def attach(self, pid):
            operations.append(("attach", pid))

        def verify_membership(self, pid):
            operations.append(("verify", pid))
            return True

        def cleanup(self):
            self.cleaned = True
            operations.append(("cleanup",))

    class _Manager:
        def __init__(self):
            self.scope = _Scope()

        def admit_reader(self, lease):
            operations.append(("admit", lease.reader_generation))
            return self.scope

    manager = _Manager()

    def process_factory(argv, **kwargs):
        process = _FakeReaderProcess()
        _write_reader_ready(argv, kwargs["env"], process)
        return process

    supervisor = SessionReaderSupervisor(
        control_home=tmp_path / "control",
        global_home=tmp_path,
        process_factory=process_factory,
        resource_manager=manager,
        startup_timeout=0.1,
    )
    handle = supervisor.get_or_start(
        {"owner_key": "ok1_resource", "owner_home": tmp_path / "owner"}
    )

    assert operations[:3] == [("admit", 1), ("attach", handle.pid), ("verify", handle.pid)]
    assert handle.resource_scope is manager.scope
    supervisor.shutdown()
    assert manager.scope.cleaned
    assert operations[-1] == ("cleanup",)


def test_reader_supervisor_membership_failure_revokes_lease_and_cleans_scope(tmp_path):
    from hermes_cli.session_reader.supervisor import (
        SessionReaderStartupError,
        SessionReaderSupervisor,
    )

    class _Scope:
        cleaned = False

        def attach(self, _pid):
            return None

        def verify_membership(self, _pid):
            return False

        def cleanup(self):
            self.cleaned = True

    class _Manager:
        def __init__(self):
            self.scope = _Scope()

        def admit_reader(self, _lease):
            return self.scope

    manager = _Manager()
    process = _FakeReaderProcess()
    supervisor = SessionReaderSupervisor(
        control_home=tmp_path / "control",
        global_home=tmp_path,
        process_factory=lambda *_args, **_kwargs: process,
        resource_manager=manager,
        startup_timeout=0.1,
    )

    with pytest.raises(SessionReaderStartupError, match="membership verification"):
        supervisor.get_or_start(
            {"owner_key": "ok1_membership", "owner_home": tmp_path / "owner"}
        )

    assert process.terminated
    assert manager.scope.cleaned
    lease = supervisor.authority_store.read_session_reader_lease("ok1_membership")
    assert lease is not None and lease.state is ReaderLeaseState.REVOKED


def test_reader_supervisor_idle_and_capacity_retirement_respect_active_uses(tmp_path, monkeypatch):
    from hermes_cli.session_reader.supervisor import (
        SessionReaderHandle,
        SessionReaderUnavailableError,
        SessionReaderSupervisor,
    )

    supervisor = SessionReaderSupervisor(
        control_home=tmp_path / "control",
        global_home=tmp_path,
        max_readers=2,
        idle_timeout=10,
    )
    now = time.time()

    def handle(owner_key, last_used_at, active_uses=0):
        return SessionReaderHandle(
            owner_key=owner_key,
            owner_home=(tmp_path / owner_key).resolve(),
            reader_generation=1,
            reader_id=f"reader-{owner_key}",
            lease_version=1,
            recovery_generation=0,
            socket_path=tmp_path / f"{owner_key}.sock",
            process=_FakeReaderProcess(),
            pid=4321,
            last_used_at=last_used_at,
            active_uses=active_uses,
        )

    idle = handle("ok1_idle", now - 20)
    active = handle("ok1_active", now - 30, active_uses=1)
    retired = []
    supervisor._handles = {idle.owner_key: idle, active.owner_key: active}
    monkeypatch.setattr(supervisor, "_retire", retired.append)
    supervisor._stop_idle(now=now)
    assert retired == [idle]
    assert supervisor._handles == {active.owner_key: active}

    newer_idle = handle("ok1_newer", now - 5)
    supervisor._handles[newer_idle.owner_key] = newer_idle
    started = handle("ok1_started", now)
    monkeypatch.setattr(supervisor, "_start", lambda *_args, **_kwargs: started)
    assert supervisor.get_or_start(
        {"owner_key": started.owner_key, "owner_home": started.owner_home}
    ) is started
    assert retired == [idle, newer_idle]
    assert supervisor._handles == {active.owner_key: active}

    supervisor.max_readers = 1
    with pytest.raises(SessionReaderUnavailableError, match="limit reached"):
        supervisor.get_or_start(
            {"owner_key": "ok1_blocked", "owner_home": tmp_path / "blocked"}
        )
    assert retired == [idle, newer_idle]


@pytest.mark.skipif(os.name == "nt", reason="Unix-domain Session Reader subprocess")
def test_reader_real_process_reads_owner_db_without_creating_missing_db(tmp_path):
    from hermes_state import SessionDB
    from hermes_cli.session_reader.client import SessionReaderClient
    from hermes_cli.session_reader.supervisor import SessionReaderSupervisor

    short_root = Path("/tmp") / f"hermes-reader-{os.getpid()}-{time.time_ns()}"
    control = short_root / "control"
    owner_home = short_root / "owner"
    owner = {"owner_key": "ok1_a", "owner_home": owner_home}
    supervisor = SessionReaderSupervisor(
        control_home=control,
        global_home=tmp_path,
        startup_timeout=3,
        poll_interval=0.01,
    )
    try:
        started = time.perf_counter()
        handle = supervisor.get_or_start(owner)
        assert time.perf_counter() - started < 3
        lease = supervisor._lease_for_handle(handle)
        client = SessionReaderClient(handle.socket_path, control_home=control)
        response = client.request(
            "GET", "/api/sessions?limit=30&offset=0&order=recent&compact=true", lease=lease,
        )
        assert response.status_code == 200
        assert response.json() == {"sessions": [], "total": 0, "limit": 30, "offset": 0}
        assert not (owner_home / "state.db").exists()

        db = SessionDB(db_path=owner_home / "state.db")
        db.create_session("reader-session", source="cli", model="test")
        db.record_gateway_session_peer(
            "reader-session",
            source="cli",
            session_key="reader-session",
            owner_key="ok1_a",
            workspace_root=str((owner_home / "workspaces").resolve()),
            worker_generation=1,
        )
        # Keep the writer open: a read-only mode=ro connection must observe
        # committed WAL frames rather than relying on close/checkpoint behavior.
        assert (owner_home / "state.db-wal").exists()
        response = client.request(
            "GET", "/api/sessions?limit=30&offset=0&order=recent&compact=true", lease=lease,
        )
        assert response.status_code == 200
        assert response.json()["total"] == 1
        assert response.json()["sessions"][0]["id"] == "reader-session"
        db.close()
    finally:
        if "db" in locals():
            db.close()
        supervisor.shutdown()
        shutil.rmtree(short_root, ignore_errors=True)


def test_reader_rejects_worker_capability_and_stale_reader_lease(tmp_path):
    from hermes_cli.dashboard_auth.authority import WorkerGenerationState, WorkerLeaseState
    from hermes_cli.owner_worker.tokens import (
        AUD_OWNER_WORKER_HTTP,
        SCOPE_OWNER_WORKER_HTTP,
        mint_owner_worker_capability,
    )

    control = tmp_path / "control"
    store = AuthorityStore(control)
    reader = _active_reader(store)
    verifier = session_reader_capability_public_config(control)
    worker_claim = store.claim_worker_start("ok1_a", worker_id="worker-a")
    worker = store.transition_worker_lease(
        worker_claim.lease,
        state=WorkerLeaseState.ACTIVE,
        generation_state=WorkerGenerationState.ACTIVE,
    )
    worker_token = mint_owner_worker_capability(
        worker,
        audience=AUD_OWNER_WORKER_HTTP,
        scope=SCOPE_OWNER_WORKER_HTTP,
        path="/api/sessions",
        control_home=control,
        now=100,
    )
    kwargs = {
        "expected_lease": reader,
        "path": "/api/sessions",
        "authority_store": store,
        "public_key": verifier["HERMES_SESSION_READER_CAPABILITY_PUBLIC_KEY"],
        "issuer_key_version": verifier["HERMES_SESSION_READER_CAPABILITY_ISSUER"],
        "retained_public_keys": verifier["HERMES_SESSION_READER_CAPABILITY_RETAINED_PUBLIC_KEYS"],
        "now": 101,
    }
    with pytest.raises(SessionReaderCapabilityInvalid):
        verify_session_reader_capability(worker_token, **kwargs)

    reader_token = mint_session_reader_capability(
        reader, path="/api/sessions", control_home=control, now=100,
    )
    store.invalidate_outstanding_credentials(reason="test stale reader")
    with pytest.raises(SessionReaderCapabilityInvalid, match="lease_invalid"):
        verify_session_reader_capability(reader_token, **kwargs)


@pytest.mark.skipif(os.name == "nt", reason="Unix-domain Session Reader subprocess")
def test_reader_concurrent_cold_start_coalesces_and_stays_below_300ms(tmp_path):
    from hermes_cli.session_reader.client import SessionReaderClient
    from hermes_cli.session_reader.supervisor import SessionReaderSupervisor

    short_root = Path("/tmp") / f"hermes-reader-cold-{os.getpid()}-{time.time_ns()}"
    control = short_root / "control"
    owner_home = short_root / "owner"
    owner = {"owner_key": "ok1_cold", "owner_home": owner_home}
    supervisor = SessionReaderSupervisor(
        control_home=control,
        global_home=tmp_path,
        startup_timeout=3,
        poll_interval=0.001,
    )
    barrier = threading.Barrier(3)
    results = []
    errors = []

    def request() -> None:
        try:
            barrier.wait(timeout=2)
            started = time.perf_counter()
            handle = supervisor.get_or_start(owner)
            lease = supervisor._lease_for_handle(handle)
            response = SessionReaderClient(
                handle.socket_path,
                control_home=control,
                signing_record=supervisor.signing_record,
            ).request(
                "GET",
                "/api/sessions?limit=30&offset=0&order=recent&compact=true",
                lease=lease,
            )
            results.append((handle.pid, time.perf_counter() - started, response.json()))
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=request) for _ in range(2)]
    try:
        for thread in threads:
            thread.start()
        barrier.wait(timeout=2)
        for thread in threads:
            thread.join(timeout=5)
        assert not errors
        assert len(results) == 2
        assert len({pid for pid, _elapsed, _payload in results}) == 1
        assert all(payload["total"] == 0 for _pid, _elapsed, payload in results)
        assert max(elapsed for _pid, elapsed, _payload in results) < 0.3
    finally:
        supervisor.shutdown()
        shutil.rmtree(short_root, ignore_errors=True)


def _populate_large_scoped_session_history(db, owner_key: str, owner_home: Path) -> None:
    base = 1_700_000_000.0
    workspace_root = str((owner_home / "workspaces").resolve())
    sessions = []
    messages = []
    message_id = 1
    for index in range(3_000):
        chain_length = 3 if index % 10 == 0 else 1
        parent_id = None
        for chain_index in range(chain_length):
            session_id = (
                f"session-{index}-root"
                if chain_index == 0
                else f"session-{index}-tip-{chain_index}"
            )
            started_at = base + index * 10 + chain_index
            compressed = chain_index < chain_length - 1
            sessions.append(
                (
                    session_id,
                    "gui",
                    parent_id,
                    started_at,
                    started_at + 0.5 if compressed else None,
                    "compression" if compressed else None,
                    3,
                    0,
                    owner_key,
                    workspace_root,
                    1,
                )
            )
            for message_index in range(3):
                messages.append(
                    (
                        message_id,
                        session_id,
                        "user" if message_index == 0 else "assistant",
                        f"message {index} {chain_index} {message_index}",
                        started_at + message_index / 10,
                    )
                )
                message_id += 1
            parent_id = session_id
    db._conn.executemany(
        """INSERT INTO sessions (
               id, source, parent_session_id, started_at, ended_at,
               end_reason, message_count, archived, owner_key,
               workspace_root, worker_generation
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        sessions,
    )
    db._conn.executemany(
        """INSERT INTO messages (id, session_id, role, content, timestamp)
           VALUES (?, ?, ?, ?, ?)""",
        messages,
    )
    db._conn.commit()


@pytest.mark.skipif(os.name == "nt", reason="Unix-domain Session Reader subprocess")
def test_reader_real_path_large_history_stays_below_300ms(tmp_path):
    from hermes_state import SessionDB
    from hermes_cli.session_reader.client import SessionReaderClient
    from hermes_cli.session_reader.supervisor import SessionReaderSupervisor

    short_root = Path("/tmp") / f"hermes-reader-large-{os.getpid()}-{time.time_ns()}"
    control = short_root / "control"
    owner_home = short_root / "owner"
    owner_home.mkdir(parents=True)
    owner_key = "ok1_large"
    db = SessionDB(db_path=owner_home / "state.db")
    _populate_large_scoped_session_history(db, owner_key, owner_home)
    supervisor = SessionReaderSupervisor(
        control_home=control,
        global_home=tmp_path,
        startup_timeout=3,
        poll_interval=0.001,
    )
    try:
        # Include lazy process startup in the first request budget. The second
        # request pins the same real UDS/capability/query path with a warm Reader.
        cold_started = time.perf_counter()
        handle = supervisor.get_or_start({"owner_key": owner_key, "owner_home": owner_home})
        lease = supervisor._lease_for_handle(handle)
        client = SessionReaderClient(
            handle.socket_path,
            control_home=control,
            signing_record=supervisor.signing_record,
        )
        responses = [
            client.request(
                "GET",
                "/api/sessions?limit=30&offset=0&order=recent&compact=true",
                lease=lease,
            )
        ]
        cold_elapsed = time.perf_counter() - cold_started
        warm_started = time.perf_counter()
        responses.append(
            client.request(
                "GET",
                "/api/sessions?limit=30&offset=0&order=recent&compact=true",
                lease=lease,
            )
        )
        warm_elapsed = time.perf_counter() - warm_started

        assert all(response.status_code == 200 for response in responses)
        payload = responses[-1].json()
        assert payload["total"] == 3_000
        assert len(payload["sessions"]) == 30
        assert payload["sessions"][0]["id"] == "session-2999-root"
        assert cold_elapsed < 0.3, f"cold Reader request took {cold_elapsed:.3f}s"
        assert warm_elapsed < 0.3, f"warm Reader request took {warm_elapsed:.3f}s"
    finally:
        db.close()
        supervisor.shutdown()
        shutil.rmtree(short_root, ignore_errors=True)
