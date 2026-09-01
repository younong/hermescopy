from __future__ import annotations

import json
from typing import Any

import pytest

from agent.image_gen_provider import ImageGenProvider
from agent.video_gen_provider import VideoGenProvider
from hermes_cli import model_registrations
from hermes_cli.config import (
    DEFAULT_CONFIG,
    load_config,
    load_env,
    read_raw_config,
    save_config,
)
from hermes_cli.deployment_inference import DeploymentInferenceRouteDescriptor
from hermes_cli.deployment_media import (
    POLICY_ID_ENV,
    ROUTES_ENV,
    DeploymentMediaPolicy,
    DeploymentMediaRoute,
    DeploymentMediaRouteDescriptor,
)
from hermes_cli.model_plane import capability as capability_module
from hermes_cli.model_plane.capability import CapabilityModel


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


class _VoiceCapability:
    kind = "voice"
    name = "voice-test"
    display_name = "Voice Test"
    capability = "tts"

    def is_available(self):
        return False

    def list_models(self):
        return [
            CapabilityModel(id="voice-v1", display="Voice V1", capability="tts"),
            CapabilityModel(id="voice-v2", display="Voice V2", capability="tts"),
        ]

    def default_model(self):
        return "voice-v1"

    def get_setup_schema(self):
        return {
            "name": "Voice Test",
            "env_vars": [{"key": "VOICE_TEST_API_KEY", "prompt": "Secret"}],
        }

    def capabilities(self):
        return {}


class _VectorCapability:
    kind = "vector"
    name = "vector-test"
    display_name = "Vector Test"
    capability = ""

    def is_available(self):
        return False

    def list_models(self):
        return [
            CapabilityModel(id="vector-v1", display="Vector V1"),
            CapabilityModel(id="vector-v2", display="Vector V2"),
        ]

    def default_model(self):
        return "vector-v1"

    def get_setup_schema(self):
        return {
            "name": "Vector Test",
            "env_vars": [{"key": "VECTOR_TEST_API_KEY", "prompt": "Secret"}],
        }

    def capabilities(self):
        return {"dimensions": [1024]}


@pytest.fixture(autouse=True)
def _registries(monkeypatch):
    capability_module._reset_for_tests()
    capability_module.register_media_generation_provider("image", _ImageProvider())
    capability_module.register_media_generation_provider("video", _VideoProvider())
    monkeypatch.setattr("hermes_cli.plugins._ensure_plugins_discovered", lambda *args, **kwargs: None)
    # Keep the catalog hermetic: real plugin discovery, TTS/STT registries,
    # and profile embedding declarations stay out of this file.
    monkeypatch.setattr(
        "hermes_cli.model_plane.catalog.ensure_capability_providers",
        lambda: None,
    )
    yield
    capability_module._reset_for_tests()


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
    from providers.base import ProviderProfile

    capability_module.register_code_provider(
        ProviderProfile(
            name="openai-codex",
            fallback_models=("gpt-5.3-codex",),
            code_models=("gpt-5.3-codex",),
            chat_enabled=True,
        )
    )
    capability_module.register_code_provider(
        ProviderProfile(
            name="kimi-coding",
            fallback_models=("kimi-k2.5",),
            code_models=("kimi-k2.5",),
            chat_enabled=True,
        )
    )
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
        "hermes_cli.deployment_media.policy_from_control_plane_environment",
        lambda: DeploymentMediaPolicy(
            routes=(
                DeploymentMediaRoute(
                    descriptor=DeploymentMediaRouteDescriptor(
                        kind="image",
                        provider="apiyi",
                        models=("gpt-image-2-medium", "nano-banana-2"),
                        default_model="gpt-image-2-medium",
                    ),
                    key_env="TEST_ADMIN_MEDIA_KEY",
                    executor="plugins.image_gen.apiyi:generate_apiyi_image_bytes",
                ),
            ),
            policy_id="deployment-media",
        ),
    )


def test_deployment_route_kind_is_model_scoped(monkeypatch):
    _deployment_registrations(monkeypatch)
    from hermes_cli.deployment_inference import DeploymentInferenceRouteDescriptor

    assert model_registrations._deployment_route_kind(
        DeploymentInferenceRouteDescriptor(
            provider="openai-codex",
            model="gpt-5.6-sol",
            api_mode="chat_completions",
        )
    ) == "chat"
    assert model_registrations._deployment_route_kind(
        DeploymentInferenceRouteDescriptor(
            provider="openai-codex",
            model="gpt-5.3-codex",
            api_mode="chat_completions",
        )
    ) == "code"


def test_admin_chat_registration_resolves_from_owner_worker_descriptor(monkeypatch):
    from hermes_cli.deployment_inference import DeploymentInferenceDescriptor

    monkeypatch.setattr(
        "hermes_cli.owner_runtime.is_owner_worker_env",
        lambda: True,
    )
    monkeypatch.setattr(
        "hermes_cli.deployment_inference.route_descriptors_from_control_plane",
        lambda: (),
    )
    monkeypatch.setattr(
        "hermes_cli.deployment_inference.deployment_descriptor_from_environment",
        lambda: DeploymentInferenceDescriptor(
            provider="deployment-provider",
            model="model-a",
            api_mode="anthropic_messages",
            policy_id="policy-a",
        ),
    )
    monkeypatch.setattr(
        "hermes_cli.model_registrations._admin_media_descriptor",
        lambda: None,
    )

    registrations = model_registrations.admin_chat_registrations_payload()
    by_model = {item["model"]: item for item in registrations}
    selected = by_model["model-a"]
    resolved = model_registrations.resolve_admin_chat_model_registration(
        selected["id"]
    )

    assert set(by_model) == {"model-a"}
    assert resolved["provider"] == "deployment-provider"
    assert resolved["model"] == "model-a"
    assert resolved["selection_source"] == "deployment"


def test_admin_chat_activation_persists_only_registration_id(monkeypatch):
    route = DeploymentInferenceRouteDescriptor(
        provider="custom:deployment",
        model="gpt-safe",
        api_mode="chat_completions",
        name="Managed Chat",
    )
    monkeypatch.setattr(
        "hermes_cli.deployment_inference.route_descriptors_from_control_plane",
        lambda: (route,),
    )
    monkeypatch.setattr(model_registrations, "_admin_media_descriptor", lambda: None)
    registration_id = model_registrations._admin_registration_id(
        "chat", route.provider, route.model
    )
    config = load_config()
    config["model"] = {
        "registration_id": "old-registration",
        "provider": "custom:old",
        "default": "old-default",
        "model": "old-model",
        "base_url": "https://old.example/v1",
        "api_mode": "responses",
        "api_key": "old-secret",
        "reasoning": {"effort": "high"},
        "context_length": 131072,
    }
    save_config(config, preserve_keys={("model",)})

    activated = model_registrations.activate_model_registration(registration_id)

    assert activated == {
        "ok": True,
        "registration_id": registration_id,
        "kind": "chat",
        "provider": route.provider,
        "model": route.model,
    }
    assert read_raw_config()["model"] == {
        "registration_id": registration_id,
        "reasoning": {"effort": "high"},
        "context_length": 131072,
    }
    assert model_registrations.get_model_registrations_payload()["active"]["chat"] == {
        "registration_id": registration_id,
        "provider": route.provider,
        "model": route.model,
    }


def test_admin_registrations_control_plane_derives_media_from_policy(monkeypatch):
    """The Control Plane needs no policy-id env to surface media routes."""
    monkeypatch.setenv(
        ROUTES_ENV,
        json.dumps([
            {
                "kind": "voice",
                "provider": "volcengine-agent-plan",
                "models": ["doubao-seed-tts-2.0", "doubao-seed-asr-2.0"],
                "default_model": "doubao-seed-tts-2.0",
                "key_env": "VOLCENGINE_AGENT_PLAN_API_KEY",
            },
            {
                "kind": "vector",
                "provider": "volcengine-agent-plan",
                "models": ["doubao-embedding-vision"],
                "default_model": "doubao-embedding-vision",
                "key_env": "VOLCENGINE_AGENT_PLAN_API_KEY",
            },
        ]),
    )
    monkeypatch.delenv(POLICY_ID_ENV, raising=False)
    monkeypatch.delenv("APIYI_API_KEY", raising=False)
    monkeypatch.setattr(
        "hermes_cli.deployment_inference.route_descriptors_from_control_plane",
        lambda: (),
    )

    registrations = model_registrations._admin_registrations()
    media = {
        (item["kind"], item["provider"], item["model"])
        for item in registrations.values()
    }
    assert ("voice", "volcengine-agent-plan", "doubao-seed-tts-2.0") in media
    assert ("voice", "volcengine-agent-plan", "doubao-seed-asr-2.0") in media
    assert ("vector", "volcengine-agent-plan", "doubao-embedding-vision") in media


def test_admin_registrations_expose_custom_codex_image_route(monkeypatch):
    monkeypatch.setenv(
        ROUTES_ENV,
        json.dumps([{
            "kind": "image",
            "provider": "custom:codex",
            "models": ["gpt-image-2"],
            "default_model": "gpt-image-2",
            "key_env": "CODEX_IMAGE_KEY",
            "executor": "plugins.image_gen.openai_compatible:generate_codex_responses_image_bytes",
            "base_urls": {"openai_base_url": "https://codex.example.com/v1"},
            "executor_params": {"chat_model": "gpt-5.5", "size_profile": "gpt-image-2"},
        }]),
    )
    monkeypatch.setattr(
        "hermes_cli.deployment_inference.route_descriptors_from_control_plane",
        lambda: (),
    )
    monkeypatch.delenv("APIYI_API_KEY", raising=False)

    registrations = model_registrations._admin_registrations()
    matches = [
        item for item in registrations.values()
        if item.get("kind") == "image"
        and item.get("provider") == "custom:codex"
    ]

    assert [(item["model"], item["use_gateway"]) for item in matches] == [
        ("gpt-image-2", False)
    ]
    assert matches[0]["execution_mode"] == "deployment_relay"


def test_image_catalog_exposes_deployment_models_with_admin_registration_ids(monkeypatch):
    _deployment_registrations(monkeypatch)

    catalog = model_registrations.get_model_registration_catalog("image")
    provider = next(
        row for row in catalog["providers"] if row["provider"] == "apiyi"
    )
    models = {model["id"]: model for model in provider["models"]}
    registration_id = model_registrations._admin_registration_id(
        "image", "apiyi", "nano-banana-2"
    )

    assert models["nano-banana-2"]["deployment_owned"] is True
    assert models["nano-banana-2"]["execution_mode"] == "deployment_relay"
    assert models["nano-banana-2"]["registration_id"] == registration_id
    assert "TEST_ADMIN_MEDIA_KEY" not in repr(catalog)
    assert "executor" not in repr(catalog)


def test_media_model_catalog_uses_deployment_route_for_selection(monkeypatch):
    _deployment_registrations(monkeypatch)

    from hermes_cli.tools_config import media_model_catalog

    models, default_model = media_model_catalog("image", "apiyi")

    assert "nano-banana-2" in models
    assert default_model == "gpt-image-2-medium"


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
    assert by_name["ChatGPT Codex"]["kind"] == "code"
    assert by_name["ChatGPT Codex"]["scope"] == "admin"
    assert by_name["ChatGPT Codex"]["mutable"] is False
    assert by_name["Kimi Code"]["kind"] == "code"
    assert by_name["Kimi Code"]["provider"] == "kimi-coding"
    assert by_name["APIYI · nano-banana-2"]["kind"] == "image"
    assert all("owner" not in item for item in payload["registrations"])
    assert all("api_key" not in item for item in payload["registrations"])


def test_admin_registrations_are_stable_resolvable_and_immutable(monkeypatch):
    _deployment_registrations(monkeypatch)
    payload = model_registrations.get_model_registrations_payload()
    code = next(item for item in payload["registrations"] if item["name"] == "Kimi Code")
    image = next(item for item in payload["registrations"] if item["name"] == "APIYI · nano-banana-2")

    assert code["kind"] == "code"
    assert code["id"] == model_registrations._admin_registration_id(
        "code", "kimi-coding", "kimi-k2.5"
    )
    assert model_registrations.resolve_code_model_registration(code["id"]) == {
        "registration_id": code["id"],
        "provider": "kimi-coding",
        "model": "kimi-k2.5",
        "source": "catalog",
        "selection_source": "deployment",
        "profile": "coding",
        "toolset": "coding",
    }
    with pytest.raises(model_registrations.ModelRegistrationImmutable):
        model_registrations.update_model_registration(code["id"], {})
    with pytest.raises(model_registrations.ModelRegistrationImmutable):
        model_registrations.delete_model_registration(image["id"])

    activated = model_registrations.activate_model_registration(image["id"])
    assert activated["provider"] == "apiyi"
    assert activated["model"] == "nano-banana-2"
    assert load_config()["image_gen"] == {
        "provider": "apiyi",
        "model": "nano-banana-2",
        "use_gateway": False,
        "registration_id": image["id"],
    }


def test_catalog_registration_cannot_duplicate_admin_target(monkeypatch):
    _deployment_registrations(monkeypatch)
    monkeypatch.setattr(model_registrations, "_capability_catalog", lambda: [{
        "provider": "kimi-coding",
        "name": "Kimi Code",
        "models": [{"id": "kimi-k2.5"}],
        "available": True,
    }])

    with pytest.raises(model_registrations.ModelRegistrationConflict):
        model_registrations.create_model_registration({
            "name": "Duplicate Kimi",
            "kind": "code",
            "provider": "kimi-coding",
            "model": "kimi-k2.5",
        })
    with pytest.raises(model_registrations.ModelRegistrationError, match="server-managed"):
        model_registrations.create_model_registration({
            "name": "Forged admin",
            "kind": "code",
            "provider": "kimi-coding",
            "model": "kimi-k2.5",
            "scope": "admin",
        })


def test_media_activation_updates_only_matching_kind_section():
    image = model_registrations.create_model_registration({
        "name": "Selected image",
        "kind": "image",
        "provider": "image-test",
        "model": "image-v1",
    })
    video = model_registrations.create_model_registration({
        "name": "Selected video",
        "kind": "video",
        "provider": "video-test",
        "model": "video-v1",
    })
    config = load_config()
    config["image_gen"] = {"provider": "image-old", "model": "image-old-model", "use_gateway": True}
    config["video_gen"] = {"provider": "video-old", "model": "video-old-model", "use_gateway": True}
    save_config(config, preserve_keys={("image_gen",), ("video_gen",)})

    model_registrations.activate_model_registration(image["id"])
    config = load_config()
    assert config["image_gen"] == {
        "provider": "image-test",
        "model": "image-v1",
        "use_gateway": False,
        "registration_id": image["id"],
    }
    assert config["video_gen"] == {
        "provider": "video-old",
        "model": "video-old-model",
        "use_gateway": True,
    }

    model_registrations.activate_model_registration(video["id"])
    config = load_config()
    assert config["video_gen"] == {
        "provider": "video-test",
        "model": "video-v1",
        "use_gateway": False,
        "registration_id": video["id"],
    }
    assert config["image_gen"] == {
        "provider": "image-test",
        "model": "image-v1",
        "use_gateway": False,
        "registration_id": image["id"],
    }


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
        "registration_id": created["id"],
    }
    with pytest.raises(model_registrations.ModelRegistrationConflict):
        model_registrations.delete_model_registration(created["id"])


def test_voice_and_vector_catalog_registration_crud_and_activation():
    capability_module.register_capability_provider(_VoiceCapability())
    capability_module.register_capability_provider(_VectorCapability())

    voice = model_registrations.create_model_registration({
        "name": "Narrator",
        "kind": "voice",
        "provider": "voice-test",
        "model": "voice-v1",
    })
    vector = model_registrations.create_model_registration({
        "name": "Memory vectors",
        "kind": "vector",
        "provider": "vector-test",
        "model": "vector-v1",
    })

    assert voice["kind"] == "voice"
    assert voice["source"] == "catalog"
    assert voice["use_gateway"] is False
    assert vector["kind"] == "vector"
    assert vector["source"] == "catalog"

    voice_catalog = model_registrations.get_model_registration_catalog("voice")
    voice_row = next(
        row for row in voice_catalog["providers"] if row["provider"] == "voice-test"
    )
    assert voice_row["models"][0] == {
        "id": "voice-v1",
        "display": "Voice V1",
        "capability": "tts",
    }

    activated = model_registrations.activate_model_registration(voice["id"])
    assert activated["model"] == "voice-v1"
    assert load_config()["voice_gen"] == {
        "provider": "voice-test",
        "model": "voice-v1",
        "use_gateway": False,
        "registration_id": voice["id"],
    }
    payload = model_registrations.get_model_registrations_payload()
    assert payload["active"]["voice"] == {
        "registration_id": voice["id"],
        "provider": "voice-test",
        "model": "voice-v1",
    }
    with pytest.raises(model_registrations.ModelRegistrationConflict):
        model_registrations.delete_model_registration(voice["id"])

    updated = model_registrations.update_model_registration(vector["id"], {
        "name": "Large memory vectors",
        "kind": "vector",
        "provider": "vector-test",
        "model": "vector-v2",
    })
    assert updated["model"] == "vector-v2"
    assert model_registrations.delete_model_registration(vector["id"]) == {
        "ok": True,
        "id": vector["id"],
    }


def test_voice_and_vector_legacy_manual_registrations_remain_editable():
    created = model_registrations.create_model_registration({
        "name": "Narrator",
        "kind": "voice",
        "source": "manual",
        "provider": "openai",
        "model": "gpt-4o-mini-tts",
    })
    assert created["source"] == "manual"

    updated = model_registrations.update_model_registration(created["id"], {
        "name": "Narrator large",
        "provider": "openai",
        "model": "gpt-4o-tts",
    })
    assert updated["source"] == "manual"
    assert updated["model"] == "gpt-4o-tts"
    assert model_registrations.delete_model_registration(created["id"]) == {
        "ok": True,
        "id": created["id"],
    }


def test_registration_source_boundaries():
    capability_module.register_capability_provider(_VoiceCapability())

    with pytest.raises(model_registrations.ModelRegistrationError, match="not available"):
        model_registrations.create_model_registration({
            "name": "Unknown voice",
            "kind": "voice",
            "provider": "voice-test",
            "model": "missing-model",
        })
    for kind in ("image", "voice", "vector"):
        with pytest.raises(
            model_registrations.ModelRegistrationError,
            match="source is not supported",
        ):
            model_registrations.create_model_registration({
                "name": f"{kind} custom",
                "kind": kind,
                "source": "custom",
                "provider": "voice-test",
                "model": "voice-v1",
            })
    # ``manual`` is a legacy escape hatch that only voice/vector keep.
    with pytest.raises(
        model_registrations.ModelRegistrationError,
        match="source is not supported",
    ):
        model_registrations.create_model_registration({
            "name": "image manual",
            "kind": "image",
            "source": "manual",
            "provider": "image-test",
            "model": "image-v1",
        })


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


def test_code_registration_has_independent_catalog_and_activation(monkeypatch):
    monkeypatch.setattr(
        model_registrations,
        "_capability_catalog",
        lambda: [{
            "provider": "openai-codex",
            "name": "OpenAI Codex",
            "models": [{"id": "gpt-5.3-codex", "display": "gpt-5.3-codex"}],
            "available": True,
            "credential_configured": True,
            "default_model": "gpt-5.3-codex",
            "capabilities": {"profile": "coding", "toolset": "coding"},
            "setup": {"env_vars": []},
        }],
    )
    created = model_registrations.create_model_registration({
        "name": "Codex",
        "kind": "code",
        "provider": "openai-codex",
        "model": "gpt-5.3-codex",
    })
    assert created["kind"] == "code"
    assert "category" not in created

    activated = model_registrations.activate_model_registration(created["id"])
    assert activated["kind"] == "code"
    config = load_config()
    assert config["code_agent"] == {
        "provider": "openai-codex",
        "model": "gpt-5.3-codex",
        "registration_id": created["id"],
    }
    assert config["model"] == ""
    assert model_registrations.resolve_code_model_registration(created["id"]) == {
        "registration_id": created["id"],
        "provider": "openai-codex",
        "model": "gpt-5.3-codex",
        "source": "catalog",
        "profile": "coding",
        "toolset": "coding",
    }


def test_legacy_code_registration_migrates_and_preserves_id(monkeypatch):
    monkeypatch.setattr(
        model_registrations,
        "_capability_catalog",
        lambda: [{
            "provider": "openai-codex",
            "models": [{"id": "gpt-5.3-codex"}],
        }],
    )
    config = load_config()
    config["model_registrations"] = {
        "legacy-code": {
            "name": "Legacy Codex",
            "kind": "chat",
            "category": "code",
            "provider": "openai-codex",
            "model": "gpt-5.3-codex",
            "source": "catalog",
        },
    }
    save_config(config, preserve_keys={("model_registrations",)})

    payload = model_registrations.get_model_registrations_payload()

    migrated = next(item for item in payload["registrations"] if item["id"] == "legacy-code")
    assert migrated["kind"] == "code"
    assert "category" not in migrated
    assert load_config()["model_registrations"]["legacy-code"]["kind"] == "code"
    assert model_registrations.resolve_code_model_registration("legacy-code")["profile"] == "coding"


def test_unmigratable_legacy_code_registration_is_exposed_and_rejected(monkeypatch):
    monkeypatch.setattr(model_registrations, "_capability_catalog", lambda: [])
    config = load_config()
    config["model_registrations"] = {
        "legacy-code": {
            "name": "Missing Codex",
            "kind": "chat",
            "category": "code",
            "provider": "removed-provider",
            "model": "removed-model",
            "source": "catalog",
        },
    }
    save_config(config, preserve_keys={("model_registrations",)})

    payload = model_registrations.get_model_registrations_payload()

    migrated = next(item for item in payload["registrations"] if item["id"] == "legacy-code")
    assert migrated["kind"] == "code"
    assert migrated["migration_error"] == (
        "Code provider 'removed-provider' is no longer available"
    )
    with pytest.raises(model_registrations.ModelRegistrationError, match="no longer available"):
        model_registrations.resolve_code_model_registration("legacy-code")


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
    monkeypatch.setattr(model_registrations, "_media_catalog", media_catalog)

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
    assert model_registrations.get_model_registration_catalog("voice") == {
        "kind": "voice",
        "providers": [],
    }
    assert model_registrations.get_model_registration_catalog("vector") == {
        "kind": "vector",
        "providers": [],
    }
    config = load_config()
    config["model"] = {
        "registration_id": "old-registration",
        "provider": "old-provider",
        "default": "old-model",
        "base_url": "https://owner.example/v1",
        "api_mode": "messages",
        "reasoning": {"effort": "medium"},
    }
    save_config(config, preserve_keys={("model",)})

    activated = model_registrations.activate_model_registration(chat["id"])
    assert activated["kind"] == "chat"
    assert read_raw_config()["model"] == {
        "registration_id": chat["id"],
        "provider": "anthropic",
        "default": "claude-test",
        "base_url": "https://owner.example/v1",
        "api_mode": "messages",
        "reasoning": {"effort": "medium"},
    }
