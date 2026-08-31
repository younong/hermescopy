from __future__ import annotations

import base64
import io
import json

import pytest
from PIL import Image

from plugins.image_gen import openai_compatible


def _png_bytes(size=(1024, 1024)) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", size).save(output, format="PNG")
    return output.getvalue()


class _SseResponse:
    def __init__(self, lines, *, status_code=200):
        self._lines = lines
        self.status_code = status_code
        self.closed = False

    def iter_lines(self):
        return iter(self._lines)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def close(self):
        self.closed = True


def _request_kwargs(captured, response):
    def fake_post(url, **kwargs):
        captured.update(url=url, kwargs=kwargs)
        return response

    return fake_post


def test_codex_responses_executor_uses_tool_model_and_sse(monkeypatch):
    image = _png_bytes()
    event = {
        "item": {
            "type": "image_generation_call",
            "result": base64.b64encode(image).decode("ascii"),
        },
    }
    response = _SseResponse([
        "event: response.output_item.done",
        f"data: {json.dumps(event)}",
        "",
        "data: [DONE]",
        "",
    ])
    captured = {}
    monkeypatch.setattr(
        "requests.post", _request_kwargs(captured, response)
    )

    result = openai_compatible.generate_codex_responses_image_bytes(
        prompt="draw a square",
        aspect_ratio="1:1",
        model="gpt-image-2",
        references=[],
        api_key="trusted-secret",
        openai_base_url="https://codex.example/v1",
        chat_model="gpt-5.5",
        size_profile="gpt-image-2",
        params={"resolution": "1K"},
    )

    assert result["image_bytes"] == image
    assert result["mime_type"] == "image/png"
    assert result["metadata"]["upstream_model"] == "gpt-image-2"
    assert result["metadata"]["responses_model"] == "gpt-5.5"
    assert response.closed is True
    assert captured["url"] == "https://codex.example/v1/responses"
    payload = captured["kwargs"]["json"]
    assert captured["kwargs"]["headers"]["Authorization"] == "Bearer trusted-secret"
    assert payload["model"] == "gpt-5.5"
    assert payload["store"] is False
    assert payload["stream"] is True
    assert payload["tools"][0]["model"] == "gpt-image-2"
    assert payload["tools"][0]["size"] == "1024x1024"
    assert payload["tools"][0]["output_format"] == "png"
    assert payload["tool_choice"]["mode"] == "required"


def test_codex_responses_executor_sends_relay_reference_bytes(monkeypatch):
    output = _png_bytes()
    reference = _png_bytes((8, 8))
    event = {
        "type": "response.completed",
        "response": {
            "output": [{
                "type": "image_generation_call",
                "result": base64.b64encode(output).decode("ascii"),
            }],
        },
    }
    response = _SseResponse([f"data: {json.dumps(event)}", ""])
    captured = {}
    monkeypatch.setattr(
        "requests.post", _request_kwargs(captured, response)
    )

    result = openai_compatible.generate_codex_responses_image_bytes(
        prompt="edit the reference",
        aspect_ratio="1:1",
        model="gpt-image-2",
        references=[{
            "name": "reference.png",
            "mime_type": "image/png",
            "data": reference,
        }],
        api_key="trusted-secret",
        openai_base_url="https://codex.example/v1",
        chat_model="gpt-5.6-sol",
    )

    assert result["image_bytes"] == output
    content = captured["kwargs"]["json"]["input"][0]["content"]
    assert content[0]["type"] == "input_text"
    assert content[1]["type"] == "input_image"
    image_url = content[1]["image_url"]
    assert image_url.startswith("data:image/png;base64,")
    assert base64.b64decode(image_url.split(",", 1)[1]) == reference


def test_codex_responses_executor_requires_fixed_image_model():
    with pytest.raises(ValueError, match="must be gpt-image-2"):
        openai_compatible.generate_codex_responses_image_bytes(
            prompt="draw",
            aspect_ratio="1:1",
            model="gpt-image-2-medium",
            references=[],
            api_key="trusted-secret",
            openai_base_url="https://codex.example/v1",
            chat_model="gpt-5.5",
        )


def test_codex_responses_executor_rejects_empty_image_response(monkeypatch):
    response = _SseResponse(["data: {\"type\": \"response.completed\"}", ""])
    monkeypatch.setattr(
        "requests.post", lambda *args, **kwargs: response
    )

    with pytest.raises(openai_compatible.OpenAICompatibleImageEmpty):
        openai_compatible.generate_codex_responses_image_bytes(
            prompt="draw",
            aspect_ratio="1:1",
            model="gpt-image-2",
            references=[],
            api_key="trusted-secret",
            openai_base_url="https://codex.example/v1",
            chat_model="gpt-5.5",
        )
