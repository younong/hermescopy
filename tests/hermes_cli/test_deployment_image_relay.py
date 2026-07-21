import base64
import os

import pytest

from hermes_cli.dashboard_auth.authority import AuthorityStore, WorkerGenerationState, WorkerLeaseState
from hermes_cli.deployment_image import DeploymentImagePolicy
from hermes_cli.owner_worker.image_relay import DeploymentImageBroker, DeploymentImageRelayError, OwnerImageRelayClient


def _policy():
    return DeploymentImagePolicy(
        runtime_resolver=lambda: {"api_key": "secret", "openai_base_url": "https://api.example/v1", "gemini_base_url": "https://api.example/v1beta"},
        image_generator=lambda **kwargs: {
            "image_bytes": b"generated",
            "mime_type": "image/png",
            "metadata": {
                "size": "1024x1024",
                "upstream_model": "gpt-image-2",
                "api_key_backup": "must-not-cross-relay",
                "x-api-key": "must-not-cross-relay",
            },
        },
        allowed_models=("gpt-image-2-medium",),
    )


def test_relay_requires_active_exact_lease_and_returns_bytes(tmp_path):
    store = AuthorityStore(tmp_path)
    claim = store.claim_worker_start("owner", worker_id="worker")
    broker = DeploymentImageBroker(policy=_policy(), authority_store=store)
    fd = broker.register(claim.lease)
    client = OwnerImageRelayClient(fd, _policy().descriptor())
    active = store.transition_worker_lease(claim.lease, state=WorkerLeaseState.ACTIVE, generation_state=WorkerGenerationState.ACTIVE)
    broker.activate(active)
    result = client.generate(prompt="draw", aspect_ratio="square", model=None, references=[])
    assert result["image_bytes"] == b"generated"
    assert result["provider"] == "apiyi"
    assert result["metadata"] == {
        "size": "1024x1024", "upstream_model": "gpt-image-2",
    }
    broker.revoke(active)
    with pytest.raises(DeploymentImageRelayError):
        client.generate(prompt="again", aspect_ratio="square", model=None, references=[])
    client.close()
    broker.close()


def test_relay_rejects_reference_over_limit(tmp_path):
    store = AuthorityStore(tmp_path)
    claim = store.claim_worker_start("owner", worker_id="worker")
    policy = _policy()
    broker = DeploymentImageBroker(policy=policy, authority_store=store)
    active = store.transition_worker_lease(claim.lease, state=WorkerLeaseState.ACTIVE, generation_state=WorkerGenerationState.ACTIVE)
    request = {
        "operation": "image_generate", "policy_id": policy.policy_id, "prompt": "draw",
        "aspect_ratio": "square", "model": policy.model,
        "references": [{"name": "x.png", "mime_type": "image/png", "data": base64.b64encode(b"x").decode()}] * 17,
    }
    with pytest.raises(DeploymentImageRelayError):
        broker._handle_request(active, request)
    broker.close()


def test_relay_rejects_stale_lease_after_durable_replacement(tmp_path):
    store = AuthorityStore(tmp_path)
    claim = store.claim_worker_start("owner", worker_id="worker")
    policy = _policy()
    broker = DeploymentImageBroker(policy=policy, authority_store=store)
    active = store.transition_worker_lease(
        claim.lease,
        state=WorkerLeaseState.ACTIVE,
        generation_state=WorkerGenerationState.ACTIVE,
    )
    request = {
        "operation": "image_generate",
        "policy_id": policy.policy_id,
        "prompt": "draw",
        "aspect_ratio": "square",
        "model": policy.model,
        "references": [],
    }
    store.transition_worker_lease(
        active,
        state=WorkerLeaseState.DRAINING,
        generation_state=WorkerGenerationState.DRAINING,
    )

    with pytest.raises(DeploymentImageRelayError, match="not active"):
        broker._handle_request(active, request)
    broker.close()
