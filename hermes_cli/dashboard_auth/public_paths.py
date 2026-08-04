"""Minimal unauthenticated API allowlist for the Web control plane.

The authenticated dashboard gate consults this module before reading a session
cookie. Keep the list limited to read-only bootstrap/liveness endpoints and
endpoints with an independent, purpose-specific authentication mechanism.
"""
from __future__ import annotations

import re

PUBLIC_API_PATHS: frozenset[str] = frozenset({
    # External liveness probe. It exposes only non-sensitive runtime metadata.
    "/api/status",
    # Read-only schema feeds needed before the Config page has loaded.
    "/api/config/defaults",
    "/api/config/schema",
    # Read-only dashboard theme manifests.
    "/api/dashboard/themes",
    # NAS managed-cron callback. The handler verifies its own short-lived JWT.
    "/api/cron/fire",
})

_ENROLLMENT_ITEM_RE = re.compile(r"^/api/public/ilink/enrollments/enr_[0-9a-f]{32}$")


def is_public_api_route(path: str, *, method: str = "GET") -> bool:
    """Return whether this exact method/path may bypass dashboard auth."""
    method = str(method or "GET").upper()
    if path in PUBLIC_API_PATHS:
        return True
    if method == "POST" and path == "/api/public/ilink/enrollments":
        return True
    return method == "GET" and bool(_ENROLLMENT_ITEM_RE.fullmatch(path))
