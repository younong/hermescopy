"""Focused real-store coverage for explicit Owner-scoped cron persistence."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest


@pytest.fixture
def owner_store(tmp_path):
    from cron.jobs import CronStore

    return CronStore(tmp_path / "owner")


def test_store_access_without_explicit_owner_fails_closed():
    from cron.jobs import list_jobs

    with pytest.raises(RuntimeError, match="CronStore\(owner_home\) must be bound"):
        list_jobs()


def test_crud_is_explicitly_owner_scoped(owner_store):
    from cron.jobs import create_job, get_job, list_jobs, remove_job, update_job, use_store

    with use_store(owner_store):
        created = create_job(prompt="report", schedule="every 1h", name="hourly")
        assert [job["id"] for job in list_jobs()] == [created["id"]]
        updated = update_job(created["id"], {"name": "renamed"})
        assert updated["name"] == "renamed"
        assert get_job(created["id"])["name"] == "renamed"
        assert remove_job(created["id"]) is True
        assert list_jobs() == []


def test_two_owner_stores_remain_isolated(tmp_path):
    from cron.jobs import CronStore, create_job, list_jobs, use_store

    first = CronStore(tmp_path / "first")
    second = CronStore(tmp_path / "second")
    with use_store(first):
        first_job = create_job(prompt="first", schedule="every 1h")
    with use_store(second):
        second_job = create_job(prompt="second", schedule="every 1h")

    with use_store(first):
        assert [job["id"] for job in list_jobs()] == [first_job["id"]]
    with use_store(second):
        assert [job["id"] for job in list_jobs()] == [second_job["id"]]
    assert first.jobs_file != second.jobs_file


def test_context_local_stores_do_not_rebind_module_globals(tmp_path):
    from cron.jobs import CronStore, create_job, list_jobs, use_store

    stores = [CronStore(tmp_path / f"owner-{index}") for index in range(2)]

    def create_for(index):
        with use_store(stores[index]):
            create_job(prompt=f"owner-{index}", schedule="every 1h")
            return [job["prompt"] for job in list_jobs()]

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(create_for, range(2))) == [["owner-0"], ["owner-1"]]


def test_output_is_saved_only_under_active_owner(owner_store):
    from cron.jobs import save_job_output, use_store

    with use_store(owner_store):
        path = save_job_output("abc123abc123", "result")

    assert path.is_relative_to(owner_store.output_dir / "abc123abc123")
    assert path.read_text(encoding="utf-8") == "result"


def test_workdir_validation_rejects_relative_or_missing_paths(owner_store, tmp_path):
    from cron.jobs import create_job, use_store

    with use_store(owner_store):
        with pytest.raises(ValueError, match="absolute path"):
            create_job(prompt="report", schedule="every 1h", workdir="relative")
        with pytest.raises(ValueError, match="does not exist"):
            create_job(
                prompt="report",
                schedule="every 1h",
                workdir=str(tmp_path / "missing"),
            )
