"""Authenticated dashboard API availability classification.

The authenticated Control Plane must fail closed by default.  A route is
available only when it is a public bootstrap endpoint, a pure auth/control-plane
endpoint, or an endpoint whose handler immediately proxies to an Owner Worker.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from hermes_cli.dashboard_auth.public_paths import PUBLIC_API_PATHS


class AuthenticatedApiBucket(str, Enum):
    PUBLIC_BOOTSTRAP = "public_bootstrap"
    CONTROL_PLANE_AUTH = "control_plane_auth"
    OWNER_WORKER = "owner_worker"
    LOCAL_ONLY_OR_UNAVAILABLE = "local_only_or_unavailable"
    TOKEN_AUTH_ONLY = "token_auth_only"


CONTROL_PLANE_AUTH_PATHS: frozenset[str] = frozenset({
    "/api/auth/me",
    "/api/auth/ws-ticket",
})
CONTROL_PLANE_AUTH_PREFIXES: tuple[str, ...] = (
    "/api/auth/",
)
OWNER_WORKER_PATHS: frozenset[str] = frozenset({
    "/api/model/info",
    "/api/analytics/usage",
    "/api/analytics/models",
    "/api/sessions",
    "/api/sessions/search",
    "/api/sessions/bulk-delete",
    "/api/sessions/empty/count",
    "/api/sessions/empty",
    "/api/sessions/stats",
    "/api/sessions/prune",
})
_SESSION_ITEM_SUFFIXES: frozenset[str] = frozenset({
    "latest-descendant",
    "messages",
    "export",
})
TOKEN_AUTH_ONLY_PATHS: frozenset[str] = frozenset({
    "/api/cron/fire",
})


@dataclass(frozen=True)
class AuthenticatedApiDecision:
    bucket: AuthenticatedApiBucket
    allowed: bool
    reason: str


def _session_item_path(path: str) -> bool:
    parts = path.split("/")
    # "", "api", "sessions", "<session_id>"
    if len(parts) == 4 and parts[:3] == ["", "api", "sessions"] and parts[3]:
        return True
    # "", "api", "sessions", "<session_id>", "messages" etc.
    return (
        len(parts) == 5
        and parts[:3] == ["", "api", "sessions"]
        and bool(parts[3])
        and parts[4] in _SESSION_ITEM_SUFFIXES
    )


def classify_authenticated_api(path: str, *, token_authenticated: bool = False) -> AuthenticatedApiDecision:
    """Classify an authenticated-mode dashboard API path.

    Unknown ``/api/*`` routes are deliberately unavailable until they are proven
    owner-insensitive or moved behind Owner Worker routing.  Owner-worker paths
    are enumerated instead of prefix-allowed so adding a new Control Plane route
    under ``/api/sessions`` does not silently bypass the fail-closed gate.
    """
    if not path.startswith("/api/"):
        return AuthenticatedApiDecision(
            AuthenticatedApiBucket.CONTROL_PLANE_AUTH,
            True,
            "non-api route",
        )
    if path in PUBLIC_API_PATHS:
        bucket = AuthenticatedApiBucket.TOKEN_AUTH_ONLY if path in TOKEN_AUTH_ONLY_PATHS else AuthenticatedApiBucket.PUBLIC_BOOTSTRAP
        return AuthenticatedApiDecision(bucket, True, bucket.value)
    if path in CONTROL_PLANE_AUTH_PATHS or any(path.startswith(prefix) for prefix in CONTROL_PLANE_AUTH_PREFIXES):
        return AuthenticatedApiDecision(AuthenticatedApiBucket.CONTROL_PLANE_AUTH, True, "control-plane auth")
    if path in OWNER_WORKER_PATHS or _session_item_path(path):
        return AuthenticatedApiDecision(AuthenticatedApiBucket.OWNER_WORKER, True, "owner-worker routed")
    if token_authenticated:
        return AuthenticatedApiDecision(AuthenticatedApiBucket.TOKEN_AUTH_ONLY, True, "token authenticated")
    return AuthenticatedApiDecision(
        AuthenticatedApiBucket.LOCAL_ONLY_OR_UNAVAILABLE,
        False,
        "not available in authenticated owner mode",
    )


def authenticated_control_plane_api_allowed(path: str) -> bool:
    decision = classify_authenticated_api(path)
    return decision.allowed and decision.bucket in {
        AuthenticatedApiBucket.PUBLIC_BOOTSTRAP,
        AuthenticatedApiBucket.CONTROL_PLANE_AUTH,
        AuthenticatedApiBucket.TOKEN_AUTH_ONLY,
    }


def authenticated_owner_worker_api_allowed(path: str) -> bool:
    decision = classify_authenticated_api(path)
    return decision.allowed and decision.bucket == AuthenticatedApiBucket.OWNER_WORKER
