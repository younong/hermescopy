"""Contracts for model-provider-owned image and embedding capabilities."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hermes_cli.model_plane.capability import (
    ProfileEmbeddingCapability,
    ProfileEmbeddingError,
)
from agent.profile_image_gen_provider import ProfileImageGenProvider
from agent.profile_transcription_provider import ProfileTranscriptionProvider
from agent.profile_tts_provider import ProfileTTSProvider
from providers import get_provider_profile


PROVIDER = "volcengine-agent-plan"
BASE_URL = "https://ark.cn-beijing.volces.com/api/plan/v3"


@pytest.fixture(autouse=True)
def _environment(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("VOLCENGINE_AGENT_PLAN_API_KEY", "fake-plan-key")
    yield


def _response(body, status_code=200):
    response = MagicMock(status_code=status_code)
    response.json.return_value = body
    if status_code >= 400:
        from requests import HTTPError

        response.raise_for_status.side_effect = HTTPError(
            f"{status_code} fake-plan-key", response=response
        )
    return response


def test_profile_declares_media_capabilities():
    profile = get_provider_profile(PROVIDER)
    assert profile.image_generation_model == "doubao-seedream-5.0-lite"
    assert profile.image_generation_path == "/images/generations"
    assert profile.embedding_model == "doubao-embedding-vision"
    assert profile.embedding_path == "/embeddings/multimodal"
    assert profile.embedding_dimensions == (1024, 2048)
    assert profile.tts_model == "doubao-seed-tts-2.0"
    assert profile.tts_url == (
        "https://openspeech.bytedance.com/api/v3/plan/tts/unidirectional"
    )
    assert profile.tts_resource_id == "seed-tts-2.0"
    assert profile.transcription_model == "doubao-seed-asr-2.0"
    assert profile.transcription_url == (
        "wss://openspeech.bytedance.com/api/v3/plan/sauc/bigmodel_nostream"
    )
    assert profile.transcription_resource_id == "volc.seedasr.sauc.duration"


def test_image_provider_catalog_and_capabilities():
    provider = ProfileImageGenProvider(get_provider_profile(PROVIDER))
    assert provider.name == PROVIDER
    assert provider.default_model() == "doubao-seedream-5.0-lite"
    assert provider.capabilities() == {
        "modalities": ["text", "image"],
        "max_reference_images": 14,
    }
    assert provider.get_setup_schema()["env_vars"][0]["key"] == (
        "VOLCENGINE_AGENT_PLAN_API_KEY"
    )


def test_image_generation_uses_plan_endpoint_and_reference_field(tmp_path):
    provider = ProfileImageGenProvider(get_provider_profile(PROVIDER))
    response = _response({
        "data": [{
            "url": "https://example.test/generated.png",
            "size": "3072x2048",
            "output_format": "png",
        }]
    })
    saved = tmp_path / "images" / "generated.png"

    with (
        patch("agent.profile_image_gen_provider.requests.post", return_value=response) as post,
        patch("agent.profile_image_gen_provider.save_url_image", return_value=saved),
    ):
        result = provider.generate(
            "A mountain",
            aspect_ratio="landscape",
            image_url="https://example.test/reference.png",
        )

    assert result["success"] is True
    assert result["provider"] == PROVIDER
    assert result["model"] == "doubao-seedream-5.0-lite"
    assert result["modality"] == "image"
    assert result["image"] == str(saved)
    call = post.call_args
    assert call.args[0] == f"{BASE_URL}/images/generations"
    assert call.kwargs["headers"]["Authorization"] == "Bearer fake-plan-key"
    assert call.kwargs["json"] == {
        "model": "doubao-seedream-5.0-lite",
        "prompt": "A mountain",
        "size": "3072x2048",
        "response_format": "url",
        "output_format": "png",
        "watermark": False,
        "image": ["https://example.test/reference.png"],
    }


def test_image_error_redacts_key():
    provider = ProfileImageGenProvider(get_provider_profile(PROVIDER))
    with patch(
        "agent.profile_image_gen_provider.requests.post",
        side_effect=RuntimeError("request failed with fake-plan-key"),
    ):
        result = provider.generate("A mountain")

    assert result["success"] is False
    assert "fake-plan-key" not in result["error"]


def test_embedding_uses_multimodal_plan_schema():
    response = _response({
        "model": "doubao-embedding-vision-251215",
        "data": {"embedding": [0.1, 0.2, 0.3]},
        "usage": {"total_tokens": 9},
    })
    capability = ProfileEmbeddingCapability(get_provider_profile(PROVIDER))
    with patch("requests.post", return_value=response) as post:
        result = capability.embed(
            text="find this",
            image_url="https://example.test/image.png",
            dimensions=1024,
            instructions="Represent the retrieval query",
        )

    assert result == {
        "provider": PROVIDER,
        "model": "doubao-embedding-vision-251215",
        "embedding": [0.1, 0.2, 0.3],
        "dimensions": 3,
        "usage": {"total_tokens": 9},
    }
    call = post.call_args
    assert call.args[0] == f"{BASE_URL}/embeddings/multimodal"
    assert call.kwargs["json"] == {
        "model": "doubao-embedding-vision",
        "input": [
            {"type": "text", "text": "find this"},
            {
                "type": "image_url",
                "image_url": {"url": "https://example.test/image.png"},
            },
        ],
        "encoding_format": "float",
        "dimensions": 1024,
        "instructions": "Represent the retrieval query",
    }


def test_embedding_failure_does_not_expose_key():
    capability = ProfileEmbeddingCapability(get_provider_profile(PROVIDER))
    with patch(
        "requests.post",
        return_value=_response({"error": "fake-plan-key"}, status_code=401),
    ):
        with pytest.raises(ProfileEmbeddingError) as exc_info:
            capability.embed(text="test")

    assert "fake-plan-key" not in str(exc_info.value)


def test_embedding_requires_input_and_supported_dimensions():
    capability = ProfileEmbeddingCapability(get_provider_profile(PROVIDER))
    with pytest.raises(ValueError, match="At least one"):
        capability.embed()
    with pytest.raises(ValueError, match="1024, 2048"):
        capability.embed(text="test", dimensions=7)


def test_tts_uses_agent_plan_chunked_protocol(tmp_path):
    response = MagicMock()
    response.iter_lines.return_value = [
        b'{"code":0,"data":"YXVkaW8t"}',
        b'{"code":0,"data":"Y2h1bms="}',
        b'{"code":20000000,"message":"done"}',
    ]
    provider = ProfileTTSProvider(get_provider_profile(PROVIDER))
    output = tmp_path / "speech.mp3"

    with patch("agent.profile_tts_provider.requests.post", return_value=response) as post:
        result = provider.synthesize("test", str(output))

    assert result == str(output.resolve())
    assert output.read_bytes() == b"audio-chunk"
    call = post.call_args
    assert call.args[0] == (
        "https://openspeech.bytedance.com/api/v3/plan/tts/unidirectional"
    )
    assert call.kwargs["headers"]["X-Api-Key"] == "fake-plan-key"
    assert call.kwargs["headers"]["X-Api-Resource-Id"] == "seed-tts-2.0"
    assert call.kwargs["json"] == {
        "req_params": {
            "text": "test",
            "speaker": "zh_female_vv_uranus_bigtts",
            "audio_params": {"format": "mp3", "sample_rate": 24000},
        }
    }


def test_tts_failure_redacts_key_and_removes_partial_output(tmp_path):
    provider = ProfileTTSProvider(get_provider_profile(PROVIDER))
    output = tmp_path / "speech.mp3"
    with patch(
        "agent.profile_tts_provider.requests.post",
        side_effect=RuntimeError("failed with fake-plan-key"),
    ):
        with pytest.raises(RuntimeError) as exc_info:
            provider.synthesize("test", str(output))

    assert "fake-plan-key" not in str(exc_info.value)
    assert not output.exists()


def test_plan_has_no_video_provider():
    from hermes_cli.model_plane.capability import get_capability_provider

    assert get_capability_provider("video", PROVIDER) is None
