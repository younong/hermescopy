"""Tests for Chat GUI model-switch persistence."""

from types import SimpleNamespace


def test_code_runtime_overrides_are_forced_on_resume():
    from tui_gateway import server

    overrides = server._stored_session_runtime_overrides({
        "model": "gpt-5.3-codex",
        "billing_provider": "openrouter",
        "model_config": {
            "model_kind": "code",
            "runtime_profile": "chat",
            "runtime_toolset": "web",
            "provider": "openai-codex",
        },
    })

    assert overrides["model_kind"] == "code"
    assert overrides["runtime_profile"] == "coding"
    assert overrides["runtime_toolset"] == "coding"
    assert overrides["model_override"] == {
        "model": "gpt-5.3-codex",
        "provider": "openai-codex",
        "base_url": None,
        "api_mode": None,
    }


def test_runtime_model_config_keeps_code_identity_and_drops_it_for_chat():
    from tui_gateway import server

    code_agent = SimpleNamespace(
        model="gpt-5.3-codex",
        provider="openai-codex",
        base_url="https://api.example/v1",
        api_mode="codex_responses",
        reasoning_config=None,
        service_tier=None,
        model_kind="code",
        runtime_profile="coding",
        runtime_toolset="coding",
    )
    assert server._runtime_model_config(code_agent) == {
        "model": "gpt-5.3-codex",
        "provider": "openai-codex",
        "base_url": "https://api.example/v1",
        "api_mode": "codex_responses",
        "model_kind": "code",
        "runtime_profile": "coding",
        "runtime_toolset": "coding",
    }

    chat_agent = SimpleNamespace(
        model="claude-test",
        provider="anthropic",
        base_url="",
        api_mode="anthropic_messages",
        reasoning_config=None,
        service_tier=None,
        model_kind="chat",
        runtime_profile=None,
        runtime_toolset=None,
    )
    assert server._runtime_model_config(
        chat_agent,
        {
            "model_kind": "code",
            "runtime_profile": "coding",
            "runtime_toolset": "coding",
        },
    ) == {
        "model": "claude-test",
        "provider": "anthropic",
        "api_mode": "anthropic_messages",
    }


def test_deferred_code_session_record_preserves_runtime_identity(monkeypatch):
    from tui_gateway import server

    monkeypatch.setattr(server, "_required_gateway_transport", lambda: object())
    record = server._deferred_session_record(
        "session-code",
        cols=80,
        cwd="/tmp",
        history=[],
        lease=None,
        resume_runtime_overrides={
            "model_kind": "code",
            "runtime_profile": "coding",
            "runtime_toolset": "coding",
        },
    )

    assert record["model_kind"] == "code"
    assert record["runtime_profile"] == "coding"
    assert record["runtime_toolset"] == "coding"


def test_persist_model_switch_uses_config_set_value_for_all_model_keys(monkeypatch):
    from hermes_cli import config
    from tui_gateway import server

    calls = []
    monkeypatch.setattr(
        config,
        "set_config_value",
        lambda key, value: calls.append((key, value)),
    )

    server._persist_model_switch(
        SimpleNamespace(
            new_model="claude-sonnet-4-6",
            target_provider="anthropic",
            base_url="",
            deployment_managed=False,
        )
    )

    assert calls == [
        ("model.default", "claude-sonnet-4-6"),
        ("model.provider", "anthropic"),
        ("model.base_url", ""),
    ]
