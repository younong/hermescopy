"""Authenticated Owner-scoped API tests for optional Feishu bindings."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hermes_cli.channel_identity import (
    create_employee,
    register_employee_feishu_binding,
    resolve_employee,
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
        "knowledge_relative_paths": [],
        "max_iterations": 20,
    }


@pytest.fixture
def store(tmp_path, monkeypatch):
    from hermes_cli.channel_identity import ChannelCrypto, ChannelIdentityStore, Keyring

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
    from starlette.testclient import TestClient

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


def _create_employee(store, session, name="Researcher"):
    return create_employee(
        store,
        owner=owner_context_from_session(session),
        profile=_policy(name),
    )


def _bind(store, session, employee_id, app_id="cli_app"):
    return register_employee_feishu_binding(
        store,
        owner=owner_context_from_session(session),
        employee_id=employee_id,
        provider_account_id=app_id,
        credentials={
            "app_id": app_id,
            "app_secret": "private-secret",
            "domain": "feishu",
            "encrypt_key": "encrypt-secret",
            "verification_token": "verification-secret",
            "bot_open_id": f"ou_{app_id}",
        },
    )


def test_create_binding_is_separate_from_employee_creation(authenticated_client, store, monkeypatch):
    client, session, runtime = authenticated_client
    employee = _create_employee(store, session)
    assert client.get(f"/api/employees/{employee.employee_id}").json()["channels"] == {}

    verify = AsyncMock(return_value={
        "app_id": "cli_new",
        "domain": "feishu",
        "bot_open_id": "ou_new",
        "bot_user_id": "",
        "bot_union_id": "",
        "bot_name": "New Bot",
    })
    monkeypatch.setattr("hermes_cli.channel_connectors.feishu.verify_feishu_credentials", verify)
    response = client.put(
        f"/api/employees/{employee.employee_id}/channels/feishu",
        json={"app_id": "cli_new", "app_secret": "private", "domain": "feishu"},
    )

    assert response.status_code == 201
    channel = response.json()["channels"]["feishu"]
    assert channel["app_id"] == "cli_new"
    assert channel["credential_version"] == 1
    assert channel["lifecycle_status"] == "active"
    assert channel["runtime_state"] == "ready"
    assert runtime.started == [("feishu", channel["connector_account_id"])]
    assert response.json()["profile"] == {
        **_policy(),
        "max_tokens": None,
        "workspace_relative_path": f"employees/{employee.employee_id}",
    }
    assert "private" not in response.text


def test_binding_summary_is_optional_owner_scoped_and_secret_free(authenticated_client, store):
    client, session, _runtime = authenticated_client
    employee = _create_employee(store, session)
    binding = _bind(store, session, employee.employee_id)

    detail = client.get(f"/api/employees/{employee.employee_id}")
    assert detail.status_code == 200
    assert detail.json()["channels"]["feishu"] == {
        "binding_id": binding.binding_id,
        "connector_account_id": binding.connector_account_id,
        "app_id": "cli_app",
        "credential_version": 1,
        "lifecycle_status": "active",
        "runtime_state": "stopped",
    }
    for secret in ("private-secret", "encrypt-secret", "verification-secret"):
        assert secret not in detail.text

    other = Session(
        user_id="owner-b",
        email="b@example.test",
        display_name="Owner B",
        org_id="org-a",
        provider="test",
        expires_at=9_999_999_999,
        access_token="other",
        refresh_token="other",
    )
    hidden = _create_employee(store, other)
    _bind(store, other, hidden.employee_id, "other_app")
    assert client.get(f"/api/employees/{hidden.employee_id}").status_code == 404
    assert client.put(
        f"/api/employees/{hidden.employee_id}/channels/feishu/lifecycle",
        json={"status": "suspended"},
    ).status_code == 404


def test_rotation_preserves_optional_secrets_and_checks_bot_identity(
    authenticated_client, store, monkeypatch
):
    client, session, runtime = authenticated_client
    employee = _create_employee(store, session)
    binding = _bind(store, session, employee.employee_id)

    verify = AsyncMock(return_value={
        "app_id": "cli_app",
        "domain": "feishu",
        "bot_open_id": "ou_cli_app",
        "bot_user_id": "",
        "bot_union_id": "",
        "bot_name": "Researcher",
    })
    monkeypatch.setattr("hermes_cli.channel_connectors.feishu.verify_feishu_credentials", verify)
    response = client.put(
        f"/api/employees/{employee.employee_id}/channels/feishu/credentials",
        json={"expected_credential_version": 1, "app_secret": "new-secret"},
    )

    assert response.status_code == 200
    candidate = verify.await_args.args[0]
    assert candidate["encrypt_key"] == "encrypt-secret"
    assert candidate["verification_token"] == "verification-secret"
    assert response.json()["channels"]["feishu"]["credential_version"] == 2
    assert runtime.stopped == [("feishu", binding.connector_account_id)]
    assert runtime.started == [("feishu", binding.connector_account_id)]

    monkeypatch.setattr(
        "hermes_cli.channel_connectors.feishu.verify_feishu_credentials",
        AsyncMock(return_value={
            "app_id": "cli_app",
            "domain": "feishu",
            "bot_open_id": "ou_attacker",
            "bot_user_id": "",
            "bot_union_id": "",
            "bot_name": "Other",
        }),
    )
    mismatch = client.put(
        f"/api/employees/{employee.employee_id}/channels/feishu/credentials",
        json={"expected_credential_version": 2, "app_secret": "attacker-secret"},
    )
    assert mismatch.status_code == 409
    assert mismatch.json() == {"detail": "feishu_bot_identity_changed"}


def test_binding_test_and_lifecycle_do_not_change_employee_lifecycle(
    authenticated_client, store, monkeypatch
):
    client, session, runtime = authenticated_client
    employee = _create_employee(store, session)
    binding = _bind(store, session, employee.employee_id)
    verify = AsyncMock(return_value={"bot_name": "Researcher"})
    monkeypatch.setattr("hermes_cli.channel_connectors.feishu.verify_feishu_credentials", verify)

    tested = client.post(f"/api/employees/{employee.employee_id}/channels/feishu/test")
    assert tested.json() == {"ok": True, "state": "connected", "bot_name": "Researcher"}

    suspended = client.put(
        f"/api/employees/{employee.employee_id}/channels/feishu/lifecycle",
        json={"status": "suspended"},
    )
    assert suspended.status_code == 200
    assert suspended.json()["channels"]["feishu"]["lifecycle_status"] == "suspended"
    assert suspended.json()["lifecycle_status"] == "active"
    assert runtime.stopped == [("feishu", binding.connector_account_id)]

    active = client.put(
        f"/api/employees/{employee.employee_id}/channels/feishu/lifecycle",
        json={"status": "active"},
    )
    assert active.status_code == 200
    assert active.json()["channels"]["feishu"]["lifecycle_status"] == "active"

    revoked = client.put(
        f"/api/employees/{employee.employee_id}/channels/feishu/lifecycle",
        json={"status": "revoked"},
    )
    assert revoked.status_code == 200
    assert revoked.json()["channels"] == {}
    assert resolve_employee(
        store,
        owner=owner_context_from_session(session),
        employee_id=employee.employee_id,
    ).lifecycle_status == "active"
    terminal = client.put(
        f"/api/employees/{employee.employee_id}/channels/feishu/lifecycle",
        json={"status": "active"},
    )
    assert terminal.status_code == 409
    assert terminal.json() == {"detail": "feishu_binding_revoked"}


def test_secret_validation_never_echoes_submitted_credentials(authenticated_client, store):
    client, session, _runtime = authenticated_client
    employee = _create_employee(store, session)
    app_secret = "submitted-app-secret"
    encrypt_key = "submitted-encrypt-key"
    verification_token = "submitted-verification-token"

    create = client.put(
        f"/api/employees/{employee.employee_id}/channels/feishu",
        json={
            "app_id": ["not-a-string"],
            "app_secret": app_secret,
            "encrypt_key": encrypt_key,
            "verification_token": verification_token,
        },
    )
    rotate = client.put(
        f"/api/employees/{employee.employee_id}/channels/feishu/credentials",
        json={
            "expected_credential_version": "invalid",
            "app_secret": app_secret,
            "encrypt_key": encrypt_key,
            "verification_token": verification_token,
        },
    )

    assert create.status_code == 422
    assert rotate.status_code == 422
    assert create.json() == {"detail": "invalid_request"}
    assert rotate.json() == {"detail": "invalid_request"}
    serialized = create.text + rotate.text
    for secret in (app_secret, encrypt_key, verification_token):
        assert secret not in serialized


def test_removed_legacy_feishu_employee_routes_are_not_registered(authenticated_client):
    client, _session, _runtime = authenticated_client
    assert client.get("/api/messaging/feishu/employees").status_code in {403, 404}
    assert client.post("/api/messaging/feishu/employees", json={}).status_code in {403, 404}
