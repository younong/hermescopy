import importlib
import json


def test_deployment_route_exposes_apiyi_without_user_key(monkeypatch):
    """A declared deployment media route alone satisfies the tool check.

    The worker needs no user credential: execution rides the relay to the
    Control Plane, which holds the deployment key. The tool surface must
    therefore describe the routed model as available and reference-capable.
    """
    monkeypatch.setenv("HERMES_OWNER_KEY", "owner")
    monkeypatch.setenv("HERMES_DEPLOYMENT_MEDIA_POLICY_ID", "policy")
    monkeypatch.setenv(
        "HERMES_DEPLOYMENT_MEDIA_ROUTES",
        json.dumps(
            [
                {
                    "kind": "image",
                    "provider": "apiyi",
                    "models": ["gpt-image-2-medium"],
                    "default_model": "gpt-image-2-medium",
                    "text_only_models": [],
                    "max_reference_images": 16,
                    "max_reference_bytes": 16 << 20,
                    "max_total_reference_bytes": 48 << 20,
                    "max_output_bytes": 32 << 20,
                }
            ]
        ),
    )
    monkeypatch.delenv("APIYI_API_KEY", raising=False)
    module = importlib.import_module("tools.image_generation_tool")
    assert module.check_image_generation_requirements() is True
    info = module._active_image_capabilities()
    assert info["provider"] == "apiyi"
    assert info["model"] == "gpt-image-2-medium"
    assert info["modalities"] == ["text", "image"]
    assert info["max_reference_images"] == 16
