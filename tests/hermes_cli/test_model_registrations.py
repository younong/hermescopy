from __future__ import annotations

from typing import Any

import pytest

from agent import image_gen_registry, video_gen_registry
from agent.image_gen_provider import ImageGenProvider
from agent.video_gen_provider import VideoGenProvider
from hermes_cli import model_registrations
from hermes_cli.config import DEFAULT_CONFIG, load_config, load_env


class _ImageProvider(ImageGenProvider):
    @property
    def name(self) -> str:
        return "image-test"

    def is_available(self) -> bool:
        return False

    def list_models(self):
        return [{"id": "image-v1", "display": "Image V1"}]

    def get_setup_schema(self):
        return {
            "name": "Image Test",
            "env_vars": [{"key": "IMAGE_TEST_API_KEY", "prompt": "Secret"}],
        }

    def capabilities(self):
        return {"modalities": ["text", "image"]}

    def generate(self, prompt, aspect_ratio="landscape", **kwargs):
        return {"success": True}


class _VideoProvider(VideoGenProvider):
    @property
    def name(self) -> str:
        return "video-test"

    def list_models(self):
        return [{"id": "video-v1", "display": "Video V1"}]

    def generate(self, prompt, **kwargs):
        return {"success": True}


@pytest.fixture(autouse=True)
def _registries(monkeypatch):
    image_gen_registry._reset_for_tests()
    video_gen_registry._reset_for_tests()
    image_gen_registry.register_provider(_ImageProvider())
    video_gen_registry.register_provider(_VideoProvider())
    monkeypatch.setattr("hermes_cli.plugins._ensure_plugins_discovered", lambda *args, **kwargs: None)
    yield
    image_gen_registry._reset_for_tests()
    video_gen_registry._reset_for_tests()


def _chat_catalog() -> list[dict[str, Any]]:
    return [{
        "slug": "anthropic",
        "name": "Anthropic",
        "models": ["claude-test"],
        "authenticated": True,
        "credential_configured": True,
    }]


def test_default_config_has_optional_registration_mapping():
    assert DEFAULT_CONFIG["model_registrations"] == {}


def test_catalog_media_crud_activation_and_active_delete_guard():
    created = model_registrations.create_model_registration({
        "name": "My image",
        "kind": "image",
        "provider": "image-test",
        "model": "image-v1",
    })
    assert created["kind"] == "image"
    assert created["credential_configured"] is None

    updated = model_registrations.update_model_registration(created["id"], {
        "name": "Edited image",
        "kind": "image",
        "provider": "image-test",
        "model": "image-v1",
    })
    assert updated["name"] == "Edited image"

    activated = model_registrations.activate_model_registration(created["id"])
    assert activated["model"] == "image-v1"
    config = load_config()
    assert config["image_gen"] == {
        "provider": "image-test",
        "model": "image-v1",
        "use_gateway": False,
    }
    with pytest.raises(model_registrations.ModelRegistrationConflict):
        model_registrations.delete_model_registration(created["id"])


def test_custom_chat_secret_is_env_only_and_empty_update_preserves():
    created = model_registrations.create_model_registration({
        "name": "Private endpoint",
        "kind": "chat",
        "source": "custom",
        "model": "private-model",
        "base_url": "https://llm.example/v1",
        "api_mode": "openai",
        "api_key": "super-secret-value",
        "context_length": 32000,
    })
    assert created["credential_configured"] is True
    assert "super-secret-value" not in repr(created)

    config = load_config()
    registration = config["model_registrations"][created["id"]]
    provider = config["providers"][registration["provider"]]
    assert provider["key_env"] == registration["key_env"]
    assert "api_key" not in provider
    assert load_env()[registration["key_env"]] == "super-secret-value"
    from hermes_cli.runtime_provider import has_named_custom_provider

    assert has_named_custom_provider(registration["provider"])

    updated = model_registrations.update_model_registration(created["id"], {
        "name": "Private endpoint renamed",
        "kind": "chat",
        "source": "custom",
        "model": "private-model",
        "base_url": "https://llm.example/v1",
        "api_mode": "openai",
        "api_key": "",
    })
    assert updated["credential_configured"] is True
    assert load_env()[registration["key_env"]] == "super-secret-value"


def test_duplicate_type_and_catalog_validation(monkeypatch):
    monkeypatch.setattr(model_registrations, "_chat_catalog", _chat_catalog)
    first = model_registrations.create_model_registration({
        "name": "Primary",
        "kind": "chat",
        "provider": "anthropic",
        "model": "claude-test",
    })

    with pytest.raises(model_registrations.ModelRegistrationConflict):
        model_registrations.create_model_registration({
            "name": "Duplicate target",
            "kind": "chat",
            "provider": "anthropic",
            "model": "claude-test",
        })
    with pytest.raises(model_registrations.ModelRegistrationError, match="kind cannot be changed"):
        model_registrations.update_model_registration(first["id"], {
            "name": "Primary",
            "kind": "image",
            "provider": "image-test",
            "model": "image-v1",
        })
    with pytest.raises(model_registrations.ModelRegistrationError, match="not available"):
        model_registrations.create_model_registration({
            "name": "Unknown media",
            "kind": "video",
            "provider": "video-test",
            "model": "missing-model",
        })


def test_chat_catalog_disables_network_discovery(monkeypatch):
    captured: dict[str, Any] = {}

    monkeypatch.setattr(
        "hermes_cli.inventory.load_picker_context",
        lambda: object(),
    )

    def _build(context, **kwargs):
        captured["context"] = context
        captured.update(kwargs)
        return {
            "providers": [{
                "slug": "anthropic",
                "name": "Anthropic",
                "models": ["claude-test"],
                "authenticated": True,
            }],
        }

    monkeypatch.setattr("hermes_cli.inventory.build_models_payload", _build)

    assert model_registrations._chat_catalog()[0]["models"] == ["claude-test"]
    assert captured["allow_network"] is False


def test_payload_is_lightweight_and_catalog_is_safe(monkeypatch):
    monkeypatch.setattr(model_registrations, "_chat_catalog", _chat_catalog)
    chat = model_registrations.create_model_registration({
        "name": "Chat",
        "kind": "chat",
        "provider": "anthropic",
        "model": "claude-test",
    })

    media_catalog = model_registrations._media_catalog
    monkeypatch.setattr(
        model_registrations,
        "_media_catalog",
        lambda kind: pytest.fail(f"unexpected {kind} catalog load"),
    )
    payload = model_registrations.get_model_registrations_payload()
    assert "catalogs" not in payload
    assert payload["registrations"][0]["id"] == chat["id"]

    image = media_catalog("image")[0]
    assert image["available"] is False
    assert image["capabilities"] == {"modalities": ["text", "image"]}
    assert image["setup"]["env_vars"] == [{"key": "IMAGE_TEST_API_KEY", "prompt": "Secret"}]
    assert "value" not in repr(image)
    assert model_registrations.get_model_registration_catalog("chat") == {
        "kind": "chat",
        "providers": _chat_catalog(),
    }
    with pytest.raises(model_registrations.ModelRegistrationError, match="kind must be"):
        model_registrations.get_model_registration_catalog("audio")
    with pytest.raises(model_registrations.ModelRegistrationError, match="session gateway"):
        model_registrations.activate_model_registration(chat["id"])
