"""Authenticated dashboard API availability classification.

The authenticated Control Plane must fail closed by default.  A route is
available only when it is a public bootstrap endpoint, a pure auth/control-plane
endpoint, or an endpoint whose handler immediately proxies to an Owner Worker.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable

from hermes_cli.dashboard_auth.public_paths import is_public_api_route


class AuthenticatedApiBucket(str, Enum):
    PUBLIC_BOOTSTRAP = "public_bootstrap"
    CONTROL_PLANE_AUTH = "control_plane_auth"
    OWNER_WORKER = "owner_worker"
    SESSION_READER = "session_reader"
    PLUGIN_API = "plugin_api"
    LOCAL_ONLY_OR_UNAVAILABLE = "local_only_or_unavailable"
    TOKEN_AUTH_ONLY = "token_auth_only"


CONTROL_PLANE_AUTH_PATHS: frozenset[str] = frozenset({
    "/api/auth/me",
    "/api/auth/ws-ticket",
})
CONTROL_PLANE_AUTH_ROUTES: frozenset[tuple[str, str]] = frozenset({
    ("GET", "/api/system/builtin-assistant-policy"),
    ("PUT", "/api/system/builtin-assistant-policy"),
    ("POST", "/api/messaging/webhook/accounts"),
    ("GET", "/api/employees"),
    ("POST", "/api/employees"),
})
CONTROL_PLANE_AUTH_PREFIXES: tuple[str, ...] = (
    "/api/auth/",
)
OWNER_WORKER_ROUTES: frozenset[tuple[str, str]] = frozenset({
    ("GET", "/api/config"),
    ("GET", "/api/dashboard/font"),
    ("GET", "/api/dashboard/plugins"),
    ("GET", "/api/skills"),
    ("POST", "/api/skills"),
    ("DELETE", "/api/skills"),
    ("GET", "/api/skills/content"),
    ("PUT", "/api/skills/content"),
    ("PUT", "/api/skills/toggle"),
    ("GET", "/api/tools/toolsets"),
    ("GET", "/api/model/info"),
    ("GET", "/api/model/options"),
    ("GET", "/api/model/registrations"),
    ("GET", "/api/model/registrations/catalog"),
    ("POST", "/api/model/registrations"),
    ("PUT", "/api/model/registrations"),
    ("DELETE", "/api/model/registrations"),
    ("PUT", "/api/model/registrations/active"),
    ("GET", "/api/logs"),
    ("GET", "/api/analytics/usage"),
    ("GET", "/api/analytics/models"),
    ("POST", "/api/sessions/bulk-delete"),
    ("DELETE", "/api/sessions/empty"),
    ("POST", "/api/sessions/prune"),
    ("GET", "/api/files"),
    ("DELETE", "/api/files"),
    ("GET", "/api/files/read"),
    ("GET", "/api/files/download"),
    ("GET", "/api/fs/read-data-url"),
    ("POST", "/api/files/upload"),
    ("POST", "/api/files/upload-stream"),
    ("POST", "/api/files/mkdir"),
    ("GET", "/api/employees/catalog"),
})
# Compatibility export for callers that only need the known path inventory.
OWNER_WORKER_PATHS: frozenset[str] = frozenset(path for _method, path in OWNER_WORKER_ROUTES)
_EMPLOYEE_ACTION_METHODS: dict[str, frozenset[str]] = {
    "avatar": frozenset({"GET", "PUT", "DELETE"}),
    "profile": frozenset({"PUT"}),
    "builtin-assistant-personalization": frozenset({"PUT"}),
    "collaboration-policy": frozenset({"PUT"}),
    "lifecycle": frozenset({"PUT"}),
    "rollover": frozenset({"POST"}),
}
_EMPLOYEE_FEISHU_ACTION_METHODS: dict[str, frozenset[str]] = {
    "credentials": frozenset({"PUT"}),
    "lifecycle": frozenset({"PUT"}),
    "test": frozenset({"POST"}),
}
SESSION_READER_ROUTES: frozenset[tuple[str, str]] = frozenset({
    ("GET", "/api/sessions"),
    ("GET", "/api/sessions/search"),
    ("GET", "/api/sessions/composition"),
    ("GET", "/api/sessions/empty/count"),
    ("GET", "/api/sessions/stats"),
})
_SESSION_ITEM_SUFFIXES: frozenset[str] = frozenset({
    "latest-descendant",
    "messages",
    "export",
})
TOKEN_AUTH_ONLY_PATHS: frozenset[str] = frozenset({
    "/api/cron/fire",
})
# Exact method/path inventory registered from validated, trusted dashboard plugin
# routers at mount time. Dynamic path parameters are represented as segment
# templates and matched one segment at a time; arbitrary plugin prefixes never
# inherit access from another route.
_PLUGIN_CONTROL_PLANE_ROUTES: dict[str, set[tuple[str, str]]] = {}
_PLUGIN_OWNER_WORKER_ROUTES: dict[str, set[tuple[str, str]]] = {}
_PLUGIN_WEBSOCKET_ROUTES: dict[str, set[str]] = {}


def clear_plugin_api_routes() -> None:
    """Clear mount-derived policy before rebuilding one Control Plane app."""
    _PLUGIN_CONTROL_PLANE_ROUTES.clear()
    _PLUGIN_OWNER_WORKER_ROUTES.clear()
    _PLUGIN_WEBSOCKET_ROUTES.clear()


def register_plugin_api_routes(
    plugin_name: str,
    *,
    api_target: str,
    routes: Iterable[Any],
    prefix: str,
) -> None:
    """Register exact authenticated availability for one mounted plugin router."""
    if api_target not in {"control-plane", "owner-worker"}:
        raise ValueError("invalid dashboard plugin api_target")
    expected_prefix = f"/api/plugins/{plugin_name}"
    if prefix != expected_prefix:
        raise ValueError("dashboard plugin prefix does not match its manifest name")
    target_registry = (
        _PLUGIN_OWNER_WORKER_ROUTES
        if api_target == "owner-worker"
        else _PLUGIN_CONTROL_PLANE_ROUTES
    )
    target: set[tuple[str, str]] = set()
    websocket_routes: set[str] = set()
    for route in routes:
        route_path = getattr(route, "path", None)
        if not isinstance(route_path, str) or not route_path.startswith("/"):
            continue
        full_path = f"{prefix}{route_path}"
        methods = getattr(route, "methods", None)
        if methods:
            for method in methods:
                target.add((str(method).upper(), full_path))
        elif api_target == "control-plane":
            websocket_routes.add(full_path)
    target_registry[plugin_name] = target
    _PLUGIN_WEBSOCKET_ROUTES[plugin_name] = websocket_routes


def _route_template_matches(template: str, path: str) -> bool:
    template_parts = template.split("/")
    path_parts = path.split("/")
    for index, template_part in enumerate(template_parts):
        if index >= len(path_parts):
            return False
        if template_part == path_parts[index]:
            continue
        if not (
            template_part.startswith("{")
            and template_part.endswith("}")
            and bool(path_parts[index])
        ):
            return False
        converter = template_part[1:-1].partition(":")[2]
        if converter == "path":
            return index == len(template_parts) - 1
    return len(template_parts) == len(path_parts)


def registered_plugin_websocket_route(path: str) -> bool:
    """Return whether path exactly matches a trusted mounted plugin WS route."""
    return any(
        _route_template_matches(template, path)
        for routes in _PLUGIN_WEBSOCKET_ROUTES.values()
        for template in routes
    )


def _registered_plugin_route(path: str, method: str) -> AuthenticatedApiBucket | None:
    for routes in _PLUGIN_OWNER_WORKER_ROUTES.values():
        for registered_method, template in routes:
            if method == registered_method and _route_template_matches(template, path):
                return AuthenticatedApiBucket.OWNER_WORKER
    for routes in _PLUGIN_CONTROL_PLANE_ROUTES.values():
        for registered_method, template in routes:
            if method == registered_method and _route_template_matches(template, path):
                return AuthenticatedApiBucket.PLUGIN_API
    return None


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


def _employee_control_plane_route(path: str, method: str) -> bool:
    parts = path.split("/")
    if len(parts) == 4 and parts[:3] == ["", "api", "employees"]:
        return bool(parts[3]) and method == "GET"
    if (
        len(parts) == 5
        and parts[:3] == ["", "api", "employees"]
        and bool(parts[3])
    ):
        return method in _EMPLOYEE_ACTION_METHODS.get(parts[4], frozenset())
    if (
        len(parts) == 6
        and parts[:3] == ["", "api", "employees"]
        and bool(parts[3])
        and parts[4:] == ["channels", "feishu"]
    ):
        return method == "PUT"
    return (
        len(parts) == 7
        and parts[:3] == ["", "api", "employees"]
        and bool(parts[3])
        and parts[4:6] == ["channels", "feishu"]
        and method
        in _EMPLOYEE_FEISHU_ACTION_METHODS.get(parts[6], frozenset())
    )


def classify_authenticated_api(
    path: str,
    *,
    method: str = "GET",
    token_authenticated: bool = False,
) -> AuthenticatedApiDecision:
    """Classify an authenticated-mode dashboard API method and path.

    Unknown ``/api/*`` routes are deliberately unavailable until they are proven
    owner-insensitive or moved behind Owner Worker routing. Owner-worker routes
    are enumerated by exact method and path so a new write handler cannot silently
    inherit permission from an existing read route.
    """
    method = str(method or "GET").upper()
    if not path.startswith("/api/"):
        return AuthenticatedApiDecision(
            AuthenticatedApiBucket.CONTROL_PLANE_AUTH,
            True,
            "non-api route",
        )
    if is_public_api_route(path, method=method):
        bucket = AuthenticatedApiBucket.TOKEN_AUTH_ONLY if path in TOKEN_AUTH_ONLY_PATHS else AuthenticatedApiBucket.PUBLIC_BOOTSTRAP
        return AuthenticatedApiDecision(bucket, True, bucket.value)
    if (
        (method, path) in CONTROL_PLANE_AUTH_ROUTES
        or _employee_control_plane_route(path, method)
        or path in CONTROL_PLANE_AUTH_PATHS
        or any(path.startswith(prefix) for prefix in CONTROL_PLANE_AUTH_PREFIXES)
    ):
        return AuthenticatedApiDecision(AuthenticatedApiBucket.CONTROL_PLANE_AUTH, True, "control-plane auth")
    if (method, path) in SESSION_READER_ROUTES or (
        method == "GET" and _session_item_path(path)
    ):
        return AuthenticatedApiDecision(AuthenticatedApiBucket.SESSION_READER, True, "session-reader routed")
    if (method, path) in OWNER_WORKER_ROUTES or (
        _session_item_path(path) and method in {"PATCH", "DELETE"}
    ):
        return AuthenticatedApiDecision(AuthenticatedApiBucket.OWNER_WORKER, True, "owner-worker routed")
    plugin_bucket = _registered_plugin_route(path, method)
    if plugin_bucket is not None:
        return AuthenticatedApiDecision(plugin_bucket, True, "plugin-api route")
    if token_authenticated:
        return AuthenticatedApiDecision(AuthenticatedApiBucket.TOKEN_AUTH_ONLY, True, "token authenticated")
    return AuthenticatedApiDecision(
        AuthenticatedApiBucket.LOCAL_ONLY_OR_UNAVAILABLE,
        False,
        "not available in authenticated owner mode",
    )


def authenticated_control_plane_api_allowed(path: str, *, method: str = "GET") -> bool:
    decision = classify_authenticated_api(path, method=method)
    return decision.allowed and decision.bucket in {
        AuthenticatedApiBucket.PUBLIC_BOOTSTRAP,
        AuthenticatedApiBucket.CONTROL_PLANE_AUTH,
        AuthenticatedApiBucket.TOKEN_AUTH_ONLY,
    }


def authenticated_owner_worker_api_allowed(path: str, *, method: str = "GET") -> bool:
    decision = classify_authenticated_api(path, method=method)
    return decision.allowed and decision.bucket == AuthenticatedApiBucket.OWNER_WORKER


def authenticated_session_reader_api_allowed(path: str, *, method: str = "GET") -> bool:
    decision = classify_authenticated_api(path, method=method)
    return decision.allowed and decision.bucket == AuthenticatedApiBucket.SESSION_READER


def authenticated_plugin_api_allowed(path: str, *, method: str = "GET") -> bool:
    decision = classify_authenticated_api(path, method=method)
    return decision.allowed and decision.bucket == AuthenticatedApiBucket.PLUGIN_API
