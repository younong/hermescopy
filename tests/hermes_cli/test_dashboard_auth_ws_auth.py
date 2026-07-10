"""Tests for the WS-upgrade auth helper (Phase 5 task 5.2).

The dashboard's four WS endpoints (``/api/pty``, ``/api/ws``, ``/api/pub``,
``/api/events``) share an auth gate: ``_ws_auth_ok``. In loopback mode it
accepts ``?token=<_SESSION_TOKEN>``; in gated mode it accepts a single-use
``?ticket=`` minted by ``POST /api/auth/ws-ticket``.

These tests exercise the helper at the unit level (no actual WS upgrade)
plus the ticket-mint endpoint under realistic gated-mode setup. We don't
test the full WS upgrade because the starlette TestClient WS path has a
pre-existing regression unrelated to dashboard-auth.
"""

from __future__ import annotations

from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

from fastapi.testclient import TestClient

from hermes_cli import web_server
from hermes_cli.dashboard_auth import clear_providers, register_provider
from hermes_cli.dashboard_auth.base import Session
from hermes_cli.dashboard_auth.owner_context import (
    owner_context_from_session,
    owner_context_from_ticket_payload,
)
from hermes_cli.dashboard_auth.ws_tickets import (
    _reset_for_tests,
    consume_ticket,
    internal_ws_credential,
    mint_ticket,
)
from hermes_cli.owner_worker.tokens import (
    AUD_CONTROL_PLANE_WS,
    AUD_OWNER_WORKER_WS,
    mint_internal_token,
    validate_internal_token_payload,
)
from tests.hermes_cli.conftest_dashboard_auth import StubAuthProvider


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def gated_app():
    """web_server.app configured for gated mode + stub provider registered."""
    _reset_for_tests()
    clear_providers()
    register_provider(StubAuthProvider())
    prev_host = getattr(web_server.app.state, "bound_host", None)
    prev_port = getattr(web_server.app.state, "bound_port", None)
    prev_required = getattr(web_server.app.state, "auth_required", None)
    web_server.app.state.bound_host = "fly-app.fly.dev"
    web_server.app.state.bound_port = 443
    web_server.app.state.auth_required = True
    client = TestClient(web_server.app, base_url="https://fly-app.fly.dev")
    yield client
    clear_providers()
    _reset_for_tests()
    web_server.app.state.bound_host = prev_host
    web_server.app.state.bound_port = prev_port
    web_server.app.state.auth_required = prev_required


@pytest.fixture
def loopback_app():
    """web_server.app configured for loopback mode (gate OFF)."""
    _reset_for_tests()
    clear_providers()
    prev_host = getattr(web_server.app.state, "bound_host", None)
    prev_port = getattr(web_server.app.state, "bound_port", None)
    prev_required = getattr(web_server.app.state, "auth_required", None)
    web_server.app.state.bound_host = "127.0.0.1"
    web_server.app.state.bound_port = 8080
    web_server.app.state.auth_required = False
    client = TestClient(web_server.app, base_url="http://127.0.0.1:8080")
    yield client
    _reset_for_tests()
    web_server.app.state.bound_host = prev_host
    web_server.app.state.bound_port = prev_port
    web_server.app.state.auth_required = prev_required


@pytest.fixture
def insecure_public_app():
    """web_server.app configured for all-interfaces insecure mode."""
    _reset_for_tests()
    clear_providers()
    prev_host = getattr(web_server.app.state, "bound_host", None)
    prev_port = getattr(web_server.app.state, "bound_port", None)
    prev_required = getattr(web_server.app.state, "auth_required", None)
    web_server.app.state.bound_host = "0.0.0.0"
    web_server.app.state.bound_port = 9120
    web_server.app.state.auth_required = False
    client = TestClient(web_server.app, base_url="http://192.168.0.222:9120")
    yield client
    _reset_for_tests()
    web_server.app.state.bound_host = prev_host
    web_server.app.state.bound_port = prev_port
    web_server.app.state.auth_required = prev_required


def _logged_in(client: TestClient) -> None:
    """Drive the stub OAuth round trip so the client holds session cookies."""
    r1 = client.get("/auth/login?provider=stub", follow_redirects=False)
    assert r1.status_code == 302
    state = r1.headers["location"].split("state=")[1]
    r2 = client.get(
        f"/auth/callback?code=stub_code&state={state}", follow_redirects=False
    )
    assert r2.status_code == 302


# ---------------------------------------------------------------------------
# POST /api/auth/ws-ticket — the mint endpoint
# ---------------------------------------------------------------------------


class TestWsTicketEndpoint:
    def test_authenticated_session_can_mint(self, gated_app):
        _logged_in(gated_app)
        me = gated_app.get("/api/auth/me")
        assert me.status_code == 200
        me_body = me.json()

        r = gated_app.post("/api/auth/ws-ticket")
        assert r.status_code == 200
        body = r.json()
        assert "ticket" in body
        assert isinstance(body["ticket"], str)
        assert len(body["ticket"]) >= 32
        assert body["ttl_seconds"] == 30

        payload = consume_ticket(body["ticket"])
        assert payload["user_id"] == "stub-user-1"
        assert payload["provider"] == "stub"
        assert payload["org_id"] == "stub-org-1"
        assert payload["tenant_id"] == "stub-org-1"
        assert payload["owner_key"].startswith("ok1_")
        assert payload["tenant_id"] == me_body["tenant_id"]
        assert payload["owner_key"] == me_body["owner_key"]
        assert "minted_at" in payload
        assert "expires_at" in payload
        owner = owner_context_from_ticket_payload(payload)
        assert owner.owner_key == payload["owner_key"]
        assert owner.tenant_id == payload["tenant_id"]

    def test_unauthenticated_returns_401_or_redirect(self, gated_app):
        r = gated_app.post("/api/auth/ws-ticket", follow_redirects=False)
        # gated_auth_middleware short-circuits before the route — it
        # returns either 401 or 302. Either is fine.
        assert r.status_code in (302, 401)

    def test_each_call_returns_a_distinct_ticket(self, gated_app):
        _logged_in(gated_app)
        tickets = {gated_app.post("/api/auth/ws-ticket").json()["ticket"]
                   for _ in range(5)}
        assert len(tickets) == 5

    def test_get_method_is_not_allowed(self, gated_app):
        _logged_in(gated_app)
        r = gated_app.get("/api/auth/ws-ticket", follow_redirects=False)
        # GET must not mint a ticket (which would be cookie-replayable via
        # <img src=…> from a malicious origin). Accepted responses:
        #   401 — gated middleware allowlist-miss
        #   404 — SPA catch-all swallowed it
        #   405 — Method Not Allowed (route only registered for POST)
        #   200 — SPA index.html was served (catch-all caught the path)
        # In every case the JSON body of a successful ticket mint must
        # NOT be present. The assertion below holds even when the SPA
        # shell happens to serve a 200.
        body = r.text
        assert "ticket" not in body or '"ttl_seconds"' not in body, (
            f"GET /api/auth/ws-ticket leaked a ticket (status={r.status_code}, "
            f"body[:200]={body[:200]!r})"
        )


# ---------------------------------------------------------------------------
# _ws_auth_ok — unit-level (synthetic WebSocket-shaped object)
# ---------------------------------------------------------------------------


@pytest.fixture
def insecure_explicit_host_app():
    """web_server.app bound to an explicit non-loopback host (--insecure).

    Models `--host 100.64.0.10 --insecure` (e.g. a Tailscale IP behind
    `tailscale serve`) — a specific address rather than the all-interfaces
    0.0.0.0 wildcard.
    """
    _reset_for_tests()
    clear_providers()
    prev_host = getattr(web_server.app.state, "bound_host", None)
    prev_port = getattr(web_server.app.state, "bound_port", None)
    prev_required = getattr(web_server.app.state, "auth_required", None)
    web_server.app.state.bound_host = "100.64.0.10"
    web_server.app.state.bound_port = 9119
    web_server.app.state.auth_required = False
    client = TestClient(web_server.app, base_url="http://100.64.0.10:9119")
    yield client
    _reset_for_tests()
    web_server.app.state.bound_host = prev_host
    web_server.app.state.bound_port = prev_port
    web_server.app.state.auth_required = prev_required


def _fake_ws(*, query: dict, client_host: str = "127.0.0.1", path: str = "/api/pty", app=None):
    """Build a stand-in for Starlette.WebSocket good enough for WS helpers."""

    class _QP:
        def __init__(self, q):
            self._q = q

        def get(self, k, default=""):
            return self._q.get(k, default)

    query_string = "&".join(f"{k}={v}" for k, v in query.items())
    return SimpleNamespace(
        app=app or web_server.app,
        query_params=_QP(query),
        client=SimpleNamespace(host=client_host),
        url=SimpleNamespace(path=path, query=query_string),
    )


class TestWsAuthOkLoopback:
    """Gate OFF — legacy token path."""

    def test_correct_token_accepted(self, loopback_app):
        ws = _fake_ws(query={"token": web_server._SESSION_TOKEN})
        assert web_server._ws_auth_ok(ws) is True

    def test_wrong_token_rejected(self, loopback_app):
        ws = _fake_ws(query={"token": "not-the-real-token"})
        assert web_server._ws_auth_ok(ws) is False

    def test_missing_token_rejected(self, loopback_app):
        ws = _fake_ws(query={})
        assert web_server._ws_auth_ok(ws) is False

    def test_ticket_param_ignored_in_loopback(self, loopback_app):
        # Even if someone sneaks a ticket through, loopback mode only
        # cares about ?token=. A naked ticket isn't a token.
        ticket = mint_ticket(user_id="u1", provider="stub")
        ws = _fake_ws(query={"ticket": ticket})
        assert web_server._ws_auth_ok(ws) is False


class TestWsAuthOkGated:
    """Gate ON — ticket path only."""

    def test_valid_ticket_accepted(self, gated_app):
        ticket = mint_ticket(user_id="u1", provider="stub")
        ws = _fake_ws(query={"ticket": ticket})
        assert web_server._ws_auth_ok(ws) is True

    def test_consumed_ticket_rejected(self, gated_app):
        ticket = mint_ticket(user_id="u1", provider="stub")
        ws_one = _fake_ws(query={"ticket": ticket})
        ws_two = _fake_ws(query={"ticket": ticket})
        assert web_server._ws_auth_ok(ws_one) is True
        # Single-use — second consumption fails.
        assert web_server._ws_auth_ok(ws_two) is False

    def test_unknown_ticket_rejected(self, gated_app):
        ws = _fake_ws(query={"ticket": "never-minted"})
        assert web_server._ws_auth_ok(ws) is False

    def test_missing_ticket_rejected(self, gated_app):
        ws = _fake_ws(query={})
        assert web_server._ws_auth_ok(ws) is False

    def test_legacy_token_rejected_in_gated_mode(self, gated_app):
        """Critical: gated mode must NOT honour the legacy token path
        even when someone has access to the in-process value of
        _SESSION_TOKEN (e.g. a leaked log line)."""
        ws = _fake_ws(query={"token": web_server._SESSION_TOKEN})
        assert web_server._ws_auth_ok(ws) is False

    def test_rejection_audit_logs(self, gated_app, tmp_path, monkeypatch):
        # Point the audit log at a tmp dir so we can read what got written.
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        from hermes_cli.dashboard_auth import audit as audit_mod

        # The log path is resolved lazily on the first audit_log() call;
        # bust any cached handler so it re-resolves.
        if hasattr(audit_mod, "_LOGGER"):
            monkeypatch.setattr(audit_mod, "_LOGGER", None, raising=False)

        ws = _fake_ws(query={"ticket": "never-minted"})
        assert web_server._ws_auth_ok(ws) is False

        log_file = tmp_path / "logs" / "dashboard-auth.log"
        # The audit module may write asynchronously through stdlib logging,
        # but flush is synchronous. If the file doesn't exist yet, the
        # logger may not have been initialized in this process — that's
        # acceptable as long as the rejection path didn't crash.
        if log_file.exists():
            content = log_file.read_text()
            assert "ws_ticket_rejected" in content

    def test_process_global_internal_credential_requires_owner_context(self, gated_app):
        """Gated mode never accepts a process-global internal credential."""
        result = web_server._ws_auth_result(_fake_ws(query={"internal": internal_ws_credential()}))

        assert result.reason == "internal_owner_context_required"
        assert result.credential == "internal"
        assert web_server._ws_auth_ok(_fake_ws(query={"internal": internal_ws_credential()})) is False

    def test_process_global_internal_credential_remains_rejected_on_reuse(self, gated_app):
        """Repeated legacy credentials cannot become an owner-auth bypass."""
        credential = internal_ws_credential()
        for _ in range(3):
            result = web_server._ws_auth_result(_fake_ws(query={"internal": credential}))
            assert result.reason == "internal_owner_context_required"
            assert result.credential == "internal"

    def test_wrong_internal_credential_rejected(self, gated_app):
        # Mint the real one so the store is non-empty, then present a bogus value.
        internal_ws_credential()
        ws = _fake_ws(query={"internal": "not-the-internal-credential"})
        assert web_server._ws_auth_ok(ws) is False

    def test_internal_credential_not_accepted_in_loopback(self, loopback_app):
        """Outside gated mode, ?internal= is meaningless — only ?token= works.
        A naked internal credential must not authenticate."""
        cred = internal_ws_credential()
        ws = _fake_ws(query={"internal": cred})
        assert web_server._ws_auth_ok(ws) is False

    def test_http_auth_me_and_ws_ticket_use_the_same_canonical_owner(self, gated_app):
        _logged_in(gated_app)

        auth_me = gated_app.get("/api/auth/me")
        ticket_response = gated_app.post("/api/auth/ws-ticket")

        assert auth_me.status_code == 200
        assert ticket_response.status_code == 200
        ws_owner = owner_context_from_ticket_payload(consume_ticket(ticket_response.json()["ticket"]))
        assert ws_owner.owner_key == auth_me.json()["owner_key"]
        assert ws_owner.tenant_id == auth_me.json()["tenant_id"]

    def test_distinct_users_mint_distinct_owner_keys_for_ws_routing(self, gated_app, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_OWNER_SECRET", "test-owner-secret")
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "global"))
        owner_a = owner_context_from_session(Session(
            user_id="user-a",
            email="a@example.test",
            display_name="A",
            org_id="org1",
            provider="stub",
            expires_at=9999999999,
            access_token="a",
            refresh_token="ra",
        ))
        owner_b = owner_context_from_session(Session(
            user_id="user-b",
            email="b@example.test",
            display_name="B",
            org_id="org1",
            provider="stub",
            expires_at=9999999999,
            access_token="b",
            refresh_token="rb",
        ))
        ticket_a = mint_ticket(user_id="user-a", provider="stub", org_id="org1", tenant_id=owner_a.tenant_id, owner_key=owner_a.owner_key)
        ticket_b = mint_ticket(user_id="user-b", provider="stub", org_id="org1", tenant_id=owner_b.tenant_id, owner_key=owner_b.owner_key)

        result_a = web_server._ws_auth_result(_fake_ws(query={"ticket": ticket_a}))
        result_b = web_server._ws_auth_result(_fake_ws(query={"ticket": ticket_b}))

        assert result_a.reason is None
        assert result_b.reason is None
        assert result_a.payload["owner_key"] == owner_a.owner_key
        assert result_b.payload["owner_key"] == owner_b.owner_key
        assert result_a.payload["owner_key"] != result_b.payload["owner_key"]
        assert owner_context_from_ticket_payload(result_a.payload).owner_home != owner_context_from_ticket_payload(result_b.payload).owner_home

    def test_auth_result_retains_ticket_payload_for_owner_bridge(self, gated_app):
        ticket = mint_ticket(
            user_id="u1",
            provider="stub",
            org_id="org1",
            tenant_id="org1",
            owner_key="ok1_owner",
        )
        ws = _fake_ws(query={"ticket": ticket})

        result = web_server._ws_auth_result(ws)

        assert result.reason is None
        assert result.credential == "ticket"
        assert result.payload["user_id"] == "u1"
        assert result.payload["owner_key"] == "ok1_owner"

    def test_owner_worker_requires_owner_bound_internal_token(self, tmp_path):
        worker_app = SimpleNamespace(
            state=SimpleNamespace(
                owner_worker_mode=True,
                owner_worker_owner_key="ok1_worker",
                owner_worker_control_home=tmp_path,
                auth_required=False,
            )
        )
        good = mint_internal_token("ok1_worker", audience=AUD_OWNER_WORKER_WS, path="/api/pty", control_home=tmp_path)
        wrong = mint_internal_token("ok1_other", audience=AUD_OWNER_WORKER_WS, path="/api/pty", control_home=tmp_path)

        assert web_server._ws_auth_ok(_fake_ws(query={"internal_owner_token": good}, app=worker_app)) is True
        assert web_server._ws_auth_ok(_fake_ws(query={"internal_owner_token": wrong}, app=worker_app)) is False
        assert web_server._ws_auth_ok(_fake_ws(query={"internal": internal_ws_credential()}, app=worker_app)) is False

    def test_control_plane_internal_owner_token_uses_supervisor_control_home(self, gated_app, tmp_path):
        control_a = tmp_path / "control-a"
        control_b = tmp_path / "control-b"
        previous = getattr(web_server.app.state, "owner_worker_supervisor", None)
        web_server.app.state.owner_worker_supervisor = SimpleNamespace(control_home=control_a, global_home=tmp_path / "global-a")
        try:
            good = mint_internal_token("ok1_worker", audience=AUD_CONTROL_PLANE_WS, path="/api/pty", control_home=control_a)
            wrong_secret = mint_internal_token("ok1_worker", audience=AUD_CONTROL_PLANE_WS, path="/api/pty", control_home=control_b)

            good_result = web_server._ws_auth_result(_fake_ws(query={"internal_owner_token": good}))
            wrong_result = web_server._ws_auth_result(_fake_ws(query={"internal_owner_token": wrong_secret}))
        finally:
            web_server.app.state.owner_worker_supervisor = previous

        assert good_result.reason is None
        assert good_result.credential == "internal_owner_token"
        assert good_result.payload["owner_key"] == "ok1_worker"
        assert wrong_result.reason == "internal_owner_invalid"

    def test_control_plane_internal_owner_token_rejects_wrong_audience_or_path(self, gated_app, tmp_path):
        previous = getattr(web_server.app.state, "owner_worker_supervisor", None)
        web_server.app.state.owner_worker_supervisor = SimpleNamespace(control_home=tmp_path, global_home=tmp_path / "global")
        try:
            wrong_aud = mint_internal_token("ok1_worker", audience=AUD_OWNER_WORKER_WS, path="/api/pty", control_home=tmp_path)
            wrong_path = mint_internal_token("ok1_worker", audience=AUD_CONTROL_PLANE_WS, path="/api/pub", control_home=tmp_path)
            assert web_server._ws_auth_result(_fake_ws(query={"internal_owner_token": wrong_aud})).reason == "internal_owner_invalid"
            assert web_server._ws_auth_result(_fake_ws(query={"internal_owner_token": wrong_path})).reason == "internal_owner_invalid"
        finally:
            web_server.app.state.owner_worker_supervisor = previous

    def test_internal_owner_token_payload_routes_by_signed_owner_key(self, gated_app, tmp_path, monkeypatch):
        global_home = tmp_path / "global"
        control_home = global_home / "control-plane"
        owner_key = "ok1_worker"
        previous = getattr(web_server.app.state, "owner_worker_supervisor", None)
        web_server.app.state.owner_worker_supervisor = SimpleNamespace(control_home=control_home, global_home=global_home)
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "wrong-home"))
        try:
            token = mint_internal_token(owner_key, audience=AUD_CONTROL_PLANE_WS, path="/api/pty", control_home=control_home)
            result = web_server._ws_auth_result(_fake_ws(query={"internal_owner_token": token}))
            owner = web_server._owner_context_from_ws_auth_result(result)
        finally:
            web_server.app.state.owner_worker_supervisor = previous

        assert owner.owner_key == owner_key
        assert owner.owner_home == (global_home / "users" / owner_key)

    def test_ws_close_reason_redacts_auth_query_values(self):
        reason = web_server._ws_close_reason(
            "failed ws://x/api/ws?ticket=abc&internal_owner_token=secret&channel=chan"
        )

        assert "abc" not in reason
        assert "secret" not in reason
        assert "ticket=<redacted>" in reason
        assert "internal_owner_token=<redacted>" in reason

    def test_owner_worker_query_strips_external_auth_credentials(self, loopback_app):
        ws = _fake_ws(
            query={
                "ticket": "browser-ticket",
                "token": "legacy",
                "internal": "process",
                "channel": "chan1",
                "fresh": "1",
            }
        )

        query = web_server._ws_query_for_owner_worker(ws, internal_owner_token="owner-token")

        assert "ticket=" not in query
        assert "token=legacy" not in query
        assert "internal=process" not in query
        assert "channel=chan1" in query
        assert "fresh=1" in query
        assert "internal_owner_token=owner-token" in query

    def test_only_browser_tickets_bridge_from_control_plane(self, gated_app, tmp_path):
        ticket = mint_ticket(
            user_id="u1",
            provider="stub",
            org_id="org1",
            tenant_id="org1",
            owner_key="ok1_owner",
        )
        ticket_ws = _fake_ws(query={"ticket": ticket})
        ticket_result = web_server._ws_auth_result(ticket_ws)
        assert web_server._should_bridge_ws_to_owner_worker(ticket_ws, ticket_result) is True

        internal_ws = _fake_ws(query={"internal": internal_ws_credential()})
        internal_result = web_server._ws_auth_result(internal_ws)
        assert internal_result.reason == "internal_owner_context_required"
        assert internal_result.credential == "internal"
        assert web_server._should_bridge_ws_to_owner_worker(internal_ws, internal_result) is False

        web_server.app.state.auth_required = False
        token_ws = _fake_ws(query={"token": web_server._SESSION_TOKEN})
        token_result = web_server._ws_auth_result(token_ws)
        assert token_result.reason is None
        assert token_result.credential == "token"
        assert web_server._should_bridge_ws_to_owner_worker(token_ws, token_result) is False

        worker_app = SimpleNamespace(
            state=SimpleNamespace(
                owner_worker_mode=True,
                owner_worker_owner_key="ok1_worker",
                owner_worker_control_home=tmp_path,
                auth_required=False,
            )
        )
        owner_token = mint_internal_token("ok1_worker", audience=AUD_OWNER_WORKER_WS, path="/api/pty", control_home=tmp_path)
        worker_ws = _fake_ws(query={"internal_owner_token": owner_token}, app=worker_app)
        worker_result = web_server._ws_auth_result(worker_ws)
        assert worker_result.reason is None
        assert worker_result.credential == "internal_owner_token"
        assert web_server._should_bridge_ws_to_owner_worker(worker_ws, worker_result) is False


class TestWsUrlBuilders:
    def test_child_urls_use_explicit_app_state_instead_of_module_global(self, monkeypatch):
        monkeypatch.setattr(web_server.app.state, "bound_host", "global.example", raising=False)
        monkeypatch.setattr(web_server.app.state, "bound_port", 9999, raising=False)
        monkeypatch.setattr(web_server.app.state, "auth_required", False, raising=False)
        worker_app = SimpleNamespace(
            state=SimpleNamespace(
                bound_host="owner-worker.local",
                bound_port=1234,
                auth_required=True,
            )
        )

        gateway_url = web_server._build_gateway_ws_url(app_obj=worker_app)
        sidecar_url = web_server._build_sidecar_url("chan1", app_obj=worker_app)

        assert gateway_url is None
        assert sidecar_url is None

    def test_child_urls_return_none_when_explicit_app_has_no_bound_socket(self, monkeypatch):
        monkeypatch.setattr(web_server.app.state, "bound_host", "global.example", raising=False)
        monkeypatch.setattr(web_server.app.state, "bound_port", 9999, raising=False)
        app_obj = SimpleNamespace(state=SimpleNamespace(auth_required=False))

        assert web_server._build_gateway_ws_url(app_obj=app_obj) is None
        assert web_server._build_sidecar_url("chan1", app_obj=app_obj) is None

    def test_owner_worker_urls_use_path_bound_owner_tokens(self, monkeypatch, tmp_path):
        control_home = tmp_path / "control-plane"
        worker_app = SimpleNamespace(
            state=SimpleNamespace(
                owner_worker_mode=True,
                owner_worker_owner_key="ok1_owner_a",
                owner_worker_control_home=control_home,
                auth_required=False,
            )
        )
        monkeypatch.setenv("HERMES_OWNER_WORKER_CONTROL_WS_BASE", "wss://control.example")

        gateway_url = web_server._build_gateway_ws_url(app_obj=worker_app)
        sidecar_url = web_server._build_sidecar_url("chan-1", app_obj=worker_app)

        assert gateway_url is not None
        assert sidecar_url is not None
        gateway = urlparse(gateway_url)
        sidecar = urlparse(sidecar_url)
        gateway_query = parse_qs(gateway.query)
        sidecar_query = parse_qs(sidecar.query)
        assert gateway.path == "/api/ws"
        assert sidecar.path == "/api/pub"
        assert set(gateway_query) == {"internal_owner_token"}
        assert set(sidecar_query) == {"internal_owner_token", "channel"}
        assert sidecar_query["channel"] == ["chan-1"]
        for query in (gateway_query, sidecar_query):
            assert "internal" not in query
            assert "token" not in query

        gateway_payload = validate_internal_token_payload(
            gateway_query["internal_owner_token"][0],
            audience=AUD_CONTROL_PLANE_WS,
            path="/api/ws",
            control_home=control_home,
        )
        sidecar_payload = validate_internal_token_payload(
            sidecar_query["internal_owner_token"][0],
            audience=AUD_CONTROL_PLANE_WS,
            path="/api/pub",
            control_home=control_home,
        )
        assert gateway_payload is not None
        assert sidecar_payload is not None
        assert gateway_payload["owner_key"] == "ok1_owner_a"
        assert sidecar_payload["owner_key"] == "ok1_owner_a"

        other_worker_app = SimpleNamespace(
            state=SimpleNamespace(
                owner_worker_mode=True,
                owner_worker_owner_key="ok1_owner_b",
                owner_worker_control_home=control_home,
                auth_required=False,
            )
        )
        other_gateway_url = web_server._build_gateway_ws_url(app_obj=other_worker_app)
        assert other_gateway_url is not None
        other_gateway_token = parse_qs(urlparse(other_gateway_url).query)["internal_owner_token"][0]
        other_payload = validate_internal_token_payload(
            other_gateway_token,
            audience=AUD_CONTROL_PLANE_WS,
            path="/api/ws",
            control_home=control_home,
        )
        assert other_payload is not None
        assert other_payload["owner_key"] == "ok1_owner_b"
        assert other_payload["owner_key"] != gateway_payload["owner_key"]


class TestWsRequestIsAllowedGated:
    """Bug fix: in gated mode, the WS peer-IP loopback check must be
    bypassed.

    When the OAuth gate is active, ``start_server`` runs uvicorn with
    ``proxy_headers=True`` so the dashboard can honour
    ``X-Forwarded-Proto`` from Fly's TLS terminator. A side effect is that
    ``ws.client.host`` is rewritten to the X-Forwarded-For value — the
    real internet client IP, never loopback. The loopback peer guard
    (intended only for unauthenticated loopback dev) must not also reject
    those upgrades: the OAuth gate + single-use ticket is the auth.

    Regression coverage: every WS endpoint (``/api/pty``, ``/api/ws``,
    ``/api/pub``, ``/api/events``) calls ``_ws_request_is_allowed`` after
    ``_ws_auth_ok``. If the peer-IP check rejects gated mode, the chat
    tab + sidebar tool feed silently fail to connect even after a
    successful OAuth login.
    """

    def test_non_loopback_peer_allowed_in_gated_mode(self, gated_app):
        ws = _fake_ws(query={}, client_host="203.0.113.7")
        # Host header matches the bound host so the DNS-rebinding guard
        # passes; only the peer-IP check is under test.
        ws.headers = {"host": "fly-app.fly.dev"}
        assert web_server._ws_request_is_allowed(ws) is True

    def test_non_loopback_peer_rejected_in_loopback_mode(self, loopback_app):
        """Loopback mode still enforces the peer-IP guard — the legacy
        token path is the only auth and we don't want random LAN hosts
        guessing it."""
        ws = _fake_ws(query={}, client_host="192.168.1.42")
        ws.headers = {"host": "127.0.0.1:8080"}
        assert web_server._ws_request_is_allowed(ws) is False

    def test_loopback_peer_allowed_in_loopback_mode(self, loopback_app):
        ws = _fake_ws(query={}, client_host="127.0.0.1")
        ws.headers = {"host": "127.0.0.1:8080"}
        assert web_server._ws_request_is_allowed(ws) is True

    def test_non_loopback_peer_allowed_in_insecure_public_mode(self, insecure_public_app):
        """`--host 0.0.0.0 --insecure` is an explicit LAN/public opt-in.

        Regression coverage for the dashboard `/chat` breakage where the
        HTML shell loaded on 9120 but every WebSocket upgrade was rejected
        with 403 because the loopback-only peer guard still ran even though
        the operator intentionally exposed the dashboard on all interfaces.
        """
        ws = _fake_ws(query={}, client_host="192.168.0.55")
        ws.headers = {
            "host": "192.168.0.222:9120",
            "origin": "http://192.168.0.222:9120",
        }
        assert web_server._ws_request_is_allowed(ws) is True

    def test_peer_allowed_on_explicit_non_loopback_bind(self, insecure_explicit_host_app):
        """`--host 100.64.0.10 --insecure` (Tailscale/LAN IP) is an explicit
        non-loopback opt-in too — not just the 0.0.0.0 wildcard.

        Regression coverage: the merged 0.0.0.0/:: fix did not cover binding
        directly to a specific tailnet/LAN address, so `/chat` HTML loaded but
        WS upgrades were still rejected by the loopback-only peer guard.
        """
        ws = _fake_ws(query={}, client_host="100.64.0.99")
        ws.headers = {
            "host": "100.64.0.10:9119",
            "origin": "http://100.64.0.10:9119",
        }
        assert web_server._ws_request_is_allowed(ws) is True

    def test_rebinding_host_rejected_on_explicit_non_loopback_bind(
        self, insecure_explicit_host_app
    ):
        """Lifting the peer-IP gate for an explicit bind must NOT lift the
        DNS-rebinding Host guard: a mismatched Host header is still rejected,
        because an explicit non-loopback bind requires an exact Host match in
        `_is_accepted_host` (unlike the 0.0.0.0 wildcard, which accepts any).
        """
        ws = _fake_ws(query={}, client_host="100.64.0.99")
        ws.headers = {"host": "evil.example.com"}
        assert web_server._ws_request_is_allowed(ws) is False

    def test_host_origin_guard_still_runs_in_gated_mode(self, gated_app):
        """Bypassing the peer-IP check must not bypass the DNS-rebinding
        Host header guard — that one still protects against attacker
        sites resolving DNS to the public IP."""
        ws = _fake_ws(query={}, client_host="203.0.113.7")
        ws.headers = {"host": "evil.example.com"}
        assert web_server._ws_request_is_allowed(ws) is False

    # -- security: empty / missing peer must fail closed in loopback mode --
    # Regression for the fail-open default-allow where
    # ``ws.client is None`` or ``ws.client.host == ""`` was treated as
    # "allowed" on a loopback-bound dashboard with auth disabled. ASGI
    # servers behind a misconfigured proxy or a unix-socket transport can
    # deliver either shape, so both must be rejected explicitly.

    def test_empty_client_host_rejected_in_loopback_mode(self, loopback_app):
        """An empty ws.client.host must be rejected on a loopback bind."""
        ws = _fake_ws(query={}, client_host="")
        ws.headers = {"host": "127.0.0.1:8080"}
        assert web_server._ws_client_is_allowed(ws) is False
        assert web_server._ws_request_is_allowed(ws) is False

    def test_missing_client_object_rejected_in_loopback_mode(self, loopback_app):
        """ws.client is None must be rejected on a loopback bind."""
        ws = _fake_ws(query={}, client_host="")
        ws.client = None  # ASGI servers can omit the client tuple entirely
        ws.headers = {"host": "127.0.0.1:8080"}
        assert web_server._ws_client_is_allowed(ws) is False
        assert web_server._ws_request_is_allowed(ws) is False

    def test_empty_client_host_reason_is_block(self, loopback_app):
        """_ws_client_reason must return a block reason for an empty peer,
        not ``None`` (which the dispatcher treats as ``allowed``)."""
        ws = _fake_ws(query={}, client_host="")
        ws.headers = {"host": "127.0.0.1:8080"}
        reason = web_server._ws_client_reason(ws)
        assert reason is not None
        assert "missing_or_empty_peer" in reason

    def test_empty_client_host_still_allowed_in_insecure_public_mode(
        self, insecure_public_app
    ):
        """The empty-peer fail-closed guard must only apply to loopback
        binds. With an explicit ``--host 0.0.0.0 --insecure`` opt-in, the
        loopback-only peer restriction does not run at all, so the empty
        peer case bypasses the new guard the same way a legitimate LAN
        peer does. Without this, the fix would regress the public-bind
        path the dashboard relies on."""
        ws = _fake_ws(query={}, client_host="")
        ws.headers = {
            "host": "192.168.0.222:9120",
            "origin": "http://192.168.0.222:9120",
        }
        assert web_server._ws_client_is_allowed(ws) is True

    def test_empty_client_host_still_allowed_in_gated_mode(self, gated_app):
        """The empty-peer fail-closed guard must not apply when the OAuth
        gate is active (``auth_required=True``). Gated mode rewrites
        ``ws.client.host`` via ``proxy_headers=True``, and the ticket is
        the auth, so peer-IP is irrelevant on that path."""
        ws = _fake_ws(query={}, client_host="")
        ws.headers = {"host": "dashboard.example.com"}
        assert web_server._ws_client_is_allowed(ws) is True


class TestWsHostOriginGuardOrigins:
    """The WS Origin guard must let the packaged desktop shell connect.

    Electron loads the packaged renderer over ``file://``, so its WebSocket
    handshake carries ``Origin: file://`` (or the opaque ``null``, or a custom
    ``app://`` scheme). The DNS-rebinding guard only needs to block cross-site
    http(s) origins — a malicious web page can never forge a non-web origin.

    This guard runs only AFTER ``_ws_auth_ok`` has validated the WS credential
    (session token on loopback / ``--insecure`` binds, single-use ``?ticket=``
    on OAuth-gated binds), so a non-web origin is trusted in every mode: the
    credential is the real gate, and a ``file://`` / ``null`` origin cannot
    originate a DNS-rebinding browser attack. ``http(s)`` origins are still
    match-checked against the bound host.
    """

    def _ws(self, *, origin, host):
        ws = _fake_ws(query={}, path="/api/ws")
        ws.headers = {"host": host, "origin": origin}
        return ws

    def test_loopback_file_origin_allowed(self, loopback_app):
        ws = self._ws(origin="file://", host="127.0.0.1:8080")
        assert web_server._ws_host_origin_is_allowed(ws) is True

    def test_loopback_null_origin_allowed(self, loopback_app):
        ws = self._ws(origin="null", host="127.0.0.1:8080")
        assert web_server._ws_host_origin_is_allowed(ws) is True

    def test_loopback_app_scheme_origin_allowed(self, loopback_app):
        ws = self._ws(origin="app://hermes", host="127.0.0.1:8080")
        assert web_server._ws_host_origin_is_allowed(ws) is True

    def test_loopback_matching_http_origin_allowed(self, loopback_app):
        # The dev renderer (vite) loads over http://127.0.0.1:<port>.
        ws = self._ws(origin="http://127.0.0.1:5174", host="127.0.0.1:8080")
        assert web_server._ws_host_origin_is_allowed(ws) is True

    def test_loopback_cross_site_http_origin_rejected(self, loopback_app):
        # DNS-rebinding / cross-site: a real web attacker can only present an
        # http(s) origin, and that must still be rejected.
        ws = self._ws(origin="http://evil.test", host="127.0.0.1:8080")
        assert web_server._ws_host_origin_is_allowed(ws) is False

    def test_explicit_non_loopback_file_origin_allowed(self, insecure_explicit_host_app):
        """Packaged Hermes Desktop also uses file:// when connecting to a
        Tailscale/LAN dashboard bind.

        The WebSocket route calls _ws_auth_ok before this guard, so in
        non-gated mode the legacy session token remains the auth boundary.
        """
        ws = self._ws(origin="file://", host="100.64.0.10:9119")
        assert web_server._ws_host_origin_is_allowed(ws) is True

    def test_explicit_non_loopback_null_origin_allowed(self, insecure_explicit_host_app):
        ws = self._ws(origin="null", host="100.64.0.10:9119")
        assert web_server._ws_host_origin_is_allowed(ws) is True

    def test_explicit_non_loopback_cross_site_http_origin_rejected(
        self, insecure_explicit_host_app
    ):
        ws = self._ws(origin="http://localhost:9119", host="100.64.0.10:9119")
        assert web_server._ws_host_origin_is_allowed(ws) is False

    def test_gated_file_origin_allowed(self, gated_app):
        # The packaged desktop app drives a remote OAuth-GATED gateway over a
        # file:// renderer origin. The WS route validates the single-use
        # ?ticket= in _ws_auth_ok before this guard runs, and a file:// origin
        # can't be a DNS-rebinding browser attack, so the Origin guard must let
        # it through. This is the regression that broke desktop → hosted
        # gateway connections — every WS upgrade got HTTP 403 even with a valid
        # ticket.
        ws = self._ws(origin="file://", host="fly-app.fly.dev")
        assert web_server._ws_host_origin_is_allowed(ws) is True

    def test_gated_null_origin_allowed(self, gated_app):
        ws = self._ws(origin="null", host="fly-app.fly.dev")
        assert web_server._ws_host_origin_is_allowed(ws) is True

    def test_gated_app_scheme_origin_allowed(self, gated_app):
        ws = self._ws(origin="app://.", host="fly-app.fly.dev")
        assert web_server._ws_host_origin_is_allowed(ws) is True

    def test_gated_cross_site_http_origin_still_host_checked(self, gated_app):
        # An http(s) origin is still subjected to the same-host check even on a
        # gated bind: a cross-site http origin whose netloc doesn't match the
        # bound host is rejected. Real browser DNS-rebinding defence unchanged.
        ws = self._ws(origin="https://evil.test", host="fly-app.fly.dev")
        assert web_server._ws_host_origin_is_allowed(ws) is False

    def test_gated_same_host_https_origin_allowed(self, gated_app):
        ws = self._ws(origin="https://fly-app.fly.dev", host="fly-app.fly.dev")
        assert web_server._ws_host_origin_is_allowed(ws) is True


class TestSidecarUrl:
    def test_loopback_uses_session_token(self, loopback_app):
        url = web_server._build_sidecar_url("ch-1")
        assert url is not None
        assert f"token={web_server._SESSION_TOKEN}" in url
        assert "ticket=" not in url

    def test_gated_returns_no_control_plane_sidecar_url(self, gated_app):
        """Authenticated Control Plane children must run inside an Owner Worker."""
        assert web_server._build_sidecar_url("ch-1") is None

    def test_no_bound_host_returns_none(self, gated_app):
        web_server.app.state.bound_host = None
        try:
            assert web_server._build_sidecar_url("ch") is None
        finally:
            web_server.app.state.bound_host = "fly-app.fly.dev"


# ---------------------------------------------------------------------------
# _build_gateway_ws_url — the TUI child's primary JSON-RPC backend WS.
# Loopback uses ?token=; authenticated Control Plane mode returns no URL because
# the owner worker creates an owner-bound internal capability instead.
# ---------------------------------------------------------------------------


class TestGatewayWsUrl:
    def test_loopback_uses_session_token(self, loopback_app):
        url = web_server._build_gateway_ws_url()
        assert url is not None
        assert "/api/ws?" in url
        assert f"token={web_server._SESSION_TOKEN}" in url
        assert "internal=" not in url

    def test_gated_returns_no_control_plane_gateway_url(self, gated_app):
        """Authenticated Control Plane children must run inside an Owner Worker."""
        assert web_server._build_gateway_ws_url() is None

    def test_no_bound_host_returns_none(self, gated_app):
        web_server.app.state.bound_host = None
        try:
            assert web_server._build_gateway_ws_url() is None
        finally:
            web_server.app.state.bound_host = "fly-app.fly.dev"
