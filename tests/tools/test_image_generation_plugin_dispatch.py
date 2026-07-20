from __future__ import annotations

import json
import pytest

from agent import image_gen_registry
from agent.image_gen_provider import ImageGenProvider


@pytest.fixture(autouse=True)
def _reset_registry():
    image_gen_registry._reset_for_tests()
    yield
    image_gen_registry._reset_for_tests()


class _FakeCodexProvider(ImageGenProvider):
    @property
    def name(self) -> str:
        return "codex"

    def generate(self, prompt, aspect_ratio="landscape", **kwargs):
        return {
            "success": True,
            "image": "/tmp/codex-test.png",
            "model": "gpt-5.2-codex",
            "prompt": prompt,
            "aspect_ratio": aspect_ratio,
            "provider": "codex",
        }


class TestPluginDispatch:
    def test_dispatch_routes_to_codex_provider(self, monkeypatch, tmp_path):
        from tools import image_generation_tool
        from agent import image_gen_registry as registry_module
        from hermes_cli import plugins as plugins_module

        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        (tmp_path / "config.yaml").write_text("image_gen:\n  provider: codex\n")
        image_gen_registry.register_provider(_FakeCodexProvider())

        monkeypatch.setattr(plugins_module, "_ensure_plugins_discovered", lambda: None)

        dispatched = image_generation_tool._dispatch_to_plugin_provider("draw cat", "square")
        payload = json.loads(dispatched)

        assert payload["success"] is True
        assert payload["provider"] == "codex"
        assert payload["image"] == "/tmp/codex-test.png"
        assert payload["aspect_ratio"] == "square"

    def test_dispatch_reports_missing_registered_provider(self, monkeypatch, tmp_path):
        from tools import image_generation_tool
        from hermes_cli import plugins as plugins_module

        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        (tmp_path / "config.yaml").write_text("image_gen:\n  provider: missing-codex\n")

        monkeypatch.setattr(plugins_module, "_ensure_plugins_discovered", lambda: None)

        dispatched = image_generation_tool._dispatch_to_plugin_provider("draw cat", "landscape")
        payload = json.loads(dispatched)

        assert payload["success"] is False
        assert payload["error_type"] == "provider_not_registered"
        assert "image_gen.provider='missing-codex'" in payload["error"]

    def test_unset_provider_autoresolves_only_available_plugin(self, monkeypatch, tmp_path):
        from tools import image_generation_tool
        from hermes_cli import plugins as plugins_module

        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        image_gen_registry.register_provider(_FakeCodexProvider())
        monkeypatch.setattr(plugins_module, "_ensure_plugins_discovered", lambda force=False: None)

        assert image_generation_tool.check_image_generation_requirements() is True
        info = image_generation_tool._active_image_capabilities()
        assert info["provider"] == "Codex"

        payload = json.loads(
            image_generation_tool._dispatch_to_plugin_provider("draw hammy", "portrait")
        )
        assert payload["success"] is True
        assert payload["provider"] == "codex"
        assert payload["aspect_ratio"] == "portrait"

    def test_unset_provider_autoresolves_apiyi_consistently(self, monkeypatch, tmp_path):
        from plugins.image_gen.apiyi import ApiyiImageGenProvider
        from tools import image_generation_tool
        from hermes_cli import plugins as plugins_module

        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setenv("APIYI_API_KEY", "test-key")
        provider = ApiyiImageGenProvider()
        provider.generate = lambda prompt, aspect_ratio="landscape", **kwargs: {
            "success": True,
            "image": "/tmp/apiyi-test.png",
            "model": provider.default_model(),
            "prompt": prompt,
            "aspect_ratio": aspect_ratio,
            "provider": "apiyi",
        }
        image_gen_registry.register_provider(provider)
        monkeypatch.setattr(
            plugins_module, "_ensure_plugins_discovered", lambda force=False: None
        )

        assert image_generation_tool.check_image_generation_requirements() is True
        info = image_generation_tool._active_image_capabilities()
        assert info["provider"] == "APIYI"
        assert info["model"] == "gpt-image-2-medium"
        assert info["modalities"] == ["text", "image"]

        payload = json.loads(
            image_generation_tool._dispatch_to_plugin_provider(
                "draw hammy", "square", image_url="/tmp/reference.png"
            )
        )
        assert payload["success"] is True
        assert payload["provider"] == "apiyi"

    def test_explicit_unavailable_provider_does_not_fall_back(self, monkeypatch, tmp_path):
        from tools import image_generation_tool
        from hermes_cli import plugins as plugins_module

        class OfflineProvider(_FakeCodexProvider):
            def is_available(self):
                return False

        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        (tmp_path / "config.yaml").write_text("image_gen:\n  provider: codex\n")
        image_gen_registry.register_provider(OfflineProvider())
        monkeypatch.setattr(plugins_module, "_ensure_plugins_discovered", lambda force=False: None)

        assert image_generation_tool.check_image_generation_requirements() is False
        payload = json.loads(
            image_generation_tool._dispatch_to_plugin_provider("draw hammy", "portrait")
        )
        assert payload["success"] is False
        assert payload["error_type"] == "provider_unavailable"
