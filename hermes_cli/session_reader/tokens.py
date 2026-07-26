"""Exact generation-fenced capabilities for owner Session Readers."""
from __future__ import annotations

import base64
import json
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

if TYPE_CHECKING:
    from hermes_cli.dashboard_auth.authority import AuthorityStore, SessionReaderAuthorityLease

AUD_SESSION_READER_HTTP = "session-reader-http"
SCOPE_SESSION_READER_HTTP = "session-reader:http"
_TOKEN_VERSION = "owc1"
_PROTOCOL_VERSION = "owc1"
_DEFAULT_TTL_SECONDS = 30
_MAX_TTL_SECONDS = 60


class SessionReaderCapabilityInvalid(ValueError):
    """A Reader capability is malformed, unauthentic, expired, or fenced."""


@dataclass(frozen=True)
class SessionReaderCapabilityVerifier:
    keys: Mapping[str, Ed25519PublicKey]


@dataclass(frozen=True)
class SessionReaderCapabilityClaims:
    issuer_key_version: str
    owner_key: str
    reader_generation: int
    reader_id: str
    lease_version: int
    recovery_generation: int
    audience: str
    scope: str
    path: str
    issued_at: int
    expires_at: int
    jti: str


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _normalize_path(path: str) -> str:
    value = str(path or "").strip().split("?", 1)[0]
    if not value or not value.startswith("/"):
        raise ValueError("capability path is required")
    return value


def _reader_lease_active(lease: Any) -> bool:
    return str(getattr(lease, "state", "")).lower().rsplit(".", 1)[-1] in {"starting", "active"}


def session_reader_capability_public_config(control_home: str | Path | None = None) -> dict[str, str]:
    """Return public verifier material using Reader-specific environment names."""
    from hermes_cli.owner_worker.tokens import _signing_record

    record = _signing_record(control_home)
    return {
        "HERMES_SESSION_READER_CAPABILITY_ISSUER": record["version"],
        "HERMES_SESSION_READER_CAPABILITY_PUBLIC_KEY": _b64url(record["public_key"]),
        "HERMES_SESSION_READER_CAPABILITY_RETAINED_PUBLIC_KEYS": json.dumps(
            {version: _b64url(key) for version, key in record["retained_public_keys"].items()},
            sort_keys=True,
            separators=(",", ":"),
        ),
    }


def mint_session_reader_capability(
    lease: "SessionReaderAuthorityLease",
    *,
    path: str,
    control_home: str | Path | None = None,
    signing_record: Mapping[str, Any] | None = None,
    ttl_seconds: int = _DEFAULT_TTL_SECONDS,
    now: int | None = None,
) -> str:
    """Mint a short-lived, exact-Reader, path-bound HTTP capability."""
    if not _reader_lease_active(lease):
        raise ValueError("reader capability lease must be starting or active")
    ttl = int(ttl_seconds)
    if ttl < 1 or ttl > _MAX_TTL_SECONDS:
        raise ValueError("reader capability ttl is outside the permitted bound")
    if signing_record is None:
        from hermes_cli.owner_worker.tokens import _signing_record

        record = _signing_record(control_home)
    else:
        record = signing_record
    issued_at = int(time.time()) if now is None else int(now)
    claims = {
        "v": _TOKEN_VERSION,
        "kind": "session-reader",
        "iss": record["version"],
        "owner_key": lease.owner_key,
        "generation": lease.reader_generation,
        "worker_id": lease.reader_id,
        "lease_version": lease.lease_version,
        "recovery_generation": lease.recovery_generation,
        "aud": AUD_SESSION_READER_HTTP,
        "scope": SCOPE_SESSION_READER_HTTP,
        "path": _normalize_path(path),
        "protocol": _PROTOCOL_VERSION,
        "iat": issued_at,
        "exp": issued_at + ttl,
        "jti": secrets.token_urlsafe(18),
    }
    header = {"v": _TOKEN_VERSION, "alg": "Ed25519", "kid": record["version"]}
    encoded_header = _b64url(json.dumps(header, sort_keys=True, separators=(",", ":")).encode())
    encoded_claims = _b64url(json.dumps(claims, sort_keys=True, separators=(",", ":")).encode())
    signature = Ed25519PrivateKey.from_private_bytes(record["private_key"]).sign(
        f"{encoded_header}.{encoded_claims}".encode("ascii")
    )
    return f"{encoded_header}.{encoded_claims}.{_b64url(signature)}"


def _verifiers(
    *,
    public_key: str | bytes | None,
    issuer_key_version: str | None,
    retained_public_keys: str | Mapping[str, str] | None,
) -> dict[str, Ed25519PublicKey]:
    encoded = public_key if public_key is not None else os.environ.get("HERMES_SESSION_READER_CAPABILITY_PUBLIC_KEY", "")
    version = str(issuer_key_version or os.environ.get("HERMES_SESSION_READER_CAPABILITY_ISSUER", "")).strip()
    retained = retained_public_keys if retained_public_keys is not None else os.environ.get("HERMES_SESSION_READER_CAPABILITY_RETAINED_PUBLIC_KEYS", "{}")
    try:
        values = json.loads(retained) if isinstance(retained, str) else dict(retained)
        if not isinstance(values, dict) or version in values:
            raise ValueError("invalid retained verification keys")
        values[version] = encoded.decode("ascii") if isinstance(encoded, bytes) else str(encoded)
        return {
            str(key): Ed25519PublicKey.from_public_bytes(_b64url_decode(str(value)))
            for key, value in values.items()
            if str(key).strip()
        }
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SessionReaderCapabilityInvalid("capability_verifier_unavailable") from exc


def prepare_session_reader_capability_verifier(
    *,
    public_key: str | bytes | None = None,
    issuer_key_version: str | None = None,
    retained_public_keys: str | Mapping[str, str] | None = None,
) -> SessionReaderCapabilityVerifier:
    return SessionReaderCapabilityVerifier(
        keys=_verifiers(
            public_key=public_key,
            issuer_key_version=issuer_key_version,
            retained_public_keys=retained_public_keys,
        )
    )


def verify_session_reader_capability(
    token: str,
    *,
    expected_lease: "SessionReaderAuthorityLease",
    path: str,
    authority_store: "AuthorityStore | None",
    public_key: str | bytes | None = None,
    issuer_key_version: str | None = None,
    retained_public_keys: str | Mapping[str, str] | None = None,
    verifier: SessionReaderCapabilityVerifier | None = None,
    now: int | None = None,
) -> SessionReaderCapabilityClaims:
    """Verify signature, service identity, exact binding, and optional durable fence."""
    try:
        encoded_header, encoded_claims, encoded_signature = token.split(".")
        header = json.loads(_b64url_decode(encoded_header).decode("utf-8"))
        payload = json.loads(_b64url_decode(encoded_claims).decode("utf-8"))
        signature = _b64url_decode(encoded_signature)
    except Exception as exc:
        raise SessionReaderCapabilityInvalid("capability_malformed") from exc
    verifiers = (
        verifier.keys
        if verifier is not None
        else _verifiers(
            public_key=public_key,
            issuer_key_version=issuer_key_version,
            retained_public_keys=retained_public_keys,
        )
    )
    key_version = str(header.get("kid") or "") if isinstance(header, dict) else ""
    if (
        not isinstance(payload, dict)
        or header.get("v") != _TOKEN_VERSION
        or header.get("alg") != "Ed25519"
        or payload.get("v") != _TOKEN_VERSION
        or payload.get("kind") != "session-reader"
        or payload.get("iss") != key_version
        or key_version not in verifiers
    ):
        raise SessionReaderCapabilityInvalid("capability_issuer_mismatch")
    try:
        verifiers[key_version].verify(
            signature, f"{encoded_header}.{encoded_claims}".encode("ascii")
        )
    except InvalidSignature as exc:
        raise SessionReaderCapabilityInvalid("capability_signature_invalid") from exc
    try:
        protocol = str(payload["protocol"])
        result = SessionReaderCapabilityClaims(
            issuer_key_version=str(payload["iss"]),
            owner_key=str(payload["owner_key"]),
            reader_generation=int(payload["generation"]),
            reader_id=str(payload["worker_id"]),
            lease_version=int(payload["lease_version"]),
            recovery_generation=int(payload["recovery_generation"]),
            audience=str(payload["aud"]),
            scope=str(payload["scope"]),
            path=_normalize_path(str(payload["path"])),
            issued_at=int(payload["iat"]),
            expires_at=int(payload["exp"]),
            jti=str(payload["jti"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise SessionReaderCapabilityInvalid("capability_claims_invalid") from exc
    current_time = int(time.time()) if now is None else int(now)
    if (
        protocol != _PROTOCOL_VERSION
        or not all((result.owner_key, result.reader_id, result.audience, result.scope, result.jti))
        or result.reader_generation < 1
        or result.lease_version < 1
        or result.recovery_generation < 0
        or result.issued_at > current_time
        or result.expires_at <= result.issued_at
        or current_time > result.expires_at
    ):
        raise SessionReaderCapabilityInvalid("capability_expired_or_invalid")
    if (
        result.owner_key != expected_lease.owner_key
        or result.reader_generation != expected_lease.reader_generation
        or result.reader_id != expected_lease.reader_id
        or result.lease_version != expected_lease.lease_version
        or result.recovery_generation != expected_lease.recovery_generation
        or result.audience != AUD_SESSION_READER_HTTP
        or result.scope != SCOPE_SESSION_READER_HTTP
        or result.path != _normalize_path(path)
    ):
        raise SessionReaderCapabilityInvalid("reader_capability_binding_mismatch")
    if authority_store is not None:
        try:
            authority_store.assert_reader_lease(expected_lease)
        except Exception as exc:
            raise SessionReaderCapabilityInvalid("reader_capability_lease_invalid") from exc
    return result
