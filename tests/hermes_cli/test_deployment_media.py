import io
import json

import pytest
from PIL import Image

from hermes_cli.deployment_media import (
    DEFAULT_POLICY_ID,
    POLICY_ID_ENV,
    ROUTES_ENV,
    DeploymentMediaDescriptor,
    DeploymentMediaPolicy,
    DeploymentMediaPolicyInvalid,
    DeploymentMediaRoute,
    DeploymentMediaRouteDescriptor,
    DeploymentMediaSelectionRejected,
    deployment_media_descriptor_from_environment,
    deployment_media_route_from_environment,
    policy_from_control_plane_environment,
)


def _png_bytes(size=(32, 32)):
    output = io.BytesIO()
    Image.new("RGB", size).save(output, format="PNG")
    return output.getvalue()


def _image_route_payload(**overrides):
    """Control-plane route declaration (secret-bearing fields included)."""
    payload = {
        "kind": "image",
        "provider": "apiyi",
        "models": ["gpt-image-2-medium", "nano-banana-2"],
        "default_model": "gpt-image-2-medium",
        "key_env": "APIYI_API_KEY",
        "executor": "plugins.image_gen.apiyi:generate_apiyi_image_bytes",
        "base_urls": {"openai_base_url": "https://api.example.com/v1"},
        "text_only_models": ["nano-banana-2"],
        "limits": {"max_reference_images": 8, "max_output_bytes": 8192},
    }
    payload.update(overrides)
    return payload


def _codex_image_route_payload(**overrides):
    payload = _image_route_payload(
        provider="custom:codex",
        models=["gpt-image-2"],
        default_model="gpt-image-2",
        key_env="CODEX_IMAGE_KEY",
        executor="plugins.image_gen.openai_compatible:generate_openai_compatible_image_bytes",
        base_urls={"openai_base_url": "https://codex.example.com/v1"},
        executor_params={"edit_protocol": "json_images", "size_profile": "gpt-image-2"},
        text_only_models=[],
    )
    payload.update(overrides)
    return payload


def _video_route_payload(**overrides):
    payload = {
        "kind": "video",
        "provider": "fal",
        "models": ["fal-video-1"],
        "default_model": "fal-video-1",
        "key_env": "FAL_KEY",
        "executor": "plugins.video_gen.fal:generate_fal_video",
    }
    payload.update(overrides)
    return payload


def _image_descriptor_payload(**overrides):
    """Worker-safe descriptor payload (no secret/executor fields)."""
    payload = {
        "kind": "image",
        "provider": "apiyi",
        "models": ["gpt-image-2-medium", "nano-banana-2"],
        "default_model": "gpt-image-2-medium",
        "text_only_models": ["nano-banana-2"],
        "max_reference_images": 8,
        "max_reference_bytes": 1024,
        "max_total_reference_bytes": 4096,
        "max_output_bytes": 8192,
    }
    payload.update(overrides)
    return payload


def _video_descriptor_payload(**overrides):
    payload = {
        "kind": "video",
        "provider": "fal",
        "models": ["fal-video-1"],
        "default_model": "fal-video-1",
        "text_only_models": [],
        "max_reference_images": 16,
        "max_reference_bytes": 1024,
        "max_total_reference_bytes": 4096,
        "max_output_bytes": 8192,
    }
    payload.update(overrides)
    return payload


def test_descriptor_round_trip_is_secret_free():
    descriptor = deployment_media_descriptor_from_environment({
        POLICY_ID_ENV: "policy-v1",
        ROUTES_ENV: json.dumps([_image_descriptor_payload(), _video_descriptor_payload()]),
    })
    assert descriptor is not None
    assert descriptor.policy_id == "policy-v1"
    assert [route.provider for route in descriptor.routes] == ["apiyi", "fal"]
    image = descriptor.routes[0]
    assert image.kind == "image"
    assert image.models == ("gpt-image-2-medium", "nano-banana-2")
    assert image.max_reference_images == 8
    assert image.max_output_bytes == 8192
    assert image.text_only_models == ("nano-banana-2",)
    round_trip = DeploymentMediaDescriptor.from_payload(descriptor.payload())
    assert round_trip == descriptor
    assert "APIYI_API_KEY" not in json.dumps(descriptor.payload())
    assert "executor" not in json.dumps(descriptor.payload())


def test_descriptor_requires_complete_environment():
    with pytest.raises(DeploymentMediaPolicyInvalid):
        deployment_media_descriptor_from_environment({POLICY_ID_ENV: "policy-v1"})
    with pytest.raises(DeploymentMediaPolicyInvalid):
        deployment_media_descriptor_from_environment(
            {ROUTES_ENV: json.dumps([_image_descriptor_payload()])}
        )
    assert deployment_media_descriptor_from_environment({}) is None


def test_route_descriptor_validation():
    with pytest.raises(DeploymentMediaPolicyInvalid):
        DeploymentMediaRouteDescriptor(
            kind="chat", provider="x", models=("m",), default_model="m"
        )
    for relay_kind in ("image", "video", "voice", "vector"):
        route = DeploymentMediaRouteDescriptor(
            kind=relay_kind, provider="x", models=("m",), default_model="m"
        )
        assert route.kind == relay_kind
    with pytest.raises(DeploymentMediaPolicyInvalid):
        DeploymentMediaRouteDescriptor(
            kind="image", provider="x", models=("m",), default_model="other"
        )
    with pytest.raises(DeploymentMediaPolicyInvalid):
        DeploymentMediaRouteDescriptor(
            kind="image", provider="x", models=("m",), default_model="m",
            max_output_bytes=0,
        )


def test_descriptor_rejects_duplicate_route_identity():
    route = DeploymentMediaRouteDescriptor(
        kind="image", provider="apiyi", models=("m",), default_model="m"
    )
    with pytest.raises(DeploymentMediaPolicyInvalid):
        DeploymentMediaDescriptor(policy_id="p", routes=(route, route))


def test_route_for_matches_selection_and_defaults():
    descriptor = deployment_media_descriptor_from_environment({
        POLICY_ID_ENV: "policy-v1",
        ROUTES_ENV: json.dumps([_image_descriptor_payload(), _video_descriptor_payload()]),
    })
    # Explicit selection matches only its (kind, provider, model).
    route = descriptor.route_for("image", "apiyi", "nano-banana-2")
    assert route is not None and route.provider == "apiyi"
    assert descriptor.route_for("image", "apiyi", "unknown-model") is None
    assert descriptor.route_for("image", "openai", "gpt-image-2-medium") is None
    assert descriptor.route_for("video", "fal", "fal-video-1") is not None
    # Empty provider matches the first route of the kind (worker default).
    assert descriptor.route_for("image", "", "").provider == "apiyi"
    # Kind mismatch never matches.
    assert descriptor.route_for("voice", "", "") is None


def test_route_declaration_validation():
    with pytest.raises(DeploymentMediaPolicyInvalid):
        DeploymentMediaRoute(
            descriptor=DeploymentMediaRouteDescriptor(
                kind="image", provider="x", models=("m",), default_model="m"
            ),
            key_env="",
            executor="mod:func",
        )
    with pytest.raises(DeploymentMediaPolicyInvalid):
        DeploymentMediaRoute(
            descriptor=DeploymentMediaRouteDescriptor(
                kind="image", provider="x", models=("m",), default_model="m"
            ),
            key_env="SOME_KEY",
            executor="no-separator",
        )
    with pytest.raises(DeploymentMediaPolicyInvalid):
        DeploymentMediaRoute(
            descriptor=DeploymentMediaRouteDescriptor(
                kind="image", provider="x", models=("m",), default_model="m"
            ),
            key_env="SOME_KEY",
            executor="mod:func",
            base_urls={"openai_base_url": "http://insecure.example.com"},
        )


def test_control_plane_policy_accepts_custom_codex_image_route(monkeypatch):
    monkeypatch.setenv(ROUTES_ENV, json.dumps([_codex_image_route_payload()]))
    monkeypatch.setenv(POLICY_ID_ENV, "policy-codex-image")
    monkeypatch.setenv("CODEX_IMAGE_KEY", "secret-value")

    policy = policy_from_control_plane_environment()

    assert policy is not None
    route = policy.route_for("image", "custom:codex", "gpt-image-2")
    assert route is not None
    assert route.executor == (
        "plugins.image_gen.openai_compatible:generate_openai_compatible_image_bytes"
    )
    descriptor = policy.descriptor()
    payload = json.dumps(descriptor.payload())
    assert "custom:codex" in payload
    assert "gpt-image-2" in payload
    assert "CODEX_IMAGE_KEY" not in payload
    assert "generate_openai_image_bytes" not in payload


def test_control_plane_policy_auto_activates_apiyi_route(monkeypatch):
    monkeypatch.delenv(ROUTES_ENV, raising=False)
    monkeypatch.delenv(POLICY_ID_ENV, raising=False)
    monkeypatch.setenv("APIYI_API_KEY", "secret-value")
    policy = policy_from_control_plane_environment()
    assert policy is not None
    assert policy.policy_id == DEFAULT_POLICY_ID
    assert len(policy.routes) == 1
    route = policy.routes[0]
    assert route.descriptor.kind == "image"
    assert route.descriptor.provider == "apiyi"
    assert set(route.descriptor.models) == {
        "gpt-image-2-low", "gpt-image-2-medium", "gpt-image-2-high", "nano-banana-2",
    }
    assert route.descriptor.default_model == "gpt-image-2-medium"
    assert route.descriptor.text_only_models == ("nano-banana-2",)
    assert route.key_env == "APIYI_API_KEY"
    assert route.executor == "plugins.image_gen.apiyi:generate_apiyi_image_bytes"
    assert "secret-value" not in repr(policy.descriptor())


def test_control_plane_policy_absent_without_routes_or_key(monkeypatch):
    monkeypatch.delenv(ROUTES_ENV, raising=False)
    monkeypatch.delenv("APIYI_API_KEY", raising=False)
    assert policy_from_control_plane_environment() is None


def test_control_plane_policy_explicit_routes(monkeypatch):
    monkeypatch.setenv(
        ROUTES_ENV, json.dumps([_image_route_payload(), _video_route_payload()])
    )
    monkeypatch.setenv(POLICY_ID_ENV, "policy-v2")
    policy = policy_from_control_plane_environment()
    assert policy is not None
    assert policy.policy_id == "policy-v2"
    assert [route.descriptor.kind for route in policy.routes] == ["image", "video"]


def _voice_route_payload(**overrides):
    """Voice route declaration: identity, models, credential — no executor."""
    payload = {
        "kind": "voice",
        "provider": "volcengine-agent-plan",
        "models": ["doubao-seed-tts-2.0", "doubao-seed-asr-2.0"],
        "default_model": "doubao-seed-tts-2.0",
        "key_env": "VOLCENGINE_AGENT_PLAN_API_KEY",
    }
    payload.update(overrides)
    return payload


def _vector_route_payload(**overrides):
    payload = {
        "kind": "vector",
        "provider": "volcengine-agent-plan",
        "models": ["doubao-embedding-vision"],
        "default_model": "doubao-embedding-vision",
        "key_env": "VOLCENGINE_AGENT_PLAN_API_KEY",
    }
    payload.update(overrides)
    return payload


def test_control_plane_policy_voice_and_vector_routes(monkeypatch):
    monkeypatch.setenv(
        ROUTES_ENV, json.dumps([_voice_route_payload(), _vector_route_payload()])
    )
    monkeypatch.setenv(POLICY_ID_ENV, "policy-v3")
    policy = policy_from_control_plane_environment()
    assert policy is not None
    assert [route.descriptor.kind for route in policy.routes] == ["voice", "vector"]
    voice = policy.routes[0]
    assert voice.executor == ""
    assert voice.descriptor.models == ("doubao-seed-tts-2.0", "doubao-seed-asr-2.0")
    # The declaration reduces to the worker-safe descriptor (executor and
    # key_env never cross into the worker payload).
    descriptor = policy.descriptor()
    assert [route.kind for route in descriptor.routes] == ["voice", "vector"]
    round_trip = DeploymentMediaDescriptor.from_payload(descriptor.payload())
    assert round_trip == descriptor
    assert "key_env" not in json.dumps(descriptor.payload())


def test_voice_and_vector_declarations_reject_executor_fields(monkeypatch):
    monkeypatch.setenv(
        ROUTES_ENV,
        json.dumps([_voice_route_payload(executor="module:attribute")]),
    )
    with pytest.raises(DeploymentMediaPolicyInvalid):
        policy_from_control_plane_environment()
    monkeypatch.setenv(
        ROUTES_ENV,
        json.dumps([_vector_route_payload(executor_params={"quality": "high"})]),
    )
    with pytest.raises(DeploymentMediaPolicyInvalid):
        policy_from_control_plane_environment()


def test_policy_execute_image_normalizes_bounded_response(monkeypatch):
    route = DeploymentMediaRoute(
        descriptor=DeploymentMediaRouteDescriptor(
            kind="image", provider="apiyi",
            models=("gpt-image-2-medium",), default_model="gpt-image-2-medium",
            max_output_bytes=8192,
        ),
        key_env="TEST_MEDIA_KEY",
        executor="plugins.image_gen.apiyi:generate_apiyi_image_bytes",
        base_urls={"openai_base_url": "https://api.example.com/v1"},
        executor_params={"quality": "high"},
    )
    policy = DeploymentMediaPolicy(routes=(route,), policy_id="p")
    monkeypatch.setenv("TEST_MEDIA_KEY", "secret")
    captured = {}

    def fake_executor(**kwargs):
        captured.update(kwargs)
        return {
            "image_bytes": _png_bytes(),
            "mime_type": "image/png",
            "metadata": {"effective_aspect_ratio": "1:1"},
        }

    monkeypatch.setattr(DeploymentMediaRoute, "load_executor", lambda self: fake_executor)
    result = policy.execute(
        "image_generate",
        provider="apiyi",
        model="gpt-image-2-medium",
        prompt="draw",
        aspect_ratio="square",
    )
    assert result["image_bytes"] == _png_bytes()
    assert result["mime_type"] == "image/png"
    assert result["provider"] == "apiyi"
    assert result["modality"] == "text"
    assert result["metadata"] == {
        "effective_aspect_ratio": "1:1",
        "width": 32,
        "height": 32,
        "actual_dimensions": {"width": 32, "height": 32},
        "actual_aspect_ratio": "1:1",
        "actual_resolution": None,
    }
    assert captured["api_key"] == "secret"
    assert captured["openai_base_url"] == "https://api.example.com/v1"
    assert captured["quality"] == "high"
    assert captured["params"] == {}


def test_policy_execute_custom_codex_image_uses_shared_executor(monkeypatch):
    route = DeploymentMediaRoute(
        descriptor=DeploymentMediaRouteDescriptor(
            kind="image", provider="custom:codex",
            models=("gpt-image-2",), default_model="gpt-image-2",
        ),
        key_env="CODEX_IMAGE_KEY",
        executor="plugins.image_gen.openai_compatible:generate_openai_compatible_image_bytes",
        base_urls={"openai_base_url": "https://codex.example.com/v1"},
        executor_params={"size_profile": "gpt-image-2"},
    )
    policy = DeploymentMediaPolicy(routes=(route,), policy_id="p")
    monkeypatch.setenv("CODEX_IMAGE_KEY", "secret")
    captured = {}

    from agent.image_size import GPT_IMAGE_2_SIZE_PROFILE, resolve_image_size

    def fake_executor(**kwargs):
        captured.update(kwargs)
        return {
            "image_bytes": _png_bytes((1024, 1024)),
            "mime_type": "image/png",
            "metadata": {
                "size": "forged",
                "effective_aspect_ratio": "16:9",
                "effective_resolution": "4K",
            },
            "size_plan": resolve_image_size(
                "1:1", "1K", profile=GPT_IMAGE_2_SIZE_PROFILE
            ),
        }

    monkeypatch.setattr(DeploymentMediaRoute, "load_executor", lambda self: fake_executor)
    result = policy.execute(
        "image_generate", provider="custom:codex", model="gpt-image-2", prompt="draw"
    )

    assert result["metadata"]["actual_dimensions"] == {
        "width": 1024, "height": 1024,
    }
    assert result["metadata"]["actual_resolution"] == "1K"
    assert result["metadata"]["size"] == "1024x1024"
    assert result["metadata"]["effective_aspect_ratio"] == "1:1"
    assert result["metadata"]["effective_resolution"] == "1K"
    assert captured["model"] == "gpt-image-2"
    assert captured["openai_base_url"] == "https://codex.example.com/v1"
    assert captured["api_key"] == "secret"


def test_policy_execute_video_accepts_url_or_bytes(monkeypatch):
    route = DeploymentMediaRoute(
        descriptor=DeploymentMediaRouteDescriptor(
            kind="video", provider="fal",
            models=("fal-video-1",), default_model="fal-video-1",
        ),
        key_env="TEST_MEDIA_KEY",
        executor="plugins.video_gen.fal:generate_fal_video",
    )
    policy = DeploymentMediaPolicy(routes=(route,), policy_id="p")
    monkeypatch.setenv("TEST_MEDIA_KEY", "secret")

    monkeypatch.setattr(
        DeploymentMediaRoute,
        "load_executor",
        lambda self: lambda **kwargs: {"video_url": "https://cdn.example.com/v.mp4"},
    )
    result = policy.execute(
        "video_generate", provider="fal", model="fal-video-1", prompt="animate"
    )
    assert result["video_url"] == "https://cdn.example.com/v.mp4"

    monkeypatch.setattr(
        DeploymentMediaRoute,
        "load_executor",
        lambda self: lambda **kwargs: {"video_bytes": b"mp4", "mime_type": "video/mp4"},
    )
    result = policy.execute(
        "video_generate", provider="fal", model="fal-video-1", prompt="animate"
    )
    assert result["video_bytes"] == b"mp4"
    assert result["mime_type"] == "video/mp4"


def test_policy_execute_rejects_out_of_policy_requests(monkeypatch):
    route = DeploymentMediaRoute(
        descriptor=DeploymentMediaRouteDescriptor(
            kind="image", provider="apiyi",
            models=("nano-banana-2",), default_model="nano-banana-2",
            text_only_models=("nano-banana-2",),
        ),
        key_env="TEST_MEDIA_KEY",
        executor="plugins.image_gen.apiyi:generate_apiyi_image_bytes",
    )
    policy = DeploymentMediaPolicy(routes=(route,), policy_id="p")
    monkeypatch.setenv("TEST_MEDIA_KEY", "secret")

    with pytest.raises(DeploymentMediaSelectionRejected):
        policy.execute("chat_completion", provider="apiyi", model="nano-banana-2", prompt="x")
    with pytest.raises(DeploymentMediaSelectionRejected):
        policy.execute(
            "image_generate", provider="other", model="nano-banana-2", prompt="x"
        )
    with pytest.raises(DeploymentMediaSelectionRejected):
        policy.execute(
            "image_generate",
            provider="apiyi",
            model="nano-banana-2",
            prompt="x",
            references=({"name": "a.png", "mime_type": "image/png", "data": b"x"},),
        )


def test_policy_execute_rejects_valid_image_with_wrong_dimensions(monkeypatch):
    route = DeploymentMediaRoute(
        descriptor=DeploymentMediaRouteDescriptor(
            kind="image", provider="custom:codex",
            models=("gpt-image-2",), default_model="gpt-image-2",
            max_output_bytes=8192,
        ),
        key_env="TEST_MEDIA_KEY",
        executor="plugins.image_gen.openai_compatible:generate_openai_compatible_image_bytes",
        base_urls={"openai_base_url": "https://codex.example.com/v1"},
        executor_params={"size_profile": "gpt-image-2"},
    )
    policy = DeploymentMediaPolicy(routes=(route,), policy_id="p")
    monkeypatch.setenv("TEST_MEDIA_KEY", "secret")
    from agent.image_size import GPT_IMAGE_2_SIZE_PROFILE, resolve_image_size

    monkeypatch.setattr(
        DeploymentMediaRoute,
        "load_executor",
        lambda self: lambda **kwargs: {
            "image_bytes": _png_bytes((32, 32)),
            "mime_type": "image/png",
            "metadata": {
                "size": "1536x2048",
                "effective_aspect_ratio": "3:4",
                "effective_resolution": "2K",
            },
            "size_plan": resolve_image_size(
                "3:4", "2K", profile=GPT_IMAGE_2_SIZE_PROFILE
            ),
        },
    )

    with pytest.raises(DeploymentMediaPolicyInvalid):
        policy.execute(
            "image_generate",
            provider="custom:codex",
            model="gpt-image-2",
            prompt="portrait",
            aspect_ratio="3:4",
            params={"resolution": "2K"},
        )


def test_policy_execute_rejects_invalid_executor_response(monkeypatch):
    route = DeploymentMediaRoute(
        descriptor=DeploymentMediaRouteDescriptor(
            kind="image", provider="apiyi",
            models=("m",), default_model="m", max_output_bytes=4,
        ),
        key_env="TEST_MEDIA_KEY",
        executor="plugins.image_gen.apiyi:generate_apiyi_image_bytes",
    )
    policy = DeploymentMediaPolicy(routes=(route,), policy_id="p")
    monkeypatch.setenv("TEST_MEDIA_KEY", "secret")

    monkeypatch.setattr(
        DeploymentMediaRoute,
        "load_executor",
        lambda self: lambda **kwargs: {"image_bytes": b"too-large", "mime_type": "image/png"},
    )
    with pytest.raises(DeploymentMediaPolicyInvalid):
        policy.execute("image_generate", provider="apiyi", model="m", prompt="x")

    monkeypatch.setattr(
        DeploymentMediaRoute,
        "load_executor",
        lambda self: lambda **kwargs: {"image_bytes": b"gif", "mime_type": "image/gif"},
    )
    with pytest.raises(DeploymentMediaPolicyInvalid):
        policy.execute("image_generate", provider="apiyi", model="m", prompt="x")


def test_route_from_environment_mirrors_worker_routing(monkeypatch):
    worker_env = {
        "HERMES_OWNER_KEY": "owner-key",
        POLICY_ID_ENV: "policy-v1",
        ROUTES_ENV: json.dumps([_image_descriptor_payload(), _video_descriptor_payload()]),
    }
    # Unconfigured users match the first route of the kind.
    route = deployment_media_route_from_environment("image", source=worker_env)
    assert route is not None and route.provider == "apiyi"
    # Explicit selection matching a declared route.
    route = deployment_media_route_from_environment(
        "video", provider="fal", model="fal-video-1", source=worker_env
    )
    assert route is not None and route.provider == "fal"
    # Explicit selection outside the deployment falls back to local plugins.
    assert (
        deployment_media_route_from_environment(
            "image", provider="openai", model="gpt-image-1", source=worker_env
        )
        is None
    )
    # Outside an owner worker there is no deployment route.
    assert deployment_media_route_from_environment("image", source={}) is None
