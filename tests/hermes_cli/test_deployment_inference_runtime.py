from __future__ import annotations

from hermes_cli import model_registrations
from hermes_cli import runtime_provider as rp
from hermes_cli.config import load_config, read_raw_config, save_config
from hermes_cli.deployment_inference import DeploymentInferenceRouteDescriptor


def _deployment_env(monkeypatch) -> None:
    monkeypatch.setenv("HERMES_OWNER_KEY", "ok1_test")
    monkeypatch.setenv("HERMES_DEPLOYMENT_INFERENCE_PROVIDER", "custom:deployment")
    monkeypatch.setenv("HERMES_DEPLOYMENT_INFERENCE_MODEL", "gpt-safe")
    monkeypatch.setenv("HERMES_DEPLOYMENT_INFERENCE_API_MODE", "chat_completions")
    monkeypatch.setenv("HERMES_DEPLOYMENT_INFERENCE_POLICY_ID", "policy-v1")
    monkeypatch.setenv("HERMES_DEPLOYMENT_INFERENCE_RELAY_BASE_URL", "http://127.0.0.1:39123/v1")
    monkeypatch.setattr(
        "hermes_cli.deployment_inference.route_descriptors_from_control_plane",
        lambda: (
            DeploymentInferenceRouteDescriptor(
                provider="custom:deployment",
                model="gpt-safe",
                api_mode="chat_completions",
            ),
            DeploymentInferenceRouteDescriptor(
                provider="custom:kimi-code",
                model="gpt-safe-mini",
                api_mode="anthropic_messages",
                name="Kimi Code",
            ),
            DeploymentInferenceRouteDescriptor(
                provider="custom:codex",
                model="gpt-5.6-sol",
                api_mode="codex_responses",
                name="Codex",
            ),
        ),
    )


def test_blank_owner_uses_deployment_relay(monkeypatch):
    _deployment_env(monkeypatch)
    monkeypatch.setattr(rp, "read_raw_config", lambda: {})

    resolved = rp.resolve_runtime_provider()

    assert resolved == {
        "provider": "custom:deployment",
        "api_mode": "chat_completions",
        "api_key": "deployment-inference-relay",
        "source": "deployment-relay",
        "selection_source": "deployment",
        "policy_id": "policy-v1",
        "model": "gpt-safe",
        "base_url": "http://127.0.0.1:39123/v1",
        "requested_provider": "custom:deployment",
        "relay_provider": "custom:deployment",
    }


def test_admin_chat_activation_preserves_deployment_relay_startup(monkeypatch):
    from tui_gateway import server

    _deployment_env(monkeypatch)
    monkeypatch.setattr(model_registrations, "_admin_media_descriptor", lambda: None)
    config = load_config()
    config["model"] = {
        "provider": "custom:old",
        "default": "old-model",
        "base_url": "https://old.example/v1",
        "api_mode": "responses",
        "api_key": "old-secret",
        "reasoning": {"effort": "high"},
    }
    save_config(config, preserve_keys={("model",)})
    registration_id = model_registrations._admin_registration_id(
        "chat", "custom:deployment", "gpt-safe"
    )

    model_registrations.activate_model_registration(registration_id)

    assert read_raw_config()["model"] == {
        "registration_id": registration_id,
        "reasoning": {"effort": "high"},
    }
    assert rp._explicit_owner_model_selection() == {}
    monkeypatch.setattr(server, "_cfg_cache", None)
    monkeypatch.setattr(server, "_cfg_mtime", None)
    monkeypatch.setattr(server, "_cfg_path", None)
    model, provider = server._resolve_startup_runtime()
    assert (model, provider) == ("gpt-safe", "custom:deployment")

    resolved = server._resolve_runtime_with_fallback({
        "requested": provider,
        "target_model": model,
    })

    assert resolved == {
        "provider": "custom:deployment",
        "api_mode": "chat_completions",
        "api_key": "deployment-inference-relay",
        "source": "deployment-relay",
        "selection_source": "deployment",
        "policy_id": "policy-v1",
        "model": "gpt-safe",
        "base_url": "http://127.0.0.1:39123/v1",
        "requested_provider": "custom:deployment",
        "relay_provider": "custom:deployment",
    }


def test_blank_owner_request_override_must_match_policy(monkeypatch):
    _deployment_env(monkeypatch)
    monkeypatch.setattr(rp, "read_raw_config", lambda: {})

    assert rp.resolve_deployment_inference_runtime(
        requested="other-provider",
        target_model="gpt-safe",
    ) is None
    assert rp.resolve_deployment_inference_runtime(
        requested="custom:deployment",
        target_model="unapproved-model",
    ) is None
    assert rp.resolve_deployment_inference_runtime(
        requested="custom:deployment",
        explicit_base_url="https://attacker.example.test/v1",
        target_model="gpt-safe",
    ) is None


def test_explicit_owner_config_never_uses_deployment_relay(monkeypatch):
    _deployment_env(monkeypatch)
    monkeypatch.setattr(
        rp,
        "read_raw_config",
        lambda: {"model": {"provider": "custom:deployment", "default": "gpt-safe"}},
    )

    assert rp.resolve_deployment_inference_runtime(target_model="gpt-safe") is None


def test_blank_owner_selects_exact_secondary_deployment_route(monkeypatch):
    _deployment_env(monkeypatch)
    monkeypatch.setattr(rp, "read_raw_config", lambda: {})

    resolved = rp.resolve_deployment_inference_runtime(
        requested="custom:kimi-code",
        target_model="gpt-safe-mini",
    )

    assert resolved["provider"] == "custom:kimi-code"
    assert resolved["api_mode"] == "anthropic_messages"
    assert resolved["model"] == "gpt-safe-mini"
    assert resolved["base_url"] == "http://127.0.0.1:39123/v1"
    assert rp.resolve_deployment_inference_runtime(
        requested="custom:deployment",
        target_model="gpt-safe-mini",
    ) is None


def test_blank_owner_selects_exact_codex_responses_route(monkeypatch):
    _deployment_env(monkeypatch)
    monkeypatch.setattr(rp, "read_raw_config", lambda: {})

    resolved = rp.resolve_deployment_inference_runtime(
        requested="custom:codex",
        target_model="gpt-5.6-sol",
    )

    assert resolved["provider"] == "custom:codex"
    assert resolved["api_mode"] == "codex_responses"
    assert resolved["api_key"] == "deployment-inference-relay"
    assert resolved["model"] == "gpt-5.6-sol"
    assert resolved["base_url"] == "http://127.0.0.1:39123/v1"
    assert rp.resolve_deployment_inference_runtime(
        requested="custom:deployment",
        target_model="gpt-5.6-sol",
    ) is None


def test_deployment_relay_requires_loopback_endpoint(monkeypatch):
    _deployment_env(monkeypatch)
    monkeypatch.setattr(rp, "read_raw_config", lambda: {})
    monkeypatch.setenv("HERMES_DEPLOYMENT_INFERENCE_RELAY_BASE_URL", "https://gateway.example.test/v1")

    assert rp.resolve_deployment_inference_runtime(target_model="gpt-safe") is None
