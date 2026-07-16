from __future__ import annotations

import socket
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from hermes_cli.dashboard_auth.authority import AuthorityStore, WorkerGenerationState, WorkerLeaseState
from hermes_cli.deployment_inference import (
    DeploymentInferencePolicy,
    DeploymentInferencePolicyInvalid,
    deployment_descriptor_from_environment,
    policy_from_control_plane_environment,
)
from hermes_cli.owner_worker.inference_relay import (
    DeploymentInferenceBroker,
    DeploymentInferenceRelayError,
    OwnerInferenceRelay,
)


def _policy(*, api_mode: str = "chat_completions") -> DeploymentInferencePolicy:
    return DeploymentInferencePolicy(
        provider="custom:deployment",
        model="gpt-safe",
        api_mode=api_mode,
        policy_id="test-deployment-v1",
        allowed_models=("gpt-safe", "gpt-safe-mini"),
        runtime_resolver=lambda: {
            "provider": "custom:deployment",
            "api_mode": api_mode,
            "base_url": "https://provider.example.test/v1",
            "api_key": "control-plane-secret",
        },
    )


def test_policy_descriptor_and_worker_environment_are_secret_free(monkeypatch):
    descriptor = _policy().descriptor()
    monkeypatch.setenv("HERMES_DEPLOYMENT_INFERENCE_PROVIDER", descriptor.provider)
    monkeypatch.setenv("HERMES_DEPLOYMENT_INFERENCE_MODEL", descriptor.model)
    monkeypatch.setenv("HERMES_DEPLOYMENT_INFERENCE_API_MODE", descriptor.api_mode)
    monkeypatch.setenv("HERMES_DEPLOYMENT_INFERENCE_POLICY_ID", descriptor.policy_id)
    monkeypatch.setenv("HERMES_DEPLOYMENT_INFERENCE_ALLOWED_MODELS", ",".join(descriptor.allowed_models))

    parsed = deployment_descriptor_from_environment()

    assert parsed == descriptor
    assert "secret" not in repr(parsed).lower()
    assert "base_url" not in parsed.__dict__


def test_descriptor_rejects_incomplete_or_unsupported_worker_environment(monkeypatch):
    monkeypatch.setenv("HERMES_DEPLOYMENT_INFERENCE_PROVIDER", "custom:deployment")
    with pytest.raises(DeploymentInferencePolicyInvalid, match="incomplete"):
        deployment_descriptor_from_environment()

    monkeypatch.setenv("HERMES_DEPLOYMENT_INFERENCE_MODEL", "gpt-safe")
    monkeypatch.setenv("HERMES_DEPLOYMENT_INFERENCE_API_MODE", "bedrock_converse")
    monkeypatch.setenv("HERMES_DEPLOYMENT_INFERENCE_POLICY_ID", "test")
    monkeypatch.setenv("HERMES_DEPLOYMENT_INFERENCE_ALLOWED_MODELS", "gpt-safe")
    with pytest.raises(DeploymentInferencePolicyInvalid, match="unsupported"):
        deployment_descriptor_from_environment()


def test_control_plane_environment_factory_does_not_resolve_until_broker_use(monkeypatch):
    monkeypatch.setenv("HERMES_DEPLOYMENT_INFERENCE_PROVIDER", "custom:deployment")
    monkeypatch.setenv("HERMES_DEPLOYMENT_INFERENCE_MODEL", "gpt-safe")
    monkeypatch.setenv("HERMES_DEPLOYMENT_INFERENCE_API_MODE", "chat_completions")
    monkeypatch.delenv("HERMES_DEPLOYMENT_INFERENCE_ALLOWED_MODELS", raising=False)

    policy = policy_from_control_plane_environment()

    assert policy.descriptor().allowed_models == ("gpt-safe",)


def test_broker_rejects_requests_for_starting_or_revoked_worker(tmp_path):
    store = AuthorityStore(tmp_path / "control")
    claim = store.claim_worker_start("ok1_owner", worker_id="worker-1")
    broker = DeploymentInferenceBroker(policy=_policy(), authority_store=store)
    worker_fd = broker.register(claim.lease)
    worker = socket.socket(fileno=worker_fd)
    key = broker._key(claim.lease)
    peer = broker._peers[key]
    request = {
        "method": "POST",
        "path": "/v1/chat/completions",
        "headers": {},
        "body": "e30=",
    }

    with pytest.raises(DeploymentInferenceRelayError, match="not active"):
        broker._handle_request(peer.lease, request)

    active = store.transition_worker_lease(
        claim.lease,
        state=WorkerLeaseState.ACTIVE,
        generation_state=WorkerGenerationState.ACTIVE,
    )
    broker.activate(active)
    broker.revoke(active)

    assert broker._key(active) not in broker._peers
    worker.close()


@contextmanager
def _upstream_server():
    received: list[dict[str, object]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers["Content-Length"])
            received.append({
                "path": self.path,
                "headers": dict(self.headers.items()),
                "body": self.rfile.read(length),
            })
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b"data: first\n\n")
            self.wfile.flush()
            self.wfile.write(b"data: second\n\n")
            self.wfile.flush()

        def log_message(self, _format, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, received
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _activate_relay(store: AuthorityStore, policy: DeploymentInferencePolicy):
    claim = store.claim_worker_start("ok1_owner", worker_id="worker-1")
    broker = DeploymentInferenceBroker(policy=policy, authority_store=store)
    worker_fd = broker.register(claim.lease)
    active = store.transition_worker_lease(
        claim.lease,
        state=WorkerLeaseState.ACTIVE,
        generation_state=WorkerGenerationState.ACTIVE,
    )
    broker.activate(active)
    relay = OwnerInferenceRelay(worker_fd)
    relay.start()
    return broker, active, relay


def test_owner_relay_streams_sse_and_injects_control_plane_credential(tmp_path):
    with _upstream_server() as (server, received):
        policy = DeploymentInferencePolicy(
            provider="custom:deployment",
            model="gpt-safe",
            api_mode="chat_completions",
            runtime_resolver=lambda: {
                "provider": "custom:deployment",
                "api_mode": "chat_completions",
                "base_url": f"http://127.0.0.1:{server.server_port}",
                "api_key": "control-plane-secret",
            },
        )
        broker, active, relay = _activate_relay(AuthorityStore(tmp_path / "control"), policy)
        try:
            with httpx.Client(timeout=5.0) as client:
                with client.stream(
                    "POST",
                    f"{relay.base_url}/chat/completions",
                    headers={"Authorization": "Bearer worker-marker"},
                    json={"model": "gpt-safe", "stream": True, "messages": []},
                ) as response:
                    assert response.status_code == 200
                    assert response.headers["content-type"] == "text/event-stream"
                    assert b"".join(response.iter_raw()) == b"data: first\n\ndata: second\n\n"

            upstream = received[0]
            assert upstream["path"] == "/v1/chat/completions"
            assert upstream["headers"]["Authorization"] == "Bearer control-plane-secret"
            assert "worker-marker" not in str(upstream["headers"])
        finally:
            broker.revoke(active)
            relay.close()


def test_owner_relay_rejects_revoked_worker_before_upstream_call(tmp_path):
    with _upstream_server() as (server, received):
        policy = DeploymentInferencePolicy(
            provider="custom:deployment",
            model="claude-safe",
            api_mode="anthropic_messages",
            runtime_resolver=lambda: {
                "provider": "custom:deployment",
                "api_mode": "anthropic_messages",
                "base_url": f"http://127.0.0.1:{server.server_port}",
                "api_key": "control-plane-secret",
            },
        )
        store = AuthorityStore(tmp_path / "control")
        broker, active, relay = _activate_relay(store, policy)
        try:
            store.transition_worker_lease(
                active,
                state=WorkerLeaseState.REVOKED,
                generation_state=WorkerGenerationState.FAILED,
            )
            with httpx.Client(timeout=5.0) as client:
                response = client.post(
                    f"{relay.base_url}/messages",
                    json={"model": "claude-safe", "messages": [], "max_tokens": 1},
                )
            assert response.status_code == 502
            assert received == []
        finally:
            broker.revoke(active)
            relay.close()


def test_policy_runtime_refuses_wrong_upstream_identity_or_credential():
    policy = DeploymentInferencePolicy(
        provider="custom:deployment",
        model="gpt-safe",
        api_mode="chat_completions",
        runtime_resolver=lambda: {
            "provider": "other",
            "api_mode": "chat_completions",
            "base_url": "https://provider.example.test/v1",
            "api_key": "secret",
        },
    )
    with pytest.raises(DeploymentInferencePolicyInvalid, match="does not match"):
        policy.resolve_runtime()
