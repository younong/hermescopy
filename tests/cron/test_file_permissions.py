"""Owner cron stores preserve private filesystem permissions."""
from __future__ import annotations

import os
import stat


def test_owner_cron_directories_are_private(tmp_path):
    from cron.jobs import CronStore, ensure_dirs, use_store

    store = CronStore(tmp_path / "owner")
    with use_store(store):
        ensure_dirs()

    assert stat.S_IMODE(os.stat(store.cron_dir).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(store.output_dir).st_mode) == 0o700


def test_owner_jobs_and_output_files_are_private(tmp_path):
    from cron.jobs import CronStore, save_job_output, save_jobs, use_store

    store = CronStore(tmp_path / "owner")
    with use_store(store):
        save_jobs([{"id": "abc123abc123", "prompt": "report"}])
        output_file = save_job_output("abc123abc123", "result")

    assert stat.S_IMODE(os.stat(store.jobs_file).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(output_file).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(output_file.parent).st_mode) == 0o700
