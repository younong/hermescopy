"""Real-path media discovery for the Volcengine Agent Plan profile."""

from __future__ import annotations


def test_model_provider_media_capabilities_register_with_catalogs(
    tmp_path, monkeypatch
):
    import hermes_cli.plugins as plugin_mod
    from hermes_cli.model_plane import capability as capability_module

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("VOLCENGINE_AGENT_PLAN_API_KEY", "fake-plan-key")
    capability_module._reset_for_tests()
    plugin_mod._plugin_manager = None
    plugin_mod._provider_media_registered = False

    plugin_mod._ensure_plugins_discovered()

    image = capability_module.get_capability_provider("image", "volcengine-agent-plan")
    assert image is not None
    assert image.is_available() is True
    assert image.default_model() == "doubao-seedream-5.0-lite"

    tts = capability_module.get_voice_delegate("volcengine-agent-plan", "tts")
    assert tts is not None
    assert tts.is_available() is True
    assert tts.default_model() == "doubao-seed-tts-2.0"
    assert tts.default_voice() == "zh_female_vv_uranus_bigtts"

    transcription = capability_module.get_voice_delegate("volcengine-agent-plan", "asr")
    assert transcription is not None
    assert transcription.is_available() is True
    assert transcription.default_model() == "doubao-seed-asr-2.0"

    embedding = capability_module.get_capability_provider("vector", "volcengine-agent-plan")
    assert embedding is not None
    assert embedding.is_available() is True
    assert embedding.default_model() == "doubao-embedding-vision"

    capability_module._reset_for_tests()
    plugin_mod._plugin_manager = None
    plugin_mod._provider_media_registered = False
