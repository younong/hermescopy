"""Tests for the APIYI image_gen plugin."""

from __future__ import annotations

import base64
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

import plugins.image_gen.apiyi as apiyi_plugin


# 1×1 transparent PNG — valid bytes for save_b64_image().
_PNG_HEX = (
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000d49444154789c6300010000000500010d0a2db40000000049454e44"
    "ae426082"
)


def _b64_png() -> str:
    return base64.b64encode(bytes.fromhex(_PNG_HEX)).decode()


class _Response:
    def __init__(self, payload, *, status_code=200, text=""):
        self._payload = payload
        self.status_code = status_code
        self.text = text
        self.headers = {"Content-Type": "image/png"}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            exc = requests.HTTPError(f"HTTP {self.status_code}")
            exc.response = self
            raise exc


@pytest.fixture(autouse=True)
def _tmp_hermes_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    yield tmp_path


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setenv("APIYI_API_KEY", "test-key")
    return apiyi_plugin.ApiyiImageGenProvider()


class TestMetadata:
    def test_name_and_default(self, provider):
        assert provider.name == "apiyi"
        assert provider.display_name == "APIYI"
        assert provider.default_model() == "gpt-image-2-medium"

    def test_list_models(self, provider):
        ids = [m["id"] for m in provider.list_models()]
        assert ids == [
            "gpt-image-2-low",
            "gpt-image-2-medium",
            "gpt-image-2-high",
            "nano-banana-2",
        ]

    def test_setup_schema_points_to_env_var(self, provider):
        schema = provider.get_setup_schema()
        assert schema["name"] == "APIYI"
        assert schema["env_vars"][0]["key"] == "APIYI_API_KEY"

    def test_register(self):
        ctx = SimpleNamespace(registered=[])
        ctx.register_image_gen_provider = ctx.registered.append

        apiyi_plugin.register(ctx)

        assert len(ctx.registered) == 1
        assert ctx.registered[0].name == "apiyi"


class TestAvailability:
    def test_missing_key_unavailable(self, monkeypatch):
        monkeypatch.delenv("APIYI_API_KEY", raising=False)
        assert apiyi_plugin.ApiyiImageGenProvider().is_available() is False

    def test_key_available(self, provider):
        assert provider.is_available() is True


class TestModelResolution:
    def test_default_model(self):
        model_id, meta = apiyi_plugin._resolve_model()
        assert model_id == "gpt-image-2-medium"
        assert meta["quality"] == "medium"

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("APIYI_IMAGE_MODEL", "nano-banana-2")
        model_id, _ = apiyi_plugin._resolve_model()
        assert model_id == "nano-banana-2"

    def test_config_model_map(self, tmp_path):
        import yaml

        (tmp_path / "config.yaml").write_text(
            yaml.safe_dump(
                {
                    "image_gen": {
                        "apiyi": {
                            "model_map": {
                                "nano-banana-2": "gemini-3.1-flash-image",
                            }
                        }
                    }
                }
            )
        )

        assert apiyi_plugin._upstream_model("nano-banana-2") == "gemini-3.1-flash-image"


class TestGenerate:
    def test_missing_api_key(self, monkeypatch):
        monkeypatch.delenv("APIYI_API_KEY", raising=False)
        result = apiyi_plugin.ApiyiImageGenProvider().generate("a cat")
        assert result["success"] is False
        assert result["error_type"] == "auth_required"
        assert "APIYI_API_KEY" in result["error"]

    def test_gpt_text_to_image_payload_and_save(self, provider, monkeypatch, tmp_path):
        calls = []

        def fake_post(url, **kwargs):
            calls.append((url, kwargs))
            return _Response({"data": [{"b64_json": _b64_png(), "revised_prompt": "cat"}]})

        monkeypatch.setattr(requests, "post", fake_post)

        result = provider.generate("a cat", aspect_ratio="landscape")

        assert result["success"] is True
        assert result["provider"] == "apiyi"
        assert result["model"] == "gpt-image-2-medium"
        assert result["quality"] == "medium"
        assert result["upstream_model"] == "gpt-image-2"
        assert result["requested_aspect_ratio"] == "16:9"
        assert result["effective_aspect_ratio"] == "16:9"
        assert result["requested_resolution"] == "2K"
        assert result["effective_resolution"] == "2K"
        saved = Path(result["image"])
        assert saved.exists()
        assert saved.parent == tmp_path / "cache" / "images"

        url, kwargs = calls[0]
        assert url == "https://api.apiyi.com/v1/images/generations"
        assert kwargs["headers"]["Authorization"] == "Bearer test-key"
        assert kwargs["json"] == {
            "model": "gpt-image-2",
            "prompt": "a cat",
            "size": "2048x1152",
            "n": 1,
            "quality": "medium",
        }

    def test_gpt_3_4_uses_requested_resolution_size(self, provider, monkeypatch):
        calls = []

        def fake_post(url, **kwargs):
            calls.append((url, kwargs))
            return _Response({"data": [{"b64_json": _b64_png()}]})

        monkeypatch.setattr(requests, "post", fake_post)

        result = provider.generate(
            "a portrait cat", aspect_ratio="3:4", resolution="4K"
        )

        assert result["success"] is True
        assert result["requested_aspect_ratio"] == "3:4"
        assert result["effective_aspect_ratio"] == "3:4"
        assert result["requested_resolution"] == "4K"
        assert result["effective_resolution"] == "4K"
        assert result["resolution_mode"] == "native"
        assert result["size"] == "2480x3312"
        assert calls[0][1]["json"]["size"] == "2480x3312"
        assert calls[0][1]["json"]["quality"] == "medium"

    def test_gpt_edit_3_4_uses_requested_resolution_size(self, provider, monkeypatch):
        calls = []
        data_url = f"data:image/png;base64,{_b64_png()}"

        def fake_post(url, **kwargs):
            calls.append((url, kwargs))
            return _Response({"data": [{"b64_json": _b64_png()}]})

        monkeypatch.setattr(requests, "post", fake_post)

        result = provider.generate(
            "make it portrait",
            image_url=data_url,
            aspect_ratio="3:4",
            resolution="2K",
        )

        assert result["success"] is True
        assert result["requested_aspect_ratio"] == "3:4"
        assert result["effective_aspect_ratio"] == "3:4"
        assert result["requested_resolution"] == "2K"
        assert result["effective_resolution"] == "2K"
        assert result["size"] == "1536x2048"
        assert calls[0][1]["data"]["size"] == "1536x2048"
        assert calls[0][1]["data"]["quality"] == "medium"

    def test_gpt_edit_payload(self, provider, monkeypatch):
        calls = []
        data_url = f"data:image/png;base64,{_b64_png()}"

        def fake_post(url, **kwargs):
            calls.append((url, kwargs))
            return _Response({"data": [{"b64_json": _b64_png()}]})

        monkeypatch.setattr(requests, "post", fake_post)

        result = provider.generate("make it rainy", image_url=data_url, aspect_ratio="square")

        assert result["success"] is True
        assert result["modality"] == "image"
        url, kwargs = calls[0]
        assert url == "https://api.apiyi.com/v1/images/edits"
        assert kwargs["data"]["model"] == "gpt-image-2"
        assert kwargs["data"]["quality"] == "medium"
        assert kwargs["data"]["size"] == "2048x2048"
        assert kwargs["files"][0][0] == "image"
        assert kwargs["files"][0][1][2] == "image/png"

    def test_nano_banana_payload_and_save(self, provider, monkeypatch):
        calls = []

        def fake_post(url, **kwargs):
            calls.append((url, kwargs))
            return _Response(
                {
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {
                                        "inlineData": {
                                            "mimeType": "image/png",
                                            "data": _b64_png(),
                                        }
                                    }
                                ]
                            }
                        }
                    ]
                }
            )

        monkeypatch.setenv("APIYI_IMAGE_MODEL", "nano-banana-2")
        monkeypatch.setattr(requests, "post", fake_post)

        result = provider.generate(
            "a banana astronaut", aspect_ratio="portrait", resolution="4K"
        )

        assert result["success"] is True
        assert result["model"] == "nano-banana-2"
        assert result["upstream_model"] == "gemini-3.1-flash-image-preview"
        assert result["aspect_ratio"] == "3:4"
        assert result["requested_aspect_ratio"] == "3:4"
        assert result["effective_aspect_ratio"] == "3:4"
        assert result["aspect_ratio_native"] == "3:4"
        assert result["requested_resolution"] == "4K"
        assert result["effective_resolution"] == "4K"
        assert result["resolution_mode"] == "native"

        url, kwargs = calls[0]
        assert url == (
            "https://api.apiyi.com/v1beta/models/"
            "gemini-3.1-flash-image-preview:generateContent"
        )
        assert kwargs["headers"]["Authorization"] == "Bearer test-key"
        payload = kwargs["json"]
        assert payload["contents"][0]["parts"][0] == {"text": "a banana astronaut"}
        assert payload["generationConfig"]["responseModalities"] == ["IMAGE", "TEXT"]
        assert payload["generationConfig"]["imageConfig"] == {
            "aspectRatio": "3:4",
            "imageSize": "4K",
        }

    def test_custom_base_urls_and_model_map(self, provider, monkeypatch, tmp_path):
        import yaml

        (tmp_path / "config.yaml").write_text(
            yaml.safe_dump(
                {
                    "image_gen": {
                        "apiyi": {
                            "openai_base_url": "https://example.test/openai",
                            "gemini_base_url": "https://example.test/gemini",
                            "model_map": {
                                "gpt-image-2-high": "custom-gpt-image",
                                "nano-banana-2": "custom-nano",
                            },
                        }
                    }
                }
            )
        )
        calls = []

        def fake_post(url, **kwargs):
            calls.append((url, kwargs))
            if "generateContent" in url:
                return _Response(
                    {
                        "candidates": [
                            {"content": {"parts": [{"inlineData": {"data": _b64_png()}}]}}
                        ]
                    }
                )
            return _Response({"data": [{"b64_json": _b64_png()}]})

        monkeypatch.setattr(requests, "post", fake_post)
        provider.generate("high cat", model="gpt-image-2-high")
        monkeypatch.setenv("APIYI_IMAGE_MODEL", "nano-banana-2")
        provider.generate("banana")

        assert calls[0][0] == "https://example.test/openai/images/generations"
        assert calls[0][1]["json"]["model"] == "custom-gpt-image"
        assert calls[1][0] == "https://example.test/gemini/models/custom-nano:generateContent"

    def test_empty_upstream_response(self, provider, monkeypatch):
        monkeypatch.setattr(requests, "post", lambda *a, **kw: _Response({"data": []}))
        result = provider.generate("a cat")
        assert result["success"] is False
        assert result["error_type"] == "empty_response"

    def test_http_error(self, provider, monkeypatch):
        monkeypatch.setattr(
            requests,
            "post",
            lambda *a, **kw: _Response(
                {"error": {"message": "bad model"}}, status_code=400, text="bad model"
            ),
        )
        result = provider.generate("a cat")
        assert result["success"] is False
        assert result["error_type"] == "api_error"
        assert "bad model" in result["error"]


def test_shared_openai_executor_forwards_exact_deployment_model(monkeypatch):
    from plugins.image_gen import apiyi

    captured = {}

    class Response:
        def raise_for_status(self):
            pass
        def json(self):
            return {"data": [{"b64_json": "cG5n", "revised_prompt": "done"}]}

    def fake_post(url, **kwargs):
        captured.update({"url": url, "kwargs": kwargs})
        return Response()

    monkeypatch.setattr("requests.post", fake_post)
    result = apiyi.generate_openai_image_bytes(
        prompt="draw", aspect_ratio="square", model="gpt-image-2",
        references=[], api_key="trusted",
        openai_base_url="https://codex.example/v1",
        params={"resolution": "1K"},
    )

    assert result["image_bytes"] == b"png"
    assert result["mime_type"] == "image/png"
    assert result["metadata"]["upstream_model"] == "gpt-image-2"
    assert captured["url"] == "https://codex.example/v1/images/generations"
    assert captured["kwargs"]["json"]["model"] == "gpt-image-2"


def test_shared_openai_executor_supports_json_image_edits(monkeypatch):
    from plugins.image_gen import apiyi

    captured = {}

    class Response:
        def raise_for_status(self):
            pass
        def json(self):
            return {"data": [{"b64_json": "cG5n"}]}

    def fake_post(url, **kwargs):
        captured.update({"url": url, "kwargs": kwargs})
        return Response()

    monkeypatch.setattr("requests.post", fake_post)
    result = apiyi.generate_openai_image_bytes(
        prompt="edit", aspect_ratio="square", model="gpt-image-2",
        references=[{"name": "a.png", "mime_type": "image/png", "data": b"png"}],
        api_key="trusted", openai_base_url="https://codex.example/v1",
        edit_protocol="json_images",
    )

    assert result["image_bytes"] == b"png"
    assert captured["kwargs"]["json"]["model"] == "gpt-image-2"
    assert captured["kwargs"]["json"]["images"][0]["image_url"].startswith(
        "data:image/png;base64,"
    )


@pytest.mark.parametrize(
    ("model", "aspect_ratio", "resolution", "size", "quality"),
    [
        ("gpt-image-2-medium", "9:16", "1K", "720x1280", "medium"),
        ("gpt-image-2-low", "16:9", "2K", "2048x1152", "low"),
        ("gpt-image-2-high", "3:4", "4K", "2480x3312", "high"),
    ],
)
def test_explicit_transport_forwards_gpt_resolution(
    monkeypatch, model, aspect_ratio, resolution, size, quality
):
    from plugins.image_gen import apiyi

    captured = {}

    class Response:
        headers = {}
        def raise_for_status(self): pass
        def json(self): return {"data": [{"b64_json": "cG5n"}]}

    def fake_post(url, **kwargs):
        captured.update({"url": url, "kwargs": kwargs})
        return Response()

    monkeypatch.setattr("requests.post", fake_post)
    result = apiyi.generate_apiyi_image_bytes(
        prompt="draw", aspect_ratio=aspect_ratio, model=model, references=[],
        api_key="trusted", openai_base_url="https://api.example/v1",
        gemini_base_url="https://api.example/v1beta",
        params={"resolution": resolution},
    )

    assert result["image_bytes"] == b"png"
    assert result["metadata"]["requested_aspect_ratio"] == aspect_ratio
    assert result["metadata"]["effective_aspect_ratio"] == aspect_ratio
    assert result["metadata"]["requested_resolution"] == resolution
    assert result["metadata"]["effective_resolution"] == resolution
    assert result["metadata"]["resolution_mode"] == "native"
    assert result["metadata"]["size"] == size
    assert captured["kwargs"]["json"]["size"] == size
    assert captured["kwargs"]["json"]["quality"] == quality
    assert "APIYI_API_KEY" not in os.environ


def test_explicit_transport_forwards_nano_resolution(monkeypatch):
    from plugins.image_gen import apiyi

    captured = {}

    class Response:
        def raise_for_status(self): pass
        def json(self):
            return {
                "candidates": [{
                    "content": {"parts": [{
                        "inlineData": {"mimeType": "image/png", "data": "cG5n"}
                    }]}
                }]
            }

    def fake_post(url, **kwargs):
        captured.update({"url": url, "kwargs": kwargs})
        return Response()

    monkeypatch.setattr("requests.post", fake_post)
    result = apiyi.generate_apiyi_image_bytes(
        prompt="draw", aspect_ratio="3:4", model="nano-banana-2", references=[],
        api_key="trusted", openai_base_url="https://api.example/v1",
        gemini_base_url="https://api.example/v1beta",
        params={"resolution": "4K"},
    )

    image_config = captured["kwargs"]["json"]["generationConfig"]["imageConfig"]
    assert image_config == {"aspectRatio": "3:4", "imageSize": "4K"}
    assert result["metadata"]["requested_resolution"] == "4K"
    assert result["metadata"]["effective_resolution"] == "4K"
    assert result["metadata"]["resolution_mode"] == "native"
