from __future__ import annotations

import logging

from hermes_cli.latency_trace import clean_latency_trace_id, log_latency_stage


def test_clean_latency_trace_id_accepts_opaque_browser_id():
    trace_id = "cdd27bc1-73df-43eb-a54d-f662ee263c33"

    assert clean_latency_trace_id(trace_id) == trace_id


def test_clean_latency_trace_id_rejects_log_injection_and_short_values():
    assert clean_latency_trace_id("short") == ""
    assert clean_latency_trace_id("valid-prefix\nforged-log-line") == ""


def test_log_latency_stage_emits_joinable_content_free_record(caplog):
    logger = logging.getLogger("tests.latency-trace")

    with caplog.at_level(logging.INFO, logger=logger.name):
        log_latency_stage(
            logger,
            trace_id="trace-id-123",
            surface="session-resume",
            stage="history.display_loaded",
            started_at=0.0,
        )

    message = caplog.messages[-1]
    assert "trace_id=trace-id-123" in message
    assert "surface=session-resume" in message
    assert "stage=history.display_loaded" in message
    assert "elapsed_ms=" in message
    assert "outcome=ok" in message
