"""Real-path coverage for authenticated-owner scheduled execution."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli.dashboard_auth.authority import AuthorizationScope, AuthorityStore
from hermes_cli.dashboard_auth.owner_context import owner_context_from_registry
from hermes_cli.owner_runtime import ensure_owner_runtime_dirs


class _UseLease:
    def __init__(self, handle):
        self.handle = handle

    def __enter__(self):
        self.handle.active_uses += 1
        return self

    def __exit__(self, *_args):
        self.handle.active_uses -= 1


class _InProcessSupervisor:
    def __init__(self, app, owner, control_home: Path):
        from fastapi.testclient import TestClient

        self.client = TestClient(app)
        self.control_home = control_home
        self.handle = SimpleNamespace(
            owner_key=owner.owner_key,
            owner_home=owner.owner_home,
            socket_path=owner.owner_home / "unused.sock",
            worker_generation=app.state.owner_worker_generation,
            worker_id=app.state.owner_worker_id,
            lease_version=app.state.owner_worker_lease.lease_version,
            recovery_generation=app.state.owner_worker_lease.recovery_generation,
            active_uses=0,
        )

    def get_or_start(self, owner, *, timeout=None):
        del timeout
        assert owner.owner_key == self.handle.owner_key
        assert owner.owner_home == self.handle.owner_home
        return self.handle

    def acquire_use(self, handle):
        assert handle is self.handle
        return _UseLease(handle)


def _register_owner(store: AuthorityStore, owner) -> None:
    store.activate(
        AuthorizationScope(
            provider=owner.auth_provider,
            tenant_id=owner.tenant_id,
            user_id=owner.owner_user_id,
            session_id="session-a",
            membership_revision="rev-a",
        )
    )


def test_dispatcher_uses_registry_and_isolates_owner_output(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_OWNER_SECRET", "cron-owner-secret")
    global_home = tmp_path / "global"
    owner = owner_context_from_registry(
        auth_provider="stub",
        tenant_id="org-a",
        canonical_user_id="user-a",
        global_home=global_home,
    )
    other = owner_context_from_registry(
        auth_provider="stub",
        tenant_id="org-b",
        canonical_user_id="user-b",
        global_home=global_home,
    )
    owner_home = ensure_owner_runtime_dirs(owner.owner_home)
    other_home = ensure_owner_runtime_dirs(other.owner_home)
    control_home = tmp_path / "control"
    authority = AuthorityStore(control_home)
    _register_owner(authority, owner)
    monkeypatch.delenv("HERMES_OWNER_SECRET")

    owner_home.joinpath("config.yaml").write_text("model: test-model\n", encoding="utf-8")
    owner_home.joinpath("scripts").mkdir()
    marker = "authenticated-owner-cron-marker"
    owner_home.joinpath("scripts/marker.py").write_text(
        f"print({marker!r})\n", encoding="utf-8"
    )

    monkeypatch.setenv("HERMES_HOME", str(owner_home))
    monkeypatch.setenv("HERMES_OWNER_KEY", owner.owner_key)
    monkeypatch.setenv("HERMES_CONTROL_HOME", str(control_home))
    from hermes_cli.owner_worker.entrypoint import create_app

    app = create_app(owner.owner_key, owner_home)
    supervisor = _InProcessSupervisor(app, owner, control_home)
    monkeypatch.setenv("HERMES_OWNER_SECRET", "cron-owner-secret")

    from hermes_cli.cron_management import create_job

    job = create_job(
        owner_home,
        {
            "name": "due owner script",
            "schedule": "every 1h",
            "script": "marker.py",
            "no_agent": True,
        },
    )
    jobs_path = owner_home / "cron" / "jobs.json"
    stored = json.loads(jobs_path.read_text(encoding="utf-8"))
    stored["jobs"][0]["next_run_at"] = "2000-01-01T00:00:00+00:00"
    jobs_path.write_text(json.dumps(stored), encoding="utf-8")

    from hermes_cli.owner_worker import cron_dispatcher

    class _Client:
        def __init__(self, *_args, **_kwargs):
            pass

        def request(self, method, path, *, lease, headers=None, content=None):
            del lease
            from tests.hermes_cli.test_owner_worker import _capability_for

            capability = _capability_for(app, path=path, control_home=control_home)
            request_headers = dict(headers or {})
            request_headers["Authorization"] = f"Bearer {capability}"
            return supervisor.client.request(
                method, path, headers=request_headers, content=content
            )

    monkeypatch.setattr(cron_dispatcher, "OwnerWorkerClient", _Client)
    assert cron_dispatcher.dispatch_owner_due_jobs(
        supervisor, global_home, authority_store=authority
    ) == 1
    assert supervisor.handle.active_uses == 0

    completed = json.loads(jobs_path.read_text(encoding="utf-8"))["jobs"][0]
    assert completed["last_status"] == "ok"
    output_files = list(owner_home.glob(f"cron/output/{job['id']}/*.md"))
    assert len(output_files) == 1
    assert marker in output_files[0].read_text(encoding="utf-8")
    assert list(other_home.glob("cron/output/**/*.md")) == []


def test_dispatcher_enqueues_and_acks_canonical_delivery(tmp_path, monkeypatch):
    from hermes_cli.owner_worker import cron_dispatcher

    owner = SimpleNamespace(owner_key="ok1_owner")
    calls = []
    responses = iter(
        [
            {"executed": True, "deliveries": [{
                "fire_id": "fire-a",
                "binding_id": "binding-a",
                "payload": "result",
            }]},
            {"recorded": True},
        ]
    )
    monkeypatch.setattr(
        cron_dispatcher,
        "_authenticated_owners",
        lambda *_args: (cron_dispatcher._StoredOwner(owner),),
    )
    monkeypatch.setattr(
        cron_dispatcher,
        "_dispatch_owner_request",
        lambda *_args, **_kwargs: next(responses),
    )

    def enqueue(**kwargs):
        calls.append(kwargs)
        return "outbound-a"

    assert cron_dispatcher.dispatch_owner_job(
        object(),
        tmp_path,
        "ok1_owner",
        "job-a",
        "fire-a",
        authority_store=object(),
        enqueue_delivery=enqueue,
    ) is True
    assert calls == [{
        "owner_key": "ok1_owner",
        "binding_id": "binding-a",
        "fire_id": "fire-a",
        "payload": "result",
    }]


def test_cron_retries_only_worker_startup_and_posts_once(monkeypatch):
    from hermes_cli.owner_worker import cron_dispatcher

    owner_context = SimpleNamespace(owner_key="ok1_retry", owner_home=Path("/tmp/owner"))
    owner = cron_dispatcher._StoredOwner(owner_context)
    handle = SimpleNamespace(
        owner_key=owner.owner_key,
        owner_home=owner.owner_home,
        worker_generation=1,
        worker_id="worker-1",
        lease_version=1,
        recovery_generation=0,
        socket_path=Path("/tmp/worker.sock"),
    )

    class Supervisor:
        def __init__(self):
            self.starts = 0
            self.uses = 0

        def get_or_start(self, _owner, *, timeout=None):
            assert timeout == 30.0
            self.starts += 1
            if self.starts < 3:
                raise TimeoutError("starting")
            return handle

        def acquire_use(self, _handle):
            self.uses += 1
            class Lease:
                def __enter__(self):
                    return self
                def __exit__(self, *_args):
                    return False
            return Lease()

    class Response:
        def raise_for_status(self):
            pass
        def json(self):
            return {"executed": 1}

    requests = []
    class Client:
        def __init__(self, *_args, **_kwargs):
            pass
        def request(self, *args, **kwargs):
            requests.append((args, kwargs))
            return Response()

    supervisor = Supervisor()
    monkeypatch.delenv("HERMES_OWNER_CRON_STARTUP_TIMEOUT", raising=False)
    monkeypatch.setattr(cron_dispatcher, "OwnerWorkerClient", Client)
    monkeypatch.setattr(cron_dispatcher.time, "sleep", lambda _delay: None)

    assert cron_dispatcher._dispatch_owner_request(
        supervisor, owner, "/internal/cron/tick", cron_startup=True
    ) == {"executed": 1}
    assert supervisor.starts == 3
    assert len(requests) == 1


def test_cron_post_timeout_is_not_retried(monkeypatch):
    from hermes_cli.owner_worker import cron_dispatcher

    owner = cron_dispatcher._StoredOwner(SimpleNamespace(
        owner_key="ok1_post_timeout", owner_home=Path("/tmp/owner")
    ))
    handle = SimpleNamespace(
        owner_key=owner.owner_key, owner_home=owner.owner_home,
        worker_generation=1, worker_id="worker-1", lease_version=1,
        recovery_generation=0, socket_path=Path("/tmp/worker.sock"),
    )
    calls = []
    class Supervisor:
        def get_or_start(self, _owner, *, timeout=None):
            calls.append(timeout)
            return handle
        def acquire_use(self, _handle):
            class Lease:
                def __enter__(self): return self
                def __exit__(self, *_args): return False
            return Lease()
    class Client:
        def __init__(self, *_args, **_kwargs): pass
        def request(self, *_args, **_kwargs):
            calls.append("post")
            raise TimeoutError("post")
    monkeypatch.setattr(cron_dispatcher, "OwnerWorkerClient", Client)
    with pytest.raises(TimeoutError, match="post"):
        cron_dispatcher._dispatch_owner_request(
            Supervisor(), owner, "/internal/cron/tick", cron_startup=True
        )
    assert calls == [30.0, "post"]


def test_dispatcher_never_enumerates_owner_directories(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_OWNER_SECRET", "cron-owner-secret")
    global_home = tmp_path / "global"
    global_home.joinpath("users/ok1_unregistered").mkdir(parents=True)
    authority = AuthorityStore(tmp_path / "control")

    def forbidden_iterdir(_self):
        raise AssertionError("owner directories must not be enumerated")

    monkeypatch.setattr(Path, "iterdir", forbidden_iterdir)
    from hermes_cli.owner_worker.cron_dispatcher import dispatch_owner_due_jobs

    supervisor = SimpleNamespace(control_home=authority.control_home)
    assert dispatch_owner_due_jobs(
        supervisor, global_home, authority_store=authority
    ) == 0
