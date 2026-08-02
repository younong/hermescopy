"""Contract tests for canonical final-payload request accounting."""

from types import SimpleNamespace

from agent.prepared_model_request import (
    ProviderManagedRequest,
    account_prepared_payload,
    prepare_model_request_snapshot,
    prepared_context_payload,
    request_route,
)


def _account(payload, mode="chat_completions", ratio=1.0):
    return account_prepared_payload(
        payload,
        api_mode=mode,
        context_limit=200_000,
        compression_threshold=150_000,
        default_output_limit=8_000,
        calibrate=lambda value: int(value * ratio),
    )


def test_chat_accounting_uses_final_system_messages_and_tools():
    payload = {
        "model": "gpt-test",
        "messages": [
            {"role": "system", "content": "base\n<available_skills>demo</available_skills>"},
            {"role": "developer", "content": "# Project Context\nFollow rules"},
            {"role": "user", "content": "hello"},
        ],
        "tools": [
            {"type": "function", "function": {"name": "terminal", "description": "run"}},
            {"type": "function", "function": {"name": "mcp_demo", "description": "mcp"}},
            {"type": "function", "function": {"name": "delegate_task", "description": "spawn"}},
        ],
        "max_completion_tokens": 4096,
    }

    accounting = _account(payload)
    categories = {item.id: item.tokens for item in accounting.categories}

    assert accounting.raw_input_tokens > 0
    assert accounting.output_token_limit == 4096
    assert sum(categories.values()) == accounting.effective_input_tokens
    assert {"system_prompt", "rules", "skills", "conversation"} <= categories.keys()
    assert {"tool_definitions", "mcp", "subagent_definitions"} <= categories.keys()


def test_anthropic_accounting_counts_top_level_system_messages_and_cache_shaped_tools():
    payload = {
        "model": "claude-test",
        "system": [{"type": "text", "text": "system identity"}],
        "messages": [{"role": "user", "content": "<memory-context>remember me</memory-context>\nquestion"}],
        "tools": [{"name": "read_file", "input_schema": {"type": "object"}}],
        "max_tokens": 8192,
    }

    accounting = _account(payload, "anthropic_messages")
    categories = {item.id: item.tokens for item in accounting.categories}

    assert accounting.output_token_limit == 8192
    assert {"system_prompt", "memory", "conversation", "tool_definitions"} <= categories.keys()
    assert sum(categories.values()) == accounting.effective_input_tokens


def test_responses_accounting_uses_final_instructions_input_and_converted_tools():
    payload = {
        "model": "gpt-test",
        "instructions": "Follow the system rules",
        "input": [{"role": "user", "content": [{"type": "input_text", "text": "hello"}]}],
        "tools": [{"type": "function", "name": "mcp_lookup", "parameters": {"type": "object"}}],
        "max_output_tokens": 2048,
        "prompt_cache_key": "ignored-routing-control",
    }

    accounting = _account(payload, "codex_responses")
    categories = {item.id: item.tokens for item in accounting.categories}

    assert accounting.output_token_limit == 2048
    assert {"system_prompt", "conversation", "mcp"} <= categories.keys()
    assert "provider_overhead" not in categories


def test_bedrock_accounting_uses_converse_wire_shape():
    payload = {
        "modelId": "anthropic.test",
        "system": [{"text": "system"}],
        "messages": [{"role": "user", "content": [{"text": "hello"}]}],
        "toolConfig": {"tools": [{"toolSpec": {"name": "terminal", "inputSchema": {"json": {"type": "object"}}}}]},
        "inferenceConfig": {"maxTokens": 1024},
    }

    accounting = _account(payload, "bedrock_converse")
    categories = {item.id: item.tokens for item in accounting.categories}

    assert accounting.output_token_limit == 1024
    assert {"system_prompt", "conversation", "tool_definitions"} <= categories.keys()
    assert "provider_overhead" not in categories


def test_calibration_is_reflected_in_category_sum():
    accounting = _account(
        {"messages": [{"role": "user", "content": "x" * 100}], "tools": []},
        ratio=1.5,
    )

    assert accounting.effective_input_tokens >= accounting.raw_input_tokens
    assert sum(item.tokens for item in accounting.categories) == accounting.effective_input_tokens


def test_snapshot_exposes_one_reconciled_context_payload():
    agent = SimpleNamespace(
        provider="openrouter",
        model="gpt-test",
        base_url="https://example.test/v1",
        api_mode="chat_completions",
        max_tokens=4096,
        context_compressor=SimpleNamespace(
            context_length=100_000,
            threshold_tokens=75_000,
            calibrated_prompt_tokens=lambda value: value,
        ),
    )
    payload = {"model": "gpt-test", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 4096}

    prepared = prepare_model_request_snapshot(agent, request_id="turn:1", payload=payload)
    context = prepared_context_payload(prepared)

    assert prepared.payload is payload
    assert context["context_used"] == context["estimated_total"]
    assert sum(item["tokens"] for item in context["categories"]) == context["context_used"]
    assert context["accounting_source"] == "prepared_request"


def test_provider_managed_runtime_does_not_fabricate_breakdown():
    agent = SimpleNamespace(provider="openai-codex", model="gpt", base_url="", api_mode="codex_app_server")
    managed = ProviderManagedRequest(request_route(agent))

    context = prepared_context_payload(managed)

    assert context["accounting_source"] == "provider_managed"
    assert context["categories"] == []
    assert context["context_used"] == 0
