"""Authenticated Owner-scoped control-plane tests for managed Feishu employees."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hermes_cli.channel_identity import (
    ChannelCrypto,
    ChannelIdentityStore,
    Keyring,
    register_managed_feishu_account_for_owner,
    resolve_employee_profile,
    resolve_managed_feishu_account,
    set_managed_feishu_account_status,
)
from hermes_cli.dashboard_auth.base import Session
from hermes_cli.dashboard_auth.owner_context import owner_context_from_session


def _policy(name="Researcher"):
    return {
        "schema_version": 1,
        "name": name,
        "role": "Research analyst",
        "model_registration_id": "registration-a",
        "system_prompt": "Research carefully.",
        "toolsets": [],
        "skills": [],
        "mcp_servers": [],
        "workspace_relative_path": "employees/researcher",
        "knowledge_relative_paths": [],
        "max_iterations": 20,
    }


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_OWNER_SECRET", "owner-secret")
    return ChannelIdentityStore(
        ChannelCrypto(
            lookup=Keyring(keys={1: b"l" * 32}, active_version=1),
            encryption=Keyring(keys={1: b"e" * 32}, active_version=1),
        ),
        tmp_path / "control-plane",
        global_home=tmp_path,
    )


@pytest.fixture
def authenticated_client(monkeypatch, store):
    try:
        from starlette.testclient import TestClient
    except ImportError:
        pytest.skip("fastapi/starlette not installed")

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
    previous_supervisor = getattr(app.state, "owner_worker_supervisor", None)
    app.state.auth_required = True

    async def fake_gate(request, call_next):
        request.state.session = session
        return await call_next(request)

    class _Connectors:
        def __init__(self):
            self.registered = set()

        def accounts(self, provider):
            assert provider == "feishu"
            return tuple(self.registered)

    class _Runtime:
        def __init__(self):
            self.store = store
            self.connectors = _Connectors()
            self.status = SimpleNamespace(states={})
            self.started = []
            self.stopped = []

        def register_feishu_account(self, account_id):
            self.connectors.registered.add(account_id)

        async def start_account(self, provider, account_id):
            self.started.append((provider, account_id))
            self.status.states[f"{provider}:{account_id}"] = "ready"

        async def stop_account(self, provider, account_id):
            self.stopped.append((provider, account_id))
            self.status.states[f"{provider}:{account_id}"] = "stopped"
            return True

        async def close(self):
            return None

    runtime = _Runtime()
    monkeypatch.setattr(auth_middleware, "gated_auth_middleware", fake_gate)
    with TestClient(app) as client:
        app.state.channel_connector_runtime = runtime
        yield client, session, runtime
    app.state.auth_required = previous_auth
    app.state.channel_connector_runtime = previous_runtime
    app.state.owner_worker_supervisor = previous_supervisor


def _register(store, session, *, app_id="cli_app", name="Researcher"):
    return register_managed_feishu_account_for_owner(
        store,
        owner=owner_context_from_session(session),
        provider_account_id=app_id,
        external_subject=f"ou_{app_id}",
        conversation_id=None,
        credentials={
            "app_id": app_id,
            "app_secret": "private-secret",
            "domain": "feishu",
            "encrypt_key": "encrypt-secret",
            "verification_token": "verification-secret",
            "bot_open_id": f"ou_{app_id}",
        },
        employee_profile=_policy(name),
    )


def test_list_and_detail_are_owner_scoped_and_never_return_secrets(authenticated_client, store):
    client, session, _runtime = authenticated_client
    registered = _register(store, session)

    listing = client.get("/api/messaging/feishu/employees")
    detail = client.get(f"/api/messaging/feishu/employees/{registered.account_id}")

    assert listing.status_code == 200
    assert detail.status_code == 200
    payload = detail.json()
    assert payload["app_id"] == "cli_app"
    assert payload["profile"] == _policy()
    serialized = detail.text + listing.text
    for secret in ("private-secret", "encrypt-secret", "verification-secret"):
        assert secret not in serialized
    assert "ciphertext" not in serialized


def test_cross_owner_lookup_returns_404(authenticated_client, store):
    client, session, _runtime = authenticated_client
    other_session = Session(
        user_id="owner-b",
        email="b@example.test",
        display_name="Owner B",
        org_id="org-a",
        provider="test",
        expires_at=9_999_999_999,
        access_token="other",
        refresh_token="other",
    )
    registered = _register(store, other_session)

    assert client.get(
        f"/api/messaging/feishu/employees/{registered.account_id}"
    ).status_code == 404
    assert client.get("/api/messaging/feishu/employees").json() == {"employees": []}


def test_profile_revision_conflict_and_suspended_profile_management(authenticated_client, store):
    client, session, _runtime = authenticated_client
    registered = _register(store, session)
    stale = client.put(
        f"/api/messaging/feishu/employees/{registered.account_id}/profile",
        json={"expected_revision": 0, "profile": _policy("Updated")},
    )
    assert stale.status_code == 409
    assert stale.json()["detail"] == "employee_profile_revision_conflict"

    owner = owner_context_from_session(session)
    set_managed_feishu_account_status(
        store, owner=owner, account_id=registered.account_id, status="suspended"
    )
    detail = client.get(f"/api/messaging/feishu/employees/{registered.account_id}")
    assert detail.status_code == 200
    assert detail.json()["profile"]["name"] == "Researcher"


def test_suspend_resume_revoke_only_selected_account(authenticated_client, store):
    client, session, runtime = authenticated_client
    first = _register(store, session, app_id="first")
    second = _register(store, session, app_id="second")

    assert client.put(
        f"/api/messaging/feishu/employees/{first.account_id}/lifecycle",
        json={"status": "suspended"},
    ).status_code == 200
    assert runtime.stopped == [("feishu", first.account_id)]

    assert client.put(
        f"/api/messaging/feishu/employees/{first.account_id}/lifecycle",
        json={"status": "active"},
    ).status_code == 200
    assert runtime.started == [("feishu", first.account_id)]

    assert client.put(
        f"/api/messaging/feishu/employees/{first.account_id}/lifecycle",
        json={"status": "revoked"},
    ).status_code == 200
    assert resolve_managed_feishu_account(
        store,
        owner=owner_context_from_session(session),
        account_id=second.account_id,
    ).lifecycle_status == "active"
    terminal = client.put(
        f"/api/messaging/feishu/employees/{first.account_id}/lifecycle",
        json={"status": "active"},
    )
    assert terminal.status_code == 409
    assert resolve_managed_feishu_account(
        store,
        owner=owner_context_from_session(session),
        account_id=first.account_id,
    ).lifecycle_status == "revoked"


def test_rotation_preserves_omitted_optional_secrets_and_checks_identity(
    authenticated_client, store, monkeypatch
):
    client, session, runtime = authenticated_client
    registered = _register(store, session)

    verify = AsyncMock(
        return_value={
            "app_id": "cli_app",
            "domain": "feishu",
            "bot_open_id": "ou_cli_app",
            "bot_user_id": "",
            "bot_union_id": "",
            "bot_name": "Researcher",
        }
    )
    monkeypatch.setattr("hermes_cli.channel_connectors.feishu.verify_feishu_credentials", verify)
    response = client.put(
        f"/api/messaging/feishu/employees/{registered.account_id}/credentials",
        json={"expected_credential_version": 1, "app_secret": "new-secret"},
    )

    assert response.status_code == 200
    candidate = verify.await_args.args[0]
    assert candidate["encrypt_key"] == "encrypt-secret"
    assert candidate["verification_token"] == "verification-secret"
    assert runtime.stopped == [("feishu", registered.account_id)]
    assert runtime.started == [("feishu", registered.account_id)]

    mismatch = AsyncMock(
        return_value={
            "app_id": "cli_app",
            "domain": "feishu",
            "bot_open_id": "ou_attacker",
            "bot_user_id": "",
            "bot_union_id": "",
            "bot_name": "Other",
        }
    )
    monkeypatch.setattr("hermes_cli.channel_connectors.feishu.verify_feishu_credentials", mismatch)
    response = client.put(
        f"/api/messaging/feishu/employees/{registered.account_id}/credentials",
        json={"expected_credential_version": 2, "app_secret": "attacker-secret"},
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "feishu_bot_identity_changed"


def test_secret_route_validation_never_echoes_submitted_credentials(authenticated_client):
    client, _session, _runtime = authenticated_client
    app_secret = "submitted-app-secret"
    encrypt_key = "submitted-encrypt-key"
    verification_token = "submitted-verification-token"

    create = client.post(
        "/api/messaging/feishu/employees",
        json={
            "app_id": ["not-a-string"],
            "app_secret": app_secret,
            "encrypt_key": encrypt_key,
            "verification_token": verification_token,
            "profile": {},
        },
    )
    rotate = client.put(
        "/api/messaging/feishu/employees/missing/credentials",
        json={
            "expected_credential_version": "invalid",
            "app_secret": app_secret,
            "encrypt_key": encrypt_key,
            "verification_token": verification_token,
        },
    )

    assert create.status_code == 422
    assert rotate.status_code == 422
    serialized = create.text + rotate.text
    for secret in (app_secret, encrypt_key, verification_token):
        assert secret not in serialized
    assert create.json() == {"detail": "invalid_request"}
    assert rotate.json() == {"detail": "invalid_request"}


def test_create_validates_full_policy_and_persists_without_fake_binding(
    authenticated_client, store, monkeypatch
):
    client, session, runtime = authenticated_client
    verify = AsyncMock(
        return_value={
            "app_id": "cli_new",
            "domain": "feishu",
            "bot_open_id": "ou_new",
            "bot_user_id": "",
            "bot_union_id": "",
            "bot_name": "New Bot",
        }
    )
    monkeypatch.setattr("hermes_cli.channel_connectors.feishu.verify_feishu_credentials", verify)

    invalid = client.post(
        "/api/messaging/feishu/employees",
        json={
            "app_id": "cli_new",
            "app_secret": "private",
            "domain": "feishu",
            "profile": {"schema_version": 1},
        },
    )
    assert invalid.status_code == 400

    response = client.post(
        "/api/messaging/feishu/employees",
        json={
            "app_id": "cli_new",
            "app_secret": "private",
            "domain": "feishu",
            "profile": _policy("New Bot"),
        },
    )
    assert response.status_code == 201
    account_id = response.json()["account_id"]
    assert runtime.started == [("feishu", account_id)]
    with store.read() as conn:
        assert conn.execute(
            "SELECT 1 FROM channel_bindings WHERE account_id=?", (account_id,)
        ).fetchone() is None
    assert resolve_employee_profile(
        store,
        owner=owner_context_from_session(session),
        account_id=account_id,
    ).profile["name"] == "New Bot"
