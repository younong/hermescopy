"""Signed browser WebSocket tickets and loopback-only internal credentials.

Browser tickets are self-contained signed capabilities, but their signing
keyring and replay continuity are Control-Plane-only state.  A signer alone is
never authority to admit a ticket: the shared :class:`AuthorityStore` consumes
its ``jti`` exactly once and independently proves replay-store continuity.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import stat
import threading
import time
from pathlib import Path
from typing import Any, Mapping

from hermes_cli.dashboard_auth.authority import (
    AuthorizationScope,
    AuthorityStore,
    AuthorityUnavailable,
    ReplayContinuity,
    control_plane_home,
)

TTL_SECONDS = 30
_TOKEN_VERSION = "bwt1"
_SIGNING_KEY_VERSION = "bwt1"
_KEYRING_SCHEMA_VERSION = 1
_KEYRING_NAME = "browser_ws_ticket_keyring.json"
_PUBLIC_AUDIENCES: frozenset[str] = frozenset({
    "browser-ws:/api/pty",
    "browser-ws:/api/ws",
    "browser-ws:/api/pub",
    "browser-ws:/api/events",
})

_lock = threading.Lock()
_keyring_lock = threading.RLock()
_internal_credential: str | None = None
INTERNAL_USER_ID = "server-internal"
INTERNAL_PROVIDER = "server-internal"


class TicketInvalid(Exception):
    """Ticket is malformed, unauthentic, expired, or unsuitable for its route."""


def browser_ws_audience(path: str) -> str:
    """Return the exact public browser WS audience for ``path`` or fail closed."""
    audience = f"browser-ws:{str(path or '').split('?', 1)[0]}"
    if audience not in _PUBLIC_AUDIENCES:
        raise ValueError("unsupported browser WebSocket audience")
    return audience


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _keyring_path() -> Path:
    return control_plane_home() / _KEYRING_NAME


def browser_ticket_keyring_backup_paths() -> tuple[Path, ...]:
    """Return the Control Plane file operators must back up for ticket keys."""
    return (_keyring_path(),)


def authority_store() -> AuthorityStore:
    """Return the Control Plane authority backend used for browser tickets."""
    return AuthorityStore(control_plane_home())


def _key_record(value: Any, *, retained: bool = False) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TicketInvalid("ticket_keyring_unavailable")
    version = str(value.get("version") or "").strip()
    secret = str(value.get("secret") or "").strip()
    try:
        created_at = int(value["created_at"])
    except (KeyError, TypeError, ValueError) as exc:
        raise TicketInvalid("ticket_keyring_unavailable") from exc
    if not version or not secret or created_at < 0:
        raise TicketInvalid("ticket_keyring_unavailable")
    record = {"version": version, "secret": secret, "created_at": created_at}
    if retained:
        try:
            verify_until = int(value["verify_until"])
            retired_at = int(value["retired_at"])
        except (KeyError, TypeError, ValueError) as exc:
            raise TicketInvalid("ticket_keyring_unavailable") from exc
        if verify_until < created_at or retired_at < created_at:
            raise TicketInvalid("ticket_keyring_unavailable")
        record.update(verify_until=verify_until, retired_at=retired_at)
    return record


def _witness(value: Any) -> ReplayContinuity:
    if not isinstance(value, dict):
        raise TicketInvalid("replay_continuity_unavailable")
    authority_id = str(value.get("authority_id") or "").strip()
    try:
        generation = int(value["recovery_generation"])
    except (KeyError, TypeError, ValueError) as exc:
        raise TicketInvalid("replay_continuity_unavailable") from exc
    if not authority_id or generation < 0 or value.get("state") != "ready":
        raise TicketInvalid("replay_continuity_unavailable")
    return ReplayContinuity(authority_id, generation, True)


def _parse_keyring(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("schema_version") != _KEYRING_SCHEMA_VERSION:
        raise TicketInvalid("ticket_keyring_unavailable")
    active = _key_record(payload.get("active"))
    retained_value = payload.get("retained")
    if not isinstance(retained_value, list):
        raise TicketInvalid("ticket_keyring_unavailable")
    retained = [_key_record(value, retained=True) for value in retained_value]
    pending_value = payload.get("pending")
    pending = None if pending_value is None else _key_record(pending_value)
    versions = [active["version"], *(record["version"] for record in retained)]
    if pending is not None:
        versions.append(pending["version"])
    if len(set(versions)) != len(versions):
        raise TicketInvalid("ticket_keyring_unavailable")
    witness = _witness(payload.get("replay_continuity"))
    return {
        "schema_version": _KEYRING_SCHEMA_VERSION,
        "active": active,
        "retained": retained,
        "pending": pending,
        "replay_continuity": witness,
    }


def _serializable_keyring(keyring: Mapping[str, Any]) -> dict[str, Any]:
    """Convert the parsed in-memory witness back to strict disk metadata."""
    witness = keyring["replay_continuity"]
    if not isinstance(witness, ReplayContinuity):
        raise TicketInvalid("ticket_keyring_unavailable")
    return {
        "schema_version": _KEYRING_SCHEMA_VERSION,
        "active": dict(keyring["active"]),
        "retained": [dict(record) for record in keyring["retained"]],
        "pending": None if keyring["pending"] is None else dict(keyring["pending"]),
        "replay_continuity": {
            "authority_id": witness.authority_id,
            "recovery_generation": witness.recovery_generation,
            "state": "ready" if witness.ready else "recovery_required",
        },
    }


def _write_keyring(payload: Mapping[str, Any]) -> None:
    path = _keyring_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        parent = path.parent.lstat()
        if stat.S_ISLNK(parent.st_mode) or not stat.S_ISDIR(parent.st_mode):
            raise TicketInvalid("ticket_keyring_unavailable")
        if os.name != "nt" and parent.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise TicketInvalid("ticket_keyring_unavailable")
        if path.exists() or path.is_symlink():
            status = path.lstat()
            if stat.S_ISLNK(status.st_mode) or not stat.S_ISREG(status.st_mode):
                raise TicketInvalid("ticket_keyring_unavailable")
            if os.name != "nt" and status.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
                raise TicketInvalid("ticket_keyring_unavailable")
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(_serializable_keyring(payload), handle, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            if os.name != "nt":
                path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
    except TicketInvalid:
        raise
    except OSError as exc:
        raise TicketInvalid("ticket_keyring_unavailable") from exc


def _new_keyring(witness: ReplayContinuity) -> dict[str, Any]:
    return {
        "schema_version": _KEYRING_SCHEMA_VERSION,
        "active": {
            "version": _SIGNING_KEY_VERSION,
            "secret": secrets.token_urlsafe(48),
            "created_at": int(time.time()),
        },
        "retained": [],
        "pending": None,
        "replay_continuity": witness,
    }


def _mark_continuity_untrusted(store: AuthorityStore) -> None:
    try:
        store.mark_replay_continuity_untrusted(reason="ticket_keyring_failure")
    except Exception:
        pass


def _load_keyring(
    store: AuthorityStore,
    *,
    create: bool = True,
    require_continuity: bool = True,
) -> dict[str, Any]:
    # Same-process callers can race initial bootstrap (notably the existing
    # thread-safety unit contract); serialize creation so only one writes the
    # witness and every other caller validates the completed file.
    with _keyring_lock:
        path = _keyring_path()
        try:
            status = path.lstat()
        except FileNotFoundError:
            if not create:
                raise TicketInvalid("ticket_keyring_unavailable")
            continuity = store.replay_continuity()
            if store.keyring_is_bound():
                # A missing keyring after successful bootstrap is evidence of a
                # replacement/loss, never a signal to silently create a new signer.
                _mark_continuity_untrusted(store)
                raise TicketInvalid("replay_continuity_unavailable")
            payload = _new_keyring(ReplayContinuity(
                continuity.authority_id,
                continuity.recovery_generation,
                True,
            ))
            _write_keyring(payload)
            parsed = _parse_keyring(_serializable_keyring(payload))
            store.bind_replay_continuity(parsed["replay_continuity"])
            return parsed
        except OSError as exc:
            raise TicketInvalid("ticket_keyring_unavailable") from exc
        if stat.S_ISLNK(status.st_mode) or not stat.S_ISREG(status.st_mode):
            raise TicketInvalid("ticket_keyring_unavailable")
        if os.name != "nt" and status.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise TicketInvalid("ticket_keyring_unavailable")
        try:
            parsed = _parse_keyring(json.loads(path.read_text(encoding="utf-8")))
            if require_continuity:
                store.assert_replay_continuity(parsed["replay_continuity"])
            return parsed
        except TicketInvalid:
            _mark_continuity_untrusted(store)
            raise
        except (AuthorityUnavailable, OSError, json.JSONDecodeError) as exc:
            _mark_continuity_untrusted(store)
            raise TicketInvalid("replay_continuity_unavailable") from exc


def _ticket_keyring(store: AuthorityStore | None = None) -> tuple[AuthorityStore, dict[str, Any]]:
    active_store = store or authority_store()
    try:
        return active_store, _load_keyring(active_store)
    except AuthorityUnavailable as exc:
        raise TicketInvalid("authority_unavailable") from exc


def begin_browser_ticket_key_rotation(next_secret: str, *, next_issuer: str) -> None:
    """Persist a pending signer without changing the active ticket issuer."""
    next_secret = str(next_secret or "").strip()
    next_issuer = str(next_issuer or "").strip()
    if not next_secret or not next_issuer:
        raise ValueError("next ticket issuer and secret are required")
    store, keyring = _ticket_keyring()
    del store
    if keyring["pending"] is not None:
        if (
            hmac.compare_digest(keyring["pending"]["secret"], next_secret)
            and keyring["pending"]["version"] == next_issuer
        ):
            return
        raise TicketInvalid("ticket_key_rotation_pending")
    used_versions = {keyring["active"]["version"], *(record["version"] for record in keyring["retained"])}
    if next_issuer in used_versions or hmac.compare_digest(keyring["active"]["secret"], next_secret):
        raise ValueError("next ticket key must differ from active and retained keys")
    keyring["pending"] = {
        "version": next_issuer,
        "secret": next_secret,
        "created_at": int(time.time()),
    }
    _write_keyring(keyring)


def complete_browser_ticket_key_rotation(*, now: int | None = None) -> None:
    """Promote the pending signer and retain the old issuer for ticket TTL."""
    current = int(time.time()) if now is None else int(now)
    _store, keyring = _ticket_keyring()
    pending = keyring["pending"]
    if pending is None:
        raise TicketInvalid("ticket_key_rotation_not_pending")
    old_active = dict(keyring["active"])
    old_active["retired_at"] = current
    old_active["verify_until"] = current + TTL_SECONDS
    keyring["retained"].append(old_active)
    keyring["active"] = pending
    keyring["pending"] = None
    _write_keyring(keyring)


def prune_expired_browser_ticket_issuers(*, now: int | None = None) -> None:
    """Remove retained issuers only after their bounded verification window."""
    current = int(time.time()) if now is None else int(now)
    _store, keyring = _ticket_keyring()
    keyring["retained"] = [
        record for record in keyring["retained"]
        if int(record["verify_until"]) >= current
    ]
    _write_keyring(keyring)


def complete_browser_ticket_replay_recovery() -> int:
    """Explicitly reconcile the keyring witness after a replay-store incident."""
    store = authority_store()
    try:
        keyring = _load_keyring(store, require_continuity=False)
    except AuthorityUnavailable as exc:
        raise TicketInvalid("replay_continuity_unavailable") from exc
    witness = keyring["replay_continuity"]
    try:
        recovered = store.complete_replay_recovery(witness)
    except AuthorityUnavailable as exc:
        raise TicketInvalid("replay_continuity_unavailable") from exc
    keyring["replay_continuity"] = recovered
    _write_keyring(keyring)
    return recovered.recovery_generation


def _scope_from_material(
    *,
    provider: str,
    tenant_id: str,
    user_id: str,
    session_id: str,
    membership_revision: str,
) -> AuthorizationScope:
    return AuthorizationScope(
        provider=provider,
        tenant_id=tenant_id,
        user_id=user_id,
        session_id=session_id,
        membership_revision=membership_revision,
    )


def mint_ticket(
    *,
    user_id: str,
    provider: str,
    org_id: str = "",
    tenant_id: str = "",
    owner_key: str = "",
    audience: str = "browser-ws:/api/pty",
    session_id: str = "",
    membership_revision: str = "v1",
    store: AuthorityStore | None = None,
    now: int | None = None,
) -> str:
    """Mint a signed, audience-bound browser capability from verified identity."""
    if audience not in _PUBLIC_AUDIENCES:
        raise ValueError("unsupported browser WebSocket audience")
    current = int(time.time()) if now is None else int(now)
    canonical_tenant_id = str(tenant_id or f"personal:{provider}")
    stable_session_id = str(session_id or f"legacy:{provider}:{canonical_tenant_id}:{user_id}")
    scope = _scope_from_material(
        provider=provider,
        tenant_id=canonical_tenant_id,
        user_id=user_id,
        session_id=stable_session_id,
        membership_revision=membership_revision,
    )
    active_store, keyring = _ticket_keyring(store)
    try:
        state = active_store.activate(scope)
    except AuthorityUnavailable as exc:
        raise TicketInvalid("authority_unavailable") from exc
    payload = {
        "v": _TOKEN_VERSION,
        "typ": "browser-ws",
        "iss": keyring["active"]["version"],
        "jti": secrets.token_urlsafe(18),
        "iat": current,
        "exp": current + TTL_SECONDS,
        "minted_at": current,
        "expires_at": current + TTL_SECONDS,
        "aud": audience,
        "epoch": state.epoch,
        "recovery_generation": state.recovery_generation,
        "user_id": user_id,
        "provider": provider,
        "org_id": org_id,
        "tenant_id": canonical_tenant_id,
        "owner_key": str(owner_key or "legacy-unverified-owner"),
        "session_id": stable_session_id,
        "membership_revision": membership_revision,
    }
    body = _b64url(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    signature = hmac.new(keyring["active"]["secret"].encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
    return f"{body}.{_b64url(signature)}"


def _payload_and_signature(ticket: str) -> tuple[str, bytes, dict[str, Any]]:
    if not ticket or "." not in ticket:
        truncated = (str(ticket)[:8] + "…") if ticket else "<empty>"
        raise TicketInvalid(f"unknown ticket:{truncated}")
    body, encoded_signature = ticket.rsplit(".", 1)
    try:
        signature = _b64url_decode(encoded_signature)
        payload = json.loads(_b64url_decode(body).decode("utf-8"))
    except Exception as exc:
        raise TicketInvalid("ticket_malformed") from exc
    if not isinstance(payload, dict):
        raise TicketInvalid("ticket_malformed")
    return body, signature, payload


def verify_ticket(
    ticket: str,
    *,
    audience: str | None = None,
    store: AuthorityStore | None = None,
    now: int | None = None,
) -> dict[str, Any]:
    """Verify signed/temporal/exact-audience claims without consuming a ticket."""
    body, actual_signature, payload = _payload_and_signature(ticket)
    active_store, keyring = _ticket_keyring(store)
    issuer = str(payload.get("iss") or "")
    keys = [keyring["active"], *keyring["retained"]]
    matches = [record for record in keys if hmac.compare_digest(record["version"], issuer)]
    current = int(time.time()) if now is None else int(now)
    if not matches:
        _mark_continuity_untrusted(active_store)
        raise TicketInvalid("ticket_issuer_unknown")
    key = matches[0]
    if "verify_until" in key and current > int(key["verify_until"]):
        _mark_continuity_untrusted(active_store)
        raise TicketInvalid("ticket_issuer_expired")
    expected_signature = hmac.new(key["secret"].encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
    if not hmac.compare_digest(actual_signature, expected_signature):
        raise TicketInvalid("ticket_signature_invalid")
    if payload.get("v") != _TOKEN_VERSION or payload.get("typ") != "browser-ws":
        raise TicketInvalid("ticket_version_invalid")
    claimed_audience = str(payload.get("aud") or "")
    if claimed_audience not in _PUBLIC_AUDIENCES:
        raise TicketInvalid("ticket_audience_invalid")
    if audience is not None and not hmac.compare_digest(claimed_audience, audience):
        raise TicketInvalid("ticket_audience_mismatch")
    try:
        iat, exp = int(payload["iat"]), int(payload["exp"])
    except (KeyError, TypeError, ValueError) as exc:
        raise TicketInvalid("ticket_time_invalid") from exc
    if iat > current or current > exp:
        raise TicketInvalid("ticket_expired")
    for required in ("jti", "user_id", "provider", "tenant_id", "owner_key", "session_id", "membership_revision"):
        if not str(payload.get(required) or "").strip():
            raise TicketInvalid("ticket_claims_invalid")
    return payload


def consume_ticket(
    ticket: str,
    *,
    audience: str | None = None,
    store: AuthorityStore | None = None,
    now: int | None = None,
) -> dict[str, Any]:
    """Verify and atomically consume a signed browser ticket."""
    active_store = store or authority_store()
    payload = verify_ticket(ticket, audience=audience, store=active_store, now=now)
    scope = _scope_from_material(
        provider=str(payload["provider"]),
        tenant_id=str(payload["tenant_id"]),
        user_id=str(payload["user_id"]),
        session_id=str(payload["session_id"]),
        membership_revision=str(payload["membership_revision"]),
    )
    try:
        active_store.check_and_consume(
            scope,
            token_class=str(payload["typ"]),
            issuer_key_version=str(payload["iss"]),
            jti=str(payload["jti"]),
            audience=str(payload["aud"]),
            expires_at=int(payload["exp"]),
            claim_epoch=int(payload["epoch"]),
            claim_recovery_generation=int(payload["recovery_generation"]),
            now=now,
        )
    except Exception as exc:
        from hermes_cli.dashboard_auth.authority import AuthorizationRejected
        if isinstance(exc, AuthorityUnavailable):
            raise TicketInvalid("authority_unavailable") from exc
        if isinstance(exc, AuthorizationRejected):
            code = "unknown ticket" if exc.code == "credential_replayed" else (
                "ticket_rejected" if exc.code in {"recovery_generation_mismatch", "epoch_mismatch"} else exc.code
            )
            raise TicketInvalid(code) from exc
        raise
    return payload


def internal_ws_credential() -> str:
    """Return the isolated loopback/server-spawned internal credential."""
    global _internal_credential
    with _lock:
        if _internal_credential is None:
            _internal_credential = secrets.token_urlsafe(32)
        return _internal_credential


def consume_internal_credential(value: str) -> dict[str, Any]:
    """Validate the intentionally separate multi-use internal credential."""
    with _lock:
        expected = _internal_credential
    if not value or expected is None or not secrets.compare_digest(value.encode(), expected.encode()):
        raise TicketInvalid("internal_credential_invalid")
    return {"user_id": INTERNAL_USER_ID, "provider": INTERNAL_PROVIDER}


def _reset_for_tests() -> None:
    """Test-only reset for the loopback internal credential."""
    global _internal_credential
    with _lock:
        _internal_credential = None
