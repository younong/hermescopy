"""Real-store coverage for stable cron fire idempotency."""
import pytest


@pytest.fixture
def temp_store(tmp_path):
    from cron.jobs import CronStore, use_store

    with use_store(CronStore(tmp_path)):
        yield tmp_path


def test_same_stable_fire_id_is_claimed_once(temp_store):
    from cron.jobs import claim_job_for_fire, create_job, get_job

    job = create_job(prompt="x", schedule="every 5m", name="t")
    before = get_job(job["id"])["next_run_at"]
    assert claim_job_for_fire(job["id"], fire_id="fire-a") is True
    assert claim_job_for_fire(job["id"], fire_id="fire-a") is False
    assert get_job(job["id"])["next_run_at"] != before


def test_completed_fire_id_cannot_replay(temp_store):
    from cron.jobs import claim_job_for_fire, create_job, mark_job_run

    job = create_job(prompt="x", schedule="every 5m", name="c")
    assert claim_job_for_fire(job["id"], fire_id="fire-a") is True
    mark_job_run(job["id"], success=True)
    assert claim_job_for_fire(job["id"], fire_id="fire-a") is False
    assert claim_job_for_fire(job["id"], fire_id="fire-b") is True


def test_stale_different_fire_is_reclaimable(temp_store):
    from cron.jobs import claim_job_for_fire, create_job

    job = create_job(prompt="x", schedule="every 5m", name="s")
    assert claim_job_for_fire(job["id"], fire_id="fire-a") is True
    assert claim_job_for_fire(
        job["id"], fire_id="fire-b", claim_ttl_seconds=0
    ) is True


def test_fire_id_is_required(temp_store):
    from cron.jobs import claim_job_for_fire, create_job

    job = create_job(prompt="x", schedule="every 5m", name="required")
    with pytest.raises(ValueError, match="fire_id is required"):
        claim_job_for_fire(job["id"], fire_id="")


def test_unknown_or_paused_job_cannot_be_claimed(temp_store):
    from cron.jobs import claim_job_for_fire, create_job, pause_job

    assert claim_job_for_fire("missing", fire_id="fire-a") is False
    job = create_job(prompt="x", schedule="every 5m", name="paused")
    pause_job(job["id"])
    assert claim_job_for_fire(job["id"], fire_id="fire-b") is False
