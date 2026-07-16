"""Generation-fenced Control Plane capabilities for Owner Workers.

``owc1`` is deliberately asymmetric: only the Control Plane opens the private
Ed25519 signing key; worker processes receive a public verifier and reject the
legacy symmetric ``ow2`` format.  A valid signature is still insufficient for
admission: verification also requires the exact durable authority lease.
"""
from __future__ import annotations

import base64
import json
import os
import secrets
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from hermes_cli.dashboard_auth.authority import (
    AuthorityStore,
    AuthorizationRejected,
    OwnerWorkerAuthorityLease,
    WorkerLeaseState,
)

_TOKEN_VERSION = "owc1"
_PROTOCOL_VERSION = "owc1"
_SIGNING_KEY_VERSION = "owc1-1"
_KEYRING_SCHEMA_VERSION = 1
_KEYRING_NAME = "owner_worker_capability_keyring.json"
_DEFAULT_TTL_SECONDS = 60
_MAX_TTL_SECONDS = 5 * 60
_CHILD_TOKEN_TTL_ENV = "HERMES_OWNER_WORKER_CHILD_TOKEN_TTL_SECONDS"
_DEFAULT_CHILD_TOKEN_TTL_SECONDS = 12 * 60 * 60
_MAX_CHILD_TOKEN_TTL_SECONDS = 24 * 60 * 60

AUD_OWNER_WORKER_HTTP = "owner-worker-http"
AUD_OWNER_WORKER_WS = "owner-worker-ws"
AUD_CONTROL_PLANE_WS = "control-plane-ws"
AUD_OWNER_WORKER_BOOTSTRAP = "owner-worker-uds-bootstrap"
SCOPE_OWNER_WORKER_HTTP = "owner-worker:http"
SCOPE_OWNER_WORKER_WS = "owner-worker:ws"
SCOPE_OWNER_WORKER_BOOTSTRAP = "owner-worker:bootstrap"
_BOOTSTRAP_PROTOCOL_VERSION = "owp1"
_DEFAULT_BOOTSTRAP_TTL_SECONDS = 20
_MAX_BOOTSTRAP_TTL_SECONDS = 60


class OwnerWorkerCapabilityInvalid(ValueError):
    """Capability is malformed, expired, unauthentic, or fenced out."""


@dataclass(frozen=True)
class OwnerWorkerCapabilityClaims:
    issuer_key_version: str
    owner_key: str
    worker_generation: int
    worker_id: str
    lease_version: int
    recovery_generation: int
    audience: str
    scope: str
    path: str
    protocol_version: str
    issued_at: int
    expires_at: int
    jti: str

    @property
    def lease(self) -> OwnerWorkerAuthorityLease:
        return OwnerWorkerAuthorityLease(
            self.owner_key,
            self.worker_generation,
            self.worker_id,
            WorkerLeaseState.ACTIVE,
            self.lease_version,
            self.recovery_generation,
        )


@dataclass(frozen=True)
class OwnerWorkerBootstrapClaims(OwnerWorkerCapabilityClaims):
    """A one-use `owp1` bootstrap bound to one internal UDS peer."""

    connection_id: str
    nonce: str


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _normalize_path(path: str) -> str:
    value = str(path or "").strip().split("?", 1)[0]
    if not value or not value.startswith("/"):
        raise ValueError("capability path is required")
    return value


def _opaque_peer_identifier(value: str, *, field: str) -> str:
    identifier = str(value or "").strip()
    if not 16 <= len(identifier) <= 128 or not all(char.isascii() and (char.isalnum() or char in "-_") for char in identifier):
        raise ValueError(f"{field} must be a URL-safe opaque identifier")
    return identifier


def _safe_control_home(control_home: str | Path | None) -> Path:
    raw = str(control_home or os.environ.get("HERMES_CONTROL_HOME", "")).strip()
    if not raw:
        raise OwnerWorkerCapabilityInvalid("control_home_required")
    path = Path(raw).expanduser().resolve()
    try:
        if path.exists() or path.is_symlink():
            status = path.lstat()
            if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
                raise OwnerWorkerCapabilityInvalid("capability_keyring_unavailable")
            if os.name != "nt" and status.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                raise OwnerWorkerCapabilityInvalid("capability_keyring_unavailable")
    except OSError as exc:
        raise OwnerWorkerCapabilityInvalid("capability_keyring_unavailable") from exc
    return path


def _keyring_path(control_home: str | Path | None) -> Path:
    return _safe_control_home(control_home) / _KEYRING_NAME


def _validate_keyring_path(path: Path) -> None:
    try:
        if path.exists() or path.is_symlink():
            status = path.lstat()
            if stat.S_ISLNK(status.st_mode) or not stat.S_ISREG(status.st_mode):
                raise OwnerWorkerCapabilityInvalid("capability_keyring_unavailable")
            if os.name != "nt" and status.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
                raise OwnerWorkerCapabilityInvalid("capability_keyring_unavailable")
    except OSError as exc:
        raise OwnerWorkerCapabilityInvalid("capability_keyring_unavailable") from exc


def _new_keyring() -> dict[str, Any]:
    private = Ed25519PrivateKey.generate()
    public = private.public_key()
    return {
        "schema_version": _KEYRING_SCHEMA_VERSION,
        "active": {
            "version": _SIGNING_KEY_VERSION,
            "private_key": _b64url(
                private.private_bytes(
                    serialization.Encoding.Raw,
                    serialization.PrivateFormat.Raw,
                    serialization.NoEncryption(),
                )
            ),
            "public_key": _b64url(public.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)),
            "created_at": int(time.time()),
        },
        # Workers receive this public-only map so they can continue to verify
        # already-issued short-lived capabilities during a Control Plane key
        # transition without ever receiving private signing material.
        "retained_public_keys": {},
    }


def _parse_public_key(value: Any, *, require_private: bool) -> tuple[str, bytes, int, bytes | None]:
    if not isinstance(value, dict):
        raise OwnerWorkerCapabilityInvalid("capability_keyring_unavailable")
    version = str(value.get("version") or "").strip()
    public = str(value.get("public_key") or "").strip()
    private = str(value.get("private_key") or "").strip()
    try:
        created_at = int(value["created_at"])
        public_bytes = _b64url_decode(public)
        Ed25519PublicKey.from_public_bytes(public_bytes)
        private_bytes = _b64url_decode(private) if private else None
        if private_bytes is not None:
            derived = Ed25519PrivateKey.from_private_bytes(private_bytes).public_key().public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw
            )
        else:
            derived = None
    except (KeyError, TypeError, ValueError) as exc:
        raise OwnerWorkerCapabilityInvalid("capability_keyring_unavailable") from exc
    if not version or created_at < 0 or (require_private and private_bytes is None) or (
        private_bytes is not None and derived != public_bytes
    ):
        raise OwnerWorkerCapabilityInvalid("capability_keyring_unavailable")
    return version, public_bytes, created_at, private_bytes


def _parse_keyring(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("schema_version") != _KEYRING_SCHEMA_VERSION:
        raise OwnerWorkerCapabilityInvalid("capability_keyring_unavailable")
    version, public_key, _created_at, private_key = _parse_public_key(value.get("active"), require_private=True)
    retained = value.get("retained_public_keys", {})
    if not isinstance(retained, dict):
        raise OwnerWorkerCapabilityInvalid("capability_keyring_unavailable")
    retained_public_keys: dict[str, bytes] = {}
    for retained_version, record in retained.items():
        parsed_version, parsed_public, _retained_created_at, parsed_private = _parse_public_key(record, require_private=False)
        if parsed_private is not None or parsed_version != str(retained_version) or parsed_version == version:
            raise OwnerWorkerCapabilityInvalid("capability_keyring_unavailable")
        retained_public_keys[parsed_version] = parsed_public
    return {
        "version": version,
        "private_key": private_key,
        "public_key": public_key,
        "retained_public_keys": retained_public_keys,
    }


def _write_keyring(path: Path, payload: Mapping[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        parent = path.parent.lstat()
        if stat.S_ISLNK(parent.st_mode) or not stat.S_ISDIR(parent.st_mode):
            raise OwnerWorkerCapabilityInvalid("capability_keyring_unavailable")
        if os.name != "nt" and parent.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise OwnerWorkerCapabilityInvalid("capability_keyring_unavailable")
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            if os.name != "nt":
                path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        finally:
            temporary.unlink(missing_ok=True)
    except OwnerWorkerCapabilityInvalid:
        raise
    except OSError as exc:
        raise OwnerWorkerCapabilityInvalid("capability_keyring_unavailable") from exc


def _signing_record(control_home: str | Path | None) -> dict[str, Any]:
    """Read/create a Control-Plane private signer; never call from workers."""
    path = _keyring_path(control_home)
    _validate_keyring_path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        payload = _new_keyring()
        _write_keyring(path, payload)
        raw = payload
    except (OSError, json.JSONDecodeError) as exc:
        raise OwnerWorkerCapabilityInvalid("capability_keyring_unavailable") from exc
    return _parse_keyring(raw)


def owner_worker_capability_public_config(control_home: str | Path | None = None) -> dict[str, str]:
    """Return only active public verifier material for a spawned worker."""
    record = _signing_record(control_home)
    return {
        "HERMES_OWNER_WORKER_CAPABILITY_ISSUER": record["version"],
        "HERMES_OWNER_WORKER_CAPABILITY_PUBLIC_KEY": _b64url(record["public_key"]),
        "HERMES_OWNER_WORKER_CAPABILITY_RETAINED_PUBLIC_KEYS": json.dumps(
            {version: _b64url(public_key) for version, public_key in record["retained_public_keys"].items()},
            sort_keys=True,
            separators=(",", ":"),
        ),
    }


def _verifiers_from_config(
    *,
    public_key: str | bytes | None = None,
    issuer_key_version: str | None = None,
    retained_public_keys: str | Mapping[str, str] | None = None,
) -> dict[str, Ed25519PublicKey]:
    encoded = public_key if public_key is not None else os.environ.get("HERMES_OWNER_WORKER_CAPABILITY_PUBLIC_KEY", "")
    version = str(issuer_key_version or os.environ.get("HERMES_OWNER_WORKER_CAPABILITY_ISSUER", "")).strip()
    retained = (
        retained_public_keys
        if retained_public_keys is not None
        else os.environ.get("HERMES_OWNER_WORKER_CAPABILITY_RETAINED_PUBLIC_KEYS", "{}")
    )
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
        raise OwnerWorkerCapabilityInvalid("capability_verifier_unavailable") from exc


def child_token_ttl_seconds(source: Mapping[str, str] | None = None) -> int:
    """Retained configuration helper; worker children no longer mint credentials."""
    src = source if source is not None else os.environ
    raw = str(src.get(_CHILD_TOKEN_TTL_ENV, "") or "").strip()
    try:
        value = int(raw) if raw else _DEFAULT_CHILD_TOKEN_TTL_SECONDS
    except (TypeError, ValueError):
        value = _DEFAULT_CHILD_TOKEN_TTL_SECONDS
    return max(1, min(value, _MAX_CHILD_TOKEN_TTL_SECONDS))


def mint_owner_worker_capability(
    lease: OwnerWorkerAuthorityLease,
    *,
    audience: str,
    scope: str,
    path: str,
    protocol_version: str = _PROTOCOL_VERSION,
    ttl_seconds: int = _DEFAULT_TTL_SECONDS,
    control_home: str | Path | None = None,
    now: int | None = None,
) -> str:
    """Mint a short-lived exact-worker capability from the Control Plane."""
    if lease.state not in {WorkerLeaseState.STARTING, WorkerLeaseState.ACTIVE}:
        raise ValueError("capability lease must be starting or active")
    ttl = int(ttl_seconds)
    if ttl < 1 or ttl > _MAX_TTL_SECONDS:
        raise ValueError("capability ttl is outside the permitted bound")
    record = _signing_record(control_home)
    issued_at = int(time.time()) if now is None else int(now)
    claims = {
        "v": _TOKEN_VERSION,
        "iss": record["version"],
        "owner_key": lease.owner_key,
        "generation": lease.worker_generation,
        "worker_id": lease.worker_id,
        "lease_version": lease.lease_version,
        "recovery_generation": lease.recovery_generation,
        "aud": str(audience or ""),
        "scope": str(scope or ""),
        "path": _normalize_path(path),
        "protocol": str(protocol_version or ""),
        "iat": issued_at,
        "exp": issued_at + ttl,
        "jti": secrets.token_urlsafe(18),
    }
    if not all(str(claims[name]).strip() for name in ("aud", "scope", "protocol")):
        raise ValueError("capability audience, scope, and protocol are required")
    header = {"v": _TOKEN_VERSION, "alg": "Ed25519", "kid": record["version"]}
    encoded_header = _b64url(json.dumps(header, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    encoded_claims = _b64url(json.dumps(claims, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{encoded_header}.{encoded_claims}".encode("ascii")
    signature = Ed25519PrivateKey.from_private_bytes(record["private_key"]).sign(signing_input)
    return f"{encoded_header}.{encoded_claims}.{_b64url(signature)}"


def mint_owner_worker_bootstrap(
    lease: OwnerWorkerAuthorityLease,
    *,
    path: str,
    connection_id: str,
    nonce: str,
    ttl_seconds: int = _DEFAULT_BOOTSTRAP_TTL_SECONDS,
    control_home: str | Path | None = None,
    now: int | None = None,
) -> str:
    """Mint one short-lived `owp1` UDS bootstrap for an exact active Worker."""
    if lease.state is not WorkerLeaseState.ACTIVE:
        raise ValueError("bootstrap lease must be active")
    ttl = int(ttl_seconds)
    if ttl < 1 or ttl > _MAX_BOOTSTRAP_TTL_SECONDS:
        raise ValueError("bootstrap ttl is outside the permitted bound")
    record = _signing_record(control_home)
    issued_at = int(time.time()) if now is None else int(now)
    claims = {
        "v": _TOKEN_VERSION,
        "kind": "owner-worker-bootstrap",
        "iss": record["version"],
        "owner_key": lease.owner_key,
        "generation": lease.worker_generation,
        "worker_id": lease.worker_id,
        "lease_version": lease.lease_version,
        "recovery_generation": lease.recovery_generation,
        "aud": AUD_OWNER_WORKER_BOOTSTRAP,
        "scope": SCOPE_OWNER_WORKER_BOOTSTRAP,
        "path": _normalize_path(path),
        "protocol": _BOOTSTRAP_PROTOCOL_VERSION,
        "iat": issued_at,
        "exp": issued_at + ttl,
        "jti": secrets.token_urlsafe(18),
        "connection_id": _opaque_peer_identifier(connection_id, field="connection_id"),
        "nonce": _opaque_peer_identifier(nonce, field="nonce"),
    }
    header = {"v": _TOKEN_VERSION, "alg": "Ed25519", "kid": record["version"]}
    encoded_header = _b64url(json.dumps(header, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    encoded_claims = _b64url(json.dumps(claims, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    signature = Ed25519PrivateKey.from_private_bytes(record["private_key"]).sign(
        f"{encoded_header}.{encoded_claims}".encode("ascii")
    )
    return f"{encoded_header}.{encoded_claims}.{_b64url(signature)}"


def _claims_from_token(
    token: str,
    *,
    public_key: str | bytes | None = None,
    issuer_key_version: str | None = None,
    retained_public_keys: str | Mapping[str, str] | None = None,
    now: int | None = None,
) -> OwnerWorkerCapabilityClaims:
    try:
        encoded_header, encoded_claims, encoded_signature = token.split(".")
        header = json.loads(_b64url_decode(encoded_header).decode("utf-8"))
        payload = json.loads(_b64url_decode(encoded_claims).decode("utf-8"))
        signature = _b64url_decode(encoded_signature)
    except Exception as exc:
        raise OwnerWorkerCapabilityInvalid("capability_malformed") from exc
    if not isinstance(header, dict) or not isinstance(payload, dict):
        raise OwnerWorkerCapabilityInvalid("capability_malformed")
    verifiers = _verifiers_from_config(
        public_key=public_key,
        issuer_key_version=issuer_key_version,
        retained_public_keys=retained_public_keys,
    )
    key_version = str(header.get("kid") or "")
    if (
        header.get("v") != _TOKEN_VERSION
        or header.get("alg") != "Ed25519"
        or payload.get("v") != _TOKEN_VERSION
        or payload.get("iss") != key_version
        or key_version not in verifiers
    ):
        raise OwnerWorkerCapabilityInvalid("capability_issuer_mismatch")
    try:
        verifiers[key_version].verify(signature, f"{encoded_header}.{encoded_claims}".encode("ascii"))
    except InvalidSignature as exc:
        raise OwnerWorkerCapabilityInvalid("capability_signature_invalid") from exc
    try:
        claims = OwnerWorkerCapabilityClaims(
            issuer_key_version=str(payload["iss"]),
            owner_key=str(payload["owner_key"]),
            worker_generation=int(payload["generation"]),
            worker_id=str(payload["worker_id"]),
            lease_version=int(payload["lease_version"]),
            recovery_generation=int(payload["recovery_generation"]),
            audience=str(payload["aud"]),
            scope=str(payload["scope"]),
            path=_normalize_path(str(payload["path"])),
            protocol_version=str(payload["protocol"]),
            issued_at=int(payload["iat"]),
            expires_at=int(payload["exp"]),
            jti=str(payload["jti"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise OwnerWorkerCapabilityInvalid("capability_claims_invalid") from exc
    current = int(time.time()) if now is None else int(now)
    if (
        not all((claims.owner_key, claims.worker_id, claims.audience, claims.scope, claims.protocol_version, claims.jti))
        or claims.worker_generation < 1
        or claims.lease_version < 1
        or claims.recovery_generation < 0
        or claims.protocol_version != _PROTOCOL_VERSION
        or claims.issued_at > current
        or claims.expires_at <= claims.issued_at
        or current > claims.expires_at
    ):
        raise OwnerWorkerCapabilityInvalid("capability_expired_or_invalid")
    return claims


def parse_owner_worker_bootstrap(
    token: str,
    *,
    expected_lease: OwnerWorkerAuthorityLease,
    path: str,
    public_key: str | bytes | None = None,
    issuer_key_version: str | None = None,
    retained_public_keys: str | Mapping[str, str] | None = None,
    now: int | None = None,
) -> OwnerWorkerBootstrapClaims:
    """Verify signed `owp1` bootstrap claims without consuming their JTI."""
    try:
        encoded_header, encoded_claims, encoded_signature = token.split(".")
        header = json.loads(_b64url_decode(encoded_header).decode("utf-8"))
        payload = json.loads(_b64url_decode(encoded_claims).decode("utf-8"))
        signature = _b64url_decode(encoded_signature)
    except Exception as exc:
        raise OwnerWorkerCapabilityInvalid("bootstrap_malformed") from exc
    if not isinstance(header, dict) or not isinstance(payload, dict):
        raise OwnerWorkerCapabilityInvalid("bootstrap_malformed")
    verifiers = _verifiers_from_config(
        public_key=public_key,
        issuer_key_version=issuer_key_version,
        retained_public_keys=retained_public_keys,
    )
    key_version = str(header.get("kid") or "")
    if (
        header.get("v") != _TOKEN_VERSION
        or header.get("alg") != "Ed25519"
        or payload.get("v") != _TOKEN_VERSION
        or payload.get("kind") != "owner-worker-bootstrap"
        or payload.get("iss") != key_version
        or key_version not in verifiers
    ):
        raise OwnerWorkerCapabilityInvalid("bootstrap_issuer_mismatch")
    try:
        verifiers[key_version].verify(signature, f"{encoded_header}.{encoded_claims}".encode("ascii"))
    except InvalidSignature as exc:
        raise OwnerWorkerCapabilityInvalid("bootstrap_signature_invalid") from exc
    try:
        claims = OwnerWorkerBootstrapClaims(
            issuer_key_version=str(payload["iss"]),
            owner_key=str(payload["owner_key"]),
            worker_generation=int(payload["generation"]),
            worker_id=str(payload["worker_id"]),
            lease_version=int(payload["lease_version"]),
            recovery_generation=int(payload["recovery_generation"]),
            audience=str(payload["aud"]),
            scope=str(payload["scope"]),
            path=_normalize_path(str(payload["path"])),
            protocol_version=str(payload["protocol"]),
            issued_at=int(payload["iat"]),
            expires_at=int(payload["exp"]),
            jti=str(payload["jti"]),
            connection_id=_opaque_peer_identifier(str(payload["connection_id"]), field="connection_id"),
            nonce=_opaque_peer_identifier(str(payload["nonce"]), field="nonce"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise OwnerWorkerCapabilityInvalid("bootstrap_claims_invalid") from exc
    current = int(time.time()) if now is None else int(now)
    if (
        not all((claims.owner_key, claims.worker_id, claims.jti))
        or claims.worker_generation < 1
        or claims.lease_version < 1
        or claims.recovery_generation < 0
        or claims.audience != AUD_OWNER_WORKER_BOOTSTRAP
        or claims.scope != SCOPE_OWNER_WORKER_BOOTSTRAP
        or claims.protocol_version != _BOOTSTRAP_PROTOCOL_VERSION
        or claims.issued_at > current
        or claims.expires_at <= claims.issued_at
        or current > claims.expires_at
        or claims.owner_key != expected_lease.owner_key
        or claims.worker_generation != expected_lease.worker_generation
        or claims.worker_id != expected_lease.worker_id
        or claims.lease_version != expected_lease.lease_version
        or claims.recovery_generation != expected_lease.recovery_generation
        or claims.path != _normalize_path(path)
    ):
        raise OwnerWorkerCapabilityInvalid("bootstrap_binding_mismatch")
    return claims


def admit_owner_worker_bootstrap(
    token: str,
    *,
    expected_lease: OwnerWorkerAuthorityLease,
    path: str,
    authority_store: AuthorityStore,
    public_key: str | bytes | None = None,
    issuer_key_version: str | None = None,
    retained_public_keys: str | Mapping[str, str] | None = None,
    now: int | None = None,
) -> OwnerWorkerBootstrapClaims:
    """Verify and atomically consume one exact Worker bootstrap capability."""
    claims = parse_owner_worker_bootstrap(
        token,
        expected_lease=expected_lease,
        path=path,
        public_key=public_key,
        issuer_key_version=issuer_key_version,
        retained_public_keys=retained_public_keys,
        now=now,
    )
    try:
        authority_store.check_and_consume_owner_worker_bootstrap(
            expected_lease,
            issuer_key_version=claims.issuer_key_version,
            jti=claims.jti,
            audience=claims.audience,
            expires_at=claims.expires_at,
            now=now,
        )
    except AuthorizationRejected as exc:
        raise OwnerWorkerCapabilityInvalid("bootstrap_lease_or_replay_invalid") from exc
    return claims


def owp1_hello(claims: OwnerWorkerBootstrapClaims) -> str:
    """Encode the sole permitted initial peer hello."""
    return json.dumps({
        "v": _BOOTSTRAP_PROTOCOL_VERSION,
        "type": "hello",
        "connection_id": claims.connection_id,
        "nonce": claims.nonce,
        "sequence": 0,
    }, sort_keys=True, separators=(",", ":"))


def owp1_ack(claims: OwnerWorkerBootstrapClaims) -> str:
    """Encode the Worker acknowledgement for a validated hello."""
    return json.dumps({
        "v": _BOOTSTRAP_PROTOCOL_VERSION,
        "type": "ack",
        "connection_id": claims.connection_id,
        "nonce": claims.nonce,
        "sequence": 0,
    }, sort_keys=True, separators=(",", ":"))


def validate_owp1_control(
    message: str | bytes,
    claims: OwnerWorkerBootstrapClaims,
    *,
    message_type: str,
) -> None:
    """Require an exact sequence-zero hello or acknowledgement."""
    try:
        payload = json.loads(message.decode("utf-8") if isinstance(message, bytes) else message)
    except (TypeError, ValueError, UnicodeDecodeError) as exc:
        raise OwnerWorkerCapabilityInvalid("owp1_control_malformed") from exc
    if not isinstance(payload, dict) or (
        payload.get("v") != _BOOTSTRAP_PROTOCOL_VERSION
        or payload.get("type") != message_type
        or payload.get("connection_id") != claims.connection_id
        or payload.get("nonce") != claims.nonce
        or payload.get("sequence") != 0
    ):
        raise OwnerWorkerCapabilityInvalid("owp1_control_mismatch")


def owp1_data(
    claims: OwnerWorkerBootstrapClaims,
    *,
    direction: str,
    sequence: int,
    text: str | None = None,
    data: bytes | None = None,
) -> str:
    """Frame one text or bytes payload with a direction-specific sequence."""
    if (text is None) == (data is None) or int(sequence) < 1:
        raise ValueError("owp1 data requires one payload and a positive sequence")
    payload = {"kind": "text", "data": text} if text is not None else {"kind": "bytes", "data": _b64url(data or b"")}
    return json.dumps({
        "v": _BOOTSTRAP_PROTOCOL_VERSION,
        "type": "data",
        "direction": direction,
        "connection_id": claims.connection_id,
        "nonce": claims.nonce,
        "sequence": int(sequence),
        "payload": payload,
    }, sort_keys=True, separators=(",", ":"))


def parse_owp1_data(
    message: str | bytes,
    claims: OwnerWorkerBootstrapClaims,
    *,
    direction: str,
    expected_sequence: int,
) -> tuple[str, str | bytes]:
    """Validate one exact next peer envelope and return its original payload."""
    try:
        value = message.decode("utf-8") if isinstance(message, bytes) else message
        envelope = json.loads(value)
        payload = envelope["payload"]
        kind = str(payload["kind"])
        raw = payload["data"]
    except (TypeError, KeyError, ValueError, UnicodeDecodeError) as exc:
        raise OwnerWorkerCapabilityInvalid("owp1_data_malformed") from exc
    if not isinstance(envelope, dict) or not isinstance(payload, dict) or (
        envelope.get("v") != _BOOTSTRAP_PROTOCOL_VERSION
        or envelope.get("type") != "data"
        or envelope.get("direction") != direction
        or envelope.get("connection_id") != claims.connection_id
        or envelope.get("nonce") != claims.nonce
        or envelope.get("sequence") != int(expected_sequence)
    ):
        raise OwnerWorkerCapabilityInvalid("owp1_data_mismatch")
    if kind == "text" and isinstance(raw, str):
        return kind, raw
    if kind == "bytes" and isinstance(raw, str):
        try:
            return kind, _b64url_decode(raw)
        except (TypeError, ValueError) as exc:
            raise OwnerWorkerCapabilityInvalid("owp1_data_malformed") from exc
    raise OwnerWorkerCapabilityInvalid("owp1_data_malformed")


def verify_owner_worker_capability(
    token: str,
    *,
    expected_lease: OwnerWorkerAuthorityLease,
    audience: str,
    scope: str,
    path: str,
    authority_store: AuthorityStore,
    allowed_states: frozenset[WorkerLeaseState] = frozenset({WorkerLeaseState.STARTING, WorkerLeaseState.ACTIVE}),
    public_key: str | bytes | None = None,
    issuer_key_version: str | None = None,
    retained_public_keys: str | Mapping[str, str] | None = None,
    now: int | None = None,
) -> OwnerWorkerCapabilityClaims:
    """Verify signature, exact claims, and the current durable admission fence."""
    claims = _claims_from_token(
        token,
        public_key=public_key,
        issuer_key_version=issuer_key_version,
        retained_public_keys=retained_public_keys,
        now=now,
    )
    if (
        claims.owner_key != expected_lease.owner_key
        or claims.worker_generation != expected_lease.worker_generation
        or claims.worker_id != expected_lease.worker_id
        or claims.lease_version != expected_lease.lease_version
        or claims.recovery_generation != expected_lease.recovery_generation
        or claims.audience != str(audience)
        or claims.scope != str(scope)
        or claims.path != _normalize_path(path)
    ):
        raise OwnerWorkerCapabilityInvalid("capability_binding_mismatch")
    try:
        current = authority_store.read_owner_worker_lease(expected_lease.owner_key)
        if (
            current is None
            or current.worker_generation != expected_lease.worker_generation
            or current.worker_id != expected_lease.worker_id
            or current.lease_version != expected_lease.lease_version
            or current.recovery_generation != expected_lease.recovery_generation
            or current.state not in allowed_states
        ):
            raise AuthorizationRejected("worker_lease_stale")
    except AuthorizationRejected as exc:
        raise OwnerWorkerCapabilityInvalid("capability_lease_invalid") from exc
    return claims


# Compatibility names deliberately do not accept or issue the retired ``ow2``
# HMAC format. Authenticated worker call sites must use the typed API above.
def mint_internal_token(*args: Any, **kwargs: Any) -> str:
    del args, kwargs
    raise OwnerWorkerCapabilityInvalid("legacy_ow2_not_supported")


def validate_internal_token_payload(*args: Any, **kwargs: Any) -> dict[str, Any] | None:
    del args, kwargs
    return None


def validate_internal_token(*args: Any, **kwargs: Any) -> bool:
    del args, kwargs
    return False
