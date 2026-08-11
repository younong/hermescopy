from __future__ import annotations

import pytest

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
    capability_module._reset_for_tests()
    monkeypatch.setattr("hermes_cli.plugins._ensure_plugins_discovered", lambda *a, **k: None)
    yield
    capability_module._reset_for_tests()


def test_kind_contract_is_single_source():
    assert kinds.KINDS == ("chat", "code", "image", "video", "voice", "vector")
    assert kinds.CAPABILITY_KINDS == ("code", "image", "video", "voice", "vector")
    assert kinds.MEDIA_KINDS == ("image", "video", "voice", "vector")
    assert kinds.ACTIVATABLE_KINDS == kinds.CAPABILITY_KINDS
    assert kinds.GATEWAY_KINDS == ("image", "video")
    assert kinds.VOICE_CAPABILITIES == ("tts", "asr")
    assert kinds.FALLBACK_CAPABILITY_PROVIDERS == {"image": "fal"}
    assert kinds.selection_section("voice") == "voice_gen"
    assert kinds.selection_section("vector") == "vector_gen"
    assert kinds.selection_section("code") == "code_agent"
    with pytest.raises(ValueError):
        kinds.selection_section("chat")


def test_registry_merges_same_name_voice_delegates():
    capability_module.register_voice_provider("tts", _TTSDouble())
    capability_module.register_voice_provider("asr", _ASRDouble())

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


def test_code_capability_adapter_uses_shared_registry(monkeypatch):
    from providers.base import ProviderProfile

    profile = ProviderProfile(
        name="code-double",
        display_name="Code Double",
        fallback_models=("code-x",),
        code_models=("code-x",),
        env_vars=("CODE_DOUBLE_API_KEY",),
    )
    monkeypatch.setattr(
        "agent.profile_provider_credentials.resolve_profile_api_key",
        lambda _profile: "secret",
    )
    capability_module.register_code_provider(profile)

    provider = capability_module.get_capability_provider("code", "code-double")
    assert provider is not None
    assert provider.kind == "code"
    assert provider.list_models() == [CapabilityModel(id="code-x", display="code-x")]
    assert provider.supports_model("code-x") is True
    assert provider.supports_model("gpt-5.6-sol") is False
    assert provider.capabilities() == {
        "api_mode": "chat_completions",
        "profile": "coding",
        "toolset": "coding",
    }
    assert catalog_module.capability_catalog("code")[0]["provider"] == "code-double"
    assert capability_module.get_capability_provider("image", "code-double") is None


def test_chat_catalog_excludes_code_only_profiles_but_keeps_dual_surface(monkeypatch):
    from providers.base import ProviderProfile

    code_only = ProviderProfile(
        name="code-only",
        display_name="Code Only",
        chat_enabled=False,
    )
    dual_surface = ProviderProfile(
        name="dual-surface",
        display_name="Dual Surface",
        chat_enabled=True,
    )
    monkeypatch.setattr(
        "hermes_cli.model_plane.catalog._inventory_catalog",
        lambda: [
            {"slug": "code-only", "models": ["code-x"]},
            {"slug": "dual-surface", "models": ["chat-x"]},
        ],
    )
    monkeypatch.setattr(
        "providers.get_provider_profile",
        lambda name: {"code-only": code_only, "dual-surface": dual_surface}[name],
    )

    rows = catalog_module.chat_catalog()

    assert [row["slug"] for row in rows] == ["dual-surface"]
    assert rows[0]["models"] == ["chat-x"]


def test_dual_surface_code_catalog_filters_models(monkeypatch):
    from providers.base import ProviderProfile

    profile = ProviderProfile(
        name="dual-code",
        display_name="Dual Code",
        fallback_models=("gpt-5.6-sol", "code-x"),
        code_models=("code-x",),
        chat_enabled=True,
    )
    capability_module.register_code_provider(profile)
    rows = catalog_module.capability_catalog("code")
    assert rows[0]["models"] == [{"id": "code-x", "display": "code-x"}]


def test_dual_surface_chat_catalog_keeps_unowned_models(monkeypatch):
    from providers.base import ProviderProfile

    profile = ProviderProfile(
        name="dual-code",
        display_name="Dual Code",
        code_models=("code-x",),
        chat_enabled=True,
    )
    capability_module.register_code_provider(profile)
    monkeypatch.setattr(
        catalog_module,
        "_inventory_catalog",
        lambda: [{
            "slug": "dual-code",
            "name": "Dual Code",
            "models": ["gpt-5.6-sol", "code-x"],
        }],
    )
    monkeypatch.setattr(
        "providers.get_provider_profile",
        lambda name: profile if name == "dual-code" else None,
    )

    rows = catalog_module.chat_catalog()

    assert rows[0]["models"] == ["gpt-5.6-sol"]


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


def test_voice_registration_validates_abc_and_builtin_names(caplog):
    # Wrong ABC for the capability raises TypeError.
    with pytest.raises(TypeError, match="TTSProvider"):
        capability_module.register_voice_provider("tts", _ASRDouble())
    with pytest.raises(TypeError, match="TranscriptionProvider"):
        capability_module.register_voice_provider("asr", _TTSDouble())
    with pytest.raises(ValueError, match="voice capability"):
        capability_module.register_voice_provider("embed", _TTSDouble())

    # Built-in shadowing is a warning, not an exception — and the
    # registration is ignored (built-ins always win).
    class _EdgeDouble(_TTSDouble):
        @property
        def name(self) -> str:
            return "edge"

    with caplog.at_level("WARNING"):
        capability_module.register_voice_provider("tts", _EdgeDouble())
    assert capability_module.get_voice_delegate("edge", "tts") is None
    assert "shadows a built-in name" in caplog.text


def test_voice_delegate_dispatch_lookup_is_case_insensitive():
    capability_module.register_voice_provider("tts", _TTSDouble())
    capability_module.register_voice_provider("asr", _ASRDouble())

    tts = capability_module.get_voice_delegate(" Voice-Double ", "tts")
    assert tts is not None
    assert tts.name == "voice-double"
    # Execution attributes delegate to the wrapped plugin.
    assert tts.synthesize("hi", "/tmp/out.mp3") == "/tmp/out.mp3"
    assert capability_module.get_voice_delegate("voice-double", "asr") is not None
    assert capability_module.get_voice_delegate("voice-double", "embed") is None
    assert capability_module.get_voice_delegate("missing", "tts") is None


def test_ensure_capability_providers_triggers_discovery(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "hermes_cli.plugins._ensure_plugins_discovered",
        lambda *a, **k: calls.append((a, k)),
    )

    capability_module.ensure_capability_providers()
    capability_module.ensure_capability_providers()
    assert len(calls) == 2


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
    capability_module.register_voice_provider("tts", _TTSDouble())
    capability_module.register_voice_provider("asr", _ASRDouble())

    rows = catalog_module.capability_catalog("voice")
    assert len(rows) == 1
    assert rows[0]["models"] == [
        {"id": "tts-x", "display": "TTS X", "capability": "tts"},
        {"id": "asr-x", "display": "ASR X", "capability": "asr"},
    ]
    assert rows[0]["available"] is True


def test_capability_model_catalog_shape_and_unknown_provider():
    capability_module.register_voice_provider("tts", _TTSDouble())

    catalog, default_model = catalog_module.capability_model_catalog("voice", "voice-double")
    assert catalog == {"tts-x": {}}
    assert default_model == "tts-x"
    with pytest.raises(KeyError, match="Unknown voice capability provider"):
        catalog_module.capability_model_catalog("voice", "missing")
    with pytest.raises(ValueError, match="kind must be"):
        catalog_module.capability_model_catalog("chat", "voice-double")


def _patch_voice_gen_config(monkeypatch, section):
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"voice_gen": section} if section is not None else {},
    )


def test_resolve_voice_tool_selection_reads_voice_gen(monkeypatch):
    capability_module.register_voice_provider("tts", _TTSDouble())
    _patch_voice_gen_config(
        monkeypatch, {"provider": " Voice-Double ", "model": "tts-x"}
    )

    assert capability_module.resolve_voice_tool_selection("tts") == (
        "voice-double",
        "tts-x",
    )


def test_resolve_voice_tool_selection_matches_the_requested_capability(monkeypatch):
    capability_module.register_voice_provider("tts", _TTSDouble())
    capability_module.register_voice_provider("asr", _ASRDouble())
    # The activated model is the ASR one: TTS dispatch must not pick it
    # up, and ASR dispatch must.
    _patch_voice_gen_config(monkeypatch, {"provider": "voice-double", "model": "asr-x"})

    assert capability_module.resolve_voice_tool_selection("tts") is None
    assert capability_module.resolve_voice_tool_selection("asr") == (
        "voice-double",
        "asr-x",
    )


def test_resolve_voice_tool_selection_falls_back_without_usable_selection(monkeypatch):
    capability_module.register_voice_provider("tts", _TTSDouble())

    _patch_voice_gen_config(monkeypatch, None)
    assert capability_module.resolve_voice_tool_selection("tts") is None
    _patch_voice_gen_config(monkeypatch, {"provider": "", "model": "tts-x"})
    assert capability_module.resolve_voice_tool_selection("tts") is None
    _patch_voice_gen_config(monkeypatch, {"provider": "voice-double", "model": ""})
    assert capability_module.resolve_voice_tool_selection("tts") is None
    _patch_voice_gen_config(monkeypatch, {"provider": "missing", "model": "tts-x"})
    assert capability_module.resolve_voice_tool_selection("tts") is None
    _patch_voice_gen_config(
        monkeypatch, {"provider": "voice-double", "model": "unknown-model"}
    )
    assert capability_module.resolve_voice_tool_selection("tts") is None

    # An unreadable config also means "no selection" — never blocks the
    # legacy tool path.
    def _boom():
        raise RuntimeError("config unreadable")

    monkeypatch.setattr("hermes_cli.config.load_config", _boom)
    assert capability_module.resolve_voice_tool_selection("tts") is None

    with pytest.raises(ValueError, match="voice capability"):
        capability_module.resolve_voice_tool_selection("embed")
