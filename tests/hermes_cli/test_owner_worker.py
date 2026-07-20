from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

from hermes_cli.dashboard_auth.authority import (
    AuthorityStore,
    AuthorizationRejected,
    WorkerGenerationState,
    WorkerLeaseState,
)
from hermes_cli.dashboard_auth.base import Session
from hermes_cli.dashboard_auth.owner_context import owner_context_from_session
from hermes_cli.controlled_roots import RootKind, controlled_roots_for
from hermes_cli.owner_runtime import REQUIRED_OWNER_DIRS, ensure_owner_runtime_dirs, owner_worker_socket_path
from hermes_cli.owner_worker import (
    OwnerWorkerClient,
    OwnerWorkerHealthError,
    OwnerWorkerStartupError,
    OwnerWorkerSupervisor,
    OwnerWorkerUnavailableError,
)
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


@pytest.fixture(autouse=True)
def _simulate_linux_controlled_roots(monkeypatch):
    import hermes_cli.controlled_roots as controlled_roots
    import hermes_cli.owner_worker.supervisor as supervisor_module

    monkeypatch.setattr(controlled_roots.ControlledRoots, "_require_linux", lambda _self: None)
    monkeypatch.setattr(controlled_roots, "_openat2", lambda *_args: None)
    monkeypatch.setattr(
        supervisor_module,
        "_seed_owner_worker_skills",
        lambda _owner_home: {"copied": [], "updated": []},
    )


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


def test_worker_ws_bootstrap_resolves_active_durable_lease_from_starting_config(tmp_path):
    from types import SimpleNamespace

    from hermes_cli.owner_worker import ws_routes

    control_home = tmp_path / "control"
    store = AuthorityStore(control_home)
    claim = store.claim_worker_start("ok1_ws_worker", worker_id="worker-a")
    active = store.transition_worker_lease(
        claim.lease,
        state=WorkerLeaseState.ACTIVE,
        generation_state=WorkerGenerationState.ACTIVE,
    )
    verifier = owner_worker_capability_public_config(control_home)
    app = SimpleNamespace(
        state=SimpleNamespace(
            owner_worker_control_home=control_home,
            owner_worker_lease=claim.lease,
            owner_worker_capability_verifier=verifier,
        ),
    )

    resolved = ws_routes._active_bootstrap_lease(app, claim.lease)

    assert resolved == active

    class _WebSocket:
        def __init__(self, token):
            self.app = app
            self.query_params = {"internal_owner_bootstrap": token}
            self.url = type("Url", (), {"path": "/api/events"})()
            self.accepted = False
            self.closed = []
            self.sent = []

        async def accept(self):
            self.accepted = True

        async def receive_text(self):
            return hello

        async def send_text(self, value):
            self.sent.append(value)

        async def close(self, **kwargs):
            self.closed.append(kwargs)

    from hermes_cli.owner_worker.tokens import owp1_hello, parse_owner_worker_bootstrap

    token = mint_owner_worker_bootstrap(
        active,
        path="/api/events",
        connection_id="connection_identifier_1234",
        nonce="control_nonce_identifier_1234",
        control_home=control_home,
    )
    claims = parse_owner_worker_bootstrap(
        token,
        expected_lease=active,
        path="/api/events",
        public_key=verifier["HERMES_OWNER_WORKER_CAPABILITY_PUBLIC_KEY"],
        issuer_key_version=verifier["HERMES_OWNER_WORKER_CAPABILITY_ISSUER"],
    )
    hello = owp1_hello(claims)
    websocket = _WebSocket(token)
    peer = asyncio.run(ws_routes._admit_bootstrap_or_close(websocket))

    assert peer is not None
    assert websocket.accepted is True
    assert websocket.closed == []
    store.invalidate_outstanding_credentials(reason="replacement")
    replacement = store.claim_worker_start("ok1_ws_worker", worker_id="worker-b")
    with pytest.raises(OwnerWorkerCapabilityInvalid, match="identity_mismatch"):
        ws_routes._active_bootstrap_lease(app, claim.lease)
    assert replacement.lease.state is WorkerLeaseState.STARTING


def test_events_ws_uses_admitted_owp1_peer_without_second_accept(monkeypatch):
    from hermes_cli.owner_worker import ws_routes

    class _Peer:
        def __init__(self):
            self.received = 0
            self.closed = []

        async def receive_text(self):
            self.received += 1
            raise ws_routes.WebSocketDisconnect(code=1000)

        async def close(self, **kwargs):
            self.closed.append(kwargs)

    peer = _Peer()
    app = type("App", (), {"state": type("State", (), {})()})()
    websocket = type(
        "WebSocket",
        (),
        {
            "app": app,
            "query_params": {"channel": "events-test"},
            "accept": lambda _self: (_ for _ in ()).throw(AssertionError("second websocket accept")),
        },
    )()

    async def _admit(_ws):
        return peer

    monkeypatch.setattr(ws_routes, "_admit_bootstrap_or_close", _admit)
    asyncio.run(ws_routes.events_ws(websocket))

    assert peer.received == 1
    assert app.state.owner_worker_live_state.event_channels == {}


def test_pub_ws_uses_admitted_owp1_peer_without_second_accept(monkeypatch):
    from hermes_cli.owner_worker import ws_routes

    class _Peer:
        def __init__(self):
            self.received = 0
            self.closed = []

        async def receive_text(self):
            self.received += 1
            raise ws_routes.WebSocketDisconnect(code=1000)

        async def close(self, **kwargs):
            self.closed.append(kwargs)

    peer = _Peer()
    app = type("App", (), {"state": type("State", (), {})()})()
    websocket = type(
        "WebSocket",
        (),
        {
            "app": app,
            "query_params": {"channel": "pub-test"},
            "accept": lambda _self: (_ for _ in ()).throw(AssertionError("second websocket accept")),
        },
    )()

    async def _admit(_ws):
        return peer

    monkeypatch.setattr(ws_routes, "_admit_bootstrap_or_close", _admit)
    asyncio.run(ws_routes.pub_ws(websocket))

    assert peer.received == 1


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


def test_supervisor_syncs_owner_skills_before_process_launch(tmp_path, monkeypatch):
    import hermes_cli.owner_worker.supervisor as supervisor_module

    owner = _Owner("ok1_skill_sync", tmp_path / "owner")
    events: list[tuple[str, Path]] = []

    def fake_seed(owner_home: Path):
        events.append(("seed", Path(owner_home)))
        skill = Path(owner_home) / "skills" / "productivity" / "common-files"
        skill.mkdir(parents=True)
        skill.joinpath("SKILL.md").write_text(
            "---\nname: common-files\ndescription: test\n---\n",
            encoding="utf-8",
        )
        return {"copied": ["common-files"]}

    def fake_process_factory(*args, **kwargs):
        del kwargs
        argv = args[0]
        events.append(("spawn", owner.owner_home))
        assert owner.owner_home.joinpath(
            "skills/productivity/common-files/SKILL.md"
        ).is_file()
        Path(argv[argv.index("--socket") + 1]).touch()
        return _FakeProcess()

    monkeypatch.setattr(supervisor_module, "_seed_owner_worker_skills", fake_seed)
    supervisor = OwnerWorkerSupervisor(
        control_home=tmp_path / "control",
        client_cls=_FakeClient,
        process_factory=fake_process_factory,
        startup_timeout=0.1,
    )

    supervisor.get_or_start(owner)

    assert events == [("seed", owner.owner_home), ("spawn", owner.owner_home)]
    supervisor.shutdown()


def test_supervisor_skill_sync_failure_prevents_generation_claim(tmp_path, monkeypatch):
    import hermes_cli.owner_worker.supervisor as supervisor_module

    owner = _Owner("ok1_skill_sync_failure", tmp_path / "owner")
    spawned = False

    def fail_seed(_owner_home: Path):
        raise TimeoutError("sync lock unavailable")

    def fake_process_factory(*_args, **_kwargs):
        nonlocal spawned
        spawned = True
        return _FakeProcess()

    monkeypatch.setattr(supervisor_module, "_seed_owner_worker_skills", fail_seed)
    supervisor = OwnerWorkerSupervisor(
        control_home=tmp_path / "control",
        client_cls=_FakeClient,
        process_factory=fake_process_factory,
        startup_timeout=0.1,
    )

    with pytest.raises(OwnerWorkerStartupError, match="skill synchronization failed"):
        supervisor.get_or_start(owner)

    assert spawned is False
    assert supervisor.authority_store.read_owner_worker_lease(owner.owner_key) is None


def test_supervisor_spawns_the_canonical_session_owner_context(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "global"))
    monkeypatch.setenv("HERMES_OWNER_SECRET", "owner-secret")
    monkeypatch.setenv("PYTHONPATH", "/operator/pythonpath")
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
    package_import_root = str(Path(__file__).resolve().parents[2])
    assert child_env["PYTHONPATH"].split(os.pathsep) == [
        package_import_root,
        "/operator/pythonpath",
    ]
    assert argv[argv.index("--worker-generation") + 1] == "1"
    assert argv[argv.index("--worker-id") + 1] == child_env["HERMES_WORKER_ID"]


def test_supervisor_passes_only_safe_deployment_descriptor(tmp_path):
    from hermes_cli.deployment_inference import DeploymentInferencePolicy

    owner = _Owner("ok1_deployment", tmp_path / "owner")
    spawned: list[dict] = []

    def fake_process_factory(*args, **kwargs):
        spawned.append({"args": args, "kwargs": kwargs})
        argv = args[0]
        Path(argv[argv.index("--socket") + 1]).touch()
        return _FakeProcess()

    policy = DeploymentInferencePolicy(
        provider="custom:deployment",
        model="gpt-safe",
        api_mode="chat_completions",
        runtime_resolver=lambda: {
            "provider": "custom:deployment",
            "api_mode": "chat_completions",
            "base_url": "https://provider.example.test/v1",
            "api_key": "control-plane-secret",
        },
    )
    supervisor = OwnerWorkerSupervisor(
        control_home=tmp_path / "control",
        client_cls=_FakeClient,
        process_factory=fake_process_factory,
        startup_timeout=0.1,
        deployment_inference_policy=policy,
    )

    supervisor.get_or_start(owner)

    child_env = spawned[0]["kwargs"]["env"]
    assert child_env["HERMES_DEPLOYMENT_INFERENCE_PROVIDER"] == "custom:deployment"
    assert child_env["HERMES_DEPLOYMENT_INFERENCE_MODEL"] == "gpt-safe"
    assert "control-plane-secret" not in child_env.values()
    assert "https://provider.example.test/v1" not in child_env.values()
    assert "HERMES_DEPLOYMENT_INFERENCE_RELAY_FD" in child_env
    supervisor.shutdown()


def test_supervisor_reclaims_conclusively_absent_orphan_lease(tmp_path):
    owner = _Owner("ok1_orphan", tmp_path / "owner")
    spawned: list[dict] = []

    def fake_process_factory(*args, **kwargs):
        spawned.append({"args": args, "kwargs": kwargs})
        argv = args[0]
        Path(argv[argv.index("--socket") + 1]).touch()
        return _FakeProcess()

    first = OwnerWorkerSupervisor(
        control_home=tmp_path / "control",
        client_cls=_FakeClient,
        process_factory=fake_process_factory,
        startup_timeout=0.1,
        startup_cooldown=0,
    )
    handle = first.get_or_start(owner)
    handle.socket_path.unlink()
    first.authority_store.transition_worker_lease(
        first._lease_for_handle(handle),
        state=WorkerLeaseState.DRAINING,
        generation_state=WorkerGenerationState.DRAINING,
    )
    restarted = OwnerWorkerSupervisor(
        control_home=tmp_path / "control",
        client_cls=_FakeClient,
        process_factory=fake_process_factory,
        startup_timeout=0.1,
        startup_cooldown=0,
    )

    replacement = restarted.get_or_start(owner)

    assert replacement.worker_generation == 2
    assert first.authority_store.read_worker_generation(owner.owner_key, 1).state is WorkerGenerationState.REVOKED
    assert restarted.authority_store.read_worker_generation(owner.owner_key, 2).state is WorkerGenerationState.ACTIVE
    assert len(spawned) == 2


def test_supervisor_does_not_reclaim_healthy_or_ambiguous_orphan_lease(tmp_path):
    owner = _Owner("ok1_orphan_live", tmp_path / "owner")

    def fake_process_factory(*args, **kwargs):
        argv = args[0]
        Path(argv[argv.index("--socket") + 1]).touch()
        return _FakeProcess()

    first = OwnerWorkerSupervisor(
        control_home=tmp_path / "control",
        client_cls=_FakeClient,
        process_factory=fake_process_factory,
        startup_timeout=0.1,
        startup_cooldown=0,
    )
    first.get_or_start(owner)
    restarted = OwnerWorkerSupervisor(
        control_home=tmp_path / "control",
        client_cls=_FakeClient,
        process_factory=fake_process_factory,
        startup_timeout=0.1,
        startup_cooldown=0,
    )

    with pytest.raises(OwnerWorkerUnavailableError, match="already_owned"):
        restarted.get_or_start(owner)

    assert restarted.authority_store.read_owner_worker_lease(owner.owner_key).state is WorkerLeaseState.ACTIVE


def test_supervisor_shutdown_drains_all_local_workers_in_order(tmp_path):
    owner = _Owner("ok1_shutdown", tmp_path / "owner")
    events = []

    class _OrderedProcess(_FakeProcess):
        def terminate(self):
            events.append("terminate")
            super().terminate()

    def fake_process_factory(*args, **kwargs):
        argv = args[0]
        Path(argv[argv.index("--socket") + 1]).touch()
        return _OrderedProcess()

    supervisor = OwnerWorkerSupervisor(
        control_home=tmp_path / "control",
        client_cls=_FakeClient,
        process_factory=fake_process_factory,
        startup_timeout=0.1,
        startup_cooldown=0,
        generation_bridge_revoker=lambda _lease: events.append("bridges"),
    )
    handle = supervisor.get_or_start(owner)

    supervisor.shutdown()

    assert events == ["bridges", "terminate"]
    assert supervisor._handles == {}
    assert not handle.socket_path.exists()
    assert supervisor.authority_store.read_owner_worker_lease(owner.owner_key).state is WorkerLeaseState.REVOKED


def test_supervisor_shutdown_revoker_can_release_active_use_from_event_loop(tmp_path):
    """Synchronous bridge revocation must not retain the supervisor lock."""
    owner = _Owner("ok1_shutdown_release", tmp_path / "owner")
    events = []
    loop = asyncio.new_event_loop()
    loop_ready = threading.Event()

    def run_loop():
        asyncio.set_event_loop(loop)
        loop_ready.set()
        loop.run_forever()

    loop_thread = threading.Thread(target=run_loop)
    loop_thread.start()
    assert loop_ready.wait(timeout=1)

    def fake_process_factory(*args, **kwargs):
        argv = args[0]
        Path(argv[argv.index("--socket") + 1]).touch()
        return _FakeProcess()

    supervisor = OwnerWorkerSupervisor(
        control_home=tmp_path / "control",
        client_cls=_FakeClient,
        process_factory=fake_process_factory,
        startup_timeout=0.1,
        startup_cooldown=0,
    )
    handle = supervisor.get_or_start(owner)
    use_lease = supervisor.acquire_use(handle)

    async def close_bridge():
        use_lease.release()
        events.append("lease_released")

    def revoke_bridges(lease):
        assert lease.state is WorkerLeaseState.DRAINING
        future = asyncio.run_coroutine_threadsafe(close_bridge(), loop)
        future.result(timeout=1)
        events.append("bridges_closed")

    supervisor.generation_bridge_revoker = revoke_bridges
    try:
        supervisor.shutdown()
    finally:
        loop.call_soon_threadsafe(loop.stop)
        loop_thread.join(timeout=1)
        loop.close()

    assert events == ["lease_released", "bridges_closed"]
    assert handle.active_uses == 0
    assert handle.process.terminated
    assert supervisor._handles == {}
    assert supervisor._terminating_handles == {}


def test_supervisor_get_or_start_skips_health_probe_while_generation_is_in_use(tmp_path):
    """A concurrent request must not retire a generation with a live bridge."""
    owner = _Owner("ok1_health_in_use", tmp_path / "owner")

    class _FailsAfterStartupClient(_FakeClient):
        health_calls = 0

        def verify_health(self, **kwargs):
            type(self).health_calls += 1
            if type(self).health_calls > 1:
                raise OwnerWorkerHealthError("worker temporarily unavailable")
            return super().verify_health(**kwargs)

    def fake_process_factory(*args, **kwargs):
        argv = args[0]
        Path(argv[argv.index("--socket") + 1]).touch()
        return _FakeProcess()

    supervisor = OwnerWorkerSupervisor(
        control_home=tmp_path / "control",
        client_cls=_FailsAfterStartupClient,
        process_factory=fake_process_factory,
        startup_timeout=0.1,
        startup_cooldown=0,
    )
    first = supervisor.get_or_start(owner)
    use_lease = supervisor.acquire_use(first)
    try:
        reused = supervisor.get_or_start(owner)
    finally:
        use_lease.release()

    assert reused is first
    assert _FailsAfterStartupClient.health_calls == 1
    assert not first.process.terminated
    assert supervisor._handles[owner.owner_key] is first


def test_supervisor_get_or_start_failed_health_retires_idle_generation(tmp_path):
    """A failed health check still retires a generation without active uses."""
    owner = _Owner("ok1_health_idle", tmp_path / "owner")

    class _FailsAfterStartupClient(_FakeClient):
        health_calls = 0

        def verify_health(self, **kwargs):
            type(self).health_calls += 1
            if type(self).health_calls == 2:
                raise OwnerWorkerHealthError("worker unavailable")
            return super().verify_health(**kwargs)

    def fake_process_factory(*args, **kwargs):
        argv = args[0]
        Path(argv[argv.index("--socket") + 1]).touch()
        return _FakeProcess(pid=4300 + _FailsAfterStartupClient.health_calls)

    supervisor = OwnerWorkerSupervisor(
        control_home=tmp_path / "control",
        client_cls=_FailsAfterStartupClient,
        process_factory=fake_process_factory,
        startup_timeout=0.1,
        startup_cooldown=0,
    )
    first = supervisor.get_or_start(owner)
    replacement = supervisor.get_or_start(owner)

    assert replacement is not first
    assert first.process.terminated
    assert supervisor._handles[owner.owner_key] is replacement
    assert supervisor._terminating_handles == {}


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
    assert "cwd" not in spawned[0]["kwargs"]
    assert spawned[0]["kwargs"]["pass_fds"]
    assert callable(spawned[0]["kwargs"]["preexec_fn"])
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

    with pytest.raises(OSError):
        os.fstat(captured["stdout"])
    with pytest.raises(OSError):
        os.fstat(captured["stderr"])


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


def test_supervisor_waiting_same_owner_start_times_out_without_releasing_leader(tmp_path):
    owner = _Owner("ok1_stalled", tmp_path / "owner")
    startup_entered = threading.Event()
    release_startup = threading.Event()
    spawned: list[dict] = []
    results: list[object] = []
    errors: list[BaseException] = []

    def fake_process_factory(*args, **kwargs):
        spawned.append({"args": args, "kwargs": kwargs})
        startup_entered.set()
        assert release_startup.wait(timeout=2)
        argv = args[0]
        Path(argv[argv.index("--socket") + 1]).touch()
        return _FakeProcess()

    supervisor = OwnerWorkerSupervisor(
        control_home=tmp_path / "control",
        client_cls=_FakeClient,
        process_factory=fake_process_factory,
        startup_timeout=1,
        startup_cooldown=0,
    )

    def start_leader():
        try:
            results.append(supervisor.get_or_start(owner))
        except BaseException as exc:  # pragma: no cover - makes thread errors visible
            errors.append(exc)

    leader = threading.Thread(target=start_leader)
    leader.start()
    assert startup_entered.wait(timeout=2)

    with pytest.raises(TimeoutError, match="timed out waiting for owner worker startup"):
        supervisor.get_or_start(owner, timeout=0.01)

    assert len(spawned) == 1
    assert owner.owner_key in supervisor._starting_owner_keys
    assert supervisor._in_flight_starts == 1

    release_startup.set()
    leader.join(timeout=2)
    assert not leader.is_alive()
    assert errors == []
    assert len(results) == 1
    assert supervisor.get_or_start(owner) is results[0]
    assert supervisor._starting_owner_keys == set()
    assert supervisor._in_flight_starts == 0
    assert len(spawned) == 1


def test_supervisor_starts_different_owners_in_parallel(tmp_path):
    owners = [_Owner("ok1_parallel_a", tmp_path / "a"), _Owner("ok1_parallel_b", tmp_path / "b")]
    spawned: list[str] = []
    factory_barrier = threading.Barrier(2, timeout=2)
    results: list[object] = []
    errors: list[BaseException] = []

    def fake_process_factory(*args, **_kwargs):
        argv = args[0]
        owner_key = argv[argv.index("--owner-key") + 1]
        spawned.append(owner_key)
        Path(argv[argv.index("--socket") + 1]).touch()
        factory_barrier.wait()
        return _FakeProcess()

    supervisor = OwnerWorkerSupervisor(
        control_home=tmp_path / "control",
        client_cls=_FakeClient,
        process_factory=fake_process_factory,
        startup_timeout=0.1,
        startup_cooldown=0,
        max_workers=2,
    )

    def start(owner):
        try:
            results.append(supervisor.get_or_start(owner))
        except BaseException as exc:  # pragma: no cover - makes thread errors visible
            errors.append(exc)

    threads = [threading.Thread(target=start, args=(owner,)) for owner in owners]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()

    assert errors == []
    assert set(spawned) == {owner.owner_key for owner in owners}
    assert {result.owner_key for result in results} == {owner.owner_key for owner in owners}


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


def test_supervisor_reaps_already_exited_process_with_wait(tmp_path):
    owner = _Owner("ok1_reaped", tmp_path / "owner")

    def fake_process_factory(*args, **kwargs):
        argv = args[0]
        Path(argv[argv.index("--socket") + 1]).touch()
        return _FakeProcess()

    supervisor = OwnerWorkerSupervisor(
        control_home=tmp_path / "control",
        client_cls=_FakeClient,
        process_factory=fake_process_factory,
        startup_timeout=0.1,
        startup_cooldown=0,
    )
    handle = supervisor.get_or_start(owner)
    handle.process.returncode = 0

    supervisor._reap_exited()

    assert handle.process.wait_calls == 1
    assert not handle.process.terminated
    assert not handle.process.killed
    assert supervisor._handles == {}
    assert not handle.socket_path.exists()
    assert supervisor.authority_store.read_worker_generation(owner.owner_key, 1).state is WorkerGenerationState.TERMINATED


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


def test_supervisor_startup_throttle_is_per_owner(tmp_path):
    owners = [_Owner("ok1_throttle_a", tmp_path / "a"), _Owner("ok1_throttle_b", tmp_path / "b")]
    spawned = []

    def fake_process_factory(*args, **kwargs):
        spawned.append((args, kwargs))
        argv = args[0]
        Path(argv[argv.index("--socket") + 1]).touch()
        return _FakeProcess()

    supervisor = OwnerWorkerSupervisor(
        control_home=tmp_path / "control", client_cls=_FakeClient, process_factory=fake_process_factory,
        startup_timeout=0.1, startup_cooldown=3600,
    )
    first = supervisor.get_or_start(owners[0])
    supervisor._terminate_handle(owners[0].owner_key, first)
    with pytest.raises(OwnerWorkerUnavailableError, match="startup throttled"):
        supervisor.get_or_start(owners[0])
    second = supervisor.get_or_start(owners[1])

    assert second.owner_key == owners[1].owner_key
    assert len(spawned) == 2


def test_supervisor_startup_exit_is_typed_and_releases_the_fence(tmp_path):
    owner = _Owner("ok1_startup_exit", tmp_path / "owner")

    def fake_process_factory(*args, **kwargs):
        del kwargs
        argv = args[0]
        Path(argv[argv.index("--socket") + 1]).touch()
        process = _FakeProcess()
        process.returncode = 1
        return process

    supervisor = OwnerWorkerSupervisor(
        control_home=tmp_path / "control",
        client_cls=_FakeClient,
        process_factory=fake_process_factory,
        startup_timeout=0.1,
        startup_cooldown=0,
    )

    with pytest.raises(OwnerWorkerStartupError, match="exited during startup"):
        supervisor.get_or_start(owner)

    assert supervisor._handles == {}
    assert supervisor._starting_owner_keys == set()
    assert supervisor._in_flight_starts == 0
    assert supervisor.authority_store.read_owner_worker_lease(owner.owner_key).state is WorkerLeaseState.REVOKED
    assert supervisor.authority_store.read_worker_generation(owner.owner_key, 1).state is WorkerGenerationState.FAILED


def test_supervisor_owner_concurrency_cap_is_exact_handle_scoped(tmp_path):
    owners = [_Owner("ok1_concurrent_a", tmp_path / "a"), _Owner("ok1_concurrent_b", tmp_path / "b")]

    def fake_process_factory(*args, **kwargs):
        argv = args[0]
        Path(argv[argv.index("--socket") + 1]).touch()
        return _FakeProcess()

    supervisor = OwnerWorkerSupervisor(
        control_home=tmp_path / "control", client_cls=_FakeClient, process_factory=fake_process_factory,
        startup_timeout=0.1, startup_cooldown=0, max_owner_concurrency=1,
    )
    first = supervisor.get_or_start(owners[0])
    second = supervisor.get_or_start(owners[1])
    lease = supervisor.acquire_use(first)

    with pytest.raises(RuntimeError, match="concurrency limit"):
        supervisor.acquire_use(first)
    assert first.active_uses == 1
    other_lease = supervisor.acquire_use(second)
    assert second.active_uses == 1

    other_lease.release()
    assert first.active_uses == 1
    lease.release()
    assert first.active_uses == 0


def test_supervisor_capacity_evicts_recent_idle_lru_and_cold_starts_new_owner(tmp_path):
    owners = [_Owner("ok1_capacity_a", tmp_path / "a"), _Owner("ok1_capacity_b", tmp_path / "b")]
    spawned = []

    def fake_process_factory(*args, **kwargs):
        spawned.append((args, kwargs))
        argv = args[0]
        Path(argv[argv.index("--socket") + 1]).touch()
        return _FakeProcess(pid=4000 + len(spawned))

    supervisor = OwnerWorkerSupervisor(
        control_home=tmp_path / "control", client_cls=_FakeClient, process_factory=fake_process_factory,
        startup_timeout=0.1, startup_cooldown=0, idle_timeout=3600, max_workers=1,
    )
    first = supervisor.get_or_start(owners[0])
    first.last_used_at = time.time()
    second = supervisor.get_or_start(owners[1])

    assert first.process.terminated
    assert second.owner_key == owners[1].owner_key
    assert second.owner_home == owners[1].owner_home.resolve()
    assert second.socket_path != first.socket_path
    assert len(spawned) == 2
    assert owners[0].owner_key not in supervisor._handles
    assert supervisor._handles[owners[1].owner_key] is second


def test_supervisor_capacity_with_only_active_workers_fails_closed_before_spawn(tmp_path):
    owners = [_Owner("ok1_capacity_active", tmp_path / "a"), _Owner("ok1_capacity_blocked", tmp_path / "b")]
    spawned = []

    def fake_process_factory(*args, **kwargs):
        spawned.append((args, kwargs))
        argv = args[0]
        Path(argv[argv.index("--socket") + 1]).touch()
        return _FakeProcess()

    supervisor = OwnerWorkerSupervisor(
        control_home=tmp_path / "control", client_cls=_FakeClient, process_factory=fake_process_factory,
        startup_timeout=0.1, startup_cooldown=0, max_workers=1,
    )
    active = supervisor.get_or_start(owners[0])
    lease = supervisor.acquire_use(active)
    try:
        with pytest.raises(RuntimeError, match="limit reached"):
            supervisor.get_or_start(owners[1])
    finally:
        lease.release()
    assert len(spawned) == 1
    assert owners[1].owner_key not in supervisor._handles


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


def test_worker_health_over_unix_socket_reports_owner_env(tmp_path, monkeypatch):
    # macOS AF_UNIX paths are capped at 104 bytes. pytest's default temporary
    # root can exceed that before the owner runtime suffix is appended, so keep
    # this real-subprocess socket under a short, per-process temporary root.
    socket_root = Path("/tmp") / f"h{os.getpid():x}"
    socket_root.mkdir(mode=0o700, exist_ok=True)
    owner_home = ensure_owner_runtime_dirs(socket_root / "u")
    control_home = socket_root / "c"
    control_home.mkdir(parents=True)
    (owner_home / "config.yaml").write_text(
        "platform_toolsets:\n  cli:\n    - x_search\n",
        encoding="utf-8",
    )
    (owner_home / "logs" / "agent.log").write_text(
        "2026-07-20 10:00:00 WARNING tools.terminal_tool: subprocess owner marker\n",
        encoding="utf-8",
    )
    owner = _Owner("ok1_worker", owner_home)
    supervisor = OwnerWorkerSupervisor(
        control_home=control_home,
        global_home=socket_root / "global",
        startup_timeout=10,
    )
    claim = supervisor.authority_store.claim_worker_start(
        owner.owner_key,
        worker_id="subprocess-test-worker",
    )
    socket_path = supervisor.socket_path_for(owner, claim.generation.worker_generation)
    # The real service starts the child from its owner's workspace. Remove the
    # test runner's ambient import path and use the supervisor's env/argv so
    # this regression fails if the launch context stops exporting the package
    # root needed by ``python -m hermes_cli.owner_worker.entrypoint``.
    monkeypatch.delenv("PYTHONPATH", raising=False)
    worker_env = supervisor._env_for(owner, claim.generation, claim.lease)
    proc = subprocess.Popen(
        supervisor._argv_for(owner, socket_path, claim.generation),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=owner_home / "workspaces" / "default",
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

        response = OwnerWorkerClient(socket_path, control_home=control_home, timeout=10).request(
            "GET",
            "/api/tools/toolsets",
            lease=lease,
        )
        assert response.status_code == 200
        toolsets_by_name = {item["name"]: item for item in response.json()}
        assert toolsets_by_name["x_search"]["enabled"] is True
        assert toolsets_by_name["x_search"]["available"] is True

        if sys.platform.startswith("linux"):
            logs = OwnerWorkerClient(socket_path, control_home=control_home).request(
                "GET",
                "/api/logs?file=agent&level=WARNING&component=tools&search=marker",
                lease=lease,
            )
            assert logs.status_code == 200
            assert logs.json() == {
                "file": "agent",
                "lines": [
                    "2026-07-20 10:00:00 WARNING tools.terminal_tool: subprocess owner marker\n"
                ],
            }

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


def test_worker_app_owns_and_closes_controlled_roots(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from hermes_cli.owner_worker.entrypoint import create_app

    owner_home = ensure_owner_runtime_dirs(tmp_path / "owner")
    monkeypatch.setenv("HERMES_HOME", str(owner_home))
    monkeypatch.setenv("HERMES_OWNER_KEY", "ok1_worker_roots")
    monkeypatch.setenv("HERMES_CONTROL_HOME", str(tmp_path / "control"))
    app = create_app("ok1_worker_roots", owner_home)
    roots = app.state.owner_worker_controlled_roots
    workspace_fd = roots.get(RootKind.WORKSPACE).directory_fd

    assert roots.get(RootKind.OWNER_WRITABLE).canonical_path == owner_home
    assert roots.get(RootKind.WORKSPACE).canonical_path == owner_home / "workspaces"
    assert roots.get(RootKind.TEMPORARY).canonical_path == owner_home / "runtime" / "tmp"
    assert app.state.owner_worker_socket_path == owner_worker_socket_path(
        owner_home, app.state.owner_worker_generation
    )
    with TestClient(app):
        assert os.fstat(workspace_fd)

    with pytest.raises(OSError):
        os.fstat(workspace_fd)


def test_worker_create_app_fails_closed_when_controlled_roots_are_unavailable(tmp_path, monkeypatch):
    import hermes_cli.owner_worker.entrypoint as entrypoint

    owner_home = ensure_owner_runtime_dirs(tmp_path / "owner")
    monkeypatch.setenv("HERMES_HOME", str(owner_home))
    monkeypatch.setenv("HERMES_OWNER_KEY", "ok1_worker_roots")
    monkeypatch.setenv("HERMES_CONTROL_HOME", str(tmp_path / "control"))
    monkeypatch.setattr(entrypoint, "controlled_roots_for", lambda _paths: (_ for _ in ()).throw(RuntimeError("unsupported")))

    with pytest.raises(RuntimeError, match="startup self-check failed: unsupported"):
        entrypoint.create_app("ok1_worker_roots", owner_home)


def test_worker_entrypoint_wires_only_explicit_deployment_policy(tmp_path, monkeypatch):
    import hermes_cli.owner_worker.entrypoint as entrypoint
    import hermes_cli.owner_worker.tool_executor_sandbox as sandbox
    import hermes_cli.owner_worker.tool_executor_supervisor as supervisor_module

    owner_home = ensure_owner_runtime_dirs(tmp_path / "owner")
    monkeypatch.setenv("HERMES_HOME", str(owner_home))
    monkeypatch.setenv("HERMES_OWNER_KEY", "ok1_worker_policy")
    monkeypatch.setenv("HERMES_CONTROL_HOME", str(tmp_path / "control"))
    policy = object()
    constructed = []

    class _Supervisor:
        def __init__(self, **kwargs):
            constructed.append(kwargs)

        def stop_generation(self):
            return None

    monkeypatch.setattr(sandbox, "load_sandbox_deployment_policy", lambda spec: policy)
    monkeypatch.setattr(supervisor_module, "ToolExecutorSupervisor", _Supervisor)
    monkeypatch.setenv("HERMES_SANDBOX_DEPLOYMENT_POLICY", "operator_policy:build")

    app = entrypoint.create_app("ok1_worker_policy", owner_home)

    assert len(constructed) == 1
    assert constructed[0]["deployment_policy"] is policy
    assert constructed[0]["owner_home"] == owner_home
    assert app.state.tool_executor_supervisor is not None
    assert app.state.owner_worker_live_state.gateway_runtime.tool_executor_supervisor is app.state.tool_executor_supervisor


def test_worker_entrypoint_missing_deployment_policy_disables_executor(tmp_path, monkeypatch):
    import hermes_cli.owner_worker.entrypoint as entrypoint
    import hermes_cli.owner_worker.tool_executor_sandbox as sandbox

    owner_home = ensure_owner_runtime_dirs(tmp_path / "owner")
    monkeypatch.setenv("HERMES_HOME", str(owner_home))
    monkeypatch.setenv("HERMES_OWNER_KEY", "ok1_worker_policy")
    monkeypatch.setenv("HERMES_CONTROL_HOME", str(tmp_path / "control"))
    monkeypatch.delenv("HERMES_SANDBOX_DEPLOYMENT_POLICY", raising=False)
    monkeypatch.setattr(
        sandbox,
        "load_sandbox_deployment_policy",
        lambda _spec: (_ for _ in ()).throw(RuntimeError("missing operator policy")),
    )

    app = entrypoint.create_app("ok1_worker_policy", owner_home)

    assert app.state.tool_executor_supervisor is None
    assert app.state.tool_executor_startup_error == "sandbox deployment policy unavailable"
    assert app.state.owner_worker_live_state.gateway_runtime.tool_executor_supervisor is None


def test_owner_worker_pty_lifecycle_audit_is_admitted_then_terminal_once(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from hermes_cli.dashboard_auth.audit import AuthorityAuditReason
    from hermes_cli.owner_worker import ws_routes

    events = []

    class _WebSocket:
        def __init__(self):
            self.app = SimpleNamespace(state=SimpleNamespace(owner_worker_generation=7))
            self.query_params = SimpleNamespace(get=lambda _key, default="": default)
            self.url = SimpleNamespace(path="/api/pty")
            self.accepted = False
            self.closed = []

        async def accept(self):
            self.accepted = True

        async def close(self, **kwargs):
            self.closed.append(kwargs)

        async def receive(self):
            return {"type": "websocket.disconnect"}

        async def send_bytes(self, _data):
            return None

    class _Bridge:
        def __init__(self):
            self.closed = 0

        def read(self, _timeout):
            return None

        def close(self):
            self.closed += 1

        def exit_code(self):
            return 0

        def resize(self, **_kwargs):
            return None

        def write(self, _data):
            return None

    bridge = _Bridge()
    websocket = _WebSocket()
    monkeypatch.setattr(ws_routes, "_admit_bootstrap_or_close", lambda _ws: asyncio.sleep(0, result=websocket))
    monkeypatch.setattr(ws_routes, "_trusted_live_metadata", lambda *_args: ("trusted",))
    monkeypatch.setattr(ws_routes, "_PTY_BRIDGE_AVAILABLE", True)
    monkeypatch.setattr(ws_routes, "_resolve_chat_argv_async", lambda **_kwargs: asyncio.sleep(0, result=(["test"], None, {})))
    monkeypatch.setattr(ws_routes, "PtyBridge", SimpleNamespace(spawn=lambda *_args, **_kwargs: bridge))
    monkeypatch.setattr(ws_routes, "_report_pty_lifecycle", lambda _app, reason: events.append(reason))

    asyncio.run(ws_routes.pty_ws(websocket))

    assert websocket.accepted is True
    assert events == [AuthorityAuditReason.ADMITTED, AuthorityAuditReason.BRIDGE_CLOSED]
    assert bridge.closed >= 1


@pytest.mark.parametrize(
    ("exit_code", "expected_close"),
    [
        (0, {}),
        (1, {"code": 1001, "reason": "owner TUI exited unexpectedly"}),
    ],
)
def test_owner_worker_pty_child_exit_controls_browser_reconnect(monkeypatch, exit_code, expected_close):
    import asyncio
    from types import SimpleNamespace

    from hermes_cli.owner_worker import ws_routes

    class _WebSocket:
        def __init__(self):
            self.app = SimpleNamespace(state=SimpleNamespace(owner_worker_generation=7))
            self.query_params = SimpleNamespace(get=lambda _key, default="": default)
            self.url = SimpleNamespace(path="/api/pty")
            self.closed = []

        async def accept(self):
            return None

        async def close(self, **kwargs):
            self.closed.append(kwargs)

        async def receive(self):
            while not self.closed:
                await asyncio.sleep(0)
            return {"type": "websocket.disconnect"}

        async def send_bytes(self, _data):
            return None

    class _Bridge:
        def read(self, _timeout):
            return None

        def close(self):
            return None

        def exit_code(self):
            return exit_code

        def resize(self, **_kwargs):
            return None

        def write(self, _data):
            return None

    websocket = _WebSocket()
    monkeypatch.setattr(ws_routes, "_admit_bootstrap_or_close", lambda _ws: asyncio.sleep(0, result=websocket))
    monkeypatch.setattr(ws_routes, "_trusted_live_metadata", lambda *_args: ("trusted",))
    monkeypatch.setattr(ws_routes, "_PTY_BRIDGE_AVAILABLE", True)
    monkeypatch.setattr(ws_routes, "_resolve_chat_argv_async", lambda **_kwargs: asyncio.sleep(0, result=(["test"], None, {})))
    monkeypatch.setattr(ws_routes, "PtyBridge", SimpleNamespace(spawn=lambda *_args, **_kwargs: _Bridge()))
    monkeypatch.setattr(ws_routes, "_report_pty_lifecycle", lambda *_args: None)

    asyncio.run(ws_routes.pty_ws(websocket))

    assert websocket.closed == [expected_close]


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


def test_worker_skill_routes_are_capability_bound_and_owner_local(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    owner_home = ensure_owner_runtime_dirs(tmp_path / "owner")
    control_home = tmp_path / "control"
    other_home = tmp_path / "other"
    other_skill = other_home / "skills" / "other-only"
    other_skill.mkdir(parents=True)
    (other_skill / "SKILL.md").write_text(
        "---\nname: other-only\ndescription: must not leak\n---\n",
        encoding="utf-8",
    )
    owner_skill = owner_home / "skills" / "owner-skill"
    owner_skill.mkdir(parents=True)
    owner_content = "---\nname: owner-skill\ndescription: owner only\n---\n\n# Owner\n"
    owner_skill.joinpath("SKILL.md").write_text(owner_content, encoding="utf-8")
    owner_home.joinpath("config.yaml").write_text("{}\n", encoding="utf-8")

    monkeypatch.setenv("HERMES_HOME", str(owner_home))
    monkeypatch.setenv("HERMES_OWNER_KEY", "ok1_worker_skills")
    monkeypatch.setenv("HERMES_CONTROL_HOME", str(control_home))
    from hermes_cli.owner_worker.entrypoint import create_app

    app = create_app("ok1_worker_skills", owner_home)
    client = TestClient(app)

    def headers(path: str) -> dict[str, str]:
        token = _capability_for(app, path=path, control_home=control_home)
        return {"Authorization": f"Bearer {token}"}

    assert client.get("/api/skills").status_code == 401
    wrong_token = _capability_for(app, path="/api/sessions", control_home=control_home)
    assert client.get(
        "/api/skills",
        headers={"Authorization": f"Bearer {wrong_token}"},
    ).status_code == 401

    listed = client.get("/api/skills", headers=headers("/api/skills"))
    assert listed.status_code == 200
    names = {skill["name"] for skill in listed.json()}
    assert "owner-skill" in names
    assert "other-only" not in names

    content = client.get(
        "/api/skills/content?name=owner-skill",
        headers=headers("/api/skills/content"),
    )
    assert content.status_code == 200
    assert content.json()["content"] == owner_content
    assert str(other_home) not in content.text

    rejected = client.get(
        "/api/skills?profile=other",
        headers=headers("/api/skills"),
    )
    assert rejected.status_code == 400


def test_worker_skill_writes_mutate_only_owner_home(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import yaml

    owner_home = ensure_owner_runtime_dirs(tmp_path / "owner")
    control_home = tmp_path / "control"
    other_home = tmp_path / "other"
    other_home.mkdir()
    other_home.joinpath("config.yaml").write_text("{}\n", encoding="utf-8")
    owner_home.joinpath("config.yaml").write_text("{}\n", encoding="utf-8")
    existing = owner_home / "skills" / "owner-skill"
    existing.mkdir(parents=True)
    existing.joinpath("SKILL.md").write_text(
        "---\nname: owner-skill\ndescription: before\n---\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("HERMES_HOME", str(owner_home))
    monkeypatch.setenv("HERMES_OWNER_KEY", "ok1_worker_skills")
    monkeypatch.setenv("HERMES_CONTROL_HOME", str(control_home))
    from hermes_cli.owner_worker.entrypoint import create_app

    app = create_app("ok1_worker_skills", owner_home)
    client = TestClient(app)
    # Direct app construction reuses this test process; production workers set
    # HERMES_HOME before these owner-sensitive modules are ever imported.
    import tools.skill_manager_tool as skill_manager_tool
    import tools.skills_tool as skills_tool

    monkeypatch.setattr(skills_tool, "HERMES_HOME", owner_home)
    monkeypatch.setattr(skills_tool, "SKILLS_DIR", owner_home / "skills")
    monkeypatch.setattr(skill_manager_tool, "HERMES_HOME", owner_home)
    monkeypatch.setattr(skill_manager_tool, "SKILLS_DIR", owner_home / "skills")

    def request(method: str, path: str, payload: dict):
        token = _capability_for(app, path=path, control_home=control_home)
        return client.request(
            method,
            path,
            json=payload,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )

    toggled = request(
        "PUT",
        "/api/skills/toggle",
        {"name": "owner-skill", "enabled": False},
    )
    assert toggled.status_code == 200
    owner_config = yaml.safe_load(owner_home.joinpath("config.yaml").read_text()) or {}
    assert "owner-skill" in owner_config.get("skills", {}).get("disabled", [])
    assert yaml.safe_load(other_home.joinpath("config.yaml").read_text()) == {}

    new_content = "---\nname: new-skill\ndescription: created\n---\n\n# New\n"
    created = request(
        "POST",
        "/api/skills",
        {"name": "new-skill", "content": new_content},
    )
    assert created.status_code == 200, created.text
    assert owner_home.joinpath("skills/new-skill/SKILL.md").read_text() == new_content
    assert not other_home.joinpath("skills/new-skill").exists()

    updated_content = "---\nname: owner-skill\ndescription: after\n---\n\n# Updated\n"
    updated = request(
        "PUT",
        "/api/skills/content",
        {"name": "owner-skill", "content": updated_content},
    )
    assert updated.status_code == 200, updated.text
    assert existing.joinpath("SKILL.md").read_text() == updated_content

    rejected = request(
        "POST",
        "/api/skills",
        {"name": "blocked", "content": new_content, "profile": "other"},
    )
    assert rejected.status_code == 400
    assert not owner_home.joinpath("skills/blocked").exists()


def test_worker_managed_files_are_descriptor_scoped_to_its_owner(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from hermes_cli.owner_worker.entrypoint import create_app

    import hermes_cli.controlled_roots as controlled_roots

    monkeypatch.setattr(controlled_roots.sys, "platform", "linux")
    monkeypatch.setattr(controlled_roots, "_openat2", lambda *_args: None)
    control_home = tmp_path / "control"
    owner_a = ensure_owner_runtime_dirs(tmp_path / "owner-a")
    owner_b = ensure_owner_runtime_dirs(tmp_path / "owner-b")
    (owner_b / "workspaces" / "secret.txt").write_text("owner-b-only")
    default_workspace = owner_a / "workspaces" / "default"
    default_workspace.joinpath("subdir").mkdir()
    default_workspace.joinpath("report.html").write_bytes(b"<h1>owner-a</h1>")
    default_workspace.joinpath("subdir/report.pdf").write_bytes(b"%PDF-owner-a")
    default_workspace.joinpath("directory").mkdir()
    default_workspace.joinpath("report-link.html").symlink_to("report.html")
    monkeypatch.setenv("HERMES_HOME", str(owner_a))
    monkeypatch.setenv("HERMES_OWNER_KEY", "ok1_owner_a")
    monkeypatch.setenv("HERMES_CONTROL_HOME", str(control_home))
    app = create_app("ok1_owner_a", owner_a)
    client = TestClient(app)

    def request(path: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {_capability_for(app, path, control_home=control_home)}"}

    created = client.post(
        "/api/files/upload",
        headers=request("/api/files/upload"),
        json={"path": "project/note.txt", "data_url": "data:text/plain;base64,b3duZXItYQ=="},
    )
    assert created.status_code == 200
    assert (owner_a / "workspaces" / "project" / "note.txt").read_text() == "owner-a"
    assert not (owner_b / "workspaces" / "project" / "note.txt").exists()

    listed = client.get("/api/files", headers=request("/api/files"))
    assert listed.status_code == 200
    assert "project" in [entry["path"] for entry in listed.json()["entries"]]

    leaked = client.get(
        "/api/files/read",
        headers=request("/api/files/read"),
        params={"path": str(owner_b / "workspaces" / "secret.txt")},
    )
    assert leaked.status_code == 400
    assert "owner-b-only" not in leaked.text

    traversal = client.get(
        "/api/files/read",
        headers=request("/api/files/read"),
        params={"path": "../secret.txt"},
    )
    assert traversal.status_code == 400

    downloaded = client.get(
        "/api/files/download",
        headers=request("/api/files/download"),
        params={
            "path": "note.txt",
            "cwd": str(owner_a / "workspaces" / "project"),
            "filename": "owner note.txt",
        },
    )
    assert downloaded.status_code == 200
    assert downloaded.content == b"owner-a"
    assert "owner%20note.txt" in downloaded.headers["content-disposition"]

    for sandbox_path, sandbox_cwd, expected, expected_type in (
        ("/workspace/report.html", None, b"<h1>owner-a</h1>", "text/html"),
        (
            "/workspace/subdir/report.pdf",
            None,
            b"%PDF-owner-a",
            "application/pdf",
        ),
        ("report.html", "/workspace", b"<h1>owner-a</h1>", "text/html"),
        (
            "report.pdf",
            "/workspace/subdir",
            b"%PDF-owner-a",
            "application/pdf",
        ),
    ):
        params = {"path": sandbox_path}
        if sandbox_cwd is not None:
            params["cwd"] = sandbox_cwd
        response = client.get(
            "/api/files/download",
            headers=request("/api/files/download"),
            params=params,
        )
        assert response.status_code == 200, response.text
        assert response.content == expected
        assert response.headers["content-type"].startswith(expected_type)
        assert response.headers["content-disposition"].startswith("attachment;")

    sandbox_path_with_owner_cwd = client.get(
        "/api/files/download",
        headers=request("/api/files/download"),
        params={
            "path": "/workspace/report.html",
            "cwd": str(default_workspace),
        },
    )
    assert sandbox_path_with_owner_cwd.status_code == 200
    assert sandbox_path_with_owner_cwd.content == b"<h1>owner-a</h1>"

    for rejected_path, rejected_cwd in (
        (str(owner_b / "workspaces" / "secret.txt"), None),
        ("../secret.txt", str(owner_a / "workspaces" / "project")),
        ("secret.txt", str(owner_b / "workspaces")),
        ("/workspace", None),
        ("/workspace2/report.html", None),
        ("/workspace/../secret.txt", None),
        ("/workspace/directory", None),
        ("/workspace/report-link.html", None),
        ("/workspace/report.html", str(owner_b / "workspaces")),
    ):
        params = {"path": rejected_path}
        if rejected_cwd is not None:
            params["cwd"] = rejected_cwd
        response = client.get(
            "/api/files/download",
            headers=request("/api/files/download"),
            params=params,
        )
        assert response.status_code == 400
        assert b"owner-b-only" not in response.content


def test_worker_image_preview_is_descriptor_scoped_to_owner_images(tmp_path, monkeypatch):
    import base64

    from fastapi.testclient import TestClient
    from hermes_cli.owner_worker.entrypoint import create_app

    import hermes_cli.controlled_roots as controlled_roots

    monkeypatch.setattr(controlled_roots.sys, "platform", "linux")
    monkeypatch.setattr(controlled_roots, "_openat2", lambda *_args: None)
    control_home = tmp_path / "control"
    owner_a = ensure_owner_runtime_dirs(tmp_path / "owner-a")
    owner_b = ensure_owner_runtime_dirs(tmp_path / "owner-b")
    image = owner_a / "images" / "upload.png"
    image.parent.mkdir(exist_ok=True)
    image.write_bytes(b"pngbytes")
    secret = owner_a / "secret.png"
    secret.write_bytes(b"secret")
    other_owner_image = owner_b / "images" / "upload.png"
    other_owner_image.parent.mkdir(exist_ok=True)
    other_owner_image.write_bytes(b"owner-b")
    monkeypatch.setenv("HERMES_HOME", str(owner_a))
    monkeypatch.setenv("HERMES_OWNER_KEY", "ok1_owner_a")
    monkeypatch.setenv("HERMES_CONTROL_HOME", str(control_home))
    app = create_app("ok1_owner_a", owner_a)
    client = TestClient(app)

    def request(path: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {_capability_for(app, path, control_home=control_home)}"}

    preview = client.get(
        "/api/fs/read-data-url",
        headers=request("/api/fs/read-data-url"),
        params={"path": str(image)},
    )
    assert preview.status_code == 200
    assert preview.json() == {
        "dataUrl": "data:image/png;base64," + base64.b64encode(b"pngbytes").decode("ascii")
    }

    for rejected_path in (secret, other_owner_image, Path("images/upload.png")):
        response = client.get(
            "/api/fs/read-data-url",
            headers=request("/api/fs/read-data-url"),
            params={"path": str(rejected_path)},
        )
        assert response.status_code == 400
        assert "pngbytes" not in response.text
        assert "secret" not in response.text
        assert "owner-b" not in response.text

    downloaded = client.get(
        "/api/files/download",
        headers=request("/api/files/download"),
        params={"path": str(image), "filename": "original upload.png"},
    )
    assert downloaded.status_code == 200
    assert downloaded.content == b"pngbytes"
    assert "original%20upload.png" in downloaded.headers["content-disposition"]

    for rejected_path in (secret, other_owner_image, Path("images/upload.png")):
        response = client.get(
            "/api/files/download",
            headers=request("/api/files/download"),
            params={"path": str(rejected_path)},
        )
        assert response.status_code in {400, 404}
        assert b"secret" not in response.content
        assert b"owner-b" not in response.content


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
    assert client.get("/api/logs").status_code == 401
    assert client.get("/api/profiles").status_code == 401
    assert client.get("/api/config").status_code == 401
    assert client.get("/api/dashboard/font").status_code == 401
    assert client.get("/api/dashboard/plugins").status_code == 401
    assert client.get("/api/tools/toolsets").status_code == 401


def test_worker_owner_startup_routes_return_owner_local_payloads(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    owner_home = tmp_path / "owner"
    monkeypatch.setenv("HERMES_HOME", str(owner_home))
    monkeypatch.setenv("HERMES_OWNER_KEY", "ok1_worker_routes")
    monkeypatch.setenv("HERMES_CONTROL_HOME", str(tmp_path / "control"))
    from hermes_cli.config import save_config
    from hermes_cli.owner_worker.entrypoint import create_app

    ensure_owner_runtime_dirs(owner_home)
    save_config({
        "model": {"default": "owner-model", "provider": "owner-provider"},
        "dashboard": {"font": "fraunces"},
        "platform_toolsets": {"cli": ["x_search"]},
    })
    (owner_home / "skills" / "owner-skill").mkdir(parents=True)
    (owner_home / "skills" / "owner-skill" / "SKILL.md").write_text("# owner", encoding="utf-8")
    app = create_app("ok1_worker_routes", owner_home)
    client = TestClient(app)

    def get(path):
        token = _capability_for(app, path=path, control_home=tmp_path / "control")
        return client.get(path, headers={"Authorization": f"Bearer {token}"})

    profiles = get("/api/profiles")
    config = get("/api/config")
    font = get("/api/dashboard/font")
    plugins = get("/api/dashboard/plugins")
    toolsets = get("/api/tools/toolsets")

    assert profiles.status_code == 200
    assert profiles.json()["management_mode"] == "owner_singleton"
    assert len(profiles.json()["profiles"]) == 1
    profile = profiles.json()["profiles"][0]
    assert profile["name"] == "default"
    assert profile["path"] is None
    assert profile["model"] == "owner-model"
    assert profile["skill_count"] == 1
    assert str(owner_home) not in profiles.text
    assert config.status_code == 200
    assert config.json()["model"] == "owner-model"
    assert "_config_version" not in config.json()
    assert font.json() == {"font": "fraunces"}
    assert plugins.status_code == 200
    assert isinstance(plugins.json(), list)
    assert toolsets.status_code == 200
    toolsets_by_name = {item["name"]: item for item in toolsets.json()}
    assert toolsets_by_name["x_search"]["enabled"] is True
    assert toolsets_by_name["x_search"]["available"] is True


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


def test_worker_logs_return_owner_local_filtered_data(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import hermes_cli.controlled_roots as controlled_roots

    owner_home = tmp_path / "owner"
    monkeypatch.setenv("HERMES_HOME", str(owner_home))
    monkeypatch.setenv("HERMES_OWNER_KEY", "ok1_worker_routes")
    monkeypatch.setenv("HERMES_CONTROL_HOME", str(tmp_path / "control"))
    monkeypatch.setattr(controlled_roots.sys, "platform", "linux")
    monkeypatch.setattr(controlled_roots, "_openat2", lambda *_args: None)
    from hermes_cli.owner_worker.entrypoint import create_app

    ensure_owner_runtime_dirs(owner_home)
    (owner_home / "logs" / "agent.log").write_text(
        "2026-07-20 10:00:00 INFO tools.terminal_tool: ignore me\n"
        "2026-07-20 10:00:01 WARNING tools.terminal_tool: owner needle\n"
        "2026-07-20 10:00:02 ERROR gateway.run: wrong component needle\n",
        encoding="utf-8",
    )
    app = create_app("ok1_worker_routes", owner_home)
    client = TestClient(app)
    token = _capability_for(
        app,
        audience=AUD_OWNER_WORKER_HTTP,
        path="/api/logs",
        control_home=tmp_path / "control",
    )
    headers = {"Authorization": f"Bearer {token}"}

    response = client.get(
        "/api/logs?file=agent&lines=10&level=WARNING&component=tools&search=NEEDLE",
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json() == {
        "file": "agent",
        "lines": ["2026-07-20 10:00:01 WARNING tools.terminal_tool: owner needle\n"],
    }
    assert client.get("/api/logs?file=unknown", headers=headers).status_code == 400
    assert client.get("/api/logs?component=unknown", headers=headers).status_code == 400
    missing = client.get("/api/logs?file=errors", headers=headers)
    assert missing.status_code == 200
    assert missing.json() == {"file": "errors", "lines": []}


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


def test_worker_live_state_is_app_local_and_stale_cleanup_is_fenced():
    """Colliding browser IDs cannot share state or erase a newer owner record."""
    import asyncio
    from types import SimpleNamespace

    from hermes_cli.owner_worker.ws_routes import (
        OwnerWorkerLiveState,
        _attach_browser_pty_bridge,
        _register_browser_pty_owner,
        _release_browser_pty_owner,
        _trusted_live_metadata,
    )

    def peer(*, owner_key: str, generation: int, worker_id: str):
        return SimpleNamespace(
            claims=SimpleNamespace(
                owner_key=owner_key,
                worker_generation=generation,
                worker_id=worker_id,
                lease_version=1,
                recovery_generation=0,
                audience="owner-worker-uds-bootstrap",
                scope="owner-worker:bootstrap",
                path="/api/pty",
            )
        )

    async def exercise() -> None:
        app_a = SimpleNamespace(state=SimpleNamespace(owner_worker_live_state=OwnerWorkerLiveState()))
        app_b = SimpleNamespace(state=SimpleNamespace(owner_worker_live_state=OwnerWorkerLiveState()))
        metadata_a = _trusted_live_metadata(peer(owner_key="owner-a", generation=1, worker_id="a"), "/api/pty")
        metadata_b = _trusted_live_metadata(peer(owner_key="owner-b", generation=1, worker_id="b"), "/api/pty")

        await _register_browser_pty_owner(
            app_a, browser_id="shared-browser", channel="shared-channel", owner_id="old", ws=object(), metadata=metadata_a
        )
        await _register_browser_pty_owner(
            app_b, browser_id="shared-browser", channel="shared-channel", owner_id="only", ws=object(), metadata=metadata_b
        )
        replaced = await _register_browser_pty_owner(
            app_a, browser_id="shared-browser", channel="shared-channel", owner_id="new", ws=object(), metadata=metadata_a
        )
        assert replaced is not None
        assert await _attach_browser_pty_bridge(
            app_a, browser_id="shared-browser", owner_id="new", bridge="a-bridge", metadata=metadata_a
        )
        await _release_browser_pty_owner(
            app_a, browser_id="shared-browser", owner_id="old", metadata=metadata_a
        )

        assert app_a.state.owner_worker_live_state.pty_browser_sessions["shared-browser"]["bridge"] == "a-bridge"
        assert app_a.state.owner_worker_live_state.pty_browser_sessions["shared-browser"]["metadata"] == metadata_a
        assert app_b.state.owner_worker_live_state.pty_browser_sessions["shared-browser"]["metadata"] == metadata_b

    asyncio.run(exercise())


def test_worker_gateway_runtime_and_attach_tokens_are_app_local():
    """An owner worker cannot reuse another worker's Gateway binding or child token."""
    from types import SimpleNamespace

    from hermes_cli.owner_worker.ws_routes import (
        OwnerWorkerLiveState,
        _consume_gateway_attach_token,
        _mint_gateway_attach_token,
    )
    from tui_gateway.server import OwnerWorkerGatewayRuntime

    app_a = SimpleNamespace(state=SimpleNamespace(owner_worker_live_state=OwnerWorkerLiveState()))
    app_b = SimpleNamespace(state=SimpleNamespace(owner_worker_live_state=OwnerWorkerLiveState()))
    runtime_a = OwnerWorkerGatewayRuntime("owner-a", 1, "worker-a", 1, 0)
    runtime_b = OwnerWorkerGatewayRuntime("owner-b", 1, "worker-b", 1, 0)
    app_a.state.owner_worker_live_state.gateway_runtime = runtime_a
    app_b.state.owner_worker_live_state.gateway_runtime = runtime_b

    assert app_a.state.owner_worker_live_state.gateway_runtime is runtime_a
    assert app_b.state.owner_worker_live_state.gateway_runtime is runtime_b
    assert runtime_a != runtime_b

    consumed = _mint_gateway_attach_token(app_a)
    assert _consume_gateway_attach_token(app_a, consumed)
    assert not _consume_gateway_attach_token(app_a, consumed)

    foreign = _mint_gateway_attach_token(app_a)
    assert not _consume_gateway_attach_token(app_b, foreign)
    assert _consume_gateway_attach_token(app_a, foreign)

    expired = _mint_gateway_attach_token(app_a)
    app_a.state.owner_worker_live_state.gateway_attach_tokens[expired] = time.monotonic() - 1
    assert not _consume_gateway_attach_token(app_a, expired)


def test_worker_gateway_ws_admits_one_owner_local_tui_attach(monkeypatch):
    """The private child attach token bypasses neither worker identity nor replay fencing."""
    import asyncio
    from types import SimpleNamespace

    from hermes_cli.owner_worker import ws_routes
    from hermes_cli.owner_worker.ws_routes import OwnerWorkerLiveState
    from tui_gateway.server import OwnerWorkerGatewayRuntime
    from tui_gateway import ws as gateway_ws_module

    class FakeWebSocket:
        def __init__(self, app, token):
            self.app = app
            self.query_params = {"owner_tui_attach": token}
            self.closed: list[tuple[int, str]] = []

        async def close(self, *, code=1000, reason=""):
            self.closed.append((code, reason))

    app_a = SimpleNamespace(state=SimpleNamespace(owner_worker_live_state=OwnerWorkerLiveState()))
    app_b = SimpleNamespace(state=SimpleNamespace(owner_worker_live_state=OwnerWorkerLiveState()))
    runtime = OwnerWorkerGatewayRuntime("owner-a", 1, "worker-a", 1, 0)
    app_a.state.owner_worker_live_state.gateway_runtime = runtime
    app_b.state.owner_worker_live_state.gateway_runtime = OwnerWorkerGatewayRuntime("owner-b", 1, "worker-b", 1, 0)
    token = ws_routes._mint_gateway_attach_token(app_a)
    admitted: list[tuple[object, object, bool]] = []

    async def fake_handle_ws(ws, *, runtime, require_owner_runtime):
        admitted.append((ws, runtime, require_owner_runtime))

    monkeypatch.setattr(gateway_ws_module, "handle_ws", fake_handle_ws)

    attached = FakeWebSocket(app_a, token)
    asyncio.run(ws_routes.gateway_ws(attached))
    assert admitted == [(attached, runtime, True)]
    assert attached.closed == []

    replay = FakeWebSocket(app_a, token)
    asyncio.run(ws_routes.gateway_ws(replay))
    assert admitted == [(attached, runtime, True)]
    assert replay.closed and replay.closed[0][0] == 4401

    foreign = FakeWebSocket(app_b, ws_routes._mint_gateway_attach_token(app_a))
    asyncio.run(ws_routes.gateway_ws(foreign))
    assert foreign.closed and foreign.closed[0][0] == 4401


def test_worker_active_session_records_are_descriptor_scoped_to_its_owner(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import hermes_cli.controlled_roots as controlled_roots
    from hermes_cli.owner_runtime import owner_worker_runtime_paths
    from hermes_cli.owner_worker import ws_routes
    from hermes_cli.owner_worker.ws_routes import OwnerWorkerLiveState

    monkeypatch.setattr(controlled_roots.sys, "platform", "linux")
    monkeypatch.setattr(controlled_roots, "_openat2", lambda *_args: None)
    owner_a = ensure_owner_runtime_dirs(tmp_path / "owner-a")
    owner_b = ensure_owner_runtime_dirs(tmp_path / "owner-b")
    roots_a = controlled_roots_for(owner_worker_runtime_paths(owner_home=owner_a, worker_generation=1))
    roots_b = controlled_roots_for(owner_worker_runtime_paths(owner_home=owner_b, worker_generation=1))
    app_a = SimpleNamespace(state=SimpleNamespace(owner_worker_controlled_roots=roots_a, owner_worker_live_state=OwnerWorkerLiveState()))
    app_b = SimpleNamespace(state=SimpleNamespace(owner_worker_controlled_roots=roots_b, owner_worker_live_state=OwnerWorkerLiveState()))

    try:
        active_path = ws_routes._active_session_file_for_channel(app_a, "browser-a")
        active_path_b = ws_routes._active_session_file_for_channel(app_b, "browser-a")
        assert Path(active_path).is_file()
        assert Path(active_path_b).is_file()
        roots_a.replace_bytes(
            RootKind.TEMPORARY,
            ws_routes._active_session_relative_path(app_a, active_path),
            b'{"session_id":"owner-a-session"}',
        )
        roots_b.replace_bytes(
            RootKind.TEMPORARY,
            ws_routes._active_session_relative_path(app_b, active_path_b),
            b'{"session_id":"owner-b-session"}',
        )
        assert ws_routes._read_active_session_file(app_a, active_path) == "owner-a-session"
        assert ws_routes._read_active_session_file(app_b, active_path) is None
        assert ws_routes._read_active_session_file(app_b, active_path_b) == "owner-b-session"
        with pytest.raises(RuntimeError, match="not owned"):
            ws_routes._active_session_relative_path(app_a, "/tmp/untrusted-session.json")

        ws_routes._forget_active_session_file(app_a, active_path)
        assert not Path(active_path).exists()
        assert Path(active_path_b).is_file()
        assert ws_routes._read_active_session_file(app_b, active_path_b) == "owner-b-session"
    finally:
        roots_a.close()
        roots_b.close()


def test_worker_chat_argv_derives_cwd_from_workspace_descriptor(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import hermes_cli.controlled_roots as controlled_roots
    from hermes_cli.owner_runtime import owner_worker_runtime_paths
    from hermes_cli.owner_worker import ws_routes
    from hermes_cli.owner_worker.ws_routes import OwnerWorkerLiveState

    monkeypatch.setattr(controlled_roots.sys, "platform", "linux")
    monkeypatch.setattr(controlled_roots, "_openat2", lambda *_args: None)
    owner_home = ensure_owner_runtime_dirs(tmp_path / "owner")
    roots = controlled_roots_for(owner_worker_runtime_paths(owner_home=owner_home, worker_generation=1))
    monkeypatch.setattr("hermes_cli.main._make_tui_argv", lambda *_args, **_kwargs: (["node", "entry.js"], str(tmp_path)))
    monkeypatch.setattr(ws_routes.os, "readlink", lambda _path: str(owner_home / "workspaces" / "default"))
    app = SimpleNamespace(
        state=SimpleNamespace(
            owner_worker_mode=True,
            owner_worker_owner_home=owner_home,
            owner_worker_generation=1,
            owner_worker_socket_path=owner_worker_socket_path(owner_home, 1),
            owner_worker_controlled_roots=roots,
            owner_worker_live_state=OwnerWorkerLiveState(),
        )
    )
    try:
        _argv, cwd, env = ws_routes._resolve_chat_argv(app_obj=app)
        assert cwd == str(owner_home / "workspaces" / "default")
        assert "HERMES_CWD" not in env
        assert "TERMINAL_CWD" not in env
    finally:
        roots.close()


def test_worker_chat_argv_requires_owner_worker_gateway_attach(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from hermes_cli.owner_worker import ws_routes
    from hermes_cli.owner_worker.ws_routes import OwnerWorkerLiveState

    owner_home = tmp_path / "owner"
    socket_path = owner_worker_socket_path(owner_home, 7)
    monkeypatch.setattr(ws_routes, "resolve_workspace_cwd", lambda *_args, **_kwargs: tmp_path)
    monkeypatch.setattr("hermes_cli.main._make_tui_argv", lambda *_args, **_kwargs: (["node", "entry.js"], str(tmp_path)))
    app = SimpleNamespace(
        state=SimpleNamespace(
            owner_worker_mode=True,
            owner_worker_owner_home=owner_home,
            owner_worker_generation=7,
            owner_worker_socket_path=socket_path,
            owner_worker_live_state=OwnerWorkerLiveState(),
        )
    )

    _argv, _cwd, env = ws_routes._resolve_chat_argv(app_obj=app)

    assert env["HERMES_OWNER_WORKER_TUI_ATTACH"] == "1"
    assert env["HERMES_TUI_GATEWAY_URL"].startswith("ws://owner-worker/api/ws?owner_tui_attach=")
    assert env["HERMES_TUI_GATEWAY_SOCKET_PATH"] == str(socket_path)


@pytest.mark.parametrize("configured", [None, "other.sock"])
def test_worker_chat_argv_rejects_unbound_gateway_socket(tmp_path, monkeypatch, configured):
    from types import SimpleNamespace

    from hermes_cli.owner_worker import ws_routes
    from hermes_cli.owner_worker.ws_routes import OwnerWorkerLiveState

    owner_home = tmp_path / "owner"
    monkeypatch.setattr(ws_routes, "resolve_workspace_cwd", lambda *_args, **_kwargs: tmp_path)
    monkeypatch.setattr("hermes_cli.main._make_tui_argv", lambda *_args, **_kwargs: (["node", "entry.js"], str(tmp_path)))
    app = SimpleNamespace(
        state=SimpleNamespace(
            owner_worker_mode=True,
            owner_worker_owner_home=owner_home,
            owner_worker_generation=7,
            owner_worker_socket_path=(tmp_path / configured if configured else None),
            owner_worker_live_state=OwnerWorkerLiveState(),
        )
    )

    with pytest.raises(RuntimeError, match="gateway socket"):
        ws_routes._resolve_chat_argv(app_obj=app)
