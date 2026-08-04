"""Owner-scoped cron context chaining."""
from __future__ import annotations

import time


def test_context_from_injects_latest_output_from_same_owner(tmp_path):
    from cron.jobs import CronStore, create_job, save_job_output, use_store
    from cron.scheduler import _build_job_prompt

    store = CronStore(tmp_path / "owner")
    with use_store(store):
        source = create_job(prompt="collect", schedule="every 1h")
        save_job_output(source["id"], "old")
        time.sleep(0.01)
        save_job_output(source["id"], "latest owner result")
        consumer = create_job(
            prompt="summarize",
            schedule="every 2h",
            context_from=source["id"],
        )
        prompt = _build_job_prompt(consumer)

    assert "latest owner result" in prompt
    assert "old" not in prompt
    assert prompt.index("latest owner result") < prompt.index("summarize")


def test_context_from_cannot_read_another_owner_store(tmp_path):
    from cron.jobs import CronStore, create_job, save_job_output, use_store
    from cron.scheduler import _build_job_prompt

    first = CronStore(tmp_path / "first")
    second = CronStore(tmp_path / "second")
    with use_store(first):
        source = create_job(prompt="collect", schedule="every 1h")
        save_job_output(source["id"], "first owner secret")
    with use_store(second):
        consumer = create_job(prompt="summarize", schedule="every 2h")
        consumer["context_from"] = [source["id"]]
        prompt = _build_job_prompt(consumer)

    assert "first owner secret" not in prompt
    assert "summarize" in prompt


def test_invalid_context_id_is_ignored(tmp_path):
    from cron.jobs import CronStore, create_job, use_store
    from cron.scheduler import _build_job_prompt

    with use_store(CronStore(tmp_path / "owner")):
        job = create_job(prompt="summarize", schedule="every 2h")
        job["context_from"] = ["../../../etc/passwd"]
        prompt = _build_job_prompt(job)

    assert "etc/passwd" not in prompt
    assert "summarize" in prompt
