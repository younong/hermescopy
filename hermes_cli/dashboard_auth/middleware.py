"""Auth-gate middleware for the dashboard.

Engaged when ``app.state.auth_required is True``. The gate's job:

  1. Allow a small set of routes through unauthenticated (login page,
     ``/auth/*`` OAuth round trip, ``/api/auth/providers``, static
     assets).
  2. For everything else, demand a valid session cookie and attach the
     verified :class:`Session` to ``request.state.session``.
  3. On HTML routes, redirect missing/invalid cookies to ``/login``.
     On ``/api/*`` routes, return 401 JSON.

The middleware is a no-op when ``auth_required`` is False (loopback
mode); the legacy ``_SESSION_TOKEN`` ``auth_middleware`` handles those
binds.
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from hermes_cli.dashboard_auth import list_session_providers
from hermes_cli.dashboard_auth.audit import AuditEvent, audit_log
from hermes_cli.dashboard_auth.authority import AuthorityUnavailable, AuthorizationRejected
from hermes_cli.dashboard_auth.base import ProviderError, RefreshExpiredError
from hermes_cli.dashboard_auth.cookies import (
    clear_sso_attempt_cookie,
    read_session_cookies,
    read_sso_attempt_cookie,
    set_sso_attempt_cookie,
)
from hermes_cli.dashboard_auth.public_paths import is_public_api_route

_log = logging.getLogger(__name__)

# Prefixes that bypass the auth gate. Match via ``path == prefix`` or
# ``path.startswith(prefix)`` — so ``/assets/`` (with trailing slash)
# matches ``/assets/foo.css`` but not ``/assetsleak``. Auth-bootstrap
# (login page, OAuth round trip, provider listing) and static asset
# mounts go here.
_GATE_PUBLIC_PREFIXES: tuple[str, ...] = (
    "/auth/login",
    "/auth/callback",
    "/auth/password-login",
    "/auth/logout",
    "/login",
    "/api/auth/providers",
    "/assets/",
    "/favicon.ico",
    "/ds-assets/",
    "/fonts/",
    "/fonts-terminal/",
)


def _password_change_required_response(request: Request) -> Response:
    """Fail closed while preserving the session needed to change its password."""
    del request
    return JSONResponse(
        {
            "error": "password_change_required",
            "detail": "Password change required",
            "password_change_url": "/api/auth/password-change",
        },
        status_code=403,
    )


def _session_requires_password_change(session) -> bool:
    """Return server-derived reset state for durable local Basic sessions only."""
    if session.provider != "basic":
        return False
    from hermes_cli.dashboard_auth import get_provider

    provider = get_provider("basic")
    resolver = getattr(provider, "local_account_for_session", None)
    if not callable(resolver):
        return False
    account = resolver(session)
    return bool(account is not None and account.must_change_password)


def _path_is_allowed_during_password_change(path: str) -> bool:
    """Only recovery endpoints may use a forced-change durable session."""
    return path in {
        "/api/auth/me",
        "/api/auth/password-change",
        "/api/auth/password/change",
    }


def _path_is_public(path: str, *, method: str = "GET") -> bool:
    """True if ``path`` bypasses the OAuth auth gate.

    Two sources of public-ness:

    * :data:`PUBLIC_API_PATHS` — the shared ``/api/*`` allowlist that
      the legacy ``_SESSION_TOKEN`` middleware also honours. Matched
      exactly (no prefix expansion) so adding ``/api/status`` doesn't
      accidentally expose ``/api/status/secret-extension``.
    * :data:`_GATE_PUBLIC_PREFIXES` — auth-bootstrap routes and static
      mounts. Prefix-matched so ``/assets/foo.css`` lights up via
      ``/assets/``.
    """
    if is_public_api_route(path, method=method):
        return True
    return any(
        path == prefix or path.startswith(prefix)
        for prefix in _GATE_PUBLIC_PREFIXES
    )


def _client_ip(request: Request) -> str:
    """Use only the ASGI-resolved peer identity.

    Trusted-proxy middleware may rewrite ``request.client`` before this gate;
    arbitrary request headers are not an identity source here.
    """
    return request.client.host if request.client else ""


class WebSocketSessionRejected(RuntimeError):
    """A browser WS upgrade has no current, matching verified session."""


class WebSocketSessionUnavailable(RuntimeError):
    """The provider needed to revalidate a browser WS session is unavailable."""


def verified_access_session(request):
    """Return a freshly verified cookie session, or ``None`` if untrusted.

    Public auth routes such as logout cannot rely on HTTP middleware state, but
    they may only revoke authority after this same provider verification step.
    A provider outage remains distinguishable from an unknown/expired cookie so
    callers can fail closed without ever creating a wildcard revocation.
    """
    access_token, _refresh_token = read_session_cookies(request)
    if not access_token:
        return None
    unreachable = False
    for provider in list_session_providers():
        try:
            session = provider.verify_session(access_token=access_token)
        except ProviderError:
            unreachable = True
            continue
        if session is not None:
            return session
    if unreachable:
        raise ProviderError("session provider is unavailable")
    return None


def authorization_scope_for_session(session):
    """Build the authority scope from a current verified provider session."""
    from hermes_cli.dashboard_auth import get_provider
    from hermes_cli.dashboard_auth.authority import AuthorizationScope
    from hermes_cli.dashboard_auth.owner_context import owner_context_from_session

    provider = get_provider(session.provider)
    if provider is None or not getattr(provider, "supports_session", True):
        raise ProviderError("session provider is unavailable")
    session_id, membership_revision = provider.authorization_state(session)
    owner = owner_context_from_session(session)
    return AuthorizationScope(
        provider=session.provider,
        tenant_id=owner.tenant_id,
        user_id=session.user_id,
        session_id=session_id,
        membership_revision=membership_revision,
    )


def revoke_session_authority(
    session, *, reason: str
) -> tuple["AuthorizationScope", "AuthorizationState"]:
    """Fail-close revoke all browser credentials bound to a verified session."""
    from hermes_cli.dashboard_auth.authority import AuthorizationScope, AuthorizationState
    from hermes_cli.dashboard_auth.ws_tickets import authority_store

    scope = authorization_scope_for_session(session)
    return scope, authority_store().revoke_and_bump(scope, reason=reason)


async def activate_verified_session_authority(request: Request, session) -> None:
    """Activate current provider authority and close locally superseded bridges."""
    from hermes_cli.dashboard_auth.ws_tickets import authority_store

    state = authority_store().activate(authorization_scope_for_session(session))
    if state.changes:
        from hermes_cli.web_server import close_authorized_bridges_by_changes

        await close_authorized_bridges_by_changes(
            request.app,
            state.changes,
            reason="session_transition",
        )


def verify_websocket_ticket_session(ws, payload: dict[str, object]):
    """Revalidate the cookie session bound to a signed browser WS ticket.

    WebSocket upgrades bypass FastAPI's HTTP middleware, so this deliberately
    repeats the provider verification that the HTTP gate performs.  It never
    refreshes a session during an upgrade: a missing or expired access-token
    cookie requires the browser to re-establish its HTTP session and mint a
    fresh ticket.  The returned session has been matched against every trusted
    principal and authorization-state claim before the caller consumes the
    ticket in the authority store.
    """
    from hermes_cli.dashboard_auth import get_provider
    from hermes_cli.dashboard_auth.owner_context import owner_context_from_session

    provider_name = str(payload.get("provider") or "").strip()
    access_token, _refresh_token = read_session_cookies(ws)
    if not provider_name or not access_token:
        raise WebSocketSessionRejected("session_missing")

    provider = get_provider(provider_name)
    if provider is None or not getattr(provider, "supports_session", True):
        raise WebSocketSessionRejected("provider_unavailable")
    try:
        session = provider.verify_session(access_token=access_token)
    except ProviderError as exc:
        raise WebSocketSessionUnavailable("provider_unavailable") from exc
    if session is None:
        raise WebSocketSessionRejected("session_invalid")
    try:
        if _session_requires_password_change(session):
            raise WebSocketSessionRejected("password_change_required")
    except ProviderError as exc:
        raise WebSocketSessionUnavailable("local_account_unavailable") from exc
    if session.provider != provider_name or session.user_id != str(payload.get("user_id") or ""):
        raise WebSocketSessionRejected("session_principal_mismatch")
    if session.org_id != str(payload.get("org_id") or ""):
        raise WebSocketSessionRejected("session_tenant_mismatch")

    try:
        owner = owner_context_from_session(session)
    except Exception as exc:
        raise WebSocketSessionUnavailable("owner_context_unavailable") from exc
    if (
        owner.tenant_id != str(payload.get("tenant_id") or "")
        or owner.owner_key != str(payload.get("owner_key") or "")
    ):
        raise WebSocketSessionRejected("session_owner_mismatch")

    try:
        session_id, membership_revision = provider.authorization_state(session)
    except ProviderError as exc:
        raise WebSocketSessionUnavailable("authorization_state_unavailable") from exc
    if (
        session_id != str(payload.get("session_id") or "")
        or membership_revision != str(payload.get("membership_revision") or "")
    ):
        raise WebSocketSessionRejected("membership_revision_mismatch")
    return session


def _unauth_response(request: Request, *, reason: str) -> Response:
    """API routes → 401 JSON with ``login_url``; HTML routes → 302 → /login.

    The JSON envelope carries a ``login_url`` field with a ``next=`` query
    string so the SPA's global 401 handler can drop the user back where
    they were after re-auth. The contract is intentionally simple so any
    fetch-wrapper can implement the redirect without parsing details:

        if response.status === 401 && body.error in ("unauthenticated",
                                                       "session_expired"):
            window.location.assign(body.login_url);

    HTML redirects also carry the ``next=`` query string so direct
    navigation to ``/sessions`` (etc.) without a cookie comes back to
    ``/sessions`` after login.

    Under a reverse proxy with ``X-Forwarded-Prefix: /hermes``, the
    ``login_url`` is prefixed (``/hermes/login?next=...``) so the
    browser's window.location.assign / Location: follow lands on the
    proxied login page rather than the bare ``/login`` (which the
    proxy doesn't route to the dashboard).
    """
    from hermes_cli.dashboard_auth.prefix import prefix_from_request

    path = request.url.path
    next_param = _safe_next_target(request)
    prefix = prefix_from_request(request)
    login_url = (
        f"{prefix}/login?next={next_param}" if next_param
        else f"{prefix}/login"
    )

    if path.startswith("/api/"):
        # API routes never get redirects: the browser fetch() API would
        # follow a 302 into the cross-origin OAuth dance opaquely. Return
        # 401 with a structured envelope so the SPA can full-page-navigate
        # to login_url.
        error_code = (
            "session_expired"
            if reason == "invalid_or_expired_session"
            else "unauthenticated"
        )
        return JSONResponse(
            {
                "error": error_code,
                "detail": "Unauthorized",
                "reason": reason,
                "login_url": login_url,
            },
            status_code=401,
        )
    return RedirectResponse(url=login_url, status_code=302)


def _auto_sso_response(request: Request) -> Response | None:
    """Maybe auto-initiate the portal OAuth redirect on an unauth HTML load.

    Returns a 302 → ``/auth/login`` (the existing OAuth-initiation route)
    when ALL of the following hold, else ``None`` (caller falls back to the
    ordinary ``/login`` interstitial):

      * the request is an HTML document navigation, not an ``/api/*`` fetch
        (a fetch() would follow the 302 into the cross-origin OAuth dance
        opaquely — same reason ``_unauth_response`` never redirects APIs);
      * exactly ONE interactive provider is registered and it is redirect
        based — a password provider must render its credential form on
        ``/login`` instead; if password and OAuth providers coexist, the
        interstitial preserves the user's choice;
      * the one-shot loop-guard marker is ABSENT. Its presence means we
        already bounced to the portal once and came back still
        unauthenticated (no portal session) — auto-redirecting again would
        ping-pong, so we fall through to ``/login`` and clear the marker.

    The portal ``/oauth/authorize`` auto-approves any current member of the
    dashboard's org and is a silent 302 when the user already holds a portal
    session, so for the common case (clicked a dashboard link while signed
    in to the portal) this removes the interstitial CLICK entirely. It
    removes a click, not a security check: the redirect lands on
    ``/auth/login`` which runs the unchanged PKCE auth-code flow.
    """
    path = request.url.path
    # APIs never auto-redirect (see _unauth_response). Only document loads.
    if path.startswith("/api/"):
        return None

    # Already bounced once and still no session → portal has no session for
    # this user. Stop here, clear the marker, let /login render.
    if read_sso_attempt_cookie(request):
        from hermes_cli.dashboard_auth.prefix import prefix_from_request
        resp = _unauth_response(request, reason="no_cookie")
        clear_sso_attempt_cookie(resp, prefix=prefix_from_request(request))
        return resp

    # list_session_providers() already filters on supports_session=True, so
    # token-only credentials (drain/service providers) are never candidates.
    # Password-capable providers authenticate through the /login form rather
    # than the OAuth-start route. Their presence also means the user must see
    # the login page to choose between password and any redirect providers.
    providers = list_session_providers()
    if len(providers) != 1 or getattr(providers[0], "supports_password", False):
        # Zero → nothing to redirect to. Multiple providers or a password
        # provider → render /login so the user can select an available flow.
        return None

    from hermes_cli.dashboard_auth.prefix import prefix_from_request

    provider = providers[0]
    prefix = prefix_from_request(request)
    next_param = _safe_next_target(request)
    from urllib.parse import quote
    auth_login = f"{prefix}/auth/login?provider={quote(provider.name, safe='')}"
    if next_param:
        auth_login = f"{auth_login}&next={next_param}"

    resp = RedirectResponse(url=auth_login, status_code=302)
    # Drop the one-shot marker so a return trip that's STILL unauthenticated
    # (portal had no session) trips the guard above next time instead of
    # looping. Detect HTTPS for the Secure flag the same way the auth routes
    # do; bind Path via the active prefix.
    from hermes_cli.dashboard_auth.cookies import detect_https
    set_sso_attempt_cookie(
        resp, use_https=detect_https(request), prefix=prefix,
    )
    audit_log(
        AuditEvent.LOGIN_START,
        provider=provider.name,
        reason="auto_sso",
        ip=_client_ip(request),
    )
    return resp


def _rejected_session_response(request: Request, *, code: str) -> Response:
    """Clear a provider-verified session rejected by the authority store."""
    _log.info("dashboard-auth: session authority rejected: %s", code)
    response = _unauth_response(request, reason="invalid_or_expired_session")

    from hermes_cli.dashboard_auth.cookies import clear_session_cookies
    from hermes_cli.dashboard_auth.prefix import prefix_from_request

    clear_session_cookies(response, prefix=prefix_from_request(request))
    return response


def _authority_unavailable_response() -> Response:
    """Fail closed without discarding cookies during a transient outage."""
    return JSONResponse(
        {"detail": "Authorization state is unavailable"},
        status_code=503,
    )


def _safe_next_target(request: Request) -> str:
    """Build the URL-encoded ``next`` query value, or empty string.

    Only same-origin relative paths are accepted; absolute URLs or
    ``//evil.com`` open-redirect attempts are silently dropped. The empty
    string return means the caller produces a bare ``/login`` URL — fine,
    user lands at the dashboard root after re-auth.
    """
    path = request.url.path
    # Reject anything that doesn't start with "/" or starts with "//"
    # (protocol-relative URL — would open-redirect to an attacker host).
    if not path or not path.startswith("/") or path.startswith("//"):
        return ""
    # Don't redirect back to the auth routes themselves — that loops.
    if any(
        path == p or path.startswith(p)
        for p in ("/login", "/auth/", "/api/auth/")
    ):
        return ""
    # Reject ALL ``/api/*`` paths. The 401-envelope code path fires for
    # any unauthenticated SPA fetch (e.g. ``GET /api/analytics/models``
    # from ModelsPage), and the SPA's global 401 handler full-page
    # navigates to ``login_url``. After the OAuth round trip the user
    # would land on the API URL and see raw JSON instead of the
    # dashboard. SPA routes survive (they don't start with ``/api/``);
    # the SPA's own ``sessionStorage["hermes.lastLocation"]`` fallback
    # in ``web/src/lib/api.ts`` covers the deep-link case.
    if path == "/api" or path.startswith("/api/"):
        return ""
    # Preserve query string if present (e.g. /sessions?page=2).
    query = request.url.query
    target = f"{path}?{query}" if query else path
    # urlencode the whole thing as a single value.
    from urllib.parse import quote
    return quote(target, safe="")


async def gated_auth_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """Engaged only when ``app.state.auth_required is True``.

    No-op pass-through in loopback mode so the legacy auth_middleware can
    handle those binds via ``_SESSION_TOKEN``.
    """
    if not getattr(request.app.state, "auth_required", False):
        return await call_next(request)

    # A request already authenticated by the token-auth seam (a service caller
    # on a registered token route) carries ``token_authenticated`` — it is NOT
    # a cookie session and must not be bounced to /login. Pass it through; the
    # seam already attached ``request.state.token_principal``.
    if getattr(request.state, "token_authenticated", False):
        return await call_next(request)

    path = request.url.path
    if _path_is_public(path, method=request.method):
        return await call_next(request)

    at, _rt = read_session_cookies(request)
    if not at and not _rt:
        # Neither token present — no session at all. Nothing to verify or
        # refresh. Before falling back to the /login interstitial, try to
        # silently bounce the user through the portal OAuth flow: the portal
        # auto-approves org members and 302s straight back when they already
        # hold a portal session, so the interstitial click is pure friction
        # for the common case. The one-shot loop-guard inside _auto_sso_response
        # prevents a ping-pong when the portal genuinely has no session.
        auto = _auto_sso_response(request)
        if auto is not None:
            return auto
        return _unauth_response(request, reason="no_cookie")

    # Try every registered provider's verify_session in turn. Providers
    # MUST return None for tokens they don't recognise (not raise). This
    # lets multiple providers stack — the first one that recognises a
    # token wins.
    #
    # When the access-token cookie is absent but a refresh-token cookie is
    # present, skip verification and go straight to the refresh path below.
    # This is the COMMON expiry case, not an edge case: the access-token
    # cookie is set with ``Max-Age = access_token_expires_in`` (~15 min), so
    # the browser EVICTS it the moment the token lapses, while the
    # refresh-token cookie lives for 30 days. From that point the browser
    # sends only ``hermes_session_rt``. If we bailed on ``not at`` here we'd
    # bounce the user to /login on every expiry despite holding a perfectly
    # good refresh token — defeating the whole transparent-refresh feature.
    session = None
    if at:
        # Try every registered provider's verify_session in turn. A provider
        # that doesn't recognise the token returns None and we move on; the
        # first provider that returns a Session wins.
        #
        # A provider may instead raise ProviderError (its IDP/JWKS is
        # unreachable, so it can neither confirm nor deny the token). With
        # multiple providers stacked, that MUST NOT abort the chain — the
        # token may belong to a *different*, reachable provider. (Concretely:
        # a self-hosted-OIDC session hits the `nous` provider first, which
        # tries to reach Nous Portal's JWKS; if that's unreachable it raises,
        # but the `self-hosted` provider can still verify the token.) So we
        # remember the unreachable error and keep going. Only if NO provider
        # verifies the token AND at least one was unreachable do we surface a
        # 503 — distinguishing "transient IDP outage" (don't force re-login)
        # from "token genuinely invalid" (fall through to refresh/relogin).
        unreachable_provider: str | None = None
        for provider in list_session_providers():
            try:
                session = provider.verify_session(access_token=at)
            except ProviderError as e:
                _log.warning(
                    "dashboard-auth: provider %r unreachable during verify: %s",
                    provider.name, e,
                )
                audit_log(
                    AuditEvent.SESSION_VERIFY_FAILURE,
                    provider=provider.name,
                    reason="provider_unreachable",
                    ip=_client_ip(request),
                )
                if unreachable_provider is None:
                    unreachable_provider = provider.name
                continue
            if session is not None:
                break
        if session is None and unreachable_provider is not None:
            # No provider could verify the token and at least one couldn't be
            # reached — treat as a transient outage rather than forcing a
            # re-login through a (possibly also-unreachable) refresh.
            return JSONResponse(
                {"detail": f"Auth provider {unreachable_provider!r} unreachable"},
                status_code=503,
            )

    if session is None:
        # Access token is expired/invalid. Before forcing re-login, try to
        # rotate it using the refresh token (if the session cookie carries
        # one). On success we re-set the rotated cookies on the response and
        # serve the request transparently; on RefreshExpiredError (RT dead /
        # revoked / reuse-detected) we fall through to clear-and-relogin.
        refreshed = _attempt_refresh(request, refresh_token=_rt)
        if refreshed is not None:
            new_session, refreshing_provider = refreshed
            try:
                # A rotating refresh can carry a new session, tenant, or
                # membership revision. Activate the new scope before serving
                # any request; AuthorityStore atomically invalidates the old
                # active scope for the same principal when necessary.
                await activate_verified_session_authority(request, new_session)
            except AuthorizationRejected as exc:
                return _rejected_session_response(request, code=exc.code)
            except AuthorityUnavailable as exc:
                _log.warning("dashboard-auth: refresh authority activation failed: %s", exc)
                return _authority_unavailable_response()
            except Exception:
                _log.exception("dashboard-auth: unexpected refresh authority activation failure")
                return _authority_unavailable_response()
            request.state.session = new_session
            try:
                requires_password_change = _session_requires_password_change(new_session)
            except ProviderError as exc:
                _log.warning("dashboard-auth: local account state unavailable: %s", exc)
                response = JSONResponse(
                    {"detail": "Local account authority is unavailable"}, status_code=503
                )
                from hermes_cli.dashboard_auth.cookies import clear_session_cookies
                from hermes_cli.dashboard_auth.prefix import prefix_from_request

                clear_session_cookies(response, prefix=prefix_from_request(request))
                return response
            if requires_password_change and not _path_is_allowed_during_password_change(path):
                response = _password_change_required_response(request)
                from hermes_cli.dashboard_auth.cookies import (
                    detect_https,
                    set_session_cookies,
                )
                from hermes_cli.dashboard_auth.prefix import prefix_from_request

                set_session_cookies(
                    response,
                    access_token=new_session.access_token,
                    refresh_token=new_session.refresh_token,
                    access_token_expires_in=_expires_in_seconds(new_session),
                    use_https=detect_https(request),
                    prefix=prefix_from_request(request),
                )
                return response
            from hermes_cli.web_server import _authenticated_owner_control_plane_gate_response

            gated = _authenticated_owner_control_plane_gate_response(request)
            if gated is not None:
                response = gated
            else:
                response = await call_next(request)
            # Persist the ROTATED tokens. Portal rotates the refresh token on
            # every refresh and runs reuse-detection, so writing the new RT
            # back is mandatory: a stale RT cookie would replay a rotated
            # token on the next refresh and (outside Portal's grace) revoke
            # the whole session. Bind cookie Secure/Path to the request shape.
            from hermes_cli.dashboard_auth.cookies import (
                detect_https,
                set_session_cookies,
            )
            from hermes_cli.dashboard_auth.prefix import prefix_from_request

            set_session_cookies(
                response,
                access_token=new_session.access_token,
                refresh_token=new_session.refresh_token,
                access_token_expires_in=_expires_in_seconds(new_session),
                use_https=detect_https(request),
                prefix=prefix_from_request(request),
            )
            audit_log(
                AuditEvent.REFRESH_SUCCESS,
                provider=refreshing_provider,
                user_id=new_session.user_id,
                ip=_client_ip(request),
            )
            return response

        audit_log(
            AuditEvent.SESSION_VERIFY_FAILURE,
            reason="no_provider_recognises",
            ip=_client_ip(request),
        )
        response = _unauth_response(request, reason="invalid_or_expired_session")
        # Clear the dead cookies so the browser doesn't keep sending them.
        # Refresh already failed (or there was no RT), so the only correct
        # next step is full re-auth via /login. Importing locally avoids a
        # cycle with cookies → middleware at module load. Pass the active
        # prefix so the deletion's Path matches the set-Path (otherwise
        # the browser ignores it).
        from hermes_cli.dashboard_auth.cookies import clear_session_cookies
        from hermes_cli.dashboard_auth.prefix import prefix_from_request
        clear_session_cookies(response, prefix=prefix_from_request(request))
        return response

    try:
        await activate_verified_session_authority(request, session)
    except AuthorizationRejected as exc:
        return _rejected_session_response(request, code=exc.code)
    except AuthorityUnavailable as exc:
        _log.warning("dashboard-auth: session authority activation failed: %s", exc)
        return _authority_unavailable_response()
    except Exception:
        _log.exception("dashboard-auth: unexpected session authority activation failure")
        return _authority_unavailable_response()
    request.state.session = session
    try:
        requires_password_change = _session_requires_password_change(session)
    except ProviderError as exc:
        _log.warning("dashboard-auth: local account state unavailable: %s", exc)
        return JSONResponse(
            {"detail": "Local account authority is unavailable"}, status_code=503
        )
    if requires_password_change and not _path_is_allowed_during_password_change(path):
        return _password_change_required_response(request)
    from hermes_cli.web_server import _authenticated_owner_control_plane_gate_response

    gated = _authenticated_owner_control_plane_gate_response(request)
    if gated is not None:
        return gated
    return await call_next(request)


def _expires_in_seconds(session) -> int:
    """Seconds until the access token's ``exp``, floored at 60.

    Mirrors the auth-route's ``max(60, exp - now)`` so the access-token
    cookie's Max-Age tracks the token lifetime even on a slightly skewed
    clock. ``time`` imported locally to keep the module's import surface
    minimal.
    """
    import time

    return max(60, int(session.expires_at) - int(time.time()))


def _attempt_refresh(request: Request, *, refresh_token):
    """Try to rotate an expired session via the refresh token.

    Returns ``(new_session, provider_name)`` on success, or ``None`` if
    there's no RT or every provider's ``refresh_session`` failed with
    ``RefreshExpiredError`` (dead/revoked/reuse-detected RT → force re-login).

    A ``ProviderError`` (Portal unreachable) is NOT swallowed into a re-login
    here — re-raising would 500 the request; instead we log and return None so
    the caller forces a clean re-login, which is the safer UX than a hard
    error on a transient network blip during the narrow refresh window.
    """
    if not refresh_token:
        return None
    for provider in list_session_providers():
        try:
            new_session = provider.refresh_session(refresh_token=refresh_token)
        except RefreshExpiredError:
            # This provider owns the RT but it's dead — stop trying others
            # (an RT belongs to exactly one provider) and force re-login.
            audit_log(
                AuditEvent.REFRESH_FAILURE,
                provider=provider.name,
                reason="refresh_expired",
                ip=_client_ip(request),
            )
            return None
        except ProviderError as e:
            _log.warning(
                "dashboard-auth: provider %r unreachable during refresh: %s",
                provider.name, e,
            )
            audit_log(
                AuditEvent.REFRESH_FAILURE,
                provider=provider.name,
                reason="provider_unreachable",
                ip=_client_ip(request),
            )
            return None
        if new_session is not None:
            return new_session, provider.name
    return None

