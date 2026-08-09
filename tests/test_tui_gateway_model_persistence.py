"""Tests for Chat GUI model-switch persistence."""

from types import SimpleNamespace


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
