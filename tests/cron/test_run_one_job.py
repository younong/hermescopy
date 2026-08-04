"""Worker-local completion pipeline for script-only cron jobs."""
from __future__ import annotations


def test_run_one_job_saves_and_marks_no_agent_result(monkeypatch):
    import cron.scheduler as scheduler

    calls = []
    monkeypatch.setattr(scheduler, "claim_dispatch", lambda _job_id: True)
    monkeypatch.setattr(
        scheduler,
        "run_job",
        lambda job: (True, "audit output", "final response", None),
    )
    monkeypatch.setattr(
        scheduler,
        "save_job_output",
        lambda job_id, output: calls.append(("save", job_id, output)) or "path",
    )
    monkeypatch.setattr(
        scheduler,
        "mark_job_run",
        lambda job_id, success, error=None, delivery_error=None: calls.append(
            ("mark", job_id, success, error, delivery_error)
        ),
    )

    assert scheduler.run_one_job({"id": "job-a", "no_agent": True}) is True
    assert calls == [
        ("save", "job-a", "audit output"),
        ("mark", "job-a", True, None, None),
    ]


def test_run_one_job_rejects_agent_job_without_gateway(monkeypatch):
    import cron.scheduler as scheduler

    marks = []
    monkeypatch.setattr(
        scheduler,
        "mark_job_run",
        lambda job_id, success, error=None, delivery_error=None: marks.append(
            (job_id, success, error)
        ),
    )

    assert scheduler.run_one_job({"id": "job-a", "prompt": "report"}) is False
    assert marks[0][0:2] == ("job-a", False)
    assert "structured gateway dispatcher" in marks[0][2]
