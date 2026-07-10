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

from hermes_cli.dashboard_auth.base import Session
from hermes_cli.dashboard_auth.owner_context import owner_context_from_session
from hermes_cli.owner_runtime import REQUIRED_OWNER_DIRS, ensure_owner_runtime_dirs
from hermes_cli.owner_worker import OwnerWorkerClient, OwnerWorkerSupervisor
from hermes_cli.owner_worker.tokens import (
    AUD_CONTROL_PLANE_WS,
    AUD_OWNER_WORKER_HTTP,
    AUD_OWNER_WORKER_WS,
    child_token_ttl_seconds,
    mint_internal_token,
    validate_internal_token,
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

    def verify_health(self, *, owner_key: str, owner_home):
        return {
            "ready": True,
            "owner_key": owner_key,
            "owner_home": str(Path(owner_home).resolve()),
            "pid": 4321,
            "hermes_home": str(Path(owner_home).resolve()),
        }


def test_internal_token_secret_env_must_match_persisted_secret(tmp_path, monkeypatch):
    secret_path = tmp_path / "owner_worker_token_secret"
    secret_path.write_bytes(b"persisted-secret")
    monkeypatch.setenv("HERMES_OWNER_WORKER_TOKEN_SECRET", "different-secret")

    with pytest.raises(RuntimeError, match="does not match persisted owner worker token secret"):
        mint_internal_token("ok1_owner_a", control_home=tmp_path)


def test_internal_token_is_owner_audience_path_bound_and_expires(tmp_path):
    token = mint_internal_token(
        "ok1_owner_a",
        audience=AUD_OWNER_WORKER_WS,
        path="/api/ws",
        ttl_seconds=2,
        control_home=tmp_path,
    )

    now = int(time.time())
    assert validate_internal_token(
        token,
        "ok1_owner_a",
        audience=AUD_OWNER_WORKER_WS,
        path="/api/ws",
        now=now,
        control_home=tmp_path,
    )
    assert not validate_internal_token(
        token,
        "ok1_owner_b",
        audience=AUD_OWNER_WORKER_WS,
        path="/api/ws",
        now=now,
        control_home=tmp_path,
    )
    assert not validate_internal_token(
        token,
        "ok1_owner_a",
        audience=AUD_CONTROL_PLANE_WS,
        path="/api/ws",
        now=now,
        control_home=tmp_path,
    )
    assert not validate_internal_token(
        token,
        "ok1_owner_a",
        audience=AUD_OWNER_WORKER_WS,
        path="/api/pub",
        now=now,
        control_home=tmp_path,
    )
    assert not validate_internal_token(
        token,
        "ok1_owner_a",
        audience=AUD_OWNER_WORKER_WS,
        path="/api/ws",
        now=now + 5,
        control_home=tmp_path,
    )


def test_internal_token_must_expire_for_server_spawned_children(tmp_path):
    with pytest.raises(ValueError, match="must expire"):
        mint_internal_token("ok1_owner_a", ttl_seconds=None, control_home=tmp_path)  # type: ignore[arg-type]


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


def test_supervisor_rejects_same_key_different_owner_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "control-home"))
    monkeypatch.setenv("HERMES_PROFILE", "control-profile")
    monkeypatch.setenv("HERMES_SESSION_PROFILE", "control-session-profile")
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
    assert child_env.get("TERMINAL_CWD") is None
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
    socket_path = owner_home / "runtime" / "worker.sock"
    owner_home.mkdir(parents=True)
    control_home.mkdir(parents=True)

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
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "HERMES_HOME": str(owner_home), "HERMES_OWNER_KEY": "ok1_worker"},
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
                    health = OwnerWorkerClient(socket_path, control_home=control_home).verify_health(
                        owner_key="ok1_worker",
                        owner_home=owner_home,
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


def test_worker_create_app_fails_when_startup_self_check_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "wrong-owner"))
    monkeypatch.setenv("HERMES_OWNER_KEY", "ok1_worker_routes")
    from hermes_cli.owner_worker.entrypoint import create_app

    owner_home = ensure_owner_runtime_dirs(tmp_path / "owner")

    with pytest.raises(RuntimeError, match="startup self-check failed"):
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

    token = mint_internal_token("ok1_worker_routes", audience=AUD_OWNER_WORKER_HTTP, path="/api/sessions", control_home=tmp_path / "control")
    response = client.get("/api/sessions", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.json()["sessions"] == []

    wrong = mint_internal_token("ok1_other", audience=AUD_OWNER_WORKER_HTTP, path="/api/sessions", control_home=tmp_path / "control")
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

    good = mint_internal_token("ok1_worker_routes", audience=AUD_OWNER_WORKER_HTTP, path="/internal/health", control_home=control_a)
    wrong_secret = mint_internal_token("ok1_worker_routes", audience=AUD_OWNER_WORKER_HTTP, path="/internal/health", control_home=control_b)

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
    wrong_path = mint_internal_token("ok1_worker_routes", audience=AUD_OWNER_WORKER_HTTP, path="/api/sessions", control_home=tmp_path / "control")

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
    token = mint_internal_token(
        "ok1_worker_routes",
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
    token = mint_internal_token("ok1_worker_routes", audience=AUD_OWNER_WORKER_HTTP, path="/api/sessions", control_home=tmp_path / "control")

    response = client.get("/api/sessions?profile=legacy", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 400


def test_worker_sessions_route_reads_owner_db_not_global_sentinel(tmp_path, monkeypatch):
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
    token = mint_internal_token("ok1_worker_routes", audience=AUD_OWNER_WORKER_HTTP, path="/api/sessions", control_home=control_home)

    response = client.get("/api/sessions?limit=20&min_messages=0", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    ids = {session["id"] for session in response.json()["sessions"]}
    assert "owner-visible" in ids
    assert "global-sentinel" not in ids


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
    usage_token = mint_internal_token("ok1_worker_routes", audience=AUD_OWNER_WORKER_HTTP, path="/api/analytics/usage", control_home=tmp_path / "control")
    models_token = mint_internal_token("ok1_worker_routes", audience=AUD_OWNER_WORKER_HTTP, path="/api/analytics/models", control_home=tmp_path / "control")

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
    token = mint_internal_token("ok1_worker_routes", audience=AUD_OWNER_WORKER_HTTP, path="/api/model/info", control_home=tmp_path / "control")

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
    token = mint_internal_token("ok1_worker_routes", audience=AUD_OWNER_WORKER_HTTP, path="/api/analytics/usage", control_home=tmp_path / "control")

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
