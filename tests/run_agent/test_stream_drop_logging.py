"""Tests for richer stream-drop diagnostics in agent.log.

When a subagent's stream drops mid-tool-call, the WARNING in agent.log must
carry enough breadcrumbs to answer "WHY did it drop" without requiring a
verbose-mode rerun.  Specifically:

- Inner exception chain (httpx errors wrapped by openai SDK)
- Upstream HTTP headers (cf-ray, x-openrouter-provider, x-openrouter-id, ...)
- HTTP status of the dying response
- Bytes streamed and chunks received before the drop
- Elapsed time on the attempt + time-to-first-byte

Plus the user-visible UI line gains an ``after Xs`` suffix when timing data
is available, distinguishing "couldn't connect at all" from "died mid-stream
after N seconds" (very different root causes).
"""

from __future__ import annotations

import logging
import time
from unittest.mock import patch

import pytest


from agent.stream_diag import (
    STREAM_DIAG_HEADERS,
    fixed_histogram,
    fixed_histogram_observe,
    fixed_histogram_percentile,
    stream_diag_finalize,
    stream_diag_mark_visible,
    stream_diag_record_event,
)
from run_agent import AIAgent


def _make_agent() -> AIAgent:
    return AIAgent(
        api_key="test-key",
        base_url="https://openrouter.ai/api/v1",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )


def test_stream_diag_init_returns_well_formed_dict():
    diag = AIAgent._stream_diag_init()
    assert "started_at" in diag
    assert diag["chunks"] == 0
    assert diag["bytes"] == 0
    assert diag["first_chunk_at"] is None
    assert diag["http_status"] is None
    assert diag["headers"] == {}


class _FakeHeaders:
    def __init__(self, d): self._d = {k.lower(): v for k, v in d.items()}
    def get(self, k, default=None): return self._d.get(k.lower(), default)


class _FakeResponse:
    def __init__(self, headers, status=200):
        self.status_code = status
        self.headers = _FakeHeaders(headers)


def test_stream_diag_capture_response_collects_known_headers():
    agent = _make_agent()
    diag = AIAgent._stream_diag_init()
    resp = _FakeResponse({
        "cf-ray": "8f1a2b3c4d5e6f7g-LAX",
        "x-openrouter-provider": "Anthropic",
        "x-openrouter-id": "gen-abc123",
        "x-request-id": "req-xyz",
        "server": "cloudflare",
        "irrelevant-header": "ignored",
    })
    agent._stream_diag_capture_response(diag, resp)
    assert diag["http_status"] == 200
    assert diag["headers"]["cf-ray"] == "8f1a2b3c4d5e6f7g-LAX"
    assert diag["headers"]["x-openrouter-provider"] == "Anthropic"
    assert diag["headers"]["x-openrouter-id"] == "gen-abc123"
    assert diag["headers"]["server"] == "cloudflare"
    # Headers not in _STREAM_DIAG_HEADERS must not be captured (PII surface).
    assert "irrelevant-header" not in diag["headers"]


def test_stream_diag_capture_response_safe_with_none():
    agent = _make_agent()
    diag = AIAgent._stream_diag_init()
    agent._stream_diag_capture_response(diag, None)
    # Must not raise; diag stays initialized.
    assert diag["headers"] == {}


def test_stream_diag_uses_monotonic_fixed_histograms_and_finalizes_once(caplog):
    diag = AIAgent._stream_diag_init()
    diag["started_monotonic"] = 10.0
    diag["response_opened_monotonic"] = 10.01

    stream_diag_record_event(diag, object(), now=10.02)
    stream_diag_record_event(diag, object(), now=10.03)
    stream_diag_record_event(diag, object(), now=10.08)
    stream_diag_mark_visible(diag, now=10.04)

    with caplog.at_level(logging.INFO, logger="agent.stream_diag"):
        summary = stream_diag_finalize(diag, outcome="success", now=10.10)
        duplicate = stream_diag_finalize(diag, outcome="error", now=10.20)

    assert summary is not None
    assert summary["outcome"] == "success"
    assert summary["duration_ms"] == pytest.approx(100.0)
    assert summary["response_open_ms"] == pytest.approx(10.0)
    assert summary["first_event_ms"] == pytest.approx(20.0)
    assert summary["first_visible_ms"] == pytest.approx(40.0)
    assert summary["events"] == 3
    assert summary["estimated_event_bytes"] > 0
    assert summary["event_gap_max_ms"] == 50.0
    assert summary["event_gap_p95_ms"] == 50.0
    assert duplicate is None
    messages = [record.getMessage() for record in caplog.records if "stream aggregate" in record.getMessage()]
    assert len(messages) == 1
    assert "success" in messages[0]


def test_fixed_histogram_overflow_percentile_uses_exact_maximum():
    counts = fixed_histogram((1.0, 5.0))
    for value in (0.5, 2.0, 12.0):
        fixed_histogram_observe(counts, (1.0, 5.0), value)
    assert fixed_histogram_percentile(counts, (1.0, 5.0), 12.0, 0.95) == 12.0


def test_stream_diag_does_not_collect_forwarded_ip():
    assert "x-forwarded-for" not in STREAM_DIAG_HEADERS
    agent = _make_agent()
    diag = AIAgent._stream_diag_init()
    response = _FakeResponse({"x-forwarded-for": "203.0.113.7", "server": "edge"})
    agent._stream_diag_capture_response(diag, response)
    assert "x-forwarded-for" not in diag["headers"]


def test_flatten_exception_chain_walks_cause():
    inner = ConnectionError("upstream closed")
    middle = TimeoutError("timed out")
    middle.__cause__ = inner
    outer = RuntimeError("wrapper")
    outer.__cause__ = middle
    chain = AIAgent._flatten_exception_chain(outer)
    assert "RuntimeError" in chain
    assert "TimeoutError" in chain
    assert "ConnectionError" in chain
    assert " <- " in chain


def test_flatten_exception_chain_caps_depth():
    """Chain renders no more than 4 deep so log lines stay bounded."""
    e0 = ValueError("0")
    prev = e0
    for i in range(1, 8):
        nxt = ValueError(str(i))
        nxt.__cause__ = prev
        prev = nxt
    chain = AIAgent._flatten_exception_chain(prev)
    # 4 layers + 3 separators max.
    assert chain.count("<-") <= 3


def test_log_stream_retry_includes_diagnostic_fields(caplog):
    agent = _make_agent()
    agent._delegate_depth = 1
    agent._subagent_id = "sa-3-deadbeef"
    agent.provider = "openrouter"

    diag = AIAgent._stream_diag_init()
    diag["http_status"] = 200
    diag["headers"] = {
        "cf-ray": "8f1a2b3c4d5e6f7g-LAX",
        "x-openrouter-provider": "Anthropic",
        "x-openrouter-id": "gen-xyz789",
    }
    diag["chunks"] = 12
    diag["bytes"] = 4096
    # Simulate 5s elapsed with first chunk at 0.5s.
    diag["started_at"] = time.time() - 5.0
    diag["first_chunk_at"] = diag["started_at"] + 0.5

    inner = ConnectionError("peer closed")
    outer = RuntimeError("Connection error.")
    outer.__cause__ = inner

    with caplog.at_level(logging.WARNING, logger="run_agent"):
        agent._log_stream_retry(
            kind="drop mid tool-call",
            error=outer,
            attempt=2,
            max_attempts=3,
            mid_tool_call=True,
            diag=diag,
        )

    msg = next(
        r.getMessage() for r in caplog.records
        if "Stream drop mid tool-call" in r.getMessage()
    )

    # Identity
    assert "subagent_id=sa-3-deadbeef" in msg
    assert "provider=openrouter" in msg

    # Inner-cause chain
    assert "RuntimeError" in msg and "ConnectionError" in msg

    # Counters and timing
    assert "http_status=200" in msg
    assert "estimated_event_bytes=4096" in msg
    assert "chunks=12" in msg
    # elapsed should be roughly 5s; allow some slack.
    assert "elapsed=" in msg
    assert "ttfb=0.50s" in msg

    # Upstream headers
    assert "cf-ray=8f1a2b3c4d5e6f7g-LAX" in msg
    assert "x-openrouter-provider=Anthropic" in msg
    assert "x-openrouter-id=gen-xyz789" in msg


def test_log_stream_retry_works_without_diag(caplog):
    """diag is optional — older callers / unit tests still work."""
    agent = _make_agent()
    agent._delegate_depth = 0
    agent.provider = "openrouter"

    with caplog.at_level(logging.WARNING, logger="run_agent"):
        agent._log_stream_retry(
            kind="drop",
            error=ConnectionError("x"),
            attempt=2,
            max_attempts=3,
            mid_tool_call=False,
        )

    msg = next(r.getMessage() for r in caplog.records if "Stream drop" in r.getMessage())
    # Without diag, the structured fields show "-" placeholders.
    assert "http_status=-" in msg
    assert "upstream=[-]" in msg
    assert "estimated_event_bytes=0" in msg
    assert "chunks=0" in msg
    assert "ttfb=-" in msg


def test_emit_stream_drop_ui_includes_elapsed_when_available():
    agent = _make_agent()
    agent.provider = "openrouter"

    diag = AIAgent._stream_diag_init()
    diag["started_at"] = time.time() - 8.0  # 8s on the wire before drop

    with patch.object(agent, "_buffer_status") as mock_emit:
        agent._emit_stream_drop(
            error=ConnectionError("x"),
            attempt=2,
            max_attempts=3,
            mid_tool_call=True,
            diag=diag,
        )

    msg = mock_emit.call_args.args[0]
    # Suffix with elapsed time helps distinguish "couldn't connect" (0s)
    # from "died mid-stream after a while".
    assert "after" in msg and "s" in msg


def test_emit_stream_drop_ui_omits_suffix_without_diag():
    """When there's no diag, no suffix — line stays compact."""
    agent = _make_agent()
    agent.provider = "openrouter"

    with patch.object(agent, "_buffer_status") as mock_emit:
        agent._emit_stream_drop(
            error=ConnectionError("x"),
            attempt=2,
            max_attempts=3,
            mid_tool_call=False,
        )

    msg = mock_emit.call_args.args[0]
    # No "after Xs" suffix when diag is not provided.
    assert " after " not in msg
    # Still names the provider and error class.
    assert "openrouter" in msg
    assert "ConnectionError" in msg


def test_quiet_mode_does_not_clobber_runagent_logger_level():
    """Regression guard for the parent fix — must persist across this PR."""
    _ = _make_agent()
    for name in ("run_agent", "tools", "trajectory_compressor", "cron", "hermes_cli"):
        logger = logging.getLogger(name)
        assert logger.getEffectiveLevel() <= logging.WARNING
