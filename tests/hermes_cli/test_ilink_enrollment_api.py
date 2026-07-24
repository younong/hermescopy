"""Public iLink enrollment API and auth-boundary tests."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from hermes_cli import web_server
from hermes_cli.channel_connectors.weixin_ilink.enrollment import EnrollmentView
from hermes_cli.dashboard_auth.api_availability import classify_authenticated_api
from hermes_cli.dashboard_auth.public_paths import is_public_api_route


@pytest.fixture
def client():
    previous_service = getattr(web_server.app.state, "weixin_ilink_service", None)
    previous_required = getattr(web_server.app.state, "auth_required", None)
    previous_host = getattr(web_server.app.state, "bound_host", None)
    web_server.app.state.auth_required = False
    web_server.app.state.bound_host = "testserver"
    enrollments = SimpleNamespace(
        create=AsyncMock(
            return_value=EnrollmentView(
                attempt_id="enr_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                qr_content="https://example.invalid/complete-qr",
                status="waiting",
                expires_at=123.0,
            )
        ),
        get=lambda attempt_id: EnrollmentView(
            attempt_id=attempt_id,
            status="confirmed",
            expires_at=123.0,
            next_action="continue_in_wechat",
        ),
    )
    service = SimpleNamespace(enrollments=enrollments, stop=AsyncMock())
    with TestClient(web_server.app, base_url="http://testserver") as test_client:
        web_server.app.state.weixin_ilink_service = service
        yield test_client, enrollments
    web_server.app.state.weixin_ilink_service = previous_service
    web_server.app.state.auth_required = previous_required
    web_server.app.state.bound_host = previous_host


def test_exact_public_routes_are_method_aware():
    attempt = "/api/public/ilink/enrollments/enr_" + "a" * 32
    assert is_public_api_route("/api/public/ilink/enrollments", method="POST")
    assert not is_public_api_route("/api/public/ilink/enrollments", method="GET")
    assert is_public_api_route(attempt, method="GET")
    assert not is_public_api_route(attempt, method="DELETE")
    assert not is_public_api_route(attempt + "/extra", method="GET")
    assert classify_authenticated_api(
        "/api/public/ilink/enrollments", method="POST"
    ).allowed
    assert not classify_authenticated_api(
        "/api/public/ilink/enrollments", method="PUT"
    ).allowed


def test_create_response_is_allowlisted_no_store_and_uses_asgi_peer(client):
    test_client, enrollments = client
    response = test_client.post(
        "/api/public/ilink/enrollments",
        json={"scene": "join", "device_id": "device-1"},
        headers={"X-Forwarded-For": "203.0.113.99"},
    )

    assert response.status_code == 201
    assert response.headers["cache-control"] == "no-store"
    assert set(response.json()) == {"attempt_id", "qr_content", "status", "expires_at"}
    call = enrollments.create.await_args.kwargs
    assert call["source"] == "testclient"
    assert call["source"] != "203.0.113.99"


def test_get_response_is_allowlisted_no_store_and_local_only(client):
    test_client, enrollments = client
    response = test_client.get("/api/public/ilink/enrollments/enr_" + "a" * 32)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert set(response.json()) == {"status", "expires_at", "next_action"}
    assert not hasattr(enrollments, "client")


@pytest.mark.parametrize(
    "path",
    [
        "/api/public/ilink/enrollments/not-opaque",
        "/api/public/ilink/enrollments/enr_" + "a" * 31,
        "/api/public/ilink/enrollments/enr_" + "A" * 32,
    ],
)
def test_malformed_or_unknown_attempt_is_generic_404(client, path):
    test_client, _ = client
    response = test_client.get(path)
    assert response.status_code in {401, 404}
    if response.status_code == 404:
        assert response.json() == {"detail": "Enrollment not found"}


def test_service_unavailable_is_generic_503(client):
    test_client, _ = client
    web_server.app.state.weixin_ilink_service = None
    response = test_client.post(
        "/api/public/ilink/enrollments",
        json={"scene": "join", "device_id": "device-1"},
    )
    assert response.status_code == 503
    assert response.json() == {"detail": "Enrollment is unavailable"}
