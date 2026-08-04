"""Cron scheduling no longer owns process-local execution pools."""


def test_scheduler_has_no_process_local_execution_pool():
    import cron.scheduler as scheduler

    assert not hasattr(scheduler, "_parallel_pool")
    assert not hasattr(scheduler, "_sequential_pool")
    assert not hasattr(scheduler, "_running_job_ids")
    assert not hasattr(scheduler, "tick")


def test_due_job_scan_only_returns_owner_store_jobs(tmp_path, monkeypatch):
    from cron.jobs import CronStore, create_job, use_store
    import cron.scheduler as scheduler

    store = CronStore(tmp_path / "owner")
    with use_store(store):
        job = create_job(prompt="report", schedule="every 1h")
        monkeypatch.setattr(scheduler, "get_due_jobs", lambda: [job])
        assert scheduler.due_jobs_for_tick() == [job]
