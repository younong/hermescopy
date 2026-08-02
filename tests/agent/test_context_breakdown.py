"""Tests for canonical live session context breakdown."""

from types import SimpleNamespace

from agent.context_breakdown import compute_session_context_breakdown
from agent.prepared_model_request import (
    ProviderManagedRequest,
    prepare_model_request_snapshot,
    request_route,
)


def _make_agent():
    return SimpleNamespace(
        provider="openai",
        model="openai/gpt-5.4",
        base_url="https://example.test/v1",
        api_mode="chat_completions",
        max_tokens=8_000,
        _prepared_model_request=None,
        context_compressor=SimpleNamespace(
            context_length=200_000,
            threshold_tokens=150_000,
            calibrated_prompt_tokens=lambda value: value,
            last_prompt_tokens=42_000,
        ),
    )


def test_breakdown_formats_canonical_prepared_request():
    agent = _make_agent()
    agent._prepared_model_request = prepare_model_request_snapshot(
        agent,
        request_id="turn:1",
        payload={
            "model": agent.model,
            "messages": [
                {"role": "system", "content": "system"},
                {"role": "user", "content": "hello"},
            ],
            "tools": [
                {"type": "function", "function": {"name": "terminal"}},
            ],
            "max_completion_tokens": 4_096,
        },
    )

    data = compute_session_context_breakdown(agent)

    assert data["accounting_source"] == "prepared_request"
    assert data["context_max"] == 200_000
    assert data["context_used"] == data["estimated_total"]
    assert sum(item["tokens"] for item in data["categories"]) == data["context_used"]
    assert all(item["color"] for item in data["categories"])
    assert data["context_used"] != agent.context_compressor.last_prompt_tokens


def test_breakdown_is_unknown_before_a_request_is_prepared():
    data = compute_session_context_breakdown(_make_agent())

    assert data["accounting_source"] == "unknown"
    assert data["categories"] == []
    assert data["context_used"] == 0


def test_breakdown_does_not_fabricate_provider_managed_context():
    agent = _make_agent()
    agent.api_mode = "codex_app_server"
    agent._prepared_model_request = ProviderManagedRequest(request_route(agent))

    data = compute_session_context_breakdown(agent)

    assert data["accounting_source"] == "provider_managed"
    assert data["categories"] == []
    assert data["context_used"] == 0
