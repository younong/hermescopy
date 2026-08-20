"""End-to-end behavioural tests for the dashboard auth gate.

Uses ``StubAuthProvider`` so the OAuth round trip can complete in-process
without any external IDP.  Exercises:

  * `/api/status` flips from public (loopback) to gated (auth_required)
  * `/` redirects to /login when no cookie present
  * `/api/auth/providers` is the public bootstrap endpoint
  * `/login` renders HTML listing all providers
  * /assets/* still passes through unauthenticated
  * Full /auth/login → /auth/callback → / round trip with the stub
  * Invalid / missing cookies return 401 (api) or 302 (html)
  * Zero-providers + gate-on fails closed
"""
from __future__ import annotations

import pytest

from fastapi.testclient import TestClient

from hermes_cli import web_server
from hermes_cli.dashboard_auth import clear_providers, register_provider
from hermes_cli.dashboard_auth.cookies import SESSION_AT_COOKIE
from hermes_cli.dashboard_auth.ws_tickets import TicketInvalid, consume_ticket
from tests.hermes_cli.conftest_dashboard_auth import StubAuthProvider


@pytest.fixture
def gated_app():
    """Configure web_server.app for gated mode + register the stub provider."""
    clear_providers()
    register_provider(StubAuthProvider())
    prev_host = getattr(web_server.app.state, "bound_host", None)
    prev_port = getattr(web_server.app.state, "bound_port", None)
    prev_required = getattr(web_server.app.state, "auth_required", None)
    web_server.app.state.bound_host = "fly-app.fly.dev"
    web_server.app.state.bound_port = 443
    web_server.app.state.auth_required = True
    # Use https base_url so cookies pick up Secure flag and host_header
    # matches the bound interface.
    client = TestClient(web_server.app, base_url="https://fly-app.fly.dev")
    yield client
    clear_providers()
    web_server.app.state.bound_host = prev_host
    web_server.app.state.bound_port = prev_port
    web_server.app.state.auth_required = prev_required


# ---------------------------------------------------------------------------
# Allowlist (public) routes
# ---------------------------------------------------------------------------


def test_gated_status_is_public(gated_app):
    """``/api/status`` MUST be public under the OAuth gate.

    Regression guard for the wildcard-subdomain rollout: NAS
    (``fly-provider.ts`` ``getInstanceRuntimeStatus``) hits
    ``/api/status`` without a cookie as its sole liveness probe. A 401
    here surfaces every healthy agent as STARTING/down in the portal
    UI. The endpoint returns only version + gateway/auth-gate metadata
    (no user data, no session content), so it stays in the shared
    ``PUBLIC_API_PATHS`` allowlist under the authenticated Web gate.

    The body also reports the gate's shape (``auth_required``,
    ``auth_providers``) so the SPA's StatusPage and external monitors
    can distinguish loopback / gated / no-providers without a separate
    round trip.
    """
    r = gated_app.get("/api/status")
    assert r.status_code == 200, (
        f"Expected 200, got {r.status_code}: {r.text}"
    )
    body = r.json()
    assert body["auth_required"] is True
    assert "version" in body
    assert "gateway_state" in body


@pytest.mark.parametrize("path", [
    "/api/config/defaults",
    "/api/config/schema",
    "/api/dashboard/themes",
])
def test_other_public_api_paths_are_public_under_gate(gated_app, path):
    """The remaining ``PUBLIC_API_PATHS`` entries must also bypass the
    gate. They're documented as non-sensitive read-only endpoints that
    the SPA pre-loads before login (themes, config schema, model
    metadata). A 401 / 302-to-login here would block the dashboard
    shell from rendering pre-auth.

    Accept any non-auth-failure status: 200 when the route succeeds,
    or any route-specific error (e.g. 400 / 404 / 500 from a missing
    dependency) — but NEVER 401, and NEVER a 302 to ``/login``.
    """
    r = gated_app.get(path, follow_redirects=False)
    assert r.status_code != 401, (
        f"{path} returned 401 under the OAuth gate — should be public"
    )
    if r.status_code == 302:
        location = r.headers.get("location", "")
        assert "/login" not in location, (
            f"{path} redirected to {location} — should be public, "
            "not bounced to /login"
        )


def test_gated_html_redirects_to_login(gated_app):
    r = gated_app.get("/", follow_redirects=False)
    assert r.status_code == 302
    # Phase 1 (cloud-auto-discovery): with a single interactive provider, an
    # unauthenticated HTML load auto-initiates the OAuth redirect to
    # /auth/login rather than rendering the /login interstitial. The /login
    # page remains the fallback (multiple/zero providers, or loop-guard trip).
    assert r.headers["location"].startswith("/auth/login?provider=stub")


def test_gated_auth_providers_is_public(gated_app):
    r = gated_app.get("/api/auth/providers")
    assert r.status_code == 200
    body = r.json()
    assert any(p["name"] == "stub" for p in body["providers"])
    assert body["providers"][0]["display_name"] == "Stub IdP (test only)"


def test_gated_login_html_is_public_and_lists_providers(gated_app):
    r = gated_app.get("/login")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "Stub IdP" in r.text
    assert 'href="/auth/login?provider=stub"' in r.text


def test_gated_static_asset_path_is_public(gated_app):
    """``/assets/*`` is allowlisted so the SPA's CSS/JS loads pre-login."""
    r = gated_app.get("/assets/_nonexistent.css")
    # 404 not 401 — proves middleware let the request through to the
    # static-files mount, which then 404'd because the file isn't there.
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# OAuth round trip
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/chat", "/chat/files"])
def test_authenticated_chat_document_schedules_owner_warmup(
    gated_app, monkeypatch, path
):
    from hermes_cli.owner_worker import readiness

    _complete_stub_login(gated_app)
    scheduled = []
    monkeypatch.setattr(
        readiness,
        "schedule_owner_worker_warmup",
        lambda app, *, owner, supervisor=None: scheduled.append((app, owner, supervisor)),
    )

    response = gated_app.get(
        path,
        headers={"Accept": "text/html", "Sec-Fetch-Dest": "document"},
    )

    assert response.status_code in (200, 404)
    assert len(scheduled) == 1
    app, owner, supervisor = scheduled[0]
    assert app is web_server.app
    assert owner.owner_key.startswith("ok1_")
    assert supervisor is None


@pytest.mark.parametrize(
    ("method", "path", "headers"),
    [
        ("GET", "/api/auth/me", {"Accept": "application/json"}),
        ("GET", "/sessions", {"Accept": "text/html", "Sec-Fetch-Dest": "document"}),
        ("GET", "/chat", {"Accept": "application/json", "Sec-Fetch-Dest": "empty"}),
        ("POST", "/chat", {"Accept": "text/html", "Sec-Fetch-Dest": "document"}),
    ],
)
def test_non_chat_documents_do_not_schedule_owner_warmup(
    gated_app, monkeypatch, method, path, headers
):
    from hermes_cli.owner_worker import readiness

    _complete_stub_login(gated_app)
    scheduled = []
    monkeypatch.setattr(
        readiness,
        "schedule_owner_worker_warmup",
        lambda *args, **kwargs: scheduled.append((args, kwargs)),
    )

    gated_app.request(method, path, headers=headers)

    assert scheduled == []


def test_chat_warmup_requires_successful_session_authorization(gated_app, monkeypatch):
    from hermes_cli.owner_worker import readiness

    scheduled = []
    monkeypatch.setattr(
        readiness,
        "schedule_owner_worker_warmup",
        lambda *args, **kwargs: scheduled.append((args, kwargs)),
    )

    response = gated_app.get(
        "/chat",
        headers={"Accept": "text/html", "Sec-Fetch-Dest": "document"},
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert scheduled == []


def test_chat_warmup_scheduling_failure_does_not_change_document_response(
    gated_app, monkeypatch, caplog
):
    from hermes_cli.owner_worker import readiness

    _complete_stub_login(gated_app)

    def fail_schedule(*_args, **_kwargs):
        raise RuntimeError("warmup unavailable")

    monkeypatch.setattr(readiness, "schedule_owner_worker_warmup", fail_schedule)

    with caplog.at_level("WARNING", logger="hermes_cli.dashboard_auth.middleware"):
        response = gated_app.get(
            "/chat",
            headers={"Accept": "text/html", "Sec-Fetch-Dest": "document"},
        )

    assert response.status_code in (200, 404)
    assert "warmup scheduling failed error_type=RuntimeError" in caplog.text
    assert "warmup unavailable" not in caplog.text


def test_full_login_round_trip_unlocks_gated_api(gated_app):
    # 1) Click "Sign in with Stub IdP" — /auth/login redirects to the stub
    #    with a PKCE cookie on the response.
    r1 = gated_app.get("/auth/login?provider=stub", follow_redirects=False)
    assert r1.status_code == 302
    pkce = next(
        (c for c in r1.headers.get_list("set-cookie")
         if "hermes_session_pkce" in c),
        None,
    )
    assert pkce and "HttpOnly" in pkce

    redirect = r1.headers["location"]
    # Stub bounces back to {redirect_uri}?code=stub_code&state=<s>
    assert "code=stub_code" in redirect
    assert "state=" in redirect
    state = redirect.split("state=")[1]

    # 2) The browser would now follow the redirect to /auth/callback.
    #    TestClient automatically carries the PKCE cookie forward.
    r2 = gated_app.get(
        f"/auth/callback?code=stub_code&state={state}",
        follow_redirects=False,
    )
    assert r2.status_code == 302
    assert r2.headers["location"] == "/"
    set_cookies = r2.headers.get_list("set-cookie")
    assert any("hermes_session_at" in c for c in set_cookies)
    assert any("hermes_session_rt" in c for c in set_cookies)

    # 3) A gated control-plane auth route now succeeds because we have a valid
    #    session cookie. Avoid owner-worker-routed routes here; this fixture does
    #    not start a real owner worker supervisor.
    r3 = gated_app.get("/api/auth/me")
    assert r3.status_code == 200, (
        f"Expected 200 for /api/auth/me post-login, got {r3.status_code}: "
        f"{r3.text}"
    )


def _complete_stub_login(client) -> None:
    """Walk the stub OAuth round trip so ``client`` carries a valid session.

    TestClient persists Set-Cookie across calls, so after this returns the
    client's cookie jar holds ``hermes_session_at`` / ``hermes_session_rt``
    and subsequent gated requests authenticate.
    """
    r1 = client.get("/auth/login?provider=stub", follow_redirects=False)
    assert r1.status_code == 302
    state = r1.headers["location"].split("state=")[1]
    r2 = client.get(
        f"/auth/callback?code=stub_code&state={state}",
        follow_redirects=False,
    )
    assert r2.status_code == 302


def test_gated_require_token_endpoint_rejects_cookie_session_in_owner_mode(gated_app):
    """Owner-authenticated management APIs must not run in Control Plane.

    These legacy ``_require_token`` endpoints read/write global dashboard state.
    In authenticated owner mode they now fail closed until each surface is
    explicitly routed through an Owner Worker.
    """
    _complete_stub_login(gated_app)
    r = gated_app.post(
        "/api/dashboard/agent-plugins/install",
        json={"identifier": "definitely not a valid identifier",
              "force": False, "enable": False},
    )
    assert r.status_code == 403
    assert "Owner Worker" in r.text


def test_gated_require_token_endpoint_still_rejects_no_cookie(gated_app):
    """The gate must still 401 a ``_require_token`` endpoint with no session.

    The fix defers to the gate — it does not make these endpoints public. A
    request with no cookie is rejected by ``gated_auth_middleware`` before the
    handler runs, so the install endpoint stays protected.
    """
    r = gated_app.post(
        "/api/dashboard/agent-plugins/install",
        json={"identifier": "owner/repo", "force": False, "enable": False},
    )
    assert r.status_code == 401, (
        f"Expected 401 for an unauthenticated install POST under the gate, "
        f"got {r.status_code}: {r.text}"
    )


# A representative spread of the OTHER ``_require_token`` endpoints (there are
# 14 in total). The install popup was just the reported symptom; the same bug
# made API-key reveal, provider validation, the OAuth-provider connect flow,
# and the rest of plugin management unreachable behind the gate. Each entry is
# (method, path, json_body); we assert only that a logged-in request is NOT
# 401'd — i.e. it cleared the auth layer and reached the handler. The
# handler's own status (400/404/429/etc.) is route-specific and not asserted.
_GATED_REQUIRE_TOKEN_ROUTES = [
    ("get", "/api/dashboard/plugins/hub", None),
    ("post", "/api/env/reveal", {"key": "NONEXISTENT_ENV_VAR_FOR_TEST"}),
    ("post", "/api/providers/validate", {"key": "OPENAI_API_KEY", "value": ""}),
    ("delete", "/api/providers/oauth/__not_a_real_provider__", None),
    ("post", "/api/dashboard/agent-plugins/__nope__/enable", None),
]


@pytest.mark.parametrize("method,path,body", _GATED_REQUIRE_TOKEN_ROUTES)
def test_gated_require_token_routes_reject_cookie_session_in_owner_mode(
    gated_app, method, path, body
):
    """Representative ``_require_token`` routes fail closed in owner mode."""
    _complete_stub_login(gated_app)
    kwargs = {"json": body} if body is not None else {}
    r = gated_app.request(method.upper(), path, **kwargs)
    assert r.status_code == 403, (
        f"{method.upper()} {path} should fail closed in authenticated owner mode. "
        f"Body: {r.text}"
    )


@pytest.mark.parametrize(
    ("path", "allowed"),
    [
        ("/api/profiles", False),
        ("/api/config", True),
        ("/api/dashboard/font", True),
        ("/api/dashboard/plugins", True),
        ("/api/skills", True),
        ("/api/skills/content", True),
        ("/api/skills/hub/search", False),
        ("/api/tools/toolsets", True),
        ("/api/sessions", False),
        ("/api/sessions/abc123/messages", False),
        ("/api/sessions/abc123/export", False),
        ("/api/analytics/usage", True),
        ("/api/model/info", True),
        ("/api/model/options", True),
        ("/api/logs", True),
        ("/api/fs/read-data-url", True),
        ("/api/sessions/abc123/unmigrated", False),
        ("/api/sessions-extra", False),
        ("/api/analytics/admin", False),
        ("/api/model/set", False),
        ("/api/profiles/sessions", False),
        ("/api/ops/checkpoints", False),
        ("/api/dashboard/agent-plugins/install", False),
    ],
)
def test_authenticated_api_availability_requires_explicit_owner_worker_routes(path, allowed):
    from hermes_cli.dashboard_auth.api_availability import authenticated_owner_worker_api_allowed

    assert authenticated_owner_worker_api_allowed(path) is allowed


@pytest.mark.parametrize("method", ["GET", "PUT", "DELETE"])
def test_authenticated_employee_avatar_routes_are_control_plane_routes(method):
    from hermes_cli.dashboard_auth.api_availability import (
        AuthenticatedApiBucket,
        classify_authenticated_api,
    )

    decision = classify_authenticated_api(
        "/api/employees/emp-id/avatar",
        method=method,
    )
    assert decision.allowed is True
    assert decision.bucket == AuthenticatedApiBucket.CONTROL_PLANE_AUTH


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/employees"),
        ("POST", "/api/employees"),
        ("GET", "/api/employees/emp-id"),
        ("PUT", "/api/employees/emp-id/profile"),
        (
            "PUT",
            "/api/employees/emp-id/builtin-assistant-personalization",
        ),
        ("PUT", "/api/employees/emp-id/collaboration-policy"),
        ("PUT", "/api/employees/emp-id/lifecycle"),
        ("POST", "/api/employees/emp-id/rollover"),
        ("PUT", "/api/employees/emp-id/channels/feishu"),
        ("PUT", "/api/employees/emp-id/channels/feishu/credentials"),
        ("PUT", "/api/employees/emp-id/channels/feishu/lifecycle"),
        ("POST", "/api/employees/emp-id/channels/feishu/test"),
    ],
)
def test_authenticated_employee_routes_are_control_plane_routes(method, path):
    from hermes_cli.dashboard_auth.api_availability import (
        AuthenticatedApiBucket,
        classify_authenticated_api,
    )

    decision = classify_authenticated_api(path, method=method)
    assert decision.allowed is True
    assert decision.bucket == AuthenticatedApiBucket.CONTROL_PLANE_AUTH


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/employees/emp-id/avatar"),
        ("GET", "/api/employees/emp-id/avatar/private"),
        ("POST", "/api/employees/emp-id/channels/feishu"),
        ("GET", "/api/employees/emp-id/channels/feishu/credentials"),
        ("PUT", "/api/employees/emp-id/channels/feishu/test"),
    ],
)
def test_authenticated_employee_routes_are_method_and_path_exact(method, path):
    from hermes_cli.dashboard_auth.api_availability import (
        AuthenticatedApiBucket,
        classify_authenticated_api,
    )

    decision = classify_authenticated_api(path, method=method)
    assert decision.allowed is False
    assert decision.bucket == AuthenticatedApiBucket.LOCAL_ONLY_OR_UNAVAILABLE


@pytest.mark.parametrize("method", ["GET", "PUT"])
def test_builtin_assistant_policy_is_exact_control_plane_route(method):
    from hermes_cli.dashboard_auth.api_availability import (
        AuthenticatedApiBucket,
        classify_authenticated_api,
    )

    decision = classify_authenticated_api(
        "/api/system/builtin-assistant-policy", method=method
    )
    assert decision.allowed is True
    assert decision.bucket == AuthenticatedApiBucket.CONTROL_PLANE_AUTH
    denied = classify_authenticated_api(
        "/api/system/builtin-assistant-policy/private", method=method
    )
    assert denied.allowed is False


def test_authenticated_webhook_provisioning_is_exact_control_plane_route():
    from hermes_cli.dashboard_auth.api_availability import (
        AuthenticatedApiBucket,
        classify_authenticated_api,
    )

    decision = classify_authenticated_api(
        "/api/messaging/webhook/accounts", method="POST"
    )
    assert decision.allowed is True
    assert decision.bucket == AuthenticatedApiBucket.CONTROL_PLANE_AUTH

    for method, path in (
        ("GET", "/api/messaging/webhook/accounts"),
        ("POST", "/api/messaging/webhook/accounts/private"),
    ):
        denied = classify_authenticated_api(path, method=method)
        assert denied.allowed is False
        assert denied.bucket == AuthenticatedApiBucket.LOCAL_ONLY_OR_UNAVAILABLE


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/profiles"),
        ("GET", "/api/profiles/summary"),
        ("POST", "/api/profiles/summary"),
        ("GET", "/api/profiles/summary/private"),
    ],
)
def test_authenticated_profile_routes_are_unavailable(method, path):
    from hermes_cli.dashboard_auth.api_availability import (
        AuthenticatedApiBucket,
        classify_authenticated_api,
    )

    decision = classify_authenticated_api(path, method=method)
    assert decision.allowed is False
    assert decision.bucket == AuthenticatedApiBucket.LOCAL_ONLY_OR_UNAVAILABLE


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/profiles"),
        ("GET", "/api/profiles/active"),
        ("PUT", "/api/config"),
        ("PUT", "/api/dashboard/font"),
        ("GET", "/api/dashboard/plugins/rescan"),
        ("POST", "/api/dashboard/plugins/example/visibility"),
        ("PATCH", "/api/skills"),
        ("GET", "/api/skills/toggle"),
        ("POST", "/api/skills/content"),
        ("GET", "/api/skills/export"),
        ("GET", "/api/skills/hub/search"),
        ("POST", "/api/logs"),
        ("PUT", "/api/tools/toolsets"),
        ("GET", "/api/tools/toolsets/example/config"),
        ("PATCH", "/api/model/registrations"),
        ("POST", "/api/model/registrations/active"),
    ],
)
def test_authenticated_owner_worker_routes_are_method_and_path_exact(method, path):
    from hermes_cli.dashboard_auth.api_availability import authenticated_owner_worker_api_allowed

    assert authenticated_owner_worker_api_allowed(path, method=method) is False


@pytest.mark.parametrize(
    "path",
    [
        "/api/sessions",
        "/api/sessions/search",
        "/api/sessions/composition",
        "/api/sessions/empty/count",
        "/api/sessions/stats",
        "/api/sessions/session-id",
        "/api/sessions/session-id/latest-descendant",
        "/api/sessions/session-id/messages",
        "/api/sessions/session-id/export",
    ],
)
def test_authenticated_session_reads_have_dedicated_reader_bucket(path):
    from hermes_cli.dashboard_auth.api_availability import (
        AuthenticatedApiBucket,
        authenticated_owner_worker_api_allowed,
        authenticated_session_reader_api_allowed,
        classify_authenticated_api,
    )

    decision = classify_authenticated_api(path, method="GET")
    assert decision.allowed is True
    assert decision.bucket == AuthenticatedApiBucket.SESSION_READER
    assert authenticated_session_reader_api_allowed(path) is True
    assert authenticated_owner_worker_api_allowed(path) is False


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/sessions/bulk-delete"),
        ("DELETE", "/api/sessions/empty"),
        ("PATCH", "/api/sessions/session-id"),
        ("DELETE", "/api/sessions/session-id"),
        ("POST", "/api/sessions/prune"),
    ],
)
def test_authenticated_session_mutations_stay_owner_worker_bound(method, path):
    from hermes_cli.dashboard_auth.api_availability import (
        authenticated_owner_worker_api_allowed,
        authenticated_session_reader_api_allowed,
    )

    assert authenticated_owner_worker_api_allowed(path, method=method) is True
    assert authenticated_session_reader_api_allowed(path, method=method) is False


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/skills"),
        ("POST", "/api/skills"),
        ("DELETE", "/api/skills"),
        ("GET", "/api/skills/content"),
        ("PUT", "/api/skills/content"),
        ("PUT", "/api/skills/toggle"),
    ],
)
def test_authenticated_skill_routes_are_explicit_owner_worker_routes(method, path):
    from hermes_cli.dashboard_auth.api_availability import authenticated_owner_worker_api_allowed

    assert authenticated_owner_worker_api_allowed(path, method=method) is True


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/plugins/scheduled-tasks/jobs"),
        ("POST", "/api/plugins/scheduled-tasks/jobs"),
        ("GET", "/api/plugins/scheduled-tasks/jobs/job-1"),
        ("PUT", "/api/plugins/scheduled-tasks/jobs/job-1"),
        ("DELETE", "/api/plugins/scheduled-tasks/jobs/job-1"),
        ("POST", "/api/plugins/scheduled-tasks/jobs/job-1/pause"),
        ("POST", "/api/plugins/scheduled-tasks/jobs/job-1/resume"),
        ("POST", "/api/plugins/scheduled-tasks/jobs/job-1/trigger"),
        ("GET", "/api/plugins/scheduled-tasks/jobs/job-1/runs"),
        ("GET", "/api/plugins/scheduled-tasks/delivery-targets"),
        ("GET", "/api/plugins/scheduled-tasks/blueprints"),
        ("POST", "/api/plugins/scheduled-tasks/blueprints/instantiate"),
    ],
)
def test_authenticated_cron_routes_are_owner_worker_bound(method, path):
    from hermes_cli.dashboard_auth.api_availability import authenticated_owner_worker_api_allowed

    assert authenticated_owner_worker_api_allowed(path, method=method) is True


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("DELETE", "/api/plugins/scheduled-tasks/jobs"),
        ("POST", "/api/plugins/scheduled-tasks/jobs/job-1"),
        ("PATCH", "/api/plugins/scheduled-tasks/jobs/job-1"),
        ("GET", "/api/plugins/scheduled-tasks/jobs/job-1/pause"),
        ("POST", "/api/plugins/scheduled-tasks/jobs/job-1/runs"),
        ("GET", "/api/plugins/scheduled-tasks/jobs/job-1/private"),
        ("POST", "/api/plugins/scheduled-tasks/delivery-targets"),
    ],
)
def test_authenticated_cron_routes_are_method_and_path_exact(method, path):
    from hermes_cli.dashboard_auth.api_availability import authenticated_owner_worker_api_allowed

    assert authenticated_owner_worker_api_allowed(path, method=method) is False


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/model/registrations"),
        ("GET", "/api/model/registrations/catalog"),
        ("POST", "/api/model/registrations"),
        ("PUT", "/api/model/registrations"),
        ("DELETE", "/api/model/registrations"),
        ("PUT", "/api/model/registrations/active"),
    ],
)
def test_authenticated_model_registration_routes_are_explicit_owner_worker_routes(method, path):
    from hermes_cli.dashboard_auth.api_availability import authenticated_owner_worker_api_allowed

    assert authenticated_owner_worker_api_allowed(path, method=method) is True


def test_login_unknown_provider_returns_404(gated_app):
    r = gated_app.get("/auth/login?provider=nonexistent", follow_redirects=False)
    assert r.status_code == 404


def test_login_non_interactive_provider_returns_404_not_500(gated_app):
    """Regression: a token-only provider (drain) has no login flow, so
    /auth/login?provider=drain-secret must 404 (not 500 on start_login) and it
    must not appear in the /api/auth/providers bootstrap.
    """
    import secrets

    import plugins.dashboard_auth.drain as drain_plugin

    register_provider(
        drain_plugin.DrainSecretProvider(secret=secrets.token_urlsafe(48))
    )

    r = gated_app.get(
        "/auth/login?provider=drain-secret&next=%2F", follow_redirects=False
    )
    assert r.status_code == 404, (
        f"drain-secret login should 404, not 500: {r.status_code} {r.text}"
    )

    bootstrap = gated_app.get("/api/auth/providers")
    assert bootstrap.status_code == 200
    names = {p["name"] for p in bootstrap.json()["providers"]}
    assert "drain-secret" not in names
    assert "stub" in names


def test_callback_without_pkce_cookie_returns_400(gated_app):
    # No prior /auth/login → no PKCE cookie.
    r = gated_app.get(
        "/auth/callback?code=stub_code&state=anything",
        follow_redirects=False,
    )
    assert r.status_code == 400


def test_callback_state_mismatch_returns_400(gated_app):
    # Walk through /auth/login first to plant the PKCE cookie.
    r1 = gated_app.get("/auth/login?provider=stub", follow_redirects=False)
    # ...then pretend the IDP returned a different state.
    r2 = gated_app.get(
        "/auth/callback?code=stub_code&state=WRONG",
        follow_redirects=False,
    )
    assert r2.status_code == 400


def test_callback_invalid_code_returns_400(gated_app):
    r1 = gated_app.get("/auth/login?provider=stub", follow_redirects=False)
    state = r1.headers["location"].split("state=")[1]
    r2 = gated_app.get(
        f"/auth/callback?code=BAD_CODE&state={state}",
        follow_redirects=False,
    )
    assert r2.status_code == 400


# ---------------------------------------------------------------------------
# Cookie validation
# ---------------------------------------------------------------------------


def test_invalid_cookie_returns_401_on_api(gated_app):
    gated_app.cookies.set(SESSION_AT_COOKIE, "garbage-not-a-real-token")
    r = gated_app.get("/api/sessions")
    assert r.status_code == 401


def test_invalid_cookie_redirects_on_html(gated_app):
    gated_app.cookies.set(SESSION_AT_COOKIE, "garbage")
    r = gated_app.get("/", follow_redirects=False)
    assert r.status_code == 302
    # Phase 6: gate carries a ``next=`` so post-login bounces back to /.
    assert r.headers["location"] in ("/login", "/login?next=%2F")


def test_logout_revokes_only_verified_sessions_ticket(gated_app):
    _complete_stub_login(gated_app)
    minted = gated_app.post(
        "/api/auth/ws-ticket", json={"audience": "browser-ws:/api/ws"}
    )
    assert minted.status_code == 200

    logged_out = gated_app.post("/auth/logout", follow_redirects=False)

    assert logged_out.status_code == 302
    with pytest.raises(TicketInvalid, match="session_revoked"):
        consume_ticket(minted.json()["ticket"])


def test_logout_without_a_trusted_session_is_idempotent(gated_app):
    first = gated_app.post("/auth/logout", follow_redirects=False)
    second = gated_app.post("/auth/logout", follow_redirects=False)

    assert first.status_code == 302
    assert second.status_code == 302


def test_provider_outage_logout_does_not_revoke_unverified_cookie_ticket(gated_app, monkeypatch):
    _complete_stub_login(gated_app)
    minted = gated_app.post(
        "/api/auth/ws-ticket", json={"audience": "browser-ws:/api/ws"}
    )
    assert minted.status_code == 200

    from hermes_cli.dashboard_auth import get_provider
    from hermes_cli.dashboard_auth.base import ProviderError

    provider = get_provider("stub")
    assert provider is not None
    monkeypatch.setattr(
        provider,
        "verify_session",
        lambda *, access_token: (_ for _ in ()).throw(ProviderError("unavailable")),
    )

    response = gated_app.post("/auth/logout", follow_redirects=False)

    assert response.status_code == 302
    assert consume_ticket(minted.json()["ticket"])["user_id"] == "stub-user-1"


def test_membership_transition_revokes_old_ticket_and_accepts_new_ticket(gated_app, monkeypatch):
    _complete_stub_login(gated_app)
    old_ticket = gated_app.post(
        "/api/auth/ws-ticket", json={"audience": "browser-ws:/api/ws"}
    ).json()["ticket"]

    from hermes_cli.dashboard_auth import get_provider

    provider = get_provider("stub")
    assert provider is not None
    original = provider.authorization_state
    access_token = next(
        value.strip('"')
        for name, value in dict(gated_app.cookies).items()
        if name.endswith("hermes_session_at")
    )
    session_id, _revision = original(
        provider.verify_session(access_token=access_token)
    )
    monkeypatch.setattr(
        provider, "authorization_state", lambda _session: (session_id, "membership-v2")
    )

    assert gated_app.get("/api/auth/me").status_code == 200
    with pytest.raises(TicketInvalid, match="membership_revision_mismatch"):
        consume_ticket(old_ticket)

    new_ticket = gated_app.post(
        "/api/auth/ws-ticket", json={"audience": "browser-ws:/api/ws"}
    ).json()["ticket"]
    assert consume_ticket(new_ticket)["membership_revision"] == "membership-v2"


def test_logout_clears_cookies_and_redirects_to_login(gated_app):
    # First log in.
    r1 = gated_app.get("/auth/login?provider=stub", follow_redirects=False)
    state = r1.headers["location"].split("state=")[1]
    gated_app.get(
        f"/auth/callback?code=stub_code&state={state}",
        follow_redirects=False,
    )
    # Now log out.
    r = gated_app.post("/auth/logout", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/login"
    set_cookies = r.headers.get_list("set-cookie")
    assert any(
        c.startswith("hermes_session_at=") and "Max-Age=0" in c
        for c in set_cookies
    )
    assert any(
        c.startswith("hermes_session_rt=") and "Max-Age=0" in c
        for c in set_cookies
    )


# ---------------------------------------------------------------------------
# Identity probe
# ---------------------------------------------------------------------------


def test_api_auth_me_returns_session_after_login(gated_app):
    r1 = gated_app.get("/auth/login?provider=stub", follow_redirects=False)
    state = r1.headers["location"].split("state=")[1]
    gated_app.get(
        f"/auth/callback?code=stub_code&state={state}",
        follow_redirects=False,
    )
    r = gated_app.get("/api/auth/me")
    assert r.status_code == 200
    body = r.json()
    assert body["user_id"] == "stub-user-1"
    assert body["email"] == "stub@example.test"
    assert body["display_name"] == "Stub User"
    assert body["provider"] == "stub"
    assert body["org_id"] == "stub-org-1"
    assert body["tenant_id"] == "stub-org-1"
    assert body["owner_key"].startswith("ok1_")
    assert body["isolation_mode"] == "owner_worker"
    assert body["legacy_sessions_imported"] is False
    assert "legacy local sessions" in body["legacy_sessions_message"]
    assert "owner_home" not in body
    assert "expires_at" in body


def test_api_auth_me_requires_auth(gated_app):
    # No cookies.
    r = gated_app.get("/api/auth/me")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Zero-providers fail-closed
# ---------------------------------------------------------------------------


def test_gated_model_info_is_not_public(gated_app):
    r = gated_app.get("/api/model/info", follow_redirects=False)
    assert r.status_code in (302, 401)


def test_gated_dashboard_plugins_manifest_is_not_public(gated_app):
    r = gated_app.get("/api/dashboard/plugins", follow_redirects=False)
    assert r.status_code in (302, 401)


def test_gated_zero_providers_fails_closed_on_api_auth_providers():
    """If gate is on but no providers are registered, /api/auth/providers 503s."""
    clear_providers()
    prev_required = getattr(web_server.app.state, "auth_required", None)
    prev_host = getattr(web_server.app.state, "bound_host", None)
    web_server.app.state.bound_host = "fly-app.fly.dev"
    web_server.app.state.auth_required = True
    try:
        client = TestClient(web_server.app, base_url="https://fly-app.fly.dev")
        r = client.get("/api/auth/providers")
        assert r.status_code == 503
        assert "no auth providers" in r.text.lower()
    finally:
        web_server.app.state.auth_required = prev_required
        web_server.app.state.bound_host = prev_host


def test_gated_zero_providers_login_page_renders_help_text():
    clear_providers()
    prev_required = getattr(web_server.app.state, "auth_required", None)
    prev_host = getattr(web_server.app.state, "bound_host", None)
    web_server.app.state.bound_host = "fly-app.fly.dev"
    web_server.app.state.auth_required = True
    try:
        client = TestClient(web_server.app, base_url="https://fly-app.fly.dev")
        r = client.get("/login")
        assert r.status_code == 200
        # Empty-provider HTML mentions the fix-up path.  (HTML wraps text
        # so we can't grep for the exact phrase; check for the canonical
        # fragments instead.)
        text = r.text.lower()
        assert "sign-in unavailable" in text
        assert "no authentication" in text
        assert "providers are installed" in text
        assert "--insecure" in text
    finally:
        web_server.app.state.auth_required = prev_required
        web_server.app.state.bound_host = prev_host


# ---------------------------------------------------------------------------
# Multi-provider verify: a ProviderError from one provider must not abort the
# chain when another provider can verify the token.
# ---------------------------------------------------------------------------


class _UnreachableProvider(StubAuthProvider):
    """A provider whose IDP is unreachable: verify_session always raises.

    Models the real-world bug — a self-hosted-OIDC session hits the ``nous``
    provider first, which tries to reach Nous Portal's JWKS; if that's
    unreachable ``nous`` raises ProviderError. The gate must keep trying the
    remaining providers rather than 503-ing the whole request.
    """

    name = "unreachable"
    display_name = "Unreachable IdP (test only)"

    def verify_session(self, *, access_token: str):
        from hermes_cli.dashboard_auth.base import ProviderError

        raise ProviderError("simulated: IDP/JWKS unreachable")

    def refresh_session(self, *, refresh_token: str):
        from hermes_cli.dashboard_auth.base import ProviderError

        raise ProviderError("simulated: IDP/JWKS unreachable")


def _mint_stub_at(stub: StubAuthProvider) -> str:
    """Mint a valid access-token cookie value from a StubAuthProvider via its
    own login round trip (so the HMAC signature matches what verify expects)."""
    ls = stub.start_login(redirect_uri="https://fly-app.fly.dev/auth/callback")
    state = dict(
        seg.split("=", 1)
        for seg in ls.cookie_payload["hermes_session_pkce"].split(";")
        if "=" in seg
    )["state"]
    verifier = dict(
        seg.split("=", 1)
        for seg in ls.cookie_payload["hermes_session_pkce"].split(";")
        if "=" in seg
    )["verifier"]
    session = stub.complete_login(
        code="stub_code",
        state=state,
        code_verifier=verifier,
        redirect_uri="https://fly-app.fly.dev/auth/callback",
    )
    return session.access_token


@pytest.fixture
def _gated_state():
    """Bare gated app-state setup WITHOUT registering any provider, so each
    test controls provider registration order itself. Yields a factory that
    builds the TestClient after providers are registered."""
    clear_providers()
    prev_host = getattr(web_server.app.state, "bound_host", None)
    prev_port = getattr(web_server.app.state, "bound_port", None)
    prev_required = getattr(web_server.app.state, "auth_required", None)
    web_server.app.state.bound_host = "fly-app.fly.dev"
    web_server.app.state.bound_port = 443
    web_server.app.state.auth_required = True

    def _client() -> TestClient:
        return TestClient(web_server.app, base_url="https://fly-app.fly.dev")

    yield _client
    clear_providers()
    web_server.app.state.bound_host = prev_host
    web_server.app.state.bound_port = prev_port
    web_server.app.state.auth_required = prev_required


def test_unreachable_first_provider_does_not_block_second(_gated_state):
    """An unreachable provider registered FIRST must not 503 a request whose
    token a later provider can verify.

    Regression for the stacked-provider bug: the verify loop used to return
    503 on the first provider's ProviderError, before the working provider
    ever got a turn. Now it logs, continues, and the working provider wins.
    """
    working = StubAuthProvider()
    register_provider(_UnreachableProvider())  # registered first → tried first
    register_provider(working)                  # the one that can verify

    at = _mint_stub_at(working)
    client = _gated_state()
    client.cookies.set(SESSION_AT_COOKIE, at)
    r = client.get("/api/auth/me")
    assert r.status_code == 200, (
        f"Expected the working provider to verify the session despite the "
        f"unreachable one being tried first; got {r.status_code}: {r.text}"
    )
    body = r.json()
    assert body["provider"] == "stub"
    assert body["user_id"] == "stub-user-1"


def test_all_providers_unreachable_returns_503(_gated_state):
    """If NO provider can verify the token AND at least one was unreachable,
    surface 503 (transient outage) rather than forcing a needless re-login."""
    register_provider(_UnreachableProvider())
    client = _gated_state()
    # Any non-empty cookie — the unreachable provider raises before parsing.
    client.cookies.set(SESSION_AT_COOKIE, "some-opaque-token")
    r = client.get("/api/auth/me")
    assert r.status_code == 503
    assert "unreachable" in r.text.lower()


def test_unverifiable_token_with_reachable_providers_redirects(_gated_state):
    """When every provider is REACHABLE but none recognises the token (all
    return None, none raises), the gate falls through to re-login — NOT 503."""
    register_provider(StubAuthProvider())
    client = _gated_state()
    client.cookies.set(SESSION_AT_COOKIE, "garbage-not-a-real-token")
    # API path → 401; HTML would 302. Either way, NOT 503.
    r = client.get("/api/auth/me")
    assert r.status_code == 401
    assert "unreachable" not in r.text.lower()


# ---------------------------------------------------------------------------
# Plugin-API bucket — cookie-auth frontend routes that bypass the gate
# ---------------------------------------------------------------------------


def test_gated_plugin_api_allowed_path_is_not_403_by_owner_gate(gated_app):
    """Allowlisted plugin paths must not be rejected by the fail-closed
    authenticated owner gate. The actual plugin router may not be mounted
    in this fixture, so anything other than 403 from the gate is acceptable."""
    _complete_stub_login(gated_app)
    r = gated_app.get("/api/plugins/kanban/boards", follow_redirects=False)
    assert r.status_code != 403, (
        f"Expected the gate to allow /api/plugins/kanban/boards, got "
        f"{r.status_code}: {r.text}"
    )


def test_gated_unknown_plugin_path_is_still_403_by_owner_gate(gated_app):
    """Unregistered plugin routes retain the gate's fail-closed semantics."""
    _complete_stub_login(gated_app)
    r = gated_app.get("/api/plugins/not-kanban/boards", follow_redirects=False)
    assert r.status_code == 403
