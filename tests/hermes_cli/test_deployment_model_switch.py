from __future__ import annotations

from unittest.mock import Mock

from hermes_cli.deployment_inference import DeploymentInferenceRouteDescriptor
from hermes_cli.model_switch import switch_model


def test_switch_model_accepts_exact_managed_route_without_owner_provider_config(monkeypatch):
    monkeypatch.setenv("HERMES_OWNER_KEY", "ok1_test")
    monkeypatch.setenv("HERMES_DEPLOYMENT_INFERENCE_PROVIDER", "custom:codex")
    monkeypatch.setenv("HERMES_DEPLOYMENT_INFERENCE_MODEL", "gpt-safe")
    monkeypatch.setenv("HERMES_DEPLOYMENT_INFERENCE_API_MODE", "chat_completions")
    monkeypatch.setenv("HERMES_DEPLOYMENT_INFERENCE_POLICY_ID", "policy-v2")
    monkeypatch.setenv("HERMES_DEPLOYMENT_INFERENCE_ALLOWED_MODELS", "gpt-safe,k3-256k")
    monkeypatch.setenv("HERMES_DEPLOYMENT_INFERENCE_RELAY_BASE_URL", "http://127.0.0.1:39123/v1")
    route_resolver = Mock(
        return_value=(
            DeploymentInferenceRouteDescriptor(
                provider="custom:codex",
                model="gpt-safe",
                api_mode="chat_completions",
            ),
            DeploymentInferenceRouteDescriptor(
                provider="custom:kimi-code",
                model="k3-256k",
                api_mode="anthropic_messages",
                name="Kimi Code",
            ),
        )
    )
    monkeypatch.setattr(
        "hermes_cli.deployment_inference.route_descriptors_from_control_plane",
        route_resolver,
    )
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.read_raw_config",
        lambda: {
            "model": {
                "provider": "custom:codex",
                "default": "gpt-safe",
            }
        },
    )
    ordinary_resolver = Mock(
        side_effect=AssertionError("managed route must not use ordinary provider resolution")
    )
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        ordinary_resolver,
    )

    result = switch_model(
        raw_input="k3-256k",
        current_provider="custom:codex",
        current_model="gpt-safe",
        explicit_provider="custom:kimi-code",
        user_providers={},
        custom_providers=[],
    )

    assert result.success is True
    assert result.target_provider == "custom:kimi-code"
    assert result.new_model == "k3-256k"
    assert result.api_mode == "anthropic_messages"
    assert result.base_url == "http://127.0.0.1:39123/v1"
    assert result.api_key == "deployment-inference-relay"
    assert result.deployment_managed is True
    ordinary_resolver.assert_not_called()
    route_resolver.assert_called_once_with()
