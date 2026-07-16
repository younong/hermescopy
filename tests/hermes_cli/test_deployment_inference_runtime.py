from __future__ import annotations

from hermes_cli import runtime_provider as rp


def _deployment_env(monkeypatch) -> None:
    monkeypatch.setenv("HERMES_OWNER_KEY", "ok1_test")
    monkeypatch.setenv("HERMES_DEPLOYMENT_INFERENCE_PROVIDER", "custom:deployment")
    monkeypatch.setenv("HERMES_DEPLOYMENT_INFERENCE_MODEL", "gpt-safe")
    monkeypatch.setenv("HERMES_DEPLOYMENT_INFERENCE_API_MODE", "chat_completions")
    monkeypatch.setenv("HERMES_DEPLOYMENT_INFERENCE_POLICY_ID", "policy-v1")
    monkeypatch.setenv("HERMES_DEPLOYMENT_INFERENCE_ALLOWED_MODELS", "gpt-safe,gpt-safe-mini")
    monkeypatch.setenv("HERMES_DEPLOYMENT_INFERENCE_RELAY_BASE_URL", "http://127.0.0.1:39123/v1")


def test_blank_owner_uses_deployment_relay(monkeypatch):
    _deployment_env(monkeypatch)
    monkeypatch.setattr(rp, "read_raw_config", lambda: {})

    resolved = rp.resolve_runtime_provider(target_model="gpt-safe")

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


def test_deployment_relay_requires_loopback_endpoint(monkeypatch):
    _deployment_env(monkeypatch)
    monkeypatch.setattr(rp, "read_raw_config", lambda: {})
    monkeypatch.setenv("HERMES_DEPLOYMENT_INFERENCE_RELAY_BASE_URL", "https://gateway.example.test/v1")

    assert rp.resolve_deployment_inference_runtime(target_model="gpt-safe") is None
