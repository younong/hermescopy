import importlib


def test_deployment_descriptor_exposes_apiyi_without_worker_key(monkeypatch):
    monkeypatch.setenv("HERMES_OWNER_KEY", "owner")
    monkeypatch.setenv("HERMES_DEPLOYMENT_IMAGE_PROVIDER", "apiyi")
    monkeypatch.setenv("HERMES_DEPLOYMENT_IMAGE_MODEL", "gpt-image-2-medium")
    monkeypatch.setenv("HERMES_DEPLOYMENT_IMAGE_POLICY_ID", "policy")
    monkeypatch.setenv("HERMES_DEPLOYMENT_IMAGE_ALLOWED_MODELS", "gpt-image-2-medium")
    monkeypatch.setenv("HERMES_DEPLOYMENT_IMAGE_MAX_REFERENCES", "16")
    monkeypatch.setenv("HERMES_DEPLOYMENT_IMAGE_MAX_REFERENCE_BYTES", str(16 << 20))
    monkeypatch.setenv("HERMES_DEPLOYMENT_IMAGE_MAX_TOTAL_REFERENCE_BYTES", str(48 << 20))
    monkeypatch.setenv("HERMES_DEPLOYMENT_IMAGE_MAX_OUTPUT_BYTES", str(32 << 20))
    monkeypatch.delenv("APIYI_API_KEY", raising=False)
    module = importlib.import_module("tools.image_generation_tool")
    assert module.check_image_generation_requirements() is True
    info = module._active_image_capabilities()
    assert info["provider"] == "APIYI"
    assert info["model"] == "gpt-image-2-medium"
    assert info["modalities"] == ["text", "image"]
