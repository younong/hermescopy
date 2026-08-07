from __future__ import annotations

from typing import Any

import pytest

from agent import image_gen_registry, video_gen_registry
from agent.image_gen_provider import ImageGenProvider
from agent.video_gen_provider import VideoGenProvider
from hermes_cli import model_registrations
from hermes_cli.config import DEFAULT_CONFIG, load_config, load_env, save_config
from hermes_cli.deployment_image import DeploymentImageDescriptor
from hermes_cli.deployment_inference import DeploymentInferenceRouteDescriptor


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


def _deployment_registrations(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.deployment_inference.route_descriptors_from_control_plane",
        lambda: (
            DeploymentInferenceRouteDescriptor(
                provider="openai-codex",
                model="gpt-5.3-codex",
                api_mode="chat_completions",
                name="ChatGPT Codex",
            ),
            DeploymentInferenceRouteDescriptor(
                provider="kimi-coding",
                model="kimi-k2.5",
                api_mode="anthropic_messages",
                name="Kimi Code",
            ),
        ),
    )
    monkeypatch.setattr(
        "hermes_cli.deployment_image.deployment_image_descriptor_from_environment",
        lambda: DeploymentImageDescriptor(
            provider="apiyi",
            model="gpt-image-2-medium",
            policy_id="deployment-image",
            allowed_models=("gpt-image-2-medium", "nano-banana-2"),
        ),
    )


def test_payload_merges_admin_descriptors_with_legacy_user_registrations(monkeypatch):
    _deployment_registrations(monkeypatch)
    config = load_config()
    config["model_registrations"] = {
        "mine-a": {
            "name": "Mine",
            "kind": "chat",
            "provider": "anthropic",
            "model": "claude-test",
            "source": "catalog",
        },
    }
    save_config(config, preserve_keys={("model_registrations",)})

    payload = model_registrations.get_model_registrations_payload()
    by_name = {item["name"]: item for item in payload["registrations"]}

    assert by_name["Mine"] == {
        "id": "mine-a",
        "name": "Mine",
        "kind": "chat",
        "provider": "anthropic",
        "model": "claude-test",
        "source": "catalog",
        "scope": "user",
        "mutable": True,
        "use_gateway": False,
        "credential_configured": None,
    }
    assert by_name["ChatGPT Codex"]["scope"] == "admin"
    assert by_name["ChatGPT Codex"]["mutable"] is False
    assert by_name["Kimi Code"]["provider"] == "kimi-coding"
    assert by_name["APIYI · nano-banana-2"]["kind"] == "image"
    assert all("owner" not in item for item in payload["registrations"])
    assert all("api_key" not in item for item in payload["registrations"])


def test_admin_registrations_are_stable_resolvable_and_immutable(monkeypatch):
    _deployment_registrations(monkeypatch)
    payload = model_registrations.get_model_registrations_payload()
    chat = next(item for item in payload["registrations"] if item["name"] == "Kimi Code")
    image = next(item for item in payload["registrations"] if item["name"] == "APIYI · nano-banana-2")

    assert chat["id"] == model_registrations._admin_registration_id(
        "chat", "kimi-coding", "kimi-k2.5"
    )
    assert model_registrations.resolve_chat_model_registration(chat["id"]) == {
        "registration_id": chat["id"],
        "provider": "kimi-coding",
        "model": "kimi-k2.5",
        "source": "catalog",
        "selection_source": "deployment",
    }
    with pytest.raises(model_registrations.ModelRegistrationImmutable):
        model_registrations.update_model_registration(chat["id"], {})
    with pytest.raises(model_registrations.ModelRegistrationImmutable):
        model_registrations.delete_model_registration(image["id"])

    activated = model_registrations.activate_model_registration(image["id"])
    assert activated["provider"] == "apiyi"
    assert activated["model"] == "nano-banana-2"
    assert load_config()["image_gen"] == {
        "provider": "apiyi",
        "model": "nano-banana-2",
        "use_gateway": False,
    }


def test_catalog_registration_cannot_duplicate_admin_target(monkeypatch):
    _deployment_registrations(monkeypatch)
    monkeypatch.setattr(model_registrations, "_chat_catalog", lambda: [{
        "slug": "kimi-coding",
        "name": "Kimi",
        "models": ["kimi-k2.5"],
        "authenticated": True,
    }])

    with pytest.raises(model_registrations.ModelRegistrationConflict):
        model_registrations.create_model_registration({
            "name": "Duplicate Kimi",
            "kind": "chat",
            "provider": "kimi-coding",
            "model": "kimi-k2.5",
        })
    with pytest.raises(model_registrations.ModelRegistrationError, match="server-managed"):
        model_registrations.create_model_registration({
            "name": "Forged admin",
            "kind": "chat",
            "provider": "kimi-coding",
            "model": "kimi-k2.5",
            "scope": "admin",
        })


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


def test_custom_chat_registration_resolves_through_runtime_provider():
    created = model_registrations.create_model_registration({
        "name": "Runtime endpoint",
        "kind": "chat",
        "source": "custom",
        "model": "runtime-model",
        "base_url": "https://runtime.example/v1",
        "api_mode": "anthropic_messages",
        "api_key": "runtime-secret",
    })

    from hermes_cli.runtime_provider import resolve_runtime_provider

    resolved = resolve_runtime_provider(requested=created["provider"])

    assert resolved["provider"] == "custom"
    assert resolved["api_mode"] == "anthropic_messages"
    assert resolved["base_url"] == "https://runtime.example/v1"
    assert resolved["api_key"] == "runtime-secret"
    assert resolved["model"] == "runtime-model"
    assert resolved["requested_provider"] == created["provider"]


def test_deleting_inactive_custom_chat_cleans_generated_provider_and_secret():
    created = model_registrations.create_model_registration({
        "name": "Disposable endpoint",
        "kind": "chat",
        "source": "custom",
        "model": "disposable-model",
        "base_url": "https://disposable.example/v1",
        "api_mode": "openai",
        "api_key": "disposable-secret",
    })
    config = load_config()
    registration = config["model_registrations"][created["id"]]
    provider = registration["provider"]
    key_env = registration["key_env"]

    assert model_registrations.delete_model_registration(created["id"]) == {
        "ok": True,
        "id": created["id"],
    }

    config = load_config()
    assert created["id"] not in config["model_registrations"]
    assert provider not in config["providers"]
    assert key_env not in load_env()


def test_active_custom_chat_delete_has_no_partial_cleanup():
    created = model_registrations.create_model_registration({
        "name": "Active endpoint",
        "kind": "chat",
        "source": "custom",
        "model": "active-model",
        "base_url": "https://active.example/v1",
        "api_mode": "openai",
        "api_key": "active-secret",
    })
    config = load_config()
    registration = config["model_registrations"][created["id"]]
    provider = registration["provider"]
    key_env = registration["key_env"]
    config["model"] = {"provider": provider, "default": registration["model"]}
    save_config(config, preserve_keys={("model",)})

    with pytest.raises(model_registrations.ModelRegistrationConflict):
        model_registrations.delete_model_registration(created["id"])

    config = load_config()
    assert config["model_registrations"][created["id"]] == registration
    assert provider in config["providers"]
    assert load_env()[key_env] == "active-secret"


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
