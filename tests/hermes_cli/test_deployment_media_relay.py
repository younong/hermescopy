import base64

import pytest

from agent.transcription_provider import TranscriptionProvider
from agent.tts_provider import TTSProvider
from hermes_cli.dashboard_auth.authority import (
    AuthorityStore,
    WorkerGenerationState,
    WorkerLeaseState,
)
from hermes_cli.deployment_media import (
    DeploymentMediaPolicy,
    DeploymentMediaPolicyInvalid,
    DeploymentMediaRoute,
    DeploymentMediaRouteDescriptor,
)
from hermes_cli.model_plane import capability as capability_module
from hermes_cli.owner_worker.media_relay import (
    DeploymentMediaBroker,
    DeploymentMediaRelayError,
    OwnerMediaRelayClient,
)


def _image_route():
    return DeploymentMediaRoute(
        descriptor=DeploymentMediaRouteDescriptor(
            kind="image",
            provider="apiyi",
            models=("gpt-image-2-medium",),
            default_model="gpt-image-2-medium",
            max_reference_images=16,
            max_reference_bytes=1024,
            max_total_reference_bytes=4096,
            max_output_bytes=8192,
        ),
        key_env="TEST_MEDIA_RELAY_KEY",
        executor="plugins.image_gen.apiyi:generate_apiyi_image_bytes",
    )


def _video_route():
    return DeploymentMediaRoute(
        descriptor=DeploymentMediaRouteDescriptor(
            kind="video",
            provider="fal",
            models=("fal-video-1",),
            default_model="fal-video-1",
        ),
        key_env="TEST_MEDIA_RELAY_KEY",
        executor="plugins.video_gen.fal:generate_fal_video",
    )


def _policy():
    return DeploymentMediaPolicy(
        routes=(_image_route(), _video_route()),
        policy_id="media-policy-v1",
    )


def _fake_executor(**kwargs):
    if kwargs["model"].startswith("fal-video"):
        if kwargs.get("params", {}).get("return_bytes"):
            return {"video_bytes": b"mp4-bytes", "mime_type": "video/mp4"}
        return {"video_url": "https://cdn.example.com/video.mp4"}
    return {
        "image_bytes": b"generated",
        "mime_type": "image/png",
        "metadata": {
            "size": "1024x1024",
            "upstream_model": "gpt-image-2",
            "api_key_backup": "must-not-cross-relay",
            "x-api-key": "must-not-cross-relay",
        },
    }


@pytest.fixture(autouse=True)
def _executor(monkeypatch):
    monkeypatch.setenv("TEST_MEDIA_RELAY_KEY", "secret")
    monkeypatch.setattr(
        DeploymentMediaRoute, "load_executor", lambda self: _fake_executor
    )


def _request(policy, **overrides):
    request = {
        "operation": "image_generate",
        "policy_id": policy.policy_id,
        "provider": "apiyi",
        "model": "gpt-image-2-medium",
        "prompt": "draw",
        "aspect_ratio": "square",
        "references": [],
        "params": {},
    }
    request.update(overrides)
    return request


def test_relay_requires_active_exact_lease_and_returns_bytes(tmp_path):
    store = AuthorityStore(tmp_path)
    claim = store.claim_worker_start("owner", worker_id="worker")
    policy = _policy()
    broker = DeploymentMediaBroker(policy=policy, authority_store=store)
    fd = broker.register(claim.lease)
    client = OwnerMediaRelayClient(fd, policy.descriptor())
    active = store.transition_worker_lease(
        claim.lease,
        state=WorkerLeaseState.ACTIVE,
        generation_state=WorkerGenerationState.ACTIVE,
    )
    broker.activate(active)
    result = client.execute(
        "image_generate",
        provider="apiyi",
        model="gpt-image-2-medium",
        prompt="draw",
        aspect_ratio="square",
        references=[],
    )
    assert result["image_bytes"] == b"generated"
    assert result["provider"] == "apiyi"
    assert result["metadata"] == {
        "size": "1024x1024", "upstream_model": "gpt-image-2",
    }
    broker.revoke(active)
    with pytest.raises(DeploymentMediaRelayError):
        client.execute(
            "image_generate",
            provider="apiyi",
            model="gpt-image-2-medium",
            prompt="again",
            aspect_ratio="square",
        )
    client.close()
    broker.close()


def test_relay_routes_video_operation_by_kind(tmp_path):
    store = AuthorityStore(tmp_path)
    claim = store.claim_worker_start("owner", worker_id="worker")
    policy = _policy()
    broker = DeploymentMediaBroker(policy=policy, authority_store=store)
    fd = broker.register(claim.lease)
    client = OwnerMediaRelayClient(fd, policy.descriptor())
    active = store.transition_worker_lease(
        claim.lease,
        state=WorkerLeaseState.ACTIVE,
        generation_state=WorkerGenerationState.ACTIVE,
    )
    broker.activate(active)

    result = client.execute(
        "video_generate",
        provider="fal",
        model="fal-video-1",
        prompt="animate",
    )
    assert result["video_url"] == "https://cdn.example.com/video.mp4"

    result = client.execute(
        "video_generate",
        provider="fal",
        model="fal-video-1",
        prompt="animate",
        params={"return_bytes": True},
    )
    assert result["video_bytes"] == b"mp4-bytes"
    assert result["mime_type"] == "video/mp4"

    client.close()
    broker.close()


def test_relay_rejects_unrouted_selection(tmp_path):
    store = AuthorityStore(tmp_path)
    claim = store.claim_worker_start("owner", worker_id="worker")
    policy = _policy()
    broker = DeploymentMediaBroker(policy=policy, authority_store=store)
    fd = broker.register(claim.lease)
    client = OwnerMediaRelayClient(fd, policy.descriptor())
    with pytest.raises(DeploymentMediaRelayError, match="selection is not allowed"):
        client.execute(
            "image_generate",
            provider="openai",
            model="gpt-image-1",
            prompt="draw",
            aspect_ratio="square",
        )
    client.close()
    broker.close()


def test_relay_rejects_reference_over_limit(tmp_path):
    store = AuthorityStore(tmp_path)
    claim = store.claim_worker_start("owner", worker_id="worker")
    policy = _policy()
    broker = DeploymentMediaBroker(policy=policy, authority_store=store)
    active = store.transition_worker_lease(
        claim.lease,
        state=WorkerLeaseState.ACTIVE,
        generation_state=WorkerGenerationState.ACTIVE,
    )
    request = _request(
        policy,
        references=[
            {
                "name": "x.png",
                "mime_type": "image/png",
                "data": base64.b64encode(b"x").decode(),
            }
        ] * 17,
    )
    with pytest.raises(DeploymentMediaRelayError):
        broker._handle_request(active, request)
    broker.close()


def test_relay_rejects_wrong_policy_and_bad_params(tmp_path):
    store = AuthorityStore(tmp_path)
    claim = store.claim_worker_start("owner", worker_id="worker")
    policy = _policy()
    broker = DeploymentMediaBroker(policy=policy, authority_store=store)
    active = store.transition_worker_lease(
        claim.lease,
        state=WorkerLeaseState.ACTIVE,
        generation_state=WorkerGenerationState.ACTIVE,
    )
    with pytest.raises(DeploymentMediaRelayError, match="policy is invalid"):
        broker._handle_request(active, _request(policy, policy_id="other-policy"))
    with pytest.raises(DeploymentMediaRelayError, match="params are invalid"):
        broker._handle_request(
            active, _request(policy, params={"nested": {"not": "scalar"}})
        )
    with pytest.raises(DeploymentMediaRelayError, match="selection is invalid"):
        broker._handle_request(active, _request(policy, aspect_ratio="ultrawide"))
    broker.close()


def test_relay_rejects_stale_lease_after_durable_replacement(tmp_path):
    store = AuthorityStore(tmp_path)
    claim = store.claim_worker_start("owner", worker_id="worker")
    policy = _policy()
    broker = DeploymentMediaBroker(policy=policy, authority_store=store)
    active = store.transition_worker_lease(
        claim.lease,
        state=WorkerLeaseState.ACTIVE,
        generation_state=WorkerGenerationState.ACTIVE,
    )
    store.transition_worker_lease(
        active,
        state=WorkerLeaseState.DRAINING,
        generation_state=WorkerGenerationState.DRAINING,
    )

    with pytest.raises(DeploymentMediaRelayError, match="not active"):
        broker._handle_request(active, _request(policy))
    broker.close()


# ---------------------------------------------------------------------------
# Voice (tts_synthesize / transcribe) and vector (embed) operations
# ---------------------------------------------------------------------------


def _voice_route():
    return DeploymentMediaRoute(
        descriptor=DeploymentMediaRouteDescriptor(
            kind="voice",
            provider="voice-double",
            models=("tts-x", "asr-x"),
            default_model="tts-x",
        ),
        key_env="TEST_MEDIA_RELAY_KEY",
        executor="",
    )


def _vector_route():
    return DeploymentMediaRoute(
        descriptor=DeploymentMediaRouteDescriptor(
            kind="vector",
            provider="vector-double",
            models=("embed-x",),
            default_model="embed-x",
        ),
        key_env="TEST_MEDIA_RELAY_KEY",
        executor="",
    )


def _voice_vector_policy():
    return DeploymentMediaPolicy(
        routes=(_voice_route(), _vector_route()),
        policy_id="media-policy-v1",
    )


class _RelayTTSDouble(TTSProvider):
    last_call = None

    @property
    def name(self):
        return "voice-double"

    @property
    def display_name(self):
        return "Voice Double"

    def is_available(self):
        return True

    def list_models(self):
        return [{"id": "tts-x", "display": "TTS X"}]

    def synthesize(self, text, output_path, **kwargs):
        type(self).last_call = {"text": text, "kwargs": kwargs}
        with open(output_path, "wb") as handle:
            handle.write(b"mp3-audio")
        return output_path


class _RelayASRDouble(TranscriptionProvider):
    last_call = None

    @property
    def name(self):
        return "voice-double"

    def is_available(self):
        return True

    def list_models(self):
        return [{"id": "asr-x", "display": "ASR X"}]

    def transcribe(self, file_path, **kwargs):
        with open(file_path, "rb") as handle:
            data = handle.read()
        type(self).last_call = {"data": data, "kwargs": kwargs}
        return {"success": True, "transcript": "hello world", "provider": self.name}


class _EmbedDouble:
    def __init__(self):
        self.last_call = None

    def embed(self, **kwargs):
        self.last_call = kwargs
        return {
            "provider": "vector-double",
            "model": "embed-x",
            "embedding": [0.5, 1.5],
            "dimensions": 2,
            "usage": {},
        }


@pytest.fixture
def _voice_capabilities(monkeypatch):
    capability_module._reset_for_tests()
    monkeypatch.setattr(
        "hermes_cli.plugins._ensure_plugins_discovered", lambda *a, **k: None
    )
    yield capability_module
    capability_module._reset_for_tests()


def _active_broker(tmp_path, policy):
    store = AuthorityStore(tmp_path)
    claim = store.claim_worker_start("owner", worker_id="worker")
    broker = DeploymentMediaBroker(policy=policy, authority_store=store)
    fd = broker.register(claim.lease)
    client = OwnerMediaRelayClient(fd, policy.descriptor())
    active = store.transition_worker_lease(
        claim.lease,
        state=WorkerLeaseState.ACTIVE,
        generation_state=WorkerGenerationState.ACTIVE,
    )
    broker.activate(active)
    return broker, client


def test_relay_tts_synthesize_returns_audio_bytes(tmp_path, _voice_capabilities):
    _RelayTTSDouble.last_call = None
    capability_module.register_voice_provider("tts", _RelayTTSDouble())
    broker, client = _active_broker(tmp_path, _voice_vector_policy())

    result = client.execute(
        "tts_synthesize",
        provider="voice-double",
        model="tts-x",
        prompt="say hello",
        params={"voice": "voice-a", "speed": 1.25, "format": "mp3"},
    )

    assert result["audio_bytes"] == b"mp3-audio"
    assert result["mime_type"] == "audio/mpeg"
    assert result["provider"] == "voice-double"
    assert result["model"] == "tts-x"
    call = _RelayTTSDouble.last_call
    assert call["text"] == "say hello"
    assert call["kwargs"]["voice"] == "voice-a"
    assert call["kwargs"]["model"] == "tts-x"
    assert call["kwargs"]["speed"] == 1.25
    assert call["kwargs"]["format"] == "mp3"
    client.close()
    broker.close()


def test_relay_transcribe_round_trips_audio_reference(tmp_path, _voice_capabilities):
    _RelayASRDouble.last_call = None
    capability_module.register_voice_provider("asr", _RelayASRDouble())
    broker, client = _active_broker(tmp_path, _voice_vector_policy())

    result = client.execute(
        "transcribe",
        provider="voice-double",
        model="asr-x",
        prompt="",
        references=[{"name": "sample.wav", "mime_type": "audio/wav", "data": b"wav-bytes"}],
        params={"language": "zh"},
    )

    assert result["text"] == "hello world"
    assert result["provider"] == "voice-double"
    call = _RelayASRDouble.last_call
    assert call["data"] == b"wav-bytes"
    assert call["kwargs"]["model"] == "asr-x"
    assert call["kwargs"]["language"] == "zh"
    client.close()
    broker.close()


def test_relay_embed_returns_vector(tmp_path, _voice_capabilities, monkeypatch):
    embed_double = _EmbedDouble()
    monkeypatch.setattr(
        "hermes_cli.model_plane.capability.resolve_embedding_capability",
        lambda provider=None: embed_double,
    )
    broker, client = _active_broker(tmp_path, _voice_vector_policy())

    result = client.execute(
        "embed",
        provider="vector-double",
        model="embed-x",
        prompt="embed this",
        params={"dimensions": 1024},
    )

    assert result["embedding"] == [0.5, 1.5]
    assert result["dimensions"] == 2
    assert embed_double.last_call["text"] == "embed this"
    assert embed_double.last_call["dimensions"] == 1024
    client.close()
    broker.close()


def test_relay_voice_reference_shape_rules(tmp_path, _voice_capabilities):
    capability_module.register_voice_provider("tts", _RelayTTSDouble())
    capability_module.register_voice_provider("asr", _RelayASRDouble())
    store = AuthorityStore(tmp_path)
    claim = store.claim_worker_start("owner", worker_id="worker")
    policy = _voice_vector_policy()
    broker = DeploymentMediaBroker(policy=policy, authority_store=store)
    active = store.transition_worker_lease(
        claim.lease,
        state=WorkerLeaseState.ACTIVE,
        generation_state=WorkerGenerationState.ACTIVE,
    )
    audio_ref = {
        "name": "a.wav",
        "mime_type": "audio/wav",
        "data": base64.b64encode(b"x").decode(),
    }

    def voice_request(operation, **overrides):
        request = _request(
            policy,
            operation=operation,
            provider="voice-double",
            model="tts-x",
            prompt="hello",
            aspect_ratio="",
            references=[],
        )
        request.update(overrides)
        return request

    # tts_synthesize carries no input references.
    with pytest.raises(DeploymentMediaRelayError, match="references are invalid"):
        broker._handle_request(active, voice_request("tts_synthesize", references=[audio_ref]))
    # transcribe requires exactly one audio reference.
    with pytest.raises(DeploymentMediaRelayError, match="references are invalid"):
        broker._handle_request(
            active, voice_request("transcribe", model="asr-x", prompt="")
        )
    with pytest.raises(DeploymentMediaRelayError, match="references are invalid"):
        broker._handle_request(
            active,
            voice_request("transcribe", model="asr-x", prompt="", references=[audio_ref, audio_ref]),
        )
    # transcribe rejects non-audio reference types.
    with pytest.raises(DeploymentMediaRelayError, match="reference type is invalid"):
        broker._handle_request(
            active,
            voice_request(
                "transcribe",
                model="asr-x",
                prompt="",
                references=[{**audio_ref, "mime_type": "image/png"}],
            ),
        )
    # embed carries no references.
    with pytest.raises(DeploymentMediaRelayError, match="references are invalid"):
        broker._handle_request(
            active,
            voice_request("embed", provider="vector-double", model="embed-x", references=[audio_ref]),
        )
    # tts_synthesize requires a non-empty prompt.
    with pytest.raises(DeploymentMediaRelayError, match="prompt is invalid"):
        broker._handle_request(active, voice_request("tts_synthesize", prompt="  "))
    broker.close()


def test_relay_voice_requires_registered_delegate(tmp_path, _voice_capabilities):
    broker, client = _active_broker(tmp_path, _voice_vector_policy())
    with pytest.raises(DeploymentMediaRelayError, match="rejected"):
        client.execute(
            "tts_synthesize",
            provider="voice-double",
            model="tts-x",
            prompt="hello",
        )
    client.close()
    broker.close()


def test_voice_and_vector_routes_reject_executors():
    with pytest.raises(DeploymentMediaPolicyInvalid, match="executor is invalid"):
        DeploymentMediaRoute(
            descriptor=DeploymentMediaRouteDescriptor(
                kind="voice", provider="x", models=("m",), default_model="m"
            ),
            key_env="K",
            executor="module:attribute",
        )
    with pytest.raises(DeploymentMediaPolicyInvalid, match="executor is invalid"):
        DeploymentMediaRoute(
            descriptor=DeploymentMediaRouteDescriptor(
                kind="vector", provider="x", models=("m",), default_model="m"
            ),
            key_env="K",
            executor="",
            base_urls={"openai_base_url": "https://api.example.com/v1"},
        )
    # Image/video routes still require a well-formed executor.
    with pytest.raises(DeploymentMediaPolicyInvalid, match="executor is invalid"):
        DeploymentMediaRoute(
            descriptor=DeploymentMediaRouteDescriptor(
                kind="image", provider="x", models=("m",), default_model="m"
            ),
            key_env="K",
            executor="",
        )
