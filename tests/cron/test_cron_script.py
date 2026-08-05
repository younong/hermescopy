"""Focused Owner Worker cron script execution coverage."""
from __future__ import annotations


def test_relative_script_executes_only_from_owner_scripts_directory(tmp_path):
    from cron.jobs import CronStore, use_store
    from cron.scheduler import _run_job_script

    owner_home = tmp_path / "owner"
    owner_home.joinpath("scripts").mkdir(parents=True)
    owner_home.joinpath("scripts/task.py").write_text("print('owner script')\n")
    with use_store(CronStore(owner_home)):
        success, output = _run_job_script("task.py")

    assert success is True
    assert output == "owner script"


def test_script_path_escape_is_blocked(tmp_path):
    from cron.jobs import CronStore, use_store
    from cron.scheduler import _run_job_script

    owner_home = tmp_path / "owner"
    owner_home.joinpath("scripts").mkdir(parents=True)
    outside = tmp_path / "outside.py"
    outside.write_text("print('blocked')\n")
    with use_store(CronStore(owner_home)):
        success, output = _run_job_script(str(outside))

    assert success is False
    assert "outside the scripts directory" in output


def test_prerun_script_output_is_injected_into_agent_prompt(tmp_path):
    from cron.jobs import CronStore, use_store
    from cron.scheduler import _build_job_prompt

    owner_home = tmp_path / "owner"
    owner_home.joinpath("scripts").mkdir(parents=True)
    owner_home.joinpath("scripts/collect.py").write_text("print('collected data')\n")
    with use_store(CronStore(owner_home)):
        prompt = _build_job_prompt(
            {"id": "abc123abc123", "prompt": "analyze", "script": "collect.py"}
        )

    assert "collected data" in prompt
    assert "analyze" in prompt


def test_script_failure_is_injected_without_agent_execution(tmp_path):
    from cron.jobs import CronStore, use_store
    from cron.scheduler import _build_job_prompt

    owner_home = tmp_path / "owner"
    owner_home.joinpath("scripts").mkdir(parents=True)
    with use_store(CronStore(owner_home)):
        prompt = _build_job_prompt(
            {"id": "abc123abc123", "prompt": "analyze", "script": "missing.py"}
        )

    assert "Script Error" in prompt
    assert "Script not found" in prompt
