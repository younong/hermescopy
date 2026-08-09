from __future__ import annotations

import pytest

from agent import tts_registry, transcription_registry
from agent.image_gen_provider import ImageGenProvider
from agent.tts_provider import TTSProvider
from agent.transcription_provider import TranscriptionProvider
from hermes_cli.model_plane import capability as capability_module
from hermes_cli.model_plane import catalog as catalog_module
from hermes_cli.model_plane import kinds
from hermes_cli.model_plane.capability import (
    CapabilityModel,
    MediaGenerationAdapter,
    ProfileEmbeddingCapability,
    _LegacyVoiceAdapter,
)


class _ImageDouble(ImageGenProvider):
    @property
    def name(self) -> str:
        return "image-double"

    def is_available(self) -> bool:
        return True

    def list_models(self):
        return [{"id": "image-x", "display": "Image X", "speed": "fast"}]

    def get_setup_schema(self):
        return {
            "name": "Image Double",
            "env_vars": [{"key": "IMAGE_DOUBLE_API_KEY", "prompt": "Key prompt"}],
        }

    def capabilities(self):
        return {"modalities": ["text", "image"]}

    def generate(self, prompt, aspect_ratio="landscape", **kwargs):
        return {"success": True, "prompt": prompt}


class _UnavailableImageDouble(_ImageDouble):
    @property
    def name(self) -> str:
        return "image-unavailable"

    def is_available(self) -> bool:
        return False


class _TTSDouble(TTSProvider):
    @property
    def name(self) -> str:
        return "voice-double"

    @property
    def display_name(self) -> str:
        return "Voice Double"

    def is_available(self) -> bool:
        return True

    def list_models(self):
        return [{"id": "tts-x", "display": "TTS X"}]

    def synthesize(self, text, output_path, **kwargs):
        return output_path


class _ASRDouble(TranscriptionProvider):
    @property
    def name(self) -> str:
        return "voice-double"

    def is_available(self) -> bool:
        return False

    def list_models(self):
        return [{"id": "asr-x", "display": "ASR X"}]

    def transcribe(self, file_path, **kwargs):
        return {"success": True, "transcript": "", "provider": self.name}


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    tts_registry._reset_for_tests()
    transcription_registry._reset_for_tests()
    capability_module._reset_for_tests()
    monkeypatch.setattr("hermes_cli.plugins._ensure_plugins_discovered", lambda *a, **k: None)
    monkeypatch.setattr("providers.list_providers", lambda: [])
    yield
    tts_registry._reset_for_tests()
    transcription_registry._reset_for_tests()
    capability_module._reset_for_tests()


def test_kind_contract_is_single_source():
    assert kinds.KINDS == ("chat", "image", "video", "voice", "vector")
    assert kinds.MEDIA_KINDS == ("image", "video", "voice", "vector")
    assert kinds.ACTIVATABLE_KINDS == kinds.MEDIA_KINDS
    assert kinds.GATEWAY_KINDS == ("image", "video")
    assert kinds.VOICE_CAPABILITIES == ("tts", "asr")
    assert kinds.FALLBACK_CAPABILITY_PROVIDERS == {"image": "fal"}
    assert kinds.selection_section("voice") == "voice_gen"
    assert kinds.selection_section("vector") == "vector_gen"
    with pytest.raises(ValueError):
        kinds.selection_section("chat")


def test_registry_merges_same_name_voice_delegates():
    capability_module.register_capability_provider(_LegacyVoiceAdapter("tts", _TTSDouble()))
    capability_module.register_capability_provider(_LegacyVoiceAdapter("asr", _ASRDouble()))

    provider = capability_module.get_capability_provider("voice", "voice-double")
    assert provider is not None
    assert provider.name == "voice-double"
    assert provider.display_name == "Voice Double"
    assert provider.is_available() is True  # tts delegate is available
    models = provider.list_models()
    assert [(m.id, m.capability) for m in models] == [("tts-x", "tts"), ("asr-x", "asr")]
    assert provider.default_model() == "tts-x"
    assert [p.name for p in capability_module.list_capability_providers("voice")] == [
        "voice-double"
    ]
    assert capability_module.get_capability_provider("image", "voice-double") is None
    assert capability_module.get_capability_provider("voice", "missing") is None


def test_registry_rejects_invalid_provider():
    class _Bad:
        kind = "chat"
        name = "nope"

    with pytest.raises(ValueError, match="capability kind"):
        capability_module.register_capability_provider(_Bad())


def test_media_generation_adapter_exposes_gen_media_contract():
    adapter = MediaGenerationAdapter("image", _ImageDouble())
    assert adapter.name == "image-double"
    assert adapter.capability == ""
    assert adapter.is_available() is True
    assert adapter.list_models() == [
        CapabilityModel(id="image-x", display="Image X")
    ]
    assert adapter.model_entries() == [
        {"id": "image-x", "display": "Image X", "speed": "fast"}
    ]
    assert adapter.default_model() == "image-x"
    assert adapter.get_setup_schema()["env_vars"] == [
        {"key": "IMAGE_DOUBLE_API_KEY", "prompt": "Key prompt"}
    ]
    assert adapter.capabilities() == {"modalities": ["text", "image"]}
    # Execution attributes delegate to the wrapped plugin.
    assert adapter.generate(prompt="hi") == {"success": True, "prompt": "hi"}


def test_media_generation_registration_rejects_non_gateway_kind():
    with pytest.raises(ValueError, match="media generation kind"):
        capability_module.register_media_generation_provider("voice", _ImageDouble())


def test_media_generation_registration_flows_into_catalog():
    capability_module.register_media_generation_provider("image", _ImageDouble())

    provider = capability_module.get_capability_provider("image", "image-double")
    assert provider is not None
    assert provider.name == "image-double"
    assert [p.name for p in capability_module.list_capability_providers("image")] == [
        "image-double"
    ]


def test_resolve_capability_provider_semantics():
    capability_module.register_media_generation_provider("image", _ImageDouble())

    # Explicit configuration never falls back and reports availability.
    resolution = capability_module.resolve_capability_provider(
        "image", "image-double", read_config=False
    )
    assert resolution.provider is not None
    assert resolution.provider.name == "image-double"
    assert resolution.explicit is True
    assert resolution.available is True
    assert resolution.error_type is None

    # An explicit unknown name errors without falling back.
    resolution = capability_module.resolve_capability_provider(
        "image", "missing", read_config=False
    )
    assert resolution.provider is None
    assert resolution.explicit is True
    assert resolution.error_type == "provider_not_registered"

    # An explicit but unavailable provider is reported, not replaced.
    capability_module.register_media_generation_provider("image", _UnavailableImageDouble())
    resolution = capability_module.resolve_capability_provider(
        "image", "image-unavailable", read_config=False
    )
    assert resolution.provider is not None
    assert resolution.available is False
    assert resolution.error_type == "provider_unavailable"

    # No configuration: exactly one available provider wins.
    resolution = capability_module.resolve_capability_provider(
        "image", None, read_config=False
    )
    assert resolution.provider is not None
    assert resolution.provider.name == "image-double"
    assert resolution.explicit is False


def test_resolve_capability_provider_fallback_default(monkeypatch):
    class _FalDouble(_ImageDouble):
        @property
        def name(self) -> str:
            return "fal"

    class _OtherDouble(_ImageDouble):
        @property
        def name(self) -> str:
            return "other"

    capability_module.register_media_generation_provider("image", _FalDouble())
    capability_module.register_media_generation_provider("image", _OtherDouble())

    # Multiple available providers: the kind's fallback provider wins.
    resolution = capability_module.resolve_capability_provider(
        "image", None, read_config=False
    )
    assert resolution.provider is not None
    assert resolution.provider.name == "fal"

    # Without an available fallback, multiple available providers are ambiguous.
    capability_module._reset_for_tests()
    capability_module.register_media_generation_provider("image", _OtherDouble())
    capability_module.register_media_generation_provider("video", _OtherDouble())
    resolution = capability_module.resolve_capability_provider(
        "video", None, read_config=False
    )
    assert resolution.provider is not None  # single available provider still wins

    capability_module.register_media_generation_provider("image", _ImageDouble())
    resolution = capability_module.resolve_capability_provider(
        "image", None, read_config=False
    )
    assert resolution.provider is None
    assert resolution.error_type == "provider_ambiguous"


def test_profile_embedding_capability_exposes_vector_contract(monkeypatch):
    from providers.base import ProviderProfile

    profile = ProviderProfile(
        name="embed-double",
        display_name="Embed Double",
        env_vars=("EMBED_DOUBLE_API_KEY",),
        base_url="https://embed.example/v1",
        embedding_model="embed-x",
        embedding_path="embeddings",
        embedding_dimensions=(512, 1024),
    )
    monkeypatch.setenv("EMBED_DOUBLE_API_KEY", "secret")
    monkeypatch.setattr(
        "agent.profile_provider_credentials.resolve_profile_api_key",
        lambda p: __import__("os").environ.get(p.env_vars[0], "") if p.env_vars else "",
    )

    capability = ProfileEmbeddingCapability(profile)
    assert capability.kind == "vector"
    assert capability.name == "embed-double"
    assert capability.display_name == "Embed Double"
    assert capability.is_available() is True
    assert capability.list_models() == [CapabilityModel(id="embed-x", display="embed-x")]
    assert capability.default_model() == "embed-x"
    assert capability.get_setup_schema()["env_vars"] == [{"key": "EMBED_DOUBLE_API_KEY"}]
    assert capability.capabilities() == {"dimensions": [512, 1024]}

    monkeypatch.delenv("EMBED_DOUBLE_API_KEY")
    assert capability.is_available() is False


def test_ensure_bridges_voice_registries_and_profiles(monkeypatch):
    from providers.base import ProviderProfile

    tts_registry.register_provider(_TTSDouble())
    transcription_registry.register_provider(_ASRDouble())
    profile = ProviderProfile(
        name="embed-double",
        env_vars=(),
        embedding_model="embed-x",
        embedding_path="embeddings",
    )
    monkeypatch.setattr("providers.list_providers", lambda: [profile])

    capability_module.ensure_capability_providers()

    voice = capability_module.get_capability_provider("voice", "voice-double")
    assert sorted(m.id for m in voice.list_models()) == ["asr-x", "tts-x"]
    vector = capability_module.get_capability_provider("vector", "embed-double")
    assert vector.list_models() == [CapabilityModel(id="embed-x", display="embed-x")]

    # Idempotent: a second call does not duplicate providers.
    capability_module.ensure_capability_providers()
    assert len(capability_module.list_capability_providers("voice")) == 1


def test_capability_catalog_rows_are_credential_safe():
    capability_module.register_media_generation_provider("image", _ImageDouble())
    capability_module.ensure_capability_providers()

    rows = catalog_module.capability_catalog("image")
    assert rows == [{
        "provider": "image-double",
        "name": "Image-Double",
        "available": True,
        "credential_configured": True,
        "models": [{"id": "image-x", "display": "Image X"}],
        "default_model": "image-x",
        "capabilities": {"modalities": ["text", "image"]},
        "setup": {
            "name": "Image Double",
            "env_vars": [{"key": "IMAGE_DOUBLE_API_KEY", "prompt": "Key prompt"}],
        },
    }]
    assert "secret" not in repr(rows).lower()
    with pytest.raises(ValueError, match="kind must be"):
        catalog_module.capability_catalog("chat")


def test_capability_catalog_voice_rows_carry_capability_tags():
    capability_module.register_capability_provider(_LegacyVoiceAdapter("tts", _TTSDouble()))
    capability_module.register_capability_provider(_LegacyVoiceAdapter("asr", _ASRDouble()))

    rows = catalog_module.capability_catalog("voice")
    assert len(rows) == 1
    assert rows[0]["models"] == [
        {"id": "tts-x", "display": "TTS X", "capability": "tts"},
        {"id": "asr-x", "display": "ASR X", "capability": "asr"},
    ]
    assert rows[0]["available"] is True


def test_capability_model_catalog_shape_and_unknown_provider():
    capability_module.register_capability_provider(_LegacyVoiceAdapter("tts", _TTSDouble()))

    catalog, default_model = catalog_module.capability_model_catalog("voice", "voice-double")
    assert catalog == {"tts-x": {}}
    assert default_model == "tts-x"
    with pytest.raises(KeyError, match="Unknown voice capability provider"):
        catalog_module.capability_model_catalog("voice", "missing")
    with pytest.raises(ValueError, match="kind must be"):
        catalog_module.capability_model_catalog("chat", "voice-double")
