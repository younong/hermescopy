import os

import pytest

from hermes_cli.deployment_image import (
    DeploymentImageDescriptor, DeploymentImagePolicy, DeploymentImagePolicyInvalid,
    deployment_image_descriptor_from_environment, policy_from_control_plane_environment,
)


def test_descriptor_round_trip_is_secret_free(monkeypatch):
    descriptor = DeploymentImageDescriptor(
        provider="apiyi", model="gpt-image-2-medium", policy_id="policy",
        allowed_models=("gpt-image-2-medium",),
    )
    env = {
        "HERMES_DEPLOYMENT_IMAGE_PROVIDER": descriptor.provider,
        "HERMES_DEPLOYMENT_IMAGE_MODEL": descriptor.model,
        "HERMES_DEPLOYMENT_IMAGE_POLICY_ID": descriptor.policy_id,
        "HERMES_DEPLOYMENT_IMAGE_ALLOWED_MODELS": descriptor.model,
        "HERMES_DEPLOYMENT_IMAGE_MAX_REFERENCES": str(descriptor.max_reference_images),
        "HERMES_DEPLOYMENT_IMAGE_MAX_REFERENCE_BYTES": str(descriptor.max_reference_bytes),
        "HERMES_DEPLOYMENT_IMAGE_MAX_TOTAL_REFERENCE_BYTES": str(descriptor.max_total_reference_bytes),
        "HERMES_DEPLOYMENT_IMAGE_MAX_OUTPUT_BYTES": str(descriptor.max_output_bytes),
    }
    assert deployment_image_descriptor_from_environment(env) == descriptor
    assert "KEY" not in " ".join(env)


def test_descriptor_rejects_unsupported_model():
    with pytest.raises(DeploymentImagePolicyInvalid):
        DeploymentImageDescriptor(provider="apiyi", model="other", policy_id="p", allowed_models=("other",))


def test_control_plane_policy_captures_key_without_serializing_it(monkeypatch):
    monkeypatch.setenv("APIYI_API_KEY", "secret-value")
    policy = policy_from_control_plane_environment()
    assert policy is not None
    assert "secret-value" not in repr(policy.descriptor())
    assert policy.resolve_runtime()["api_key"] == "secret-value"


def test_policy_generation_validates_bounded_image_response():
    policy = DeploymentImagePolicy(
        runtime_resolver=lambda: {"api_key": "secret", "openai_base_url": "https://api.example/v1", "gemini_base_url": "https://api.example/v1beta"},
        image_generator=lambda **kwargs: {"image_bytes": b"png", "mime_type": "image/png", "metadata": {"size": "1024x1024"}},
        allowed_models=("gpt-image-2-medium",),
    )
    result = policy.generate(prompt="draw", aspect_ratio="square", model="gpt-image-2-medium", references=[])
    assert result["image_bytes"] == b"png"
    assert result["provider"] == "apiyi"
