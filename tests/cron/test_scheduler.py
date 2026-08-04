"""Owner Worker cron scheduler boundary tests."""
from __future__ import annotations

import pytest


def test_agent_job_requires_structured_gateway_dispatch():
    from cron.scheduler import run_job

    with pytest.raises(RuntimeError, match="Owner Worker structured gateway dispatcher"):
        run_job({"id": "abc123abc123", "prompt": "report"})


def test_silence_marker_detection_remains_local_only():
    from cron.scheduler import _is_cron_silence_response

    assert _is_cron_silence_response("[SILENT]") is True
    assert _is_cron_silence_response("[SILENT]\nno changes") is True
    assert _is_cron_silence_response("The word [SILENT] appears here") is False


def test_binding_delivery_persists_stable_pending_request(monkeypatch):
    import cron.scheduler as scheduler

    calls = []
    pending = []
    monkeypatch.setattr(scheduler, "save_job_output", lambda *_args: "path")
    monkeypatch.setattr(
        scheduler,
        "mark_job_run",
        lambda job_id, success, error=None, delivery_error=None: calls.append(
            (job_id, success, error, delivery_error)
        ),
    )
    monkeypatch.setattr(
        "cron.jobs.record_pending_delivery",
        lambda **kwargs: pending.append(kwargs) or kwargs,
    )

    delivery = scheduler.complete_job_run(
        {"id": "abc123abc123", "binding_id": "binding-a"},
        success=True,
        output="audit",
        final_response="deliver me",
        fire_id="fire-a",
    )
    assert delivery == {
        "job_id": "abc123abc123",
        "fire_id": "fire-a",
        "binding_id": "binding-a",
        "payload": "deliver me",
    }
    assert pending == [delivery]
    assert calls == [
        ("abc123abc123", True, None, "delivery enqueue pending")
    ]
