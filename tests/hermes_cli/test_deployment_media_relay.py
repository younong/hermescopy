import base64

import pytest

from hermes_cli.dashboard_auth.authority import (
    AuthorityStore,
    WorkerGenerationState,
    WorkerLeaseState,
)
from hermes_cli.deployment_media import (
    DeploymentMediaPolicy,
    DeploymentMediaRoute,
    DeploymentMediaRouteDescriptor,
)
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
