"""Real-path coverage for authenticated-owner scheduled execution."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

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
    def __init__(self, app, owner_key: str, owner_home: Path, control_home: Path):
        from fastapi.testclient import TestClient

        self.client = TestClient(app)
        self.control_home = control_home
        self.handle = SimpleNamespace(
            owner_key=owner_key,
            owner_home=owner_home,
            socket_path=owner_home / "unused.sock",
            worker_generation=app.state.owner_worker_generation,
            worker_id=app.state.owner_worker_id,
            lease_version=app.state.owner_worker_lease.lease_version,
            recovery_generation=app.state.owner_worker_lease.recovery_generation,
            active_uses=0,
        )

    def get_or_start(self, owner):
        assert owner.owner_key == self.handle.owner_key
        assert owner.owner_home == self.handle.owner_home
        return self.handle

    def acquire_use(self, handle):
        assert handle is self.handle
        return _UseLease(handle)


def test_dispatcher_naturally_executes_due_owner_script_and_isolates_output(
    tmp_path,
    monkeypatch,
):
    owner_key = "ok1_cron_owner"
    other_key = "ok1_other_owner"
    global_home = tmp_path / "global"
    owner_home = ensure_owner_runtime_dirs(global_home / "users" / owner_key)
    other_home = ensure_owner_runtime_dirs(global_home / "users" / other_key)
    control_home = tmp_path / "control"
    for home in (owner_home, other_home):
        home.joinpath("config.yaml").write_text("model: test-model\n", encoding="utf-8")
    owner_home.joinpath("scripts").mkdir()
    marker = "authenticated-owner-cron-marker"
    owner_home.joinpath("scripts/marker.py").write_text(
        f"print({marker!r})\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("HERMES_HOME", str(owner_home))
    monkeypatch.setenv("HERMES_OWNER_KEY", owner_key)
    monkeypatch.setenv("HERMES_CONTROL_HOME", str(control_home))
    from hermes_cli.owner_worker.entrypoint import create_app

    app = create_app(owner_key, owner_home)
    supervisor = _InProcessSupervisor(app, owner_key, owner_home, control_home)

    from hermes_cli.cron_management import create_job

    job = create_job(
        owner_home,
        {
            "name": "natural due owner script",
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
            token = app.state.owner_worker_capability_verifier
            del token, lease
            from tests.hermes_cli.test_owner_worker import _capability_for

            capability = _capability_for(app, path=path, control_home=control_home)
            request_headers = dict(headers or {})
            request_headers["Authorization"] = f"Bearer {capability}"
            return supervisor.client.request(
                method,
                path,
                headers=request_headers,
                content=content,
            )

    monkeypatch.setattr(cron_dispatcher, "OwnerWorkerClient", _Client)
    assert cron_dispatcher.dispatch_owner_due_jobs(supervisor, global_home) == 1
    assert supervisor.handle.active_uses == 0

    stored = json.loads(owner_home.joinpath("cron/jobs.json").read_text(encoding="utf-8"))
    completed = next(row for row in stored["jobs"] if row["id"] == job["id"])
    assert completed["last_run_at"]
    assert completed["last_status"] == "ok"
    assert completed["state"] == "scheduled"
    output_files = list(owner_home.glob(f"cron/output/{job['id']}/*.md"))
    assert len(output_files) == 1
    assert marker in output_files[0].read_text(encoding="utf-8")
    assert list(other_home.glob("cron/output/**/*.md")) == []


def test_dispatcher_ignores_symlink_and_malformed_owner_homes(tmp_path):
    from hermes_cli.owner_worker.cron_dispatcher import _canonical_owner_homes

    global_home = tmp_path / "global"
    users = global_home / "users"
    valid = users / "ok1_valid"
    valid.mkdir(parents=True)
    malformed = users / "not-an-owner"
    malformed.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    users.joinpath("ok1_symlink").symlink_to(external, target_is_directory=True)

    owners = _canonical_owner_homes(global_home)
    assert [(owner.owner_key, owner.owner_home) for owner in owners] == [
        ("ok1_valid", valid.resolve())
    ]
