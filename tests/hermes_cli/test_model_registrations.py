from __future__ import annotations

import json
from typing import Any

import pytest

from agent.image_gen_provider import ImageGenProvider
from agent.video_gen_provider import VideoGenProvider
from hermes_cli import model_registrations
from hermes_cli.config import DEFAULT_CONFIG, load_config, load_env, save_config
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
    with pytest.raises(
        model_registrations.ModelRegistrationError,
        match="Only code, image, video, voice, and vector",
    ):
        model_registrations.activate_model_registration(chat["id"])
