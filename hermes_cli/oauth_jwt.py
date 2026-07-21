"""Helpers for reading non-authoritative metadata from OAuth JWTs."""

from __future__ import annotations

from typing import Any, Dict

import jwt


def decode_unverified_oauth_jwt_claims(token: Any) -> Dict[str, Any]:
    """Decode metadata-only OAuth claims without verifying the JWT.

    The result is untrusted and must never be used to authenticate a user or
    authorize an action. Callers may inspect hints such as expiry or account ID
    while the actual token remains subject to verification by its OAuth service.
    """
    if not isinstance(token, str) or token.count(".") != 2:
        return {}
    try:
        # Some OAuth fixtures and legacy providers emit a non-JSON header even
        # though the payload is a normal JWT claims object. This helper only
        # reads payload metadata, so normalize the ignored header and signature
        # before delegating payload parsing to PyJWT.
        _, payload, _ = token.split(".")
        unsigned_token = f"e30.{payload}."
        claims = jwt.decode(
            unsigned_token,
            options={
                "verify_signature": False,
                "verify_exp": False,
                "verify_aud": False,
                "verify_iss": False,
            },
        )
    except (jwt.PyJWTError, TypeError, ValueError):
        return {}
    return claims if isinstance(claims, dict) else {}
