"""Focused API tests for durable local Basic user management and reset gating."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from hermes_cli import web_server
from hermes_cli.dashboard_auth import (
    RefreshExpiredError,
    clear_providers,
    get_provider,
    register_provider,
)
from hermes_cli.dashboard_auth.cookies import SESSION_AT_COOKIE
from hermes_cli.dashboard_auth.local_users import LocalUserStore
from plugins.dashboard_auth.basic import BasicAuthProvider


_SECRET = b"t" * 32


@pytest.fixture
def local_basic_client(tmp_path):
    clear_providers()
    store = LocalUserStore(secret=_SECRET, control_home=tmp_path / "control")
    provider = BasicAuthProvider(secret=_SECRET, store=store)
    register_provider(provider)
    previous = {
        name: getattr(web_server.app.state, name, None)
        for name in ("bound_host", "bound_port", "auth_required")
    }
    web_server.app.state.bound_host = "dashboard.test"
    web_server.app.state.bound_port = 443
    web_server.app.state.auth_required = True
    client = TestClient(web_server.app, base_url="https://dashboard.test")
    yield client, store
    clear_providers()
    for name, value in previous.items():
        setattr(web_server.app.state, name, value)


def _login(client: TestClient, username: str, password: str):
    return client.post(
        "/auth/password-login",
        json={"provider": "basic", "username": username, "password": password},
    )


def test_admin_management_requires_durable_local_basic(local_basic_client):
    client, store = local_basic_client
    store.create_account(username="admin", password="admin-password", role="admin")
    assert client.get("/api/auth/users").status_code == 401
    assert _login(client, "admin", "admin-password").status_code == 200
    assert client.get("/api/auth/users").status_code == 200
    assert client.get("/api/auth/local-users").status_code == 200


def test_admin_create_reset_and_update_are_server_authorized(local_basic_client):
    client, store = local_basic_client
    store.create_account(username="admin", password="admin-password", role="admin")
    store.create_account(username="member", password="member-password")
    assert _login(client, "member", "member-password").status_code == 200
    assert client.post("/api/auth/users", json={"username": "newuser"}).status_code == 403

    client.cookies.clear()
    assert _login(client, "admin", "admin-password").status_code == 200
    created = client.post(
        "/api/auth/users",
        json={"username": "newuser", "display_name": "New User", "role": "member"},
    )
    assert created.status_code == 200
    created_body = created.json()
    assert created_body["user"]["must_change_password"] is True
    assert created_body["user"]["status"] == "active"
    assert created_body["temporary_password"]

    updated = client.patch(
        "/api/auth/users/newuser",
        json={"display_name": "Renamed", "role": "admin", "status": "active"},
    )
    assert updated.status_code == 200
    assert updated.json()["user"]["display_name"] == "Renamed"
    assert updated.json()["user"]["role"] == "admin"

    reset = client.post("/api/auth/users/newuser/reset-password")
    assert reset.status_code == 200
    assert reset.json()["temporary_password"]
    assert reset.json()["user"]["must_change_password"] is True


def test_create_failure_never_returns_a_temporary_password(local_basic_client):
    client, store = local_basic_client
    store.create_account(username="admin", password="admin-password", role="admin")
    assert _login(client, "admin", "admin-password").status_code == 200
    assert client.post("/api/auth/users", json={"username": "admin"}).status_code == 409
    assert "temporary_password" not in client.post(
        "/api/auth/users", json={"username": "admin"}
    ).json()


def test_forced_change_blocks_regular_api_and_ws_ticket_but_allows_recovery(local_basic_client):
    client, store = local_basic_client
    store.create_account(
        username="resetuser", password="temporary-password", must_change_password=True,
    )
    assert _login(client, "resetuser", "temporary-password").status_code == 200

    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["must_change_password"] is True
    assert me.json()["local_user_management"] == {"enabled": True, "is_admin": False}
    assert me.json()["capabilities"] == ["auth.me", "auth.password.change", "auth.logout"]
    assert client.get("/api/sessions").status_code == 403
    assert client.post(
        "/api/auth/ws-ticket", json={"audience": "browser-ws:/api/pty"}
    ).status_code == 403

    wrong = client.post(
        "/api/auth/password/change",
        json={"current_password": "wrong", "new_password": "replacement-password"},
    )
    assert wrong.status_code == 400
    assert wrong.json()["detail"] == "Current password is incorrect"

    changed = client.post(
        "/api/auth/password/change",
        json={
            "current_password": "temporary-password",
            "new_password": "replacement-password",
        },
    )
    assert changed.status_code == 200
    assert changed.json()["user"]["must_change_password"] is False
    assert any("Max-Age=0" in value for value in changed.headers.get_list("set-cookie"))
    # The durable revision invalidates the pre-change cookie session.
    assert client.get("/api/auth/me").status_code == 401

    assert _login(client, "resetuser", "replacement-password").status_code == 200
    assert client.get("/api/auth/me").json()["must_change_password"] is False


def test_password_change_rejects_invalid_replacement_without_revoking_session(local_basic_client):
    client, store = local_basic_client
    store.create_account(username="member", password="member-password")
    assert _login(client, "member", "member-password").status_code == 200

    response = client.post(
        "/api/auth/password-change",
        json={"current_password": "member-password", "new_password": ""},
    )

    assert response.status_code == 400
    assert client.get("/api/auth/me").status_code == 200


def test_logout_revokes_durable_refresh_session_without_access_cookie(local_basic_client):
    client, store = local_basic_client
    store.create_account(username="member", password="member-password")
    assert _login(client, "member", "member-password").status_code == 200
    provider = get_provider("basic")
    assert isinstance(provider, BasicAuthProvider)
    refresh_token = next(
        value for name, value in client.cookies.items() if name.endswith("hermes_session_rt")
    )
    client.cookies.delete(SESSION_AT_COOKIE)

    response = client.post("/auth/logout", follow_redirects=False)

    assert response.status_code == 302
    with pytest.raises(RefreshExpiredError):
        provider.refresh_session(refresh_token=refresh_token)


def test_legacy_basic_does_not_expose_local_management(local_basic_client):
    client, _store = local_basic_client
    # The fixture already registers local Basic; replacing the provider avoids
    # accidental API availability for configured single-account deployments.
    clear_providers()
    legacy = BasicAuthProvider(
        secret=_SECRET, username="admin", password_hash="scrypt$bad$hash"
    )
    register_provider(legacy)
    client.cookies.set(SESSION_AT_COOKIE, "not-a-local-token")
    assert client.get("/api/auth/users").status_code == 401
