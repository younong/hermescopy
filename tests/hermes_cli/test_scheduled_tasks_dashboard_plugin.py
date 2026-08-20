from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient


_PLUGIN_API = (
    Path(__file__).parents[2]
    / "plugins"
    / "scheduled-tasks"
    / "dashboard"
    / "plugin_api.py"
)


def _load_plugin_module():
    name = "test_scheduled_tasks_dashboard_plugin_api"
    spec = importlib.util.spec_from_file_location(name, _PLUGIN_API)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _client(*, owner_worker: bool) -> tuple[TestClient, object]:
    module = _load_plugin_module()
    app = FastAPI()
    app.state.owner_worker_mode = owner_worker
    app.include_router(module.router, prefix="/api/plugins/scheduled-tasks")
    return TestClient(app), module


def test_owner_worker_crud_notifies_provider_and_rejects_selectors(
    tmp_path, monkeypatch,
):
    owner_home = tmp_path / "owner"
    owner_home.mkdir()
    workspace = owner_home / "workspaces" / "default"
    workspace.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(owner_home))

    notifications = []
    monkeypatch.setattr(
        "cron.scheduler._notify_provider_jobs_changed",
        lambda: notifications.append(True),
    )
    client, _module = _client(owner_worker=True)
    try:
        created_response = client.post(
            "/api/plugins/scheduled-tasks/jobs",
            json={"name": "report", "prompt": "make report", "schedule": "every 1h"},
        )
        assert created_response.status_code == 200
        created = created_response.json()
        job_id = created["id"]
        assert "profile" not in created
        assert (owner_home / "cron" / "jobs.json").is_file()

        listed = client.get("/api/plugins/scheduled-tasks/jobs")
        assert listed.status_code == 200
        assert [job["id"] for job in listed.json()] == [job_id]

        updated = client.put(
            f"/api/plugins/scheduled-tasks/jobs/{job_id}",
            json={"updates": {"name": "renamed"}},
        )
        assert updated.status_code == 200
        assert updated.json()["name"] == "renamed"
        assert client.post(
            f"/api/plugins/scheduled-tasks/jobs/{job_id}/pause"
        ).json()["enabled"] is False
        assert client.post(
            f"/api/plugins/scheduled-tasks/jobs/{job_id}/resume"
        ).json()["enabled"] is True

        assert client.get(
            "/api/plugins/scheduled-tasks/jobs", params={"owner_key": "other"}
        ).status_code == 400
        assert client.post(
            "/api/plugins/scheduled-tasks/jobs",
            json={
                "prompt": "bad",
                "schedule": "every 1h",
                "owner_home": str(tmp_path / "other"),
            },
        ).status_code == 400
        assert client.put(
            f"/api/plugins/scheduled-tasks/jobs/{job_id}",
            json={"updates": {"profile": "other"}},
        ).status_code == 400

        assert client.delete(
            f"/api/plugins/scheduled-tasks/jobs/{job_id}"
        ).json() == {"ok": True}
        assert len(notifications) == 5
    finally:
        client.close()


def test_owner_worker_enforces_ten_task_quota_and_allows_reuse_after_delete(
    tmp_path, monkeypatch,
):
    owner_home = tmp_path / "owner"
    owner_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(owner_home))
    monkeypatch.setattr("cron.scheduler._notify_provider_jobs_changed", lambda: None)
    client, _module = _client(owner_worker=True)
    try:
        responses = [
            client.post(
                "/api/plugins/scheduled-tasks/jobs",
                json={"prompt": f"task-{index}", "schedule": "every 1h"},
            )
            for index in range(10)
        ]
        assert all(response.status_code == 200 for response in responses)
        job_ids = [response.json()["id"] for response in responses]

        rejected = client.post(
            "/api/plugins/scheduled-tasks/jobs",
            json={"prompt": "task-10", "schedule": "every 1h"},
        )
        assert rejected.status_code == 409
        assert "10" in rejected.json()["detail"]

        assert client.delete(
            f"/api/plugins/scheduled-tasks/jobs/{job_ids[0]}"
        ).json() == {"ok": True}
        reused = client.post(
            "/api/plugins/scheduled-tasks/jobs",
            json={"prompt": "reused", "schedule": "every 1h"},
        )
        assert reused.status_code == 200
    finally:
        client.close()


def test_local_all_profile_list_preserves_profile_annotations(tmp_path, monkeypatch):
    from hermes_cli.cron_management import create_job

    default_home = tmp_path / "default"
    worker_home = tmp_path / "worker"
    create_job(default_home, {"prompt": "default", "schedule": "every 1h"})
    create_job(worker_home, {"prompt": "worker", "schedule": "every 2h"})

    client, module = _client(owner_worker=False)
    monkeypatch.setattr(
        module,
        "_profiles_for_lookup",
        lambda _request: [("default", default_home), ("worker", worker_home)],
    )
    try:
        response = client.get("/api/plugins/scheduled-tasks/jobs")
        assert response.status_code == 200
        jobs = {job["prompt"]: job for job in response.json()}
        assert jobs["default"]["profile"] == "default"
        assert jobs["default"]["is_default_profile"] is True
        assert jobs["worker"]["profile"] == "worker"
        assert jobs["worker"]["hermes_home"] == str(worker_home.resolve())
    finally:
        client.close()


def test_runs_delivery_targets_and_blueprints_use_owner_home(tmp_path, monkeypatch):
    owner_home = tmp_path / "owner"
    owner_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(owner_home))
    monkeypatch.setattr(
        "cron.scheduler._notify_provider_jobs_changed", lambda: None
    )
    monkeypatch.setattr(
        "hermes_cli.cron_management.delivery_targets",
        lambda home: [
            {
                "id": "local",
                "name": "Local (save only)",
                "home_target_set": True,
                "home_env_var": None,
            },
            {
                "id": "matrix",
                "name": "Matrix",
                "home_target_set": True,
                "home_env_var": "MATRIX_HOME_ROOM",
            },
        ],
    )

    client, _module = _client(owner_worker=True)
    try:
        created = client.post(
            "/api/plugins/scheduled-tasks/jobs",
            json={"prompt": "report", "schedule": "every 1h"},
        ).json()
        runs = client.get(
            f"/api/plugins/scheduled-tasks/jobs/{created['id']}/runs"
        )
        assert runs.status_code == 200
        assert runs.json() == {"runs": [], "limit": 20}
        assert (owner_home / "state.db").is_file()

        targets = client.get(
            "/api/plugins/scheduled-tasks/delivery-targets"
        ).json()["targets"]
        assert [target["id"] for target in targets] == ["local", "matrix"]

        blueprints = client.get(
            "/api/plugins/scheduled-tasks/blueprints"
        )
        assert blueprints.status_code == 200
        deliver_fields = [
            field
            for blueprint in blueprints.json()["blueprints"]
            for field in blueprint.get("fields", [])
            if field.get("name") == "deliver"
        ]
        assert deliver_fields
        assert all("matrix" in field["options"] for field in deliver_fields)

        instantiated = client.post(
            "/api/plugins/scheduled-tasks/blueprints/instantiate",
            json={
                "blueprint": "morning-brief",
                "values": {"time": "08:30", "deliver": "matrix"},
            },
        )
        assert instantiated.status_code == 200
        assert instantiated.json()["schedule"]["expr"] == "30 8 * * *"
        assert instantiated.json()["deliver"] == "matrix"
    finally:
        client.close()
