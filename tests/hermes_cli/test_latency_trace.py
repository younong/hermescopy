from __future__ import annotations

import asyncio
import logging

from hermes_cli.latency_trace import (
    clean_latency_trace_id,
    latency_trace_scope,
    log_latency_stage,
    observe_latency_stage,
)


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
    assert "path=" not in message


def test_log_latency_stage_emits_validated_path(caplog):
    logger = logging.getLogger("tests.latency-trace-path")

    with caplog.at_level(logging.INFO, logger=logger.name):
        log_latency_stage(
            logger,
            trace_id="trace-id-123",
            surface="owner-http-proxy",
            stage="owner_worker.ready",
            path="cold_start",
        )

    assert "path=cold_start" in caplog.messages[-1]


def test_latency_trace_scope_propagates_to_thread_and_resets(caplog):
    logger = logging.getLogger("tests.latency-trace-thread")

    async def exercise():
        with latency_trace_scope(
            logger, trace_id="trace-thread-123", surface="owner-http-proxy"
        ):
            await asyncio.to_thread(
                observe_latency_stage,
                stage="owner_worker.ready",
                path="cold_start",
            )
        observe_latency_stage(stage="owner_worker.ready", path="hot_active")

    with caplog.at_level(logging.INFO, logger=logger.name):
        asyncio.run(exercise())

    assert len(caplog.messages) == 1
    assert "trace_id=trace-thread-123" in caplog.messages[0]
    assert "surface=owner-http-proxy" in caplog.messages[0]
    assert "path=cold_start" in caplog.messages[0]


def test_latency_trace_scopes_isolate_concurrent_tasks(caplog):
    logger = logging.getLogger("tests.latency-trace-concurrent")

    async def emit(trace_id, surface, path):
        with latency_trace_scope(logger, trace_id=trace_id, surface=surface):
            await asyncio.to_thread(
                observe_latency_stage,
                stage="owner_worker.ready",
                path=path,
            )

    async def exercise():
        await asyncio.gather(
            emit("trace-http-123", "owner-http-proxy", "hot_active"),
            emit("trace-ws-12345", "owner-ws-bridge", "hot_health_probe"),
        )

    with caplog.at_level(logging.INFO, logger=logger.name):
        asyncio.run(exercise())

    assert len(caplog.messages) == 2
    assert any(
        "trace_id=trace-http-123" in message
        and "surface=owner-http-proxy" in message
        and "path=hot_active" in message
        for message in caplog.messages
    )
    assert any(
        "trace_id=trace-ws-12345" in message
        and "surface=owner-ws-bridge" in message
        and "path=hot_health_probe" in message
        for message in caplog.messages
    )


def test_invalid_trace_and_observer_logging_failure_are_isolated(caplog, monkeypatch):
    import hermes_cli.latency_trace as latency_trace

    logger = logging.getLogger("tests.latency-trace-failure")
    with latency_trace_scope(logger, trace_id="short", surface="owner-http-proxy"):
        observe_latency_stage(stage="owner_worker.ready", path="cold_start")
    assert caplog.messages == []

    def fail_log(*_args, **_kwargs):
        raise RuntimeError("logging failed")

    monkeypatch.setattr(latency_trace, "log_latency_stage", fail_log)
    with latency_trace_scope(
        logger, trace_id="trace-failure-123", surface="owner-http-proxy"
    ):
        observe_latency_stage(stage="owner_worker.ready", path="cold_start")
