"""Isolation and real-path tests for the owner Session Reader."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
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


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory hardening")
def test_reader_runtime_preparation_hardens_owned_legacy_directory(tmp_path):
    from hermes_cli.session_reader.runtime import prepare_session_reader_runtime

    owner_home = tmp_path / "owner"
    readers = owner_home / "runtime" / "r"
    readers.mkdir(parents=True, mode=0o755)
    readers.chmod(0o755)
    before = readers.lstat()

    paths = prepare_session_reader_runtime(owner_home, 3)

    after = readers.lstat()
    assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
    assert stat.S_IMODE(after.st_mode) == 0o700
    assert stat.S_IMODE(paths.reader_runtime_dir.lstat().st_mode) == 0o700
    for worker_only in ("workspaces", "skills", "sessions", "checkpoints"):
        assert not (owner_home / worker_only).exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory hardening")
@pytest.mark.parametrize(
    ("kind", "code"),
    [("symlink", "symlink"), ("file", "not_directory")],
)
def test_reader_runtime_preparation_rejects_non_directory_components(
    tmp_path, kind, code
):
    from hermes_cli.session_reader.runtime import (
        SessionReaderRuntimePreparationError,
        prepare_session_reader_runtime,
    )

    owner_home = tmp_path / "owner"
    runtime = owner_home / "runtime"
    runtime.mkdir(parents=True, mode=0o700)
    logs = runtime / "logs"
    if kind == "symlink":
        target = tmp_path / "outside"
        target.mkdir(mode=0o700)
        try:
            logs.symlink_to(target, target_is_directory=True)
        except OSError:
            pytest.skip("symlinks unavailable")
    else:
        logs.write_text("not a directory")

    with pytest.raises(SessionReaderRuntimePreparationError) as caught:
        prepare_session_reader_runtime(owner_home, 1)

    assert caught.value.code == code
    assert caught.value.component == "runtime_logs"


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory ownership")
def test_reader_runtime_preparation_rejects_wrong_owner_before_chmod(
    tmp_path, monkeypatch
):
    import hermes_cli.session_reader.runtime as reader_runtime

    owner_home = tmp_path / "owner"
    runtime = owner_home / "runtime"
    runtime.mkdir(parents=True, mode=0o755)
    runtime.chmod(0o755)
    actual_uid = os.getuid()
    monkeypatch.setattr(reader_runtime.os, "getuid", lambda: actual_uid + 1)

    with pytest.raises(reader_runtime.SessionReaderRuntimePreparationError) as caught:
        reader_runtime.prepare_session_reader_runtime(owner_home, 1)

    assert caught.value.code == "wrong_owner"
    assert caught.value.component == "runtime"
    assert stat.S_IMODE(runtime.lstat().st_mode) == 0o755


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor identity")
def test_reader_runtime_preparation_rejects_replaced_inode_before_chmod(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    import hermes_cli.session_reader.runtime as reader_runtime

    owner_home = tmp_path / "owner"
    runtime = owner_home / "runtime"
    runtime.mkdir(parents=True, mode=0o755)
    runtime.chmod(0o755)
    actual_fstat = reader_runtime.os.fstat

    def changed_fstat(descriptor):
        info = actual_fstat(descriptor)
        return SimpleNamespace(
            st_dev=info.st_dev,
            st_ino=info.st_ino + 1,
            st_mode=info.st_mode,
            st_uid=info.st_uid,
        )

    monkeypatch.setattr(reader_runtime.os, "fstat", changed_fstat)

    with pytest.raises(reader_runtime.SessionReaderRuntimePreparationError) as caught:
        reader_runtime.prepare_session_reader_runtime(owner_home, 1)

    assert caught.value.code == "identity_changed"
    assert caught.value.component == "runtime"
    assert stat.S_IMODE(runtime.lstat().st_mode) == 0o755


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


def test_reader_lifecycle_skips_start_task_for_current_handle():
    from types import SimpleNamespace

    from hermes_cli.session_reader.readiness import SessionReaderLifecycle

    class _Supervisor:
        idle_timeout = 1800

        def needs_start(self, _owner):
            return False

        def ensure_started(self, _owner):
            raise AssertionError("healthy Reader must not schedule startup")

    async def run() -> None:
        lifecycle = SessionReaderLifecycle(_Supervisor())
        owner = SimpleNamespace(owner_key="ok1_current")
        lifecycle.observe_verified_owner(owner)
        await asyncio.sleep(0)
        assert lifecycle._startups == {}
        await lifecycle.close()

    asyncio.run(run())


def test_reader_lifecycle_logs_safe_runtime_preparation_metadata(
    tmp_path, caplog
):
    import asyncio
    from types import SimpleNamespace

    from hermes_cli.session_reader.readiness import SessionReaderLifecycle
    from hermes_cli.session_reader.runtime import SessionReaderRuntimePreparationError

    private_path = tmp_path / "private-owner-path"

    class _Supervisor:
        idle_timeout = 1800

        def ensure_started(self, _owner):
            try:
                private_path.write_text("private")
            except OSError:
                pass
            raise SessionReaderRuntimePreparationError(
                "wrong_owner", "readers_root"
            )

        def maintenance_tick(self):
            return None

    async def run() -> None:
        lifecycle = SessionReaderLifecycle(
            _Supervisor(), initial_backoff=0.1, max_backoff=5
        )
        observed = SimpleNamespace(
            owner=SimpleNamespace(owner_key="ok1_safe_log"),
            last_observed_at=0.0,
            failures=0,
            retry_at=0.0,
        )
        await lifecycle._ensure_started("ok1_safe_log", observed)

    with caplog.at_level(
        "WARNING", logger="hermes_cli.session_reader.readiness"
    ):
        asyncio.run(run())

    assert "error_type=SessionReaderRuntimePreparationError" in caplog.text
    assert "failure_stage=runtime_prepare" in caplog.text
    assert "failure_code=wrong_owner" in caplog.text
    assert "component=readers_root" in caplog.text
    assert "attempt=1" in caplog.text
    assert "retry_delay=5.000" in caplog.text
    assert str(private_path) not in caplog.text


def test_reader_supervisor_attaches_resource_scope_before_accepting_health(tmp_path):
    from hermes_cli.session_reader.supervisor import SessionReaderSupervisor

    operations = []

    class _Scope:
        def __init__(self):
            self.cleaned = False

        def attach(self, pid):
            operations.append(("attach", pid))

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
    handle = supervisor.ensure_started(
        {"owner_key": "ok1_resource", "owner_home": tmp_path / "owner"}
    )

    assert operations[:2] == [("admit", 1), ("attach", handle.pid)]
    assert not any(operation[0] == "verify" for operation in operations)
    assert handle.resource_scope is manager.scope
    supervisor.shutdown()
    assert manager.scope.cleaned
    assert operations[-1] == ("cleanup",)


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory ownership")
def test_reader_runtime_preparation_failure_revokes_lease_without_launch(tmp_path):
    from hermes_cli.session_reader.runtime import SessionReaderRuntimePreparationError
    from hermes_cli.session_reader.supervisor import SessionReaderSupervisor

    owner_home = tmp_path / "owner"
    runtime = owner_home / "runtime"
    runtime.mkdir(parents=True, mode=0o700)
    (runtime / "logs").write_text("not a directory")
    launched = False

    def process_factory(*_args, **_kwargs):
        nonlocal launched
        launched = True
        raise AssertionError("runtime rejection must precede process launch")

    supervisor = SessionReaderSupervisor(
        control_home=tmp_path / "control",
        global_home=tmp_path,
        process_factory=process_factory,
        startup_timeout=0.1,
    )

    with pytest.raises(SessionReaderRuntimePreparationError) as caught:
        supervisor.ensure_started(
            {"owner_key": "ok1_runtime_rejected", "owner_home": owner_home}
        )

    assert caught.value.code == "not_directory"
    assert not launched
    lease = supervisor.authority_store.read_session_reader_lease(
        "ok1_runtime_rejected"
    )
    assert lease is not None and lease.state is ReaderLeaseState.REVOKED


def test_reader_supervisor_membership_failure_revokes_lease_and_cleans_scope(tmp_path):
    from hermes_cli.session_reader.supervisor import (
        SessionReaderStartupError,
        SessionReaderSupervisor,
    )

    class _Scope:
        cleaned = False

        def attach(self, _pid):
            raise SessionReaderStartupError(
                "session reader cgroup membership verification failed"
            )

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
        supervisor.ensure_started(
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
    supervisor.maintenance_tick(now=now)
    assert retired == [idle]
    assert supervisor._handles == {active.owner_key: active}

    newer_idle = handle("ok1_newer", now - 5)
    supervisor._handles[newer_idle.owner_key] = newer_idle
    started = handle("ok1_started", now)
    monkeypatch.setattr(supervisor, "_start", lambda *_args, **_kwargs: started)
    with pytest.raises(SessionReaderUnavailableError, match="limit reached"):
        supervisor.ensure_started(
            {"owner_key": started.owner_key, "owner_home": started.owner_home}
        )
    assert retired == [idle]
    assert supervisor._handles == {
        active.owner_key: active,
        newer_idle.owner_key: newer_idle,
    }

    supervisor.maintenance_tick(now=now + 20)
    assert retired == [idle, newer_idle]
    assert supervisor._handles == {active.owner_key: active}

    supervisor.max_readers = 1
    with pytest.raises(SessionReaderUnavailableError, match="limit reached"):
        supervisor.ensure_started(
            {"owner_key": "ok1_blocked", "owner_home": tmp_path / "blocked"}
        )
    assert retired == [idle, newer_idle]


def test_reader_supervisor_reuses_and_closes_generation_client(tmp_path):
    from hermes_cli.session_reader.supervisor import (
        SessionReaderHandle,
        SessionReaderSupervisor,
    )

    closed = []

    class _Client:
        def __init__(self, socket_path, **_kwargs):
            self.socket_path = socket_path

        async def aclose(self):
            closed.append(self.socket_path)

    async def run() -> None:
        supervisor = SessionReaderSupervisor(
            control_home=tmp_path / "control",
            global_home=tmp_path,
            client_cls=_Client,
        )
        handle = SessionReaderHandle(
            owner_key="ok1_client",
            owner_home=(tmp_path / "owner").resolve(),
            reader_generation=1,
            reader_id="reader-1",
            lease_version=1,
            recovery_generation=0,
            socket_path=tmp_path / "reader.sock",
            process=_FakeReaderProcess(),
            pid=4321,
        )
        supervisor._handles[handle.owner_key] = handle

        first = supervisor.client_for(handle)
        assert supervisor.client_for(handle) is first
        await supervisor.close_client(handle)
        assert closed == [handle.socket_path]

        second = supervisor.client_for(handle)
        assert second is not first
        await supervisor.close_clients()
        assert closed == [handle.socket_path, handle.socket_path]

    asyncio.run(run())


@pytest.mark.skipif(os.name == "nt", reason="Unix-domain Session Reader subprocess")
def test_reader_real_process_reads_owner_db_without_creating_missing_db(tmp_path):
    from hermes_state import SessionDB
    from hermes_cli.session_reader.client import SessionReaderClient
    from hermes_cli.session_reader.supervisor import SessionReaderSupervisor

    short_root = Path("/tmp") / f"hermes-reader-{os.getpid()}-{time.time_ns()}"
    control = short_root / "control"
    owner_home = short_root / "owner"
    readers = owner_home / "runtime" / "r"
    readers.mkdir(parents=True, mode=0o755)
    if os.name != "nt":
        readers.chmod(0o755)
    owner = {"owner_key": "ok1_a", "owner_home": owner_home}
    supervisor = SessionReaderSupervisor(
        control_home=control,
        global_home=tmp_path,
        startup_timeout=3,
        poll_interval=0.01,
    )
    try:
        started = time.perf_counter()
        handle = supervisor.ensure_started(owner)
        assert time.perf_counter() - started < 3
        assert stat.S_IMODE(readers.lstat().st_mode) == 0o700
        lease = supervisor._lease_for_handle(handle)
        loop = asyncio.new_event_loop()
        client = SessionReaderClient(handle.socket_path, control_home=control)
        response = loop.run_until_complete(client.request(
            "GET", "/api/sessions?limit=30&offset=0&order=recent&compact=true", lease=lease,
        ))
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
        response = loop.run_until_complete(client.request(
            "GET", "/api/sessions?limit=30&offset=0&order=recent&compact=true", lease=lease,
        ))
        assert response.status_code == 200
        assert response.json()["total"] == 1
        assert response.json()["sessions"][0]["id"] == "reader-session"
        db.close()
    finally:
        if "db" in locals():
            db.close()
        if "client" in locals():
            loop.run_until_complete(client.aclose())
            loop.close()
        supervisor.shutdown()
        shutil.rmtree(short_root, ignore_errors=True)


def test_reader_read_only_adapter_matches_session_db_payloads(tmp_path):
    from hermes_cli.session_api import (
        empty_count_payload,
        export_session_payload,
        latest_descendant_payload,
        list_sessions_payload,
        search_sessions_payload,
        session_detail_payload,
        session_messages_payload,
        stats_payload,
    )
    from hermes_cli.session_reader.db import ReadOnlySessionDB
    from hermes_state import SessionDB

    owner_home = tmp_path / "owner"
    db = SessionDB(owner_home / "state.db")
    scope = {
        "owner_key": "ok1_parity",
        "workspace_root": str((owner_home / "workspaces").resolve()),
        "worker_generation": 1,
        "historical_resume": True,
    }
    try:
        db.create_session(
            "root",
            source="gui",
            owner_key=scope["owner_key"],
            workspace_root=scope["workspace_root"],
            worker_generation=1,
        )
        db.append_message("root", "user", "before compression")
        db.end_session("root", "compression")
        db.create_session(
            "tip",
            source="gui",
            parent_session_id="root",
            owner_key=scope["owner_key"],
            workspace_root=scope["workspace_root"],
            worker_generation=2,
        )
        db.append_message("tip", "assistant", "after compression")
        db.create_session(
            "archived",
            source="cli",
            owner_key=scope["owner_key"],
            workspace_root=scope["workspace_root"],
            worker_generation=1,
        )
        db.set_session_archived("archived", True)

        reader_db = ReadOnlySessionDB(owner_home / "state.db")
        try:
            reader_scope = {
                "owner_key": scope["owner_key"],
                "workspace_root": scope["workspace_root"],
                "historical_resume": True,
            }
            for options in (
                {"order": "recent", "compact": True},
                {"order": "recent", "compact": False},
                {"order": "created", "archived": "include", "compact": True},
                {"source": "gui", "order": "recent", "compact": True},
            ):
                expected = list_sessions_payload(db, recovery_scope=scope, **options)
                actual = list_sessions_payload(reader_db, recovery_scope=reader_scope, **options)
                assert actual == expected

            assert search_sessions_payload(
                reader_db,
                q="compression",
                recovery_scope=reader_scope,
            ) == search_sessions_payload(
                db,
                q="compression",
            )
            assert session_detail_payload(
                reader_db,
                "root",
                recovery_scope=reader_scope,
            ) == session_detail_payload(
                db,
                "root",
                recovery_scope=scope,
            )
            assert latest_descendant_payload(
                reader_db,
                "root",
                recovery_scope=reader_scope,
            ) == latest_descendant_payload(db, "root", recovery_scope=scope)
            assert session_messages_payload(
                reader_db,
                "root",
                recovery_scope=reader_scope,
            ) == session_messages_payload(db, "root", recovery_scope=scope)
            assert export_session_payload(
                reader_db,
                "root",
                recovery_scope=reader_scope,
            ) == export_session_payload(db, "root")
            assert empty_count_payload(
                reader_db,
                recovery_scope=reader_scope,
            ) == empty_count_payload(db)
            assert stats_payload(
                reader_db,
                recovery_scope=reader_scope,
            ) == stats_payload(db)
        finally:
            reader_db.close()
    finally:
        db.close()


def test_reader_queries_are_batched_and_read_only_fts_is_available(tmp_path):
    from hermes_cli.session_api import (
        list_sessions_payload,
        search_sessions_payload,
        stats_payload,
    )
    from hermes_cli.session_reader.db import ReadOnlySessionDB
    from hermes_state import SessionDB

    state_path = tmp_path / "state.db"
    writer = SessionDB(state_path)
    try:
        for index in range(12):
            session_id = f"session-{index}"
            writer.create_session(session_id, source="gui")
            writer.append_message(session_id, "user", f"shared marker {index}")

        reader = ReadOnlySessionDB(state_path)
        profile_reader = SessionDB(state_path, read_only=True)
        try:
            for db in (reader, profile_reader):
                assert db._fts_enabled is True
                assert db.search_messages("shared", limit=2)

            statements = []
            reader._conn.set_trace_callback(statements.append)
            list_sessions_payload(reader, limit=10, order="recent")
            list_count = len(statements)
            statements.clear()
            stats_payload(reader)
            stats_count = len(statements)
            statements.clear()
            search_sessions_payload(reader, q="shared", limit=10)
            search_count = sum(
                not sql.lstrip().startswith("--") for sql in statements
            )

            assert list_count <= 6
            assert stats_count == 3
            assert search_count <= 7
        finally:
            profile_reader.close()
            reader.close()
    finally:
        writer.close()


def test_reader_query_runtime_reuses_bounded_connections_and_closes(tmp_path):
    import concurrent.futures

    from hermes_cli.session_reader import entrypoint
    from hermes_cli.session_reader.db import ReadOnlySessionDB
    from hermes_state import SessionDB

    state_path = tmp_path / "state.db"
    writer = SessionDB(state_path)
    writer.close()
    created = []
    original_init = ReadOnlySessionDB.__init__

    def tracked_init(self, db_path):
        original_init(self, db_path)
        created.append(self)

    ReadOnlySessionDB.__init__ = tracked_init
    runtime = entrypoint.SessionReaderQueryRuntime(state_path, pool_size=2)
    try:
        def use_runtime(_index):
            with runtime.borrow() as db:
                return db.session_count(include_archived=True)

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            assert list(executor.map(use_runtime, range(20))) == [0] * 20
        assert 1 <= len(created) <= 2
        runtime.close()
        assert all(db._conn is not None for db in created)
        for db in created:
            with pytest.raises(Exception):
                db._conn.execute("SELECT 1")
    finally:
        ReadOnlySessionDB.__init__ = original_init
        runtime.close()


def test_reader_route_parser_rejects_ambiguous_or_encoded_session_paths():
    from hermes_cli.session_reader.entrypoint import _session_route

    assert _session_route("/api/sessions/session-1") == (
        "/api/sessions/session-1",
        "session-1",
    )
    assert _session_route("/api/sessions/session-1/messages") == (
        "/api/sessions/session-1/messages",
        "session-1",
    )
    for path in (
        "/api/sessions/%",
        "/api/sessions/%2",
        "/api/sessions/%GG",
        "/api/sessions/%2F",
        "/api/sessions/%5C",
        "/api/sessions/%2E",
        "/api/sessions/%2e%2e",
        "/api/sessions/session-1/unknown",
        "/api/sessions/session-1/messages/extra",
        "/api/sessions//messages",
    ):
        assert _session_route(path) is None


def test_reader_payloads_exclude_rows_outside_authenticated_owner_scope(tmp_path):
    from starlette.exceptions import HTTPException

    from hermes_cli.session_api import (
        empty_count_payload,
        export_session_payload,
        latest_descendant_payload,
        search_sessions_payload,
        session_detail_payload,
        session_messages_payload,
        stats_payload,
    )
    from hermes_cli.session_reader.db import ReadOnlySessionDB
    from hermes_state import SessionDB

    owner_home = tmp_path / "owner"
    workspace_root = str((owner_home / "workspaces").resolve())
    db = SessionDB(owner_home / "state.db")
    try:
        db.create_session(
            "owned",
            source="gui",
            owner_key="ok1_owned",
            workspace_root=workspace_root,
            worker_generation=1,
        )
        db.append_message("owned", "user", "visible owner content")
        db.create_session(
            "owned-empty",
            source="cli",
            owner_key="ok1_owned",
            workspace_root=workspace_root,
            worker_generation=1,
        )
        db.end_session("owned-empty", "completed")
        for session_id, owner_key, workspace, marker in (
            ("foreign-owner", "ok1_foreign", workspace_root, "foreignownermarker"),
            (
                "foreign-workspace",
                "ok1_owned",
                str((owner_home / "other-workspace").resolve()),
                "foreignworkspacemarker",
            ),
        ):
            db.create_session(
                session_id,
                source="gateway",
                owner_key=owner_key,
                workspace_root=workspace,
                worker_generation=2,
            )
            db.append_message(session_id, "user", marker)

        reader_db = ReadOnlySessionDB(owner_home / "state.db")
        try:
            scope = {
                "owner_key": "ok1_owned",
                "workspace_root": workspace_root,
                "historical_resume": True,
            }
            for marker in ("foreignownermarker", "foreignworkspacemarker"):
                assert search_sessions_payload(
                    reader_db,
                    q=marker,
                    recovery_scope=scope,
                ) == {"results": []}
            for session_id in ("foreign-owner", "foreign-workspace"):
                for payload in (
                    session_detail_payload,
                    latest_descendant_payload,
                    session_messages_payload,
                    export_session_payload,
                ):
                    with pytest.raises(HTTPException) as exc_info:
                        payload(reader_db, session_id, recovery_scope=scope)
                    assert exc_info.value.status_code == 404

            assert empty_count_payload(reader_db, recovery_scope=scope) == {"count": 1}
            assert stats_payload(reader_db, recovery_scope=scope) == {
                "total": 2,
                "active_store": 2,
                "archived": 0,
                "messages": 1,
                "by_source": {"gui": 1, "cli": 1},
            }
        finally:
            reader_db.close()
    finally:
        db.close()


def test_prepared_reader_verifier_reuses_keys_but_revalidates_tokens(tmp_path, monkeypatch):
    from hermes_cli.dashboard_auth.authority import AuthorityStore
    from hermes_cli.session_reader import tokens

    store = AuthorityStore(tmp_path / "control")
    lease = store.claim_reader_start(
        "ok1_verifier",
        reader_id="reader-1",
    ).lease
    config = tokens.session_reader_capability_public_config(tmp_path / "control")
    calls = 0
    original = tokens._verifiers

    def tracked_verifiers(**kwargs):
        nonlocal calls
        calls += 1
        return original(**kwargs)

    monkeypatch.setattr(tokens, "_verifiers", tracked_verifiers)
    verifier = tokens.prepare_session_reader_capability_verifier(
        public_key=config["HERMES_SESSION_READER_CAPABILITY_PUBLIC_KEY"],
        issuer_key_version=config["HERMES_SESSION_READER_CAPABILITY_ISSUER"],
        retained_public_keys=config[
            "HERMES_SESSION_READER_CAPABILITY_RETAINED_PUBLIC_KEYS"
        ],
    )
    token = tokens.mint_session_reader_capability(
        lease,
        path="/api/sessions",
        control_home=tmp_path / "control",
        now=100,
    )
    for now in (100, 101):
        tokens.verify_session_reader_capability(
            token,
            expected_lease=lease,
            path="/api/sessions",
            authority_store=None,
            verifier=verifier,
            now=now,
        )
    assert calls == 1
    with pytest.raises(tokens.SessionReaderCapabilityInvalid):
        tokens.verify_session_reader_capability(
            token,
            expected_lease=lease,
            path="/api/sessions/stats",
            authority_store=None,
            verifier=verifier,
            now=101,
        )


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


def test_reader_acquire_active_never_waits_for_concurrent_startup(tmp_path):
    from hermes_cli.session_reader.supervisor import (
        SessionReaderSupervisor,
        SessionReaderUnavailableError,
    )

    supervisor = SessionReaderSupervisor(
        control_home=tmp_path / "control",
        global_home=tmp_path,
    )
    owner = {"owner_key": "ok1_cold", "owner_home": tmp_path / "owner"}
    supervisor._starting.add(owner["owner_key"])

    started = time.perf_counter()
    with pytest.raises(SessionReaderUnavailableError, match="not active"):
        supervisor.acquire_active(owner)
    assert time.perf_counter() - started < 0.05


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
        # Lifecycle startup is measured separately. The business budget starts
        # only once the exact ACTIVE Reader can be pinned without waiting.
        startup_started = time.perf_counter()
        handle = supervisor.ensure_started({"owner_key": owner_key, "owner_home": owner_home})
        startup_elapsed = time.perf_counter() - startup_started
        cold_started = time.perf_counter()
        use = supervisor.acquire_active({"owner_key": owner_key, "owner_home": owner_home})
        lease = use.lease
        client = SessionReaderClient(
            handle.socket_path,
            control_home=control,
            signing_record=supervisor.signing_record,
        )
        loop = asyncio.new_event_loop()
        responses = [
            loop.run_until_complete(client.request(
                "GET",
                "/api/sessions?limit=30&offset=0&order=recent&compact=true",
                lease=lease,
            ))
        ]
        cold_elapsed = time.perf_counter() - cold_started
        use.release()
        warm_started = time.perf_counter()
        responses.append(
            loop.run_until_complete(client.request(
                "GET",
                "/api/sessions?limit=30&offset=0&order=recent&compact=true",
                lease=lease,
            ))
        )
        warm_elapsed = time.perf_counter() - warm_started

        assert all(response.status_code == 200 for response in responses)
        payload = responses[-1].json()
        assert payload["total"] == 3_000
        assert len(payload["sessions"]) == 30
        assert payload["sessions"][0]["id"] == "session-2999-root"
        assert startup_elapsed < 3
        assert cold_elapsed < 0.3, f"ACTIVE Reader request took {cold_elapsed:.3f}s"
        assert warm_elapsed < 0.3, f"warm Reader request took {warm_elapsed:.3f}s"
    finally:
        db.close()
        if "client" in locals():
            loop.run_until_complete(client.aclose())
            loop.close()
        supervisor.shutdown()
        shutil.rmtree(short_root, ignore_errors=True)
