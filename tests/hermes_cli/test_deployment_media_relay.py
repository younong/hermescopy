import base64
import io
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from PIL import Image

from agent.image_gen_provider import canonical_aspect_ratio
from agent.transcription_provider import TranscriptionProvider
from agent.tts_provider import TTSProvider
from hermes_cli.dashboard_auth.authority import (
    AuthorityStore,
    WorkerGenerationState,
    WorkerLeaseState,
)
import hermes_cli.controlled_roots as controlled_roots_module
import hermes_cli.deployment_media as deployment_media_module
from hermes_cli.authenticated_file_context import AuthenticatedWorkspaceContext
from hermes_cli.controlled_roots import controlled_roots_for
from hermes_cli.deployment_media import (
    DeploymentMediaPolicy,
    DeploymentMediaPolicyInvalid,
    DeploymentMediaRoute,
    DeploymentMediaRouteDescriptor,
)
from hermes_cli.model_plane import capability as capability_module
from hermes_cli.owner_runtime import ensure_owner_runtime_dirs, owner_worker_runtime_paths
from hermes_cli.owner_worker.media_dispatch import dispatch_deployment_media
from hermes_cli.owner_worker.entrypoint import _dispatch_deployment_media_only
from hermes_cli.owner_worker.media_relay import (
    DeploymentMediaBroker,
    DeploymentMediaRelayError,
    OwnerMediaRelayClient,
)


def _png_bytes(size=(300, 400)):
    output = io.BytesIO()
    Image.new("RGB", size).save(output, format="PNG")
    return output.getvalue()


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
    aspect = kwargs.get("aspect_ratio")
    dimensions = {
        "1:1": (300, 300), "square": (300, 300),
        "3:4": (300, 400), "portrait": (300, 400),
        "2:3": (300, 450), "4:3": (400, 300),
        "3:2": (450, 300), "16:9": (480, 270),
        "landscape": (480, 270), "9:16": (270, 480),
        "47:20": (470, 200),
    }[aspect]
    return {
        "image_bytes": _png_bytes(dimensions),
        "mime_type": "image/png",
        "metadata": {
            "upstream_model": "gpt-image-2",
            "requested_aspect_ratio": aspect,
            "effective_aspect_ratio": aspect,
            "requested_resolution": kwargs.get("params", {}).get("resolution"),
            "effective_resolution": kwargs.get("params", {}).get("resolution"),
            "resolution_mode": "native",
            "api_key_backup": "must-not-cross-relay",
            "x-api-key": "must-not-cross-relay",
        },
    }


@pytest.fixture(autouse=True)
def _executor(monkeypatch, request):
    monkeypatch.setenv("TEST_MEDIA_RELAY_KEY", "secret")
    if not request.node.name.startswith("test_custom_codex_real_path_"):
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
        "aspect_ratio": "3:4",
        "references": [],
        "params": {},
    }
    request.update(overrides)
    return request


class _OpenAIImageHandler(BaseHTTPRequestHandler):
    image_bytes = _png_bytes((1536, 2048))
    requests = []

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length))
        type(self).requests.append((self.path, body))
        payload = json.dumps({
            "data": [{
                "b64_json": base64.b64encode(type(self).image_bytes).decode()
            }]
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args):
        pass


@pytest.fixture
def _openai_image_server():
    _OpenAIImageHandler.requests = []
    _OpenAIImageHandler.image_bytes = _png_bytes((1536, 2048))
    server = ThreadingHTTPServer(("127.0.0.1", 0), _OpenAIImageHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", _OpenAIImageHandler
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def _custom_codex_policy(base_url):
    return DeploymentMediaPolicy(
        routes=(DeploymentMediaRoute(
            descriptor=DeploymentMediaRouteDescriptor(
                kind="image",
                provider="custom:codex",
                models=("gpt-image-2",),
                default_model="gpt-image-2",
                max_output_bytes=32 << 20,
            ),
            key_env="TEST_MEDIA_RELAY_KEY",
            executor=(
                "plugins.image_gen.openai_compatible:"
                "generate_openai_compatible_image_bytes"
            ),
            base_urls={"openai_base_url": base_url},
            executor_params={
                "edit_protocol": "json_images",
                "size_profile": "gpt-image-2",
            },
        ),),
        policy_id="media-policy-v1",
    )


def _dispatch_runtime(tmp_path, policy):
    owner = tmp_path / "owner"
    ensure_owner_runtime_dirs(owner)
    paths = owner_worker_runtime_paths(owner_home=owner, worker_generation=1)
    roots = controlled_roots_for(paths)
    store = AuthorityStore(tmp_path / "authority")
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
    return owner, paths, roots, policy.descriptor().routes[0], broker, client


def test_custom_codex_real_path_validates_then_publishes(
    tmp_path, monkeypatch, _openai_image_server
):
    monkeypatch.setenv("TEST_MEDIA_RELAY_KEY", "secret")
    monkeypatch.setattr(
        deployment_media_module,
        "_validate_https_url",
        lambda value, *, field: value,
    )
    monkeypatch.setattr(controlled_roots_module.sys, "platform", "linux")
    monkeypatch.setattr(controlled_roots_module, "_openat2", lambda *_args: None)
    base_url, handler = _openai_image_server
    policy = _custom_codex_policy(base_url)
    owner, paths, roots, descriptor, broker, client = _dispatch_runtime(
        tmp_path, policy
    )
    try:
        result = json.loads(dispatch_deployment_media(
            {
                "prompt": "draw a portrait",
                "aspect_ratio": "3:4",
                "resolution": "2K",
            },
            kind="image",
            model="gpt-image-2",
            relay_client=client,
            descriptor=descriptor,
            workspace_context=AuthenticatedWorkspaceContext(roots),
            owner_home=owner,
        ))
        output = Path(result["image"])
        assert result["image"].startswith("generated/images/")
        assert (paths.default_workspace / output).read_bytes() == handler.image_bytes
        assert str(owner) not in json.dumps(result)
        assert len(handler.requests) == 1
        request_path, request_payload = handler.requests[0]
        assert request_path == "/v1/images/generations"
        assert request_payload["model"] == "gpt-image-2"
        assert request_payload["size"] == "1536x2048"
        assert request_payload["n"] == 1
        assert request_payload["quality"] == "medium"
        assert request_payload["prompt"].startswith(
            "draw a portrait\n\nOutput requirements:"
        )
        assert "Aspect ratio: exactly 3:4" in request_payload["prompt"]
        assert "Preferred pixel dimensions: 1536x2048" in request_payload["prompt"]
        assert result["requested_aspect_ratio"] == "3:4"
        assert result["effective_aspect_ratio"] == "3:4"
        assert result["actual_aspect_ratio"] == "3:4"
        assert result["requested_resolution"] == "2K"
        assert result["effective_resolution"] == "2K"
        assert result["actual_resolution"] == "2K"
        assert result["resolution_mode"] == "native"
        assert result["actual_dimensions"] == {"width": 1536, "height": 2048}
    finally:
        client.close()
        broker.close()
        roots.close()


def test_custom_codex_real_path_accepts_non_native_matching_ratio(
    tmp_path, monkeypatch, _openai_image_server
):
    monkeypatch.setenv("TEST_MEDIA_RELAY_KEY", "secret")
    monkeypatch.setattr(
        deployment_media_module,
        "_validate_https_url",
        lambda value, *, field: value,
    )
    monkeypatch.setattr(controlled_roots_module.sys, "platform", "linux")
    monkeypatch.setattr(controlled_roots_module, "_openat2", lambda *_args: None)
    base_url, handler = _openai_image_server
    handler.image_bytes = _png_bytes((1086, 1448))
    policy = _custom_codex_policy(base_url)
    owner, paths, roots, descriptor, broker, client = _dispatch_runtime(
        tmp_path, policy
    )
    try:
        result = json.loads(dispatch_deployment_media(
            {
                "prompt": "draw a portrait",
                "aspect_ratio": "3:4",
                "resolution": "2K",
            },
            kind="image",
            model="gpt-image-2",
            relay_client=client,
            descriptor=descriptor,
            workspace_context=AuthenticatedWorkspaceContext(roots),
            owner_home=owner,
        ))
        output = Path(result["image"])
        assert result["image"].startswith("generated/images/")
        assert (paths.default_workspace / output).read_bytes() == handler.image_bytes
        assert str(owner) not in json.dumps(result)
        assert result["actual_dimensions"] == {"width": 1086, "height": 1448}
        assert result["actual_aspect_ratio"] == "3:4"
        assert result["actual_resolution"] is None
    finally:
        client.close()
        broker.close()
        roots.close()


def test_custom_codex_real_path_rejects_before_publication(
    tmp_path, monkeypatch, _openai_image_server
):
    monkeypatch.setenv("TEST_MEDIA_RELAY_KEY", "secret")
    monkeypatch.setattr(
        deployment_media_module,
        "_validate_https_url",
        lambda value, *, field: value,
    )
    monkeypatch.setattr(controlled_roots_module.sys, "platform", "linux")
    monkeypatch.setattr(controlled_roots_module, "_openat2", lambda *_args: None)
    base_url, handler = _openai_image_server
    handler.image_bytes = _png_bytes((1254, 1254))
    policy = _custom_codex_policy(base_url)
    owner, paths, roots, descriptor, broker, client = _dispatch_runtime(
        tmp_path, policy
    )
    try:
        with pytest.raises(DeploymentMediaRelayError, match="rejected"):
            dispatch_deployment_media(
                {
                    "prompt": "draw a portrait",
                    "aspect_ratio": "3:4",
                    "resolution": "2K",
                },
                kind="image",
                model="gpt-image-2",
                relay_client=client,
                descriptor=descriptor,
                workspace_context=AuthenticatedWorkspaceContext(roots),
                owner_home=owner,
            )
        output_dir = paths.default_workspace / "generated" / "images"
        assert not output_dir.exists() or not tuple(output_dir.iterdir())
    finally:
        client.close()
        broker.close()
        roots.close()


def test_relay_rejects_when_media_capacity_wait_expires(tmp_path, monkeypatch, caplog):
    store = AuthorityStore(tmp_path)
    claim = store.claim_worker_start("owner", worker_id="worker")
    policy = _policy()
    broker = DeploymentMediaBroker(
        policy=policy,
        authority_store=store,
        max_concurrent_requests=1,
        request_wait_timeout_seconds=0.01,
    )
    active = store.transition_worker_lease(
        claim.lease,
        state=WorkerLeaseState.ACTIVE,
        generation_state=WorkerGenerationState.ACTIVE,
    )
    started = threading.Event()
    release = threading.Event()

    def blocking_executor(**kwargs):
        started.set()
        assert release.wait(2)
        return _fake_executor(**kwargs)

    monkeypatch.setattr(DeploymentMediaRoute, "load_executor", lambda self: blocking_executor)
    first = threading.Thread(target=broker._handle_request, args=(active, _request(policy, prompt="first")))
    first.start()
    assert started.wait(1)
    with caplog.at_level(logging.WARNING, logger="hermes_cli.owner_worker.media_relay"):
        with pytest.raises(DeploymentMediaRelayError, match="capacity"):
            broker._handle_request(active, _request(policy, prompt="must-not-log"))
    assert "must-not-log" not in caplog.text
    release.set()
    first.join(timeout=2)
    assert not first.is_alive()
    broker.close()


def test_relay_waits_for_media_capacity_and_releases_slot(tmp_path, monkeypatch):
    store = AuthorityStore(tmp_path)
    claim = store.claim_worker_start("owner", worker_id="worker")
    policy = _policy()
    broker = DeploymentMediaBroker(
        policy=policy,
        authority_store=store,
        max_concurrent_requests=1,
        request_wait_timeout_seconds=1,
    )
    active = store.transition_worker_lease(
        claim.lease,
        state=WorkerLeaseState.ACTIVE,
        generation_state=WorkerGenerationState.ACTIVE,
    )
    started = threading.Event()
    release = threading.Event()

    def blocking_executor(**kwargs):
        started.set()
        assert release.wait(2)
        return _fake_executor(**kwargs)

    monkeypatch.setattr(DeploymentMediaRoute, "load_executor", lambda self: blocking_executor)
    first_result = []
    first = threading.Thread(
        target=lambda: first_result.append(broker._handle_request(active, _request(policy, prompt="first")))
    )
    first.start()
    assert started.wait(1)
    second_result = []
    second = threading.Thread(
        target=lambda: second_result.append(broker._handle_request(active, _request(policy, prompt="second")))
    )
    second.start()
    release.set()
    first.join(timeout=2)
    second.join(timeout=2)
    assert len(first_result) == 1
    assert len(second_result) == 1
    broker.close()


def test_relay_logs_safe_media_outcome(tmp_path, caplog):
    store = AuthorityStore(tmp_path)
    claim = store.claim_worker_start("owner", worker_id="worker")
    policy = _policy()
    broker = DeploymentMediaBroker(policy=policy, authority_store=store)
    active = store.transition_worker_lease(
        claim.lease,
        state=WorkerLeaseState.ACTIVE,
        generation_state=WorkerGenerationState.ACTIVE,
    )
    with caplog.at_level(logging.INFO, logger="hermes_cli.owner_worker.media_relay"):
        broker._handle_request(active, _request(policy, prompt="do-not-log", params={"resolution": "4K"}))
    assert "do-not-log" not in caplog.text
    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.outcome == "complete"
    assert record.operation == "image_generate"
    assert record.provider == "apiyi"
    assert record.model == "gpt-image-2-medium"
    broker.close()


def test_deployment_media_dispatch_fails_closed_without_route(tmp_path, monkeypatch):
    store = AuthorityStore(tmp_path)
    claim = store.claim_worker_start("owner", worker_id="worker")
    policy = _policy()
    broker = DeploymentMediaBroker(policy=policy, authority_store=store)
    fd = broker.register(claim.lease)
    client = OwnerMediaRelayClient(fd, policy.descriptor())
    monkeypatch.setattr(
        "hermes_cli.owner_worker.media_dispatch.active_media_selection",
        lambda _kind: ("missing-provider", "missing-model"),
    )
    try:
        with pytest.raises(DeploymentMediaRelayError, match="selection is unavailable"):
            _dispatch_deployment_media_only(
                "image_generate",
                {"prompt": "draw", "aspect_ratio": "square"},
                relay_client=client,
                workspace_context=None,
                owner_home=tmp_path,
            )
    finally:
        client.close()
        broker.close()


def test_authenticated_image_generation_smoke_mocks_only_provider(tmp_path, monkeypatch):
    """Exercise the authenticated relay path with a deterministic image provider."""
    store = AuthorityStore(tmp_path)
    claim = store.claim_worker_start("owner", worker_id="worker")
    image_route = _image_route()
    policy = DeploymentMediaPolicy(routes=(image_route,), policy_id="image-smoke-v1")
    provider_calls = []

    def image_provider(**kwargs):
        provider_calls.append(kwargs)
        return {
            "image_bytes": _png_bytes((300, 400)),
            "mime_type": "image/png",
            "metadata": {"upstream_model": "smoke-image-model"},
        }

    monkeypatch.setattr(
        DeploymentMediaRoute,
        "load_executor",
        lambda route: image_provider if route.descriptor.kind == "image" else pytest.fail(
            "smoke must not mock or invoke a non-image provider"
        ),
    )
    broker = DeploymentMediaBroker(policy=policy, authority_store=store)
    fd = broker.register(claim.lease)
    client = OwnerMediaRelayClient(fd, policy.descriptor())
    active = store.transition_worker_lease(
        claim.lease,
        state=WorkerLeaseState.ACTIVE,
        generation_state=WorkerGenerationState.ACTIVE,
    )
    broker.activate(active)
    try:
        result = client.execute(
            "image_generate",
            provider="apiyi",
            model="gpt-image-2-medium",
            prompt="smoke image",
            aspect_ratio="3:4",
            params={"resolution": "2K"},
        )
    finally:
        client.close()
        broker.close()

    assert result["image_bytes"] == _png_bytes((300, 400))
    assert result["mime_type"] == "image/png"
    assert result["provider"] == "apiyi"
    assert result["model"] == "gpt-image-2-medium"
    assert len(provider_calls) == 1
    assert provider_calls[0]["prompt"] == "smoke image"
    assert provider_calls[0]["model"] == "gpt-image-2-medium"


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
        aspect_ratio="3:4",
        references=[],
        params={"resolution": "4K"},
    )
    assert result["image_bytes"] == _png_bytes()
    assert result["provider"] == "apiyi"
    assert result["aspect_ratio"] == "3:4"
    assert result["metadata"] == {
        "upstream_model": "gpt-image-2",
        "requested_aspect_ratio": "3:4",
        "effective_aspect_ratio": "3:4",
        "requested_resolution": "4K",
        "effective_resolution": "4K",
        "resolution_mode": "native",
        "width": 300,
        "height": 400,
        "actual_dimensions": {"width": 300, "height": 400},
        "actual_aspect_ratio": "3:4",
        "actual_resolution": None,
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


@pytest.mark.parametrize(
    "aspect_ratio",
    ["16:9", "3:4", "2.35:1", "landscape", "square", "portrait"],
)
def test_relay_accepts_supported_image_aspect_ratios(tmp_path, aspect_ratio):
    store = AuthorityStore(tmp_path)
    claim = store.claim_worker_start("owner", worker_id="worker")
    policy = _policy()
    broker = DeploymentMediaBroker(policy=policy, authority_store=store)
    active = store.transition_worker_lease(
        claim.lease,
        state=WorkerLeaseState.ACTIVE,
        generation_state=WorkerGenerationState.ACTIVE,
    )
    result = broker._handle_request(
        active,
        _request(policy, aspect_ratio=aspect_ratio),
    )
    assert result["aspect_ratio"] == canonical_aspect_ratio(aspect_ratio)
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
    discovery_calls = []
    monkeypatch.setattr(
        "hermes_cli.plugins._ensure_plugins_discovered",
        lambda *a, **k: discovery_calls.append(1),
    )
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
    # The Control Plane embed path guarantees capability registration ran.
    assert discovery_calls
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
