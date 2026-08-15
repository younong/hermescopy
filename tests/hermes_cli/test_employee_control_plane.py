"""Authenticated Owner-scoped generic employee API tests."""

from __future__ import annotations

import io
import os
import stat
from types import SimpleNamespace

import pytest

from hermes_cli.channel_identity import create_employee
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
        "reasoning_effort": "high",
    }


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

    class _Runtime:
        def __init__(self):
            self.store = store
            self.status = SimpleNamespace(states={})

        async def close(self):
            return None

    monkeypatch.setattr(auth_middleware, "gated_auth_middleware", fake_gate)
    with TestClient(app) as client:
        app.state.channel_connector_runtime = _Runtime()
        yield client, session
    app.state.auth_required = previous_auth
    app.state.channel_connector_runtime = previous_runtime
    app.state.owner_worker_supervisor = previous_supervisor


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


def test_create_without_channel_and_list_detail_are_owner_scoped(authenticated_client, store):
    client, session = authenticated_client

    created = client.post("/api/employees", json={"profile": _policy()})

    assert created.status_code == 201
    payload = created.json()
    employee_id = payload["employee_id"]
    assert employee_id.startswith("emp_")
    assert payload["employee_kind"] == "managed"
    assert payload["protected"] is False
    assert payload["chat_eligible"] is True
    assert payload["profile"] == {
        **_policy(),
        "max_tokens": None,
        "workspace_relative_path": f"employees/{employee_id}",
    }
    assert (
        owner_context_from_session(session).host_owner_home
        / "workspaces"
        / "default"
        / "employees"
        / employee_id
    ).is_dir()
    assert payload["channels"] == {}
    assert payload["collaboration_policy"] == {
        "may_participate": True,
        "may_create_groups": False,
        "invite_quota": 5,
    }
    listed = client.get("/api/employees").json()["employees"]
    assert len(listed) == 2
    builtin = next(item for item in listed if item["employee_kind"] == "builtin_assistant")
    assert builtin["protected"] is True
    assert builtin["chat_eligible"] is True
    assert builtin["profile"] is None
    assert builtin["collaboration_policy"] == {
        "may_participate": True,
        "may_create_groups": True,
        "invite_quota": None,
    }
    assert client.get(f"/api/employees/{employee_id}").json() == payload

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
    hidden = create_employee(
        store, owner=owner_context_from_session(other), profile=_policy("Hidden")
    )
    assert client.get(f"/api/employees/{hidden.employee_id}").status_code == 404
    listed_ids = {
        item["employee_id"]
        for item in client.get("/api/employees").json()["employees"]
    }
    assert employee_id in listed_ids
    assert hidden.employee_id not in listed_ids
    assert len(listed_ids) == 2


def test_list_employees_filters_keyword_status_and_paginates(authenticated_client):
    client, _session = authenticated_client
    client.post("/api/employees", json={"profile": _policy("Alpha")})
    client.post("/api/employees", json={"profile": _policy("Beta")})
    gamma = client.post("/api/employees", json={"profile": _policy("Gamma")}).json()
    revoked = client.put(
        f"/api/employees/{gamma['employee_id']}/lifecycle", json={"status": "revoked"}
    )
    assert revoked.status_code == 200

    listed = client.get("/api/employees").json()
    names = [item["profile"]["name"] for item in listed["employees"] if item["profile"]]
    assert listed["total"] == 3
    assert listed["page"] == 1
    assert listed["page_size"] == 50
    assert names == ["Alpha", "Beta"]

    revoked_only = client.get("/api/employees", params={"status": "revoked"}).json()
    assert revoked_only["total"] == 1
    assert revoked_only["employees"][0]["employee_id"] == gamma["employee_id"]

    active_only = client.get("/api/employees", params={"status": "active"}).json()
    assert active_only["total"] == 3

    matched = client.get("/api/employees", params={"query": "alpha"}).json()
    assert matched["total"] == 1
    assert matched["employees"][0]["profile"]["name"] == "Alpha"

    second_page = client.get(
        "/api/employees", params={"page": 2, "page_size": 2}
    ).json()
    assert second_page["total"] == 3
    assert len(second_page["employees"]) == 1
    assert second_page["page"] == 2
    assert second_page["page_size"] == 2

    assert (
        client.get("/api/employees", params={"status": "unknown"}).status_code == 400
    )
    assert client.get("/api/employees", params={"page": 0}).status_code == 400
    assert client.get("/api/employees", params={"page": "x"}).status_code == 400
    assert client.get("/api/employees", params={"page_size": 0}).status_code == 400
    assert client.get("/api/employees", params={"page_size": 201}).status_code == 400


def test_create_rejects_invalid_reasoning_effort(authenticated_client):
    client, _session = authenticated_client

    response = client.post(
        "/api/employees",
        json={"profile": {**_policy(), "reasoning_effort": "extreme"}},
    )

    assert response.status_code == 400
    assert response.json() == {"detail": "reasoning_effort is invalid"}


def test_profile_policy_and_lifecycle_are_generic_employee_routes(authenticated_client):
    client, _session = authenticated_client
    employee_id = client.post("/api/employees", json={"profile": _policy()}).json()["employee_id"]

    stale = client.put(
        f"/api/employees/{employee_id}/profile",
        json={"expected_revision": 0, "profile": _policy("Updated")},
    )
    assert stale.status_code == 409
    assert stale.json() == {"detail": "employee_profile_revision_conflict"}

    updated = client.put(
        f"/api/employees/{employee_id}/profile",
        json={"expected_revision": 1, "profile": _policy("Updated")},
    )
    assert updated.status_code == 200
    assert updated.json()["profile"]["name"] == "Updated"

    policy = client.put(
        f"/api/employees/{employee_id}/collaboration-policy",
        json={
            "may_participate": False,
            "may_create_groups": True,
            "invite_quota": None,
        },
    )
    assert policy.status_code == 200
    assert policy.json()["collaboration_policy"] == {
        "may_participate": False,
        "may_create_groups": True,
        "invite_quota": None,
    }

    assert client.put(
        f"/api/employees/{employee_id}/lifecycle", json={"status": "suspended"}
    ).json()["lifecycle_status"] == "suspended"
    assert client.put(
        f"/api/employees/{employee_id}/lifecycle", json={"status": "active"}
    ).json()["lifecycle_status"] == "active"
    assert client.put(
        f"/api/employees/{employee_id}/lifecycle", json={"status": "revoked"}
    ).json()["lifecycle_status"] == "revoked"
    terminal = client.put(
        f"/api/employees/{employee_id}/lifecycle", json={"status": "active"}
    )
    assert terminal.status_code == 409
    assert terminal.json() == {"detail": "employee_revoked"}


def test_builtin_employee_mutation_routes_return_protected_conflict(
    authenticated_client,
):
    client, _session = authenticated_client
    builtin = next(
        employee
        for employee in client.get("/api/employees").json()["employees"]
        if employee["employee_kind"] == "builtin_assistant"
    )
    employee_id = builtin["employee_id"]
    requests = (
        client.put(
            f"/api/employees/{employee_id}/profile",
            json={"expected_revision": 0, "profile": _policy()},
        ),
        client.put(
            f"/api/employees/{employee_id}/collaboration-policy",
            json={
                "may_participate": False,
                "may_create_groups": False,
                "invite_quota": 0,
            },
        ),
        client.put(
            f"/api/employees/{employee_id}/lifecycle",
            json={"status": "suspended"},
        ),
        client.post(f"/api/employees/{employee_id}/rollover"),
        client.put(
            f"/api/employees/{employee_id}/channels/feishu",
            json={"app_id": "app-a", "app_secret": "secret"},
        ),
        client.put(
            f"/api/employees/{employee_id}/avatar",
            files={"file": ("avatar.png", _image_bytes(), "image/png")},
        ),
        client.delete(f"/api/employees/{employee_id}/avatar"),
    )
    for response in requests:
        assert response.status_code == 409
        assert response.json() == {"detail": "builtin_employee_protected"}


def _image_bytes(format="PNG", color="red"):
    from PIL import Image

    output = io.BytesIO()
    Image.new("RGB", (24, 18), color).save(output, format=format)
    return output.getvalue()


def test_avatar_is_owner_scoped_validated_and_replaceable(authenticated_client, store):
    client, _session = authenticated_client
    created = client.post("/api/employees", json={"profile": _policy()}).json()
    employee_id = created["employee_id"]
    avatar_path = f"/api/employees/{employee_id}/avatar"

    assert created["avatar_url"] is None
    assert client.get(avatar_path).status_code == 404
    invalid = client.put(
        avatar_path, files={"file": ("avatar.png", b"not-an-image", "image/png")}
    )
    assert invalid.status_code == 400
    uploaded = client.put(
        avatar_path,
        files={"file": ("avatar.png", _image_bytes(), "image/png")},
    )
    first_avatar_url = uploaded.json()["avatar_url"]
    assert first_avatar_url.startswith(f"{avatar_path}?v=")
    fetched = client.get(avatar_path)
    assert fetched.status_code == 200
    assert fetched.headers["content-type"] == "image/webp"
    assert fetched.headers["cache-control"] == "private, no-cache"
    assert client.get(f"/api/employees/{employee_id}").json()["avatar_url"] == first_avatar_url

    replaced = client.put(
        avatar_path,
        files={"file": ("avatar.png", _image_bytes(color="blue"), "image/png")},
    )
    second_avatar_url = replaced.json()["avatar_url"]
    assert client.get(f"/api/employees/{employee_id}").json()["avatar_url"] == second_avatar_url
    assert second_avatar_url.startswith(f"{avatar_path}?v=")
    assert second_avatar_url != first_avatar_url

    if os.name != "nt":
        from hermes_cli.channel_identity.employee_avatars import employee_avatar_path

        target = employee_avatar_path(store, employee_id)
        assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(target.stat().st_mode) == 0o600

    assert client.delete(avatar_path).json() == {"ok": True, "deleted": True}
    assert client.get(avatar_path).status_code == 404


def test_avatar_upload_has_hard_size_limit(authenticated_client):
    client, _session = authenticated_client
    employee_id = client.post("/api/employees", json={"profile": _policy()}).json()["employee_id"]
    response = client.put(
        f"/api/employees/{employee_id}/avatar",
        files={"file": ("avatar.png", b"x" * (5 * 1024 * 1024 + 1), "image/png")},
    )
    assert response.status_code == 413


def test_rollover_uses_employee_identity(authenticated_client):
    client, _session = authenticated_client
    employee_id = client.post("/api/employees", json={"profile": _policy()}).json()["employee_id"]
    response = client.post(f"/api/employees/{employee_id}/rollover")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "retired_sessions": 0}
