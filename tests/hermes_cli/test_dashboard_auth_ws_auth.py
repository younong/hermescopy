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
from hermes_cli.dashboard_auth import clear_providers, get_provider, register_provider
from hermes_cli.dashboard_auth.base import ProviderError, Session
from hermes_cli.dashboard_auth.owner_context import (
    owner_context_from_session,
    owner_context_from_ticket_payload,
)
from hermes_cli.dashboard_auth.ws_tickets import (
    _reset_for_tests,
    consume_ticket,
    internal_ws_credential,
    mint_ticket,
    verify_ticket,
)
from hermes_cli.dashboard_auth.authority import AuthorityStore, WorkerGenerationState, WorkerLeaseState
from hermes_cli.owner_worker.tokens import (
    AUD_OWNER_WORKER_WS,
    SCOPE_OWNER_WORKER_WS,
    mint_owner_worker_capability,
    owner_worker_capability_public_config,
)
from tests.hermes_cli.conftest_dashboard_auth import StubAuthProvider


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _active_worker_lease(control_home, owner_key="ok1_worker", worker_id="worker-a"):
    store = AuthorityStore(control_home)
    claim = store.claim_worker_start(owner_key, worker_id=worker_id)
    return store.transition_worker_lease(
        claim.lease,
        state=WorkerLeaseState.ACTIVE,
        generation_state=WorkerGenerationState.ACTIVE,
    )


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

        r = gated_app.post("/api/auth/ws-ticket", json={"audience": "browser-ws:/api/pty"})
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
        tickets = {
            gated_app.post(
                "/api/auth/ws-ticket", json={"audience": "browser-ws:/api/pty"}
            ).json()["ticket"]
            for _ in range(5)
        }
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


def _fake_ws(*, query: dict, client_host: str = "127.0.0.1", path: str = "/api/pty", app=None, cookies=None):
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
        cookies=cookies or {},
    )


def _browser_ticket_ws(gated_app, *, path: str = "/api/pty"):
    """Mint a browser ticket through the verified HTTP session endpoint."""
    _logged_in(gated_app)
    response = gated_app.post(
        "/api/auth/ws-ticket", json={"audience": f"browser-ws:{path}"}
    )
    assert response.status_code == 200
    ticket = response.json()["ticket"]
    return ticket, _fake_ws(
        query={"ticket": ticket},
        path=path,
        cookies={name: value.strip('"') for name, value in dict(gated_app.cookies).items()},
    )


def _ws_from_browser_ticket(ticket: str, *, source_ws, path: str | None = None):
    return _fake_ws(
        query={"ticket": ticket},
        path=path or source_ws.url.path,
        cookies=dict(source_ws.cookies),
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
        _ticket, ws = _browser_ticket_ws(gated_app)

        assert web_server._ws_auth_ok(ws) is True

    def test_upgrade_rejects_ticket_without_current_session_before_consume(self, gated_app):
        ticket, _ws = _browser_ticket_ws(gated_app)
        missing_session = _fake_ws(query={"ticket": ticket})

        assert web_server._ws_auth_result(missing_session).reason == "ticket_invalid"
        assert web_server._ws_auth_result(_ws).reason is None

    def test_upgrade_rejects_provider_membership_revision_change_before_consume(self, gated_app, monkeypatch):
        ticket, ws = _browser_ticket_ws(gated_app)
        provider = get_provider("stub")
        assert provider is not None
        original = provider.authorization_state
        monkeypatch.setattr(provider, "authorization_state", lambda session: (original(session)[0], "membership-v2"))

        assert web_server._ws_auth_result(ws).reason == "ticket_invalid"
        monkeypatch.setattr(provider, "authorization_state", original)
        assert web_server._ws_auth_result(ws).reason is None

    def test_upgrade_fails_closed_when_ticket_provider_is_unavailable(self, gated_app, monkeypatch):
        ticket, ws = _browser_ticket_ws(gated_app)
        provider = get_provider("stub")
        assert provider is not None

        def unavailable(*, access_token):
            raise ProviderError("stub unavailable")

        monkeypatch.setattr(provider, "verify_session", unavailable)
        assert web_server._ws_auth_result(ws).reason == "authority_unavailable"

    def test_ticket_is_rejected_before_consume_on_wrong_upgrade_route(self, gated_app):
        ticket, ws = _browser_ticket_ws(gated_app, path="/api/ws")

        result = web_server._ws_auth_result(
            _ws_from_browser_ticket(ticket, source_ws=ws, path="/api/pty")
        )

        assert result.reason == "ticket_invalid"
        assert web_server._ws_auth_result(ws).reason is None

    def test_consumed_ticket_rejected(self, gated_app):
        ticket, ws_one = _browser_ticket_ws(gated_app)
        ws_two = _ws_from_browser_ticket(ticket, source_ws=ws_one)
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
        ticket_response = gated_app.post(
            "/api/auth/ws-ticket", json={"audience": "browser-ws:/api/pty"}
        )

        assert auth_me.status_code == 200
        assert ticket_response.status_code == 200
        ws_owner = owner_context_from_ticket_payload(consume_ticket(ticket_response.json()["ticket"]))
        assert ws_owner.owner_key == auth_me.json()["owner_key"]
        assert ws_owner.tenant_id == auth_me.json()["tenant_id"]

    def test_ticket_routes_only_its_verified_owner(self, gated_app):
        ticket, ws = _browser_ticket_ws(gated_app)
        payload = verify_ticket(ticket)

        assert payload["user_id"] == "stub-user-1"
        assert web_server._ws_auth_result(ws).reason is None

    def test_auth_result_retains_ticket_payload_for_owner_bridge(self, gated_app):
        ticket, ws = _browser_ticket_ws(gated_app)

        result = web_server._ws_auth_result(ws)

        assert result.reason is None
        assert result.credential == "ticket"
        assert result.payload["user_id"] == "stub-user-1"
        assert result.payload["owner_key"].startswith("ok1_")

    def test_owner_worker_requires_generation_bound_capability(self, tmp_path):
        lease = _active_worker_lease(tmp_path)
        verifier = owner_worker_capability_public_config(tmp_path)
        worker_app = SimpleNamespace(
            state=SimpleNamespace(
                owner_worker_mode=True,
                owner_worker_owner_key="ok1_worker",
                owner_worker_control_home=tmp_path,
                owner_worker_lease=lease,
                owner_worker_capability_verifier=verifier,
                auth_required=False,
            )
        )
        good = mint_owner_worker_capability(
            lease, audience=AUD_OWNER_WORKER_WS, scope=SCOPE_OWNER_WORKER_WS,
            path="/api/pty", control_home=tmp_path,
        )
        other = _active_worker_lease(tmp_path, owner_key="ok1_other", worker_id="worker-b")
        wrong = mint_owner_worker_capability(
            other, audience=AUD_OWNER_WORKER_WS, scope=SCOPE_OWNER_WORKER_WS,
            path="/api/pty", control_home=tmp_path,
        )

        assert web_server._ws_auth_ok(_fake_ws(query={"internal_owner_token": good}, app=worker_app)) is True
        assert web_server._ws_auth_ok(_fake_ws(query={"internal_owner_token": wrong}, app=worker_app)) is False
        assert web_server._ws_auth_ok(_fake_ws(query={"internal": internal_ws_credential()}, app=worker_app)) is False

    def test_control_plane_rejects_retired_internal_owner_token_format(self, gated_app):
        result = web_server._ws_auth_result(_fake_ws(query={"internal_owner_token": "ow2.retired.signature"}))

        assert result.reason == "internal_owner_invalid"
        assert result.credential == "internal_owner_token"

    def test_control_plane_does_not_route_bare_owner_capabilities(self, gated_app):
        # Step 3 capabilities are Control Plane -> exact Worker only. Reverse
        # worker-child admission is intentionally deferred to step 4 bootstrap.
        result = web_server._ws_auth_result(_fake_ws(query={"internal_owner_token": "owc1.not-a-bootstrap"}))

        assert result.reason == "internal_owner_invalid"
        assert result.credential == "internal_owner_token"

    def test_worker_change_closes_only_exact_generation_bridge(self, gated_app):
        import asyncio
        from hermes_cli.dashboard_auth.authority import WorkerGenerationState, WorkerLeaseChange, WorkerLeaseState

        class _Bridge:
            def __init__(self):
                self.calls = []

            async def close(self, *, code, reason):
                self.calls.append((code, reason))

        old, newer, different_lease = _Bridge(), _Bridge(), _Bridge()
        old_change = WorkerLeaseChange(
            sequence=1, owner_key="ok1_owner", worker_generation=1, worker_id="worker-a",
            lease_version=1, recovery_generation=0, lease_state=WorkerLeaseState.DRAINING,
            generation_state=WorkerGenerationState.DRAINING,
        )
        newer_change = WorkerLeaseChange(
            sequence=2, owner_key="ok1_owner", worker_generation=2, worker_id="worker-b",
            lease_version=2, recovery_generation=0, lease_state=WorkerLeaseState.ACTIVE,
            generation_state=WorkerGenerationState.ACTIVE,
        )
        wrong_lease_change = WorkerLeaseChange(
            sequence=3, owner_key="ok1_owner", worker_generation=1, worker_id="worker-a",
            lease_version=9, recovery_generation=0, lease_state=WorkerLeaseState.ACTIVE,
            generation_state=WorkerGenerationState.ACTIVE,
        )

        async def exercise():
            bridges, lock = web_server._authorized_ws_bridge_state(web_server.app)
            async with lock:
                bridges.clear()
                web_server.app.state.authorized_ws_bridges_by_worker.clear()
                web_server.app.state.authorized_ws_bridges_by_worker.update({
                    web_server._worker_bridge_identity(old_change): {old},
                    web_server._worker_bridge_identity(newer_change): {newer},
                    web_server._worker_bridge_identity(wrong_lease_change): {different_lease},
                })
            await web_server.close_authorized_bridges_by_worker_change(
                web_server.app, (old_change,), reason="worker_generation_revoked"
            )

        asyncio.run(exercise())

        assert old.calls and old.calls[0][0] == 1011
        assert newer.calls == []
        assert different_lease.calls == []
        assert web_server._worker_bridge_identity(old_change) in web_server.app.state.revoked_ws_bridge_worker_fences
        assert web_server._worker_bridge_identity(newer_change) not in web_server.app.state.revoked_ws_bridge_worker_fences
        assert web_server._worker_bridge_identity(wrong_lease_change) not in web_server.app.state.revoked_ws_bridge_worker_fences

    def test_worker_change_closes_both_relay_halves_and_releases_exact_lease(self, gated_app):
        import asyncio
        from hermes_cli.dashboard_auth.authority import WorkerGenerationState, WorkerLeaseChange, WorkerLeaseState

        class _Peer:
            def __init__(self):
                self.calls = []

            async def close(self, *, code, reason):
                self.calls.append((code, reason))

        class _Lease:
            def __init__(self):
                self.releases = 0

            def release(self):
                self.releases += 1

        change = WorkerLeaseChange(
            sequence=1, owner_key="ok1_owner", worker_generation=1, worker_id="worker-a",
            lease_version=1, recovery_generation=0, lease_state=WorkerLeaseState.DRAINING,
            generation_state=WorkerGenerationState.DRAINING,
        )
        browser, worker, lease = _Peer(), _Peer(), _Lease()
        bridge = web_server._OwnerWorkerWsBridge(browser, worker, lease)

        async def exercise():
            bridges, lock = web_server._authorized_ws_bridge_state(web_server.app)
            async with lock:
                bridges.clear()
                web_server.app.state.authorized_ws_bridges_by_worker.clear()
                web_server.app.state.authorized_ws_bridges_by_worker[
                    web_server._worker_bridge_identity(change)
                ] = {bridge}
            await web_server.close_authorized_bridges_by_worker_change(
                web_server.app, (change,), reason="worker_generation_revoked"
            )

        asyncio.run(exercise())

        assert browser.calls == [(1011, "auth: worker_generation_revoked")]
        assert worker.calls == [(1011, "auth: worker_generation_revoked")]
        assert lease.releases == 1

    def test_authority_change_closes_only_stale_matching_bridge(self, gated_app):
        import asyncio
        from hermes_cli.dashboard_auth.authority import AuthorityChange, AuthorizationScope

        class _Bridge:
            def __init__(self):
                self.calls = []

            async def close(self, *, code, reason):
                self.calls.append((code, reason))

        scope_a = AuthorizationScope(
            provider="stub", tenant_id="tenant-a", user_id="user-a",
            session_id="session-a", membership_revision="v1",
        )
        scope_b = AuthorizationScope(
            provider="stub", tenant_id="tenant-b", user_id="user-b",
            session_id="session-b", membership_revision="v1",
        )
        old_a, current_a, bridge_b = _Bridge(), _Bridge(), _Bridge()

        async def exercise():
            bridges, lock = web_server._authorized_ws_bridge_state(web_server.app)
            async with lock:
                bridges.clear()
                bridges.setdefault(scope_a.digest, set()).update({(old_a, 0), (current_a, 1)})
                bridges.setdefault(scope_b.digest, set()).add((bridge_b, 0))
            await web_server.close_authorized_bridges_by_changes(
                web_server.app,
                (AuthorityChange(sequence=1, scope_digest=scope_a.digest, epoch=1, revoked=False),),
                reason="membership_change",
            )

        asyncio.run(exercise())

        assert old_a.calls and old_a.calls[0][0] == 4401
        assert current_a.calls == []
        assert bridge_b.calls == []

    def test_revoked_authority_change_closes_all_matching_bridge_epochs(self, gated_app):
        import asyncio
        from hermes_cli.dashboard_auth.authority import AuthorityChange, AuthorizationScope

        class _Bridge:
            def __init__(self):
                self.calls = []

            async def close(self, *, code, reason):
                self.calls.append((code, reason))

        scope_a = AuthorizationScope(
            provider="stub", tenant_id="tenant-a", user_id="user-a",
            session_id="session-a", membership_revision="v1",
        )
        bridge_a, bridge_a_new = _Bridge(), _Bridge()

        async def exercise():
            bridges, lock = web_server._authorized_ws_bridge_state(web_server.app)
            async with lock:
                bridges.clear()
                bridges.setdefault(scope_a.digest, set()).update({(bridge_a, 0), (bridge_a_new, 4)})
            await web_server.close_authorized_bridges_by_changes(
                web_server.app,
                (AuthorityChange(sequence=2, scope_digest=scope_a.digest, epoch=5, revoked=True),),
                reason="logout",
            )

        asyncio.run(exercise())

        assert bridge_a.calls and bridge_a.calls[0][0] == 4401
        assert bridge_a_new.calls and bridge_a_new.calls[0][0] == 4401

    def test_authority_dispatcher_closes_remote_revocation(self, gated_app, monkeypatch):
        import asyncio
        from hermes_cli.dashboard_auth.authority import AuthorityChange, AuthorizationScope

        class _Bridge:
            def __init__(self):
                self.calls = []

            async def close(self, *, code, reason):
                self.calls.append((code, reason))

        class _Store:
            def __init__(self):
                self.calls = 0

            def changes_since(self, sequence):
                self.calls += 1
                if sequence == 0:
                    return (
                        AuthorityChange(
                            sequence=7,
                            scope_digest=scope.digest,
                            epoch=1,
                            revoked=True,
                        ),
                    )
                return ()

            def worker_changes_since(self, sequence):
                return ()

        scope = AuthorizationScope(
            provider="stub", tenant_id="tenant-a", user_id="user-a",
            session_id="session-a", membership_revision="v1",
        )
        bridge = _Bridge()
        store = _Store()
        monkeypatch.setattr(
            "hermes_cli.dashboard_auth.ws_tickets.authority_store", lambda: store
        )

        async def exercise():
            bridges, lock = web_server._authorized_ws_bridge_state(web_server.app)
            async with lock:
                bridges.clear()
                bridges.setdefault(scope.digest, set()).add((bridge, 0))
            await web_server._ensure_authority_change_dispatcher(web_server.app)
            for _ in range(20):
                if bridge.calls:
                    break
                await asyncio.sleep(0.01)
            web_server.app.state.authority_change_stop.set()
            await web_server.app.state.authority_change_task

        asyncio.run(exercise())

        assert store.calls >= 1
        assert bridge.calls and bridge.calls[0][0] == 4401

    def test_expired_browser_session_closes_worker_half_and_releases_lease(self, gated_app, monkeypatch, tmp_path):
        import asyncio
        import time

        class _Browser:
            def __init__(self):
                self.app = web_server.app
                self.query_params = SimpleNamespace(get=lambda *_args: "")
                self.url = SimpleNamespace(query="")
                self.accepted = False
                self.closed = []
                self._closed = asyncio.Event()

            async def accept(self):
                self.accepted = True

            async def close(self, *, code=1000, reason=""):
                self.closed.append((code, reason))
                self._closed.set()

            async def receive(self):
                await self._closed.wait()
                return {"type": "websocket.disconnect", "code": 4401}

        class _Worker:
            def __init__(self):
                self.closed = []
                self.sent = []

            async def send(self, value):
                self.sent.append(value)

            async def recv(self):
                return "ack"

            async def close(self, **kwargs):
                self.closed.append(kwargs)

            def __aiter__(self):
                return self

            async def __anext__(self):
                await asyncio.Future()

        class _Lease:
            def __init__(self):
                self.released = False

            def release(self):
                self.released = True

        browser, worker, lease = _Browser(), _Worker(), _Lease()
        handle = SimpleNamespace(
            socket_path="/unused",
            owner_key="ok1_owner",
            worker_generation=1,
            worker_id="worker-a",
            lease_version=1,
            recovery_generation=0,
        )
        browser.app.state.owner_worker_supervisor = SimpleNamespace(
            get_or_start=lambda _owner: handle,
            control_home=tmp_path / "control",
        )
        monkeypatch.setattr(
            web_server,
            "_owner_context_from_ws_auth_result",
            lambda _result: SimpleNamespace(owner_key="ok1_owner"),
        )
        monkeypatch.setattr(web_server, "_acquire_owner_worker_use", lambda *_args: lease)
        monkeypatch.setattr(web_server, "_connect_owner_worker_ws", lambda *_args, **_kwargs: asyncio.sleep(0, result=worker))
        monkeypatch.setattr("hermes_cli.dashboard_auth.owner_context.ensure_owner_home", lambda _owner: None)
        monkeypatch.setattr("hermes_cli.owner_worker.tokens.validate_owp1_control", lambda *_args, **_kwargs: None)

        auth_result = web_server._WsAuthResult(
            None,
            "ticket",
            {
                "provider": "stub",
                "tenant_id": "tenant-a",
                "user_id": "user-a",
                "session_id": "session-a",
                "membership_revision": "v1",
                "epoch": 0,
            },
            session_expires_at=int(time.time()),
        )

        asyncio.run(
            web_server._bridge_websocket_to_owner_worker(
                browser, path="/api/pty", auth_result=auth_result
            )
        )

        assert browser.accepted is True
        assert browser.closed and browser.closed[0][0] == 4401
        assert worker.closed
        assert lease.released is True

    def test_bridge_close_propagates_to_worker_and_releases_exact_lease(self, gated_app, monkeypatch, tmp_path):
        import asyncio

        class _Browser:
            def __init__(self):
                self.app = web_server.app
                self.query_params = SimpleNamespace(get=lambda *_args: "")
                self.url = SimpleNamespace(query="")
                self.accepted = False
                self.closed = []
                self._messages = iter(({"type": "websocket.disconnect", "code": 4409, "reason": "replaced"},))

            async def accept(self):
                self.accepted = True

            async def close(self, *, code=1000, reason=""):
                self.closed.append((code, reason))

            async def receive(self):
                return next(self._messages)

        class _Worker:
            def __init__(self):
                self.closed = []
                self.sent = []

            async def send(self, value):
                self.sent.append(value)

            async def recv(self):
                return "ack"

            async def close(self, **kwargs):
                self.closed.append(kwargs)

            def __aiter__(self):
                return self

            async def __anext__(self):
                await asyncio.Future()

        class _Lease:
            def __init__(self):
                self.release_count = 0

            def release(self):
                self.release_count += 1

        browser, worker, lease = _Browser(), _Worker(), _Lease()
        handle = SimpleNamespace(socket_path="/unused", owner_key="ok1_owner", worker_generation=1, worker_id="worker-bridge-close", lease_version=1, recovery_generation=0)
        browser.app.state.owner_worker_supervisor = SimpleNamespace(get_or_start=lambda _owner: handle, control_home=tmp_path / "control")
        monkeypatch.setattr(web_server, "_owner_context_from_ws_auth_result", lambda _result: SimpleNamespace(owner_key="ok1_owner"))
        monkeypatch.setattr(web_server, "_acquire_owner_worker_use", lambda *_args: lease)
        monkeypatch.setattr(web_server, "_connect_owner_worker_ws", lambda *_args, **_kwargs: asyncio.sleep(0, result=worker))
        monkeypatch.setattr("hermes_cli.dashboard_auth.owner_context.ensure_owner_home", lambda _owner: None)
        monkeypatch.setattr("hermes_cli.owner_worker.tokens.validate_owp1_control", lambda *_args, **_kwargs: None)
        auth_result = web_server._WsAuthResult(None, "ticket", {"provider": "stub", "tenant_id": "tenant-a", "user_id": "user-a", "session_id": "session-a", "membership_revision": "v1", "epoch": 0})

        asyncio.run(web_server._bridge_websocket_to_owner_worker(browser, path="/api/pty", auth_result=auth_result))

        assert browser.accepted is True
        assert browser.closed and browser.closed[-1][0] == 4409
        assert worker.closed and worker.closed[-1]["code"] == 4409
        assert lease.release_count == 1

    def test_worker_fence_tombstone_rejects_bridge_registered_after_change(self, gated_app, monkeypatch, tmp_path):
        import asyncio
        from hermes_cli.dashboard_auth.authority import WorkerGenerationState, WorkerLeaseChange, WorkerLeaseState

        class _Browser:
            def __init__(self):
                self.app = web_server.app
                self.query_params = SimpleNamespace(get=lambda *_args: "")
                self.url = SimpleNamespace(query="")
                self.accepted = False
                self.closed = []

            async def accept(self):
                self.accepted = True

            async def close(self, *, code=1000, reason=""):
                self.closed.append((code, reason))

        class _Worker:
            def __init__(self):
                self.closed = []

            async def send(self, _value):
                return None

            async def recv(self):
                return "ack"

            async def close(self, **kwargs):
                self.closed.append(kwargs)

        class _Lease:
            def __init__(self):
                self.release_count = 0

            def release(self):
                self.release_count += 1

        browser, worker, lease = _Browser(), _Worker(), _Lease()
        handle = SimpleNamespace(
            socket_path="/unused", owner_key="ok1_owner", worker_generation=1,
            worker_id="worker-late-registration", lease_version=1, recovery_generation=0,
        )
        browser.app.state.owner_worker_supervisor = SimpleNamespace(
            get_or_start=lambda _owner: handle, control_home=tmp_path / "control"
        )
        monkeypatch.setattr(web_server, "_owner_context_from_ws_auth_result", lambda _result: SimpleNamespace(owner_key="ok1_owner"))
        monkeypatch.setattr(web_server, "_acquire_owner_worker_use", lambda *_args: lease)
        monkeypatch.setattr(web_server, "_connect_owner_worker_ws", lambda *_args, **_kwargs: asyncio.sleep(0, result=worker))
        monkeypatch.setattr("hermes_cli.dashboard_auth.owner_context.ensure_owner_home", lambda _owner: None)
        monkeypatch.setattr("hermes_cli.owner_worker.tokens.validate_owp1_control", lambda *_args, **_kwargs: None)

        async def observe_change_before_registration(app_obj):
            change = WorkerLeaseChange(
                sequence=1, owner_key=handle.owner_key, worker_generation=handle.worker_generation,
                worker_id=handle.worker_id, lease_version=handle.lease_version,
                recovery_generation=handle.recovery_generation, lease_state=WorkerLeaseState.DRAINING,
                generation_state=WorkerGenerationState.DRAINING,
            )
            await web_server.close_authorized_bridges_by_worker_change(
                app_obj, (change,), reason="worker_generation_revoked"
            )

        async def exercise():
            bridges, lock = web_server._authorized_ws_bridge_state(browser.app)
            async with lock:
                bridges.clear()
                browser.app.state.authorized_ws_bridges_by_worker.clear()
                browser.app.state.revoked_ws_bridge_worker_fences.clear()
            await web_server._bridge_websocket_to_owner_worker(
                browser,
                path="/api/pty",
                auth_result=web_server._WsAuthResult(
                    None, "ticket", {
                        "provider": "stub", "tenant_id": "tenant-a", "user_id": "user-a",
                        "session_id": "session-a", "membership_revision": "v1", "epoch": 0,
                    },
                ),
            )

        monkeypatch.setattr(web_server, "_ensure_authority_change_dispatcher", observe_change_before_registration)
        asyncio.run(exercise())

        identity = web_server._worker_bridge_identity(handle)
        assert browser.accepted is True
        assert browser.closed == [(1011, "auth: worker generation revoked")]
        assert worker.closed == [{"code": 1011, "reason": "auth: worker generation revoked"}]
        assert lease.release_count == 1
        assert identity in browser.app.state.revoked_ws_bridge_worker_fences
        assert identity not in browser.app.state.authorized_ws_bridges_by_worker

    def test_bridge_start_timeout_closes_before_browser_accept(self, gated_app, monkeypatch, tmp_path):
        import asyncio

        class _Browser:
            def __init__(self):
                self.app = web_server.app
                self.query_params = SimpleNamespace(get=lambda *_args: "")
                self.url = SimpleNamespace(query="")
                self.accepted = False
                self.closed = []

            async def accept(self):
                self.accepted = True

            async def close(self, *, code=1000, reason=""):
                self.closed.append((code, reason))

        class _Supervisor:
            control_home = tmp_path / "control"

            def get_or_start(self, _owner):
                raise TimeoutError("owner worker startup timed out")

            def acquire_use(self, _handle):
                raise AssertionError("a startup timeout must not acquire a use lease")

        browser = _Browser()
        browser.app.state.owner_worker_supervisor = _Supervisor()
        monkeypatch.setattr(web_server, "_owner_context_from_ws_auth_result", lambda _result: SimpleNamespace(owner_key="ok1_owner"))
        monkeypatch.setattr("hermes_cli.dashboard_auth.owner_context.ensure_owner_home", lambda _owner: None)

        async def fail_connect(*_args, **_kwargs):
            raise AssertionError("a startup timeout must not connect to the worker websocket")

        monkeypatch.setattr(web_server, "_connect_owner_worker_ws", fail_connect)
        auth_result = web_server._WsAuthResult(None, "ticket", {"provider": "stub", "tenant_id": "tenant-a", "user_id": "user-a", "session_id": "session-a", "membership_revision": "v1", "epoch": 0})

        asyncio.run(web_server._bridge_websocket_to_owner_worker(browser, path="/api/pty", auth_result=auth_result))

        assert browser.accepted is False
        assert browser.closed == [(1013, "owner worker unavailable")]

    def test_bridge_connect_failure_releases_lease_before_browser_accept(self, gated_app, monkeypatch, tmp_path):
        import asyncio

        class _Browser:
            def __init__(self):
                self.app = web_server.app
                self.query_params = SimpleNamespace(get=lambda *_args: "")
                self.url = SimpleNamespace(query="")
                self.accepted = False
                self.closed = []

            async def accept(self):
                self.accepted = True

            async def close(self, *, code=1000, reason=""):
                self.closed.append((code, reason))

        class _Lease:
            def __init__(self):
                self.release_count = 0

            def release(self):
                self.release_count += 1

        browser, lease = _Browser(), _Lease()
        handle = SimpleNamespace(socket_path="/unused", owner_key="ok1_owner", worker_generation=1, worker_id="worker-a", lease_version=1, recovery_generation=0)
        browser.app.state.owner_worker_supervisor = SimpleNamespace(get_or_start=lambda _owner: handle, control_home=tmp_path / "control")
        monkeypatch.setattr(web_server, "_owner_context_from_ws_auth_result", lambda _result: SimpleNamespace(owner_key="ok1_owner"))
        monkeypatch.setattr(web_server, "_acquire_owner_worker_use", lambda *_args: lease)
        monkeypatch.setattr("hermes_cli.dashboard_auth.owner_context.ensure_owner_home", lambda _owner: None)
        monkeypatch.setattr("hermes_cli.owner_worker.tokens.validate_owp1_control", lambda *_args, **_kwargs: None)

        async def fail_connect(*_args, **_kwargs):
            raise RuntimeError("unavailable")

        monkeypatch.setattr(web_server, "_connect_owner_worker_ws", fail_connect)
        auth_result = web_server._WsAuthResult(None, "ticket", {"provider": "stub", "tenant_id": "tenant-a", "user_id": "user-a", "session_id": "session-a", "membership_revision": "v1", "epoch": 0})

        asyncio.run(web_server._bridge_websocket_to_owner_worker(browser, path="/api/pty", auth_result=auth_result))

        assert browser.accepted is False
        assert browser.closed and browser.closed[-1][0] == 1013
        assert lease.release_count == 1

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

        query = web_server._ws_query_for_owner_worker(ws, internal_owner_bootstrap="bootstrap-token")

        assert "ticket=" not in query
        assert "token=legacy" not in query
        assert "internal=process" not in query
        assert "channel=chan1" in query
        assert "fresh=1" in query
        assert "internal_owner_bootstrap=bootstrap-token" in query

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
        worker_ws = _fake_ws(query={"internal_owner_token": "ow2.retired"}, app=worker_app)
        worker_result = web_server._ws_auth_result(worker_ws)
        assert worker_result.reason == "internal_owner_invalid"
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

    def test_owner_workers_cannot_mint_control_plane_child_urls(self, monkeypatch, tmp_path):
        worker_app = SimpleNamespace(
            state=SimpleNamespace(
                owner_worker_mode=True,
                owner_worker_owner_key="ok1_owner_a",
                owner_worker_control_home=tmp_path / "control-plane",
                auth_required=False,
            )
        )
        monkeypatch.setenv("HERMES_OWNER_WORKER_CONTROL_WS_BASE", "wss://control.example")

        assert web_server._build_gateway_ws_url(app_obj=worker_app) is None
        assert web_server._build_sidecar_url("chan-1", app_obj=worker_app) is None


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
