"""Owner-bound internal tokens for Control Plane -> Owner Worker calls."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any, Mapping

_TOKEN_VERSION = "ow2"
_DEFAULT_TTL_SECONDS = 60
_CHILD_TOKEN_TTL_ENV = "HERMES_OWNER_WORKER_CHILD_TOKEN_TTL_SECONDS"
_DEFAULT_CHILD_TOKEN_TTL_SECONDS = 12 * 60 * 60
_MAX_CHILD_TOKEN_TTL_SECONDS = 24 * 60 * 60

AUD_OWNER_WORKER_HTTP = "owner-worker-http"
AUD_OWNER_WORKER_WS = "owner-worker-ws"
AUD_CONTROL_PLANE_WS = "control-plane-ws"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def _token_secret(control_home: str | Path | None = None) -> bytes:
    explicit = os.environ.get("HERMES_OWNER_WORKER_TOKEN_SECRET", "").strip()

    # The supervisor passes this to worker subprocesses. Falling back keeps unit
    # tests and hand-launched workers usable without depending on auth modules.
    control_home_value = str(control_home or os.environ.get("HERMES_CONTROL_HOME", "")).strip()
    if control_home_value:
        secret_path = Path(control_home_value) / "owner_worker_token_secret"
    else:
        from hermes_constants import get_hermes_home

        secret_path = get_hermes_home() / "control-plane" / "owner_worker_token_secret"

    try:
        secret = secret_path.read_bytes()
        if secret:
            if explicit and not hmac.compare_digest(explicit.encode("utf-8"), secret):
                raise RuntimeError(
                    "HERMES_OWNER_WORKER_TOKEN_SECRET does not match persisted owner worker token secret "
                    f"at {secret_path}; refusing to split Control Plane/worker credentials"
                )
            return secret
    except FileNotFoundError:
        pass

    if explicit:
        return explicit.encode("utf-8")

    secret_path.parent.mkdir(parents=True, exist_ok=True)
    secret = secrets.token_bytes(32)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        fd = os.open(secret_path, flags, 0o600)
    except FileExistsError:
        deadline = time.monotonic() + 2.0
        while True:
            existing = secret_path.read_bytes()
            if existing:
                return existing
            if time.monotonic() >= deadline:
                raise RuntimeError(f"owner worker token secret exists but is empty: {secret_path}")
            time.sleep(0.02)
    with os.fdopen(fd, "wb") as handle:
        handle.write(secret)
    try:
        os.chmod(secret_path, 0o600)
    except OSError:
        pass
    return secret


def child_token_ttl_seconds(source: Mapping[str, str] | None = None) -> int:
    """Return the bounded TTL for worker-spawned child WS attach URLs."""
    src = source if source is not None else os.environ
    raw = str(src.get(_CHILD_TOKEN_TTL_ENV, "") or "").strip()
    try:
        value = int(raw) if raw else _DEFAULT_CHILD_TOKEN_TTL_SECONDS
    except (TypeError, ValueError):
        value = _DEFAULT_CHILD_TOKEN_TTL_SECONDS
    return max(1, min(value, _MAX_CHILD_TOKEN_TTL_SECONDS))


def _normalize_path(path: str) -> str:
    path = str(path or "").strip()
    if not path:
        raise ValueError("path is required for owner worker token")
    return path.split("?", 1)[0]


def mint_internal_token(
    owner_key: str,
    *,
    audience: str = AUD_OWNER_WORKER_HTTP,
    path: str = "/internal/health",
    ttl_seconds: int = _DEFAULT_TTL_SECONDS,
    control_home: str | Path | None = None,
) -> str:
    """Return a signed token bound to an owner, audience, path, and expiry."""
    if ttl_seconds is None:  # type: ignore[comparison-overlap]
        raise ValueError("owner worker tokens must expire")
    now = int(time.time())
    payload: dict[str, Any] = {
        "v": _TOKEN_VERSION,
        "owner_key": owner_key,
        "aud": str(audience or ""),
        "path": _normalize_path(path),
        "iat": now,
        "exp": now + max(1, int(ttl_seconds)),
        "nonce": secrets.token_urlsafe(12),
    }
    payload_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    body = _b64url(payload_bytes)
    sig = hmac.new(_token_secret(control_home), body.encode("ascii"), hashlib.sha256).digest()
    return f"{body}.{_b64url(sig)}"


def validate_internal_token_payload(
    token: str,
    *,
    audience: str | None = None,
    path: str | None = None,
    now: int | None = None,
    control_home: str | Path | None = None,
) -> dict[str, Any] | None:
    """Return the signed, unexpired token payload, or None when invalid."""
    if not token or "." not in token:
        return None
    body, sig = token.rsplit(".", 1)
    expected = hmac.new(_token_secret(control_home), body.encode("ascii"), hashlib.sha256).digest()
    try:
        actual = _b64url_decode(sig)
    except Exception:
        return None
    if not hmac.compare_digest(actual, expected):
        return None
    try:
        payload = json.loads(_b64url_decode(body).decode("utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict) or payload.get("v") != _TOKEN_VERSION:
        return None
    current = int(time.time()) if now is None else int(now)
    try:
        iat = int(payload.get("iat", 0))
        exp = int(payload.get("exp"))
    except (TypeError, ValueError):
        return None
    if iat > current:
        return None
    if current > exp:
        return None
    if not str(payload.get("owner_key", "")):
        return None
    payload_aud = str(payload.get("aud") or "")
    payload_path = str(payload.get("path") or "")
    if not payload_aud or not payload_path:
        return None
    if audience is not None and not hmac.compare_digest(payload_aud, str(audience)):
        return None
    if path is not None and not hmac.compare_digest(payload_path, _normalize_path(path)):
        return None
    return payload


def validate_internal_token(
    token: str,
    owner_key: str,
    *,
    audience: str | None = None,
    path: str | None = None,
    now: int | None = None,
    control_home: str | Path | None = None,
) -> bool:
    """Return True only for valid tokens matching owner, audience, and path."""
    payload = validate_internal_token_payload(
        token,
        audience=audience,
        path=path,
        now=now,
        control_home=control_home,
    )
    return bool(
        payload is not None
        and owner_key
        and hmac.compare_digest(str(payload.get("owner_key", "")), owner_key)
    )
