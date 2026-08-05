"""Skill-reference rewrites remain confined to the active Owner cron store."""


def test_rewrite_skill_refs_updates_only_active_owner(tmp_path):
    from cron.jobs import CronStore, create_job, get_job, rewrite_skill_refs, use_store

    first = CronStore(tmp_path / "first")
    second = CronStore(tmp_path / "second")
    with use_store(first):
        first_job = create_job(prompt="", schedule="every 1h", skills=["old"])
    with use_store(second):
        second_job = create_job(prompt="", schedule="every 1h", skills=["old"])

    with use_store(first):
        report = rewrite_skill_refs(consolidated={"old": "new"})
        assert report["jobs_updated"] == 1
        assert get_job(first_job["id"])["skills"] == ["new"]
    with use_store(second):
        assert get_job(second_job["id"])["skills"] == ["old"]
