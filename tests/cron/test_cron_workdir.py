"""Owner Worker cron workdir behavior without process-global cwd mutation."""
from __future__ import annotations

import os

import pytest


def test_no_agent_script_receives_explicit_cwd_and_sanitized_environment(
    tmp_path, monkeypatch
):
    from cron.jobs import CronStore, use_store
    from cron.scheduler import _run_job_script

    owner_home = tmp_path / "owner"
    scripts_dir = owner_home / "scripts"
    scripts_dir.mkdir(parents=True)
    workspace = owner_home / "workspaces" / "project"
    workspace.mkdir(parents=True)
    script = scripts_dir / "inspect.py"
    script.write_text(
        "import os\nprint(os.getcwd())\nprint(os.getenv('OPENAI_API_KEY', '<missing>'))\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    process_cwd = os.getcwd()

    with use_store(CronStore(owner_home)):
        success, output = _run_job_script("inspect.py", cwd=workspace)

    assert success is True
    assert output.splitlines() == [str(workspace.resolve()), "<missing>"]
    assert os.getcwd() == process_cwd


def test_agent_prerun_script_uses_job_workdir(tmp_path, monkeypatch):
    import cron.scheduler as scheduler
    from cron.jobs import CronStore, use_store

    owner_home = tmp_path / "owner"
    owner_home.joinpath("scripts").mkdir(parents=True)
    workspace = owner_home / "workspaces" / "project"
    workspace.mkdir(parents=True)
    observed = {}

    def fake_run(script_path, *, cwd=None):
        observed["script_path"] = script_path
        observed["cwd"] = cwd
        return True, "collected"

    monkeypatch.setattr(scheduler, "_run_job_script", fake_run)
    with use_store(CronStore(owner_home)):
        prompt = scheduler._build_job_prompt(
            {
                "id": "abc123abc123",
                "prompt": "report",
                "script": "collect.py",
                "workdir": str(workspace),
            }
        )

    assert observed == {"script_path": "collect.py", "cwd": str(workspace)}
    assert "collected" in prompt


def test_no_agent_run_never_mutates_process_cwd(tmp_path, monkeypatch):
    from cron.jobs import CronStore, use_store
    from cron.scheduler import run_job

    owner_home = tmp_path / "owner"
    scripts_dir = owner_home / "scripts"
    scripts_dir.mkdir(parents=True)
    workspace = owner_home / "workspaces" / "project"
    workspace.mkdir(parents=True)
    scripts_dir.joinpath("task.py").write_text("print('ok')\n", encoding="utf-8")
    process_cwd = os.getcwd()

    def forbidden_chdir(_path):
        raise AssertionError("cron must never mutate process cwd")

    monkeypatch.setattr(os, "chdir", forbidden_chdir)
    with use_store(CronStore(owner_home)):
        success, _output, final_response, error = run_job(
            {
                "id": "abc123abc123",
                "name": "script",
                "no_agent": True,
                "script": "task.py",
                "workdir": str(workspace),
            }
        )

    assert success is True
    assert final_response == "ok"
    assert error is None
    assert os.getcwd() == process_cwd


def test_agent_jobs_fail_closed_outside_structured_worker_gateway():
    from cron.scheduler import run_job

    with pytest.raises(RuntimeError, match="structured gateway dispatcher"):
        run_job({"id": "abc123abc123", "prompt": "report"})
