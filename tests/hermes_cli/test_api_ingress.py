"""Focused tests for immutable Owner-bound OpenAI ingress."""
from __future__ import annotations

import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli.api_ingress import API_INGRESS_SCOPE, router
from hermes_cli.dashboard_auth.authority import AuthorityStore
from hermes_cli.dashboard_auth.machine_credentials import MachineCredentialProvider
from hermes_cli.dashboard_auth.owner_context import owner_context_from_registry
from hermes_cli.dashboard_auth.token_auth import token_auth_middleware


class _GatewayClient:
    instances = []

    def __init__(self, supervisor, owner):
        self.supervisor = supervisor
        self.owner = owner
        self.calls = []
        self.close = AsyncMock()
        self.instances.append(self)

    async def connect(self):
        return None

    async def call(self, method, params):
        self.calls.append((method, params))
        if method == "session.create":
            return {"session_id": "live-session"}
        return {"status": "accepted"}

    async def wait_for_event(self, method, *, session_id, timeout=None):
        return {
            "method": method,
            "params": {
                "session_id": session_id,
                "status": "complete",
                "text": "hello from worker",
                "usage": {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5},
            },
        }

    async def next_event(self, *, timeout=None):
        if not hasattr(self, "_streamed"):
            self._streamed = True
            return {
                "method": "message.delta",
                "params": {"session_id": "live-session", "text": "hello"},
            }
        return {
            "method": "message.complete",
            "params": {"session_id": "live-session", "status": "complete", "text": "hello"},
        }


@pytest.fixture
def api_app(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_OWNER_SECRET", "api-ingress-owner-secret")
    global_home = tmp_path / "global"
    store = AuthorityStore(global_home / "control-plane")
    owner = owner_context_from_registry(
        auth_provider="stub",
        tenant_id="tenant-a",
        canonical_user_id="user-a",
        global_home=global_home,
    )
    _credential, token = store.create_machine_credential(
        auth_provider=owner.auth_provider,
        tenant_id=owner.tenant_id,
        canonical_user_id=owner.owner_user_id,
        owner_key=owner.owner_key,
        scope=API_INGRESS_SCOPE,
    )
    provider = MachineCredentialProvider(store)
    app = FastAPI()
    app.state.auth_required = True
    app.state.authority_store = store
    app.state.owner_worker_supervisor = SimpleNamespace(global_home=global_home)

    @app.middleware("http")
    async def authenticate(request, call_next):
        token_value = request.headers.get("authorization", "").removeprefix("Bearer ")
        principal = provider.verify_token(token=token_value)
        if principal is not None:
            request.state.token_principal = principal
            request.state.token_authenticated = True
            return await call_next(request)
        return await token_auth_middleware(request, call_next)

    app.include_router(router)
    return app, token, owner


def test_chat_completion_uses_exact_owner_worker_and_stable_idempotency(api_app):
    app, token, owner = api_app
    _GatewayClient.instances.clear()
    with patch("hermes_cli.api_ingress.OwnerWorkerGatewayClient", _GatewayClient):
        response = TestClient(app).post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "request-123"},
            json={
                "model": "test-model",
                "messages": [
                    {"role": "system", "content": "be concise"},
                    {"role": "user", "content": "hello"},
                ],
            },
        )

    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "hello from worker"
    client = _GatewayClient.instances[0]
    assert client.owner.owner_key == owner.owner_key
    assert [method for method, _params in client.calls] == ["session.create", "prompt.submit"]
    assert client.calls[0][1]["stored_session_id"].startswith("api_")
    assert client.calls[1][1]["idempotency_key"].startswith("api:mc_")
    assert "request-123" not in client.calls[1][1]["idempotency_key"]
    client.close.assert_awaited_once()


def test_inline_image_is_attached_and_remote_url_is_rejected(api_app):
    app, token, _owner = api_app
    image = base64.b64encode(b"\x89PNG\r\n\x1a\nimage").decode()
    _GatewayClient.instances.clear()
    with patch("hermes_cli.api_ingress.OwnerWorkerGatewayClient", _GatewayClient):
        response = TestClient(app).post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "messages": [{"role": "user", "content": [
                    {"type": "text", "text": "inspect"},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image}"}},
                ]}],
            },
        )
    assert response.status_code == 200
    assert [method for method, _params in _GatewayClient.instances[0].calls] == [
        "session.create", "image.attach_bytes", "prompt.submit"
    ]

    response = TestClient(app).post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}"},
        json={"messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "https://example.com/image.png"}}
        ]}]},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_image_url"


def test_streaming_emits_chunks_and_done(api_app):
    app, token, _owner = api_app
    _GatewayClient.instances.clear()
    with patch("hermes_cli.api_ingress.OwnerWorkerGatewayClient", _GatewayClient):
        response = TestClient(app).post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {token}"},
            json={"stream": True, "messages": [{"role": "user", "content": "hello"}]},
        )
    assert response.status_code == 200
    assert '"content": "hello"' in response.text
    assert "data: [DONE]" in response.text
    _GatewayClient.instances[0].close.assert_awaited_once()


def test_missing_scope_and_runtime_fail_closed(api_app):
    app, token, _owner = api_app
    app.state.authority_store.resolve_machine_credential = lambda **_kwargs: None
    response = TestClient(app).post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}"},
        json={"messages": [{"role": "user", "content": "hello"}]},
    )
    assert response.status_code == 403


def test_request_size_and_idempotency_validation(api_app):
    app, token, _owner = api_app
    client = TestClient(app)
    response = client.post(
        "/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Length": "10000001",
        },
        content=b"{}",
    )
    assert response.status_code == 413

    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "bad key"},
        json={"messages": [{"role": "user", "content": "hello"}]},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_idempotency_key"
