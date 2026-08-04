"""Focused authenticated control-plane tests for Webhook provisioning."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from hermes_cli.channel_identity import (
    ChannelCrypto,
    ChannelIdentityStore,
    Keyring,
    resolve_connector_account,
)
from hermes_cli.dashboard_auth.base import Session
from hermes_cli.dashboard_auth.owner_context import owner_context_from_session


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_OWNER_SECRET", "owner-secret")
    crypto = ChannelCrypto(
        lookup=Keyring(keys={1: b"l" * 32}, active_version=1),
        encryption=Keyring(keys={1: b"e" * 32}, active_version=1),
    )
    return ChannelIdentityStore(
        crypto,
        tmp_path / "control-plane",
        global_home=tmp_path,
    )


@pytest.fixture
def authenticated_client(monkeypatch, store):
    try:
        from starlette.testclient import TestClient
    except ImportError:
        pytest.skip("fastapi/starlette not installed")

    from hermes_cli.dashboard_auth.base import Session
    import hermes_cli.dashboard_auth.middleware as auth_middleware
    from hermes_cli.web_server import app

    session = Session(
        user_id="owner-a",
        email="owner@example.test",
        display_name="Owner A",
        org_id="org-a",
        provider="test",
        expires_at=9_999_999_999,
        access_token="access",
        refresh_token="refresh",
    )
    previous_auth = getattr(app.state, "auth_required", False)
    previous_runtime = getattr(app.state, "channel_connector_runtime", None)
    app.state.auth_required = True

    async def fake_gate(request, call_next):
        request.state.session = session
        return await call_next(request)

    class _Runtime:
        def __init__(self):
            self.store = store

        async def close(self):
            return None

    monkeypatch.setattr(auth_middleware, "gated_auth_middleware", fake_gate)
    with TestClient(app) as client:
        app.state.channel_connector_runtime = _Runtime()
        yield client, session
    app.state.auth_required = previous_auth
    app.state.channel_connector_runtime = previous_runtime


def test_provision_requires_authenticated_owner_mode(monkeypatch, store):
    from fastapi import HTTPException
    from hermes_cli.web_server import (
        WebhookConnectorCreate,
        app,
        create_webhook_connector_account,
    )

    previous = getattr(app.state, "auth_required", False)
    app.state.auth_required = False
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(auth_required=False)),
        state=SimpleNamespace(session=None),
    )
    try:
        with pytest.raises(HTTPException) as excinfo:
            import asyncio

            asyncio.run(
                create_webhook_connector_account(
                    request,
                    WebhookConnectorCreate(
                        response_url="https://callback.example/result"
                    ),
                )
            )
        assert excinfo.value.status_code == 403
    finally:
        app.state.auth_required = previous


def test_authenticated_provisioning_generates_and_encrypts_credentials(
    authenticated_client,
    store,
):
    client, session = authenticated_client
    response = client.post(
        "/api/messaging/webhook/accounts",
        json={
            "prompt_template": "Build {build.id}: {status}",
            "allowed_events": ["build.completed"],
            "response_url": "https://callback.example/result",
        },
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["webhook_path"] == f"/webhooks/{payload['route_token']}"
    assert len(payload["hmac_secret"].encode()) >= 32
    assert len(payload["response_hmac_secret"].encode()) >= 32

    account = resolve_connector_account(
        store,
        provider="webhook",
        account_id=payload["account_id"],
    )
    assert account.provider_account_id == payload["route_token"]
    assert account.credentials == {
        "hmac_secret": payload["hmac_secret"],
        "prompt_template": "Build {build.id}: {status}",
        "allowed_events": ["build.completed"],
        "response_url": "https://callback.example/result",
        "response_hmac_secret": payload["response_hmac_secret"],
    }
    with store.read() as conn:
        binding = conn.execute(
            """
            SELECT o.owner_key
            FROM channel_bindings b
            JOIN external_identities e
              ON e.external_identity_id=b.external_identity_id
            JOIN owner_bindings o ON o.canonical_user_id=e.canonical_user_id
            WHERE b.binding_id=?
            """,
            (payload["binding_id"],),
        ).fetchone()
    assert binding["owner_key"] == owner_context_from_session(session).owner_key


def test_provisioning_rejects_unsafe_callback(authenticated_client):
    client, _session = authenticated_client
    response = client.post(
        "/api/messaging/webhook/accounts",
        json={"response_url": "https://127.0.0.1/callback"},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "callback_url_invalid"
