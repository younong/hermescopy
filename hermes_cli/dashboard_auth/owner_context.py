"""Owner identity derivation for authenticated dashboard users.

The Control Plane must derive owner identity only from trusted backend
artifacts: a verified dashboard-auth :class:`Session`, a server-minted WS
 ticket payload, or a server-spawned worker context. Frontend-provided owner
fields are display hints at most and are never authorization inputs.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from hermes_constants import get_hermes_home
from hermes_cli.dashboard_auth.base import Session
from hermes_cli.owner_runtime import ensure_owner_runtime_dirs, owner_worker_env_for

_OWNER_KEY_VERSION = "ok1"
_OWNER_KEY_DIGEST_BYTES = 18
_PERSONAL_TENANT_PREFIX = "personal"
_SECRET_ENV = "HERMES_OWNER_SECRET"
_SECRET_PATH = Path("control-plane") / "owner_secret"
_KEYRING_PATH = Path("control-plane") / "owner_keyring.json"
_KEYRING_SCHEMA_VERSION = 1
_SAFE_COMPONENT_RE = re.compile(r"[^A-Za-z0-9_.-]+")
_OWNER_KEY_RE = re.compile(r"^ok1_[A-Za-z0-9_.-]{1,128}$")


@dataclass(frozen=True)
class OwnerContext:
    """Stable authenticated identity plus Control Plane host-side paths.

    ``host_global_home`` and ``host_owner_home`` are Control Plane filesystem
    facts. A worker's ``runtime_owner_home`` is its own mount/runtime view and
    must be verified by the worker; matching absolute path strings are not an
    authorization proof.
    """

    auth_provider: str
    tenant_id: str
    owner_user_id: str
    owner_key: str
    host_global_home: Path
    host_owner_home: Path

    @property
    def owner_home(self) -> Path:
        """Compatibility alias for the Control Plane's host owner home."""

        return self.host_owner_home


def _safe_component(value: str, *, fallback: str) -> str:
    value = (value or "").strip()
    if not value:
        value = fallback
    safe = _SAFE_COMPONENT_RE.sub("_", value)
    safe = safe.strip("._-")
    return safe or fallback


def tenant_id_from_org_id(org_id: str) -> str:
    """Return the legacy tenant id for callers that only know org_id."""

    org_id = (org_id or "").strip()
    if org_id:
        return org_id
    return _PERSONAL_TENANT_PREFIX


def tenant_id_from_provider_org(auth_provider: str, org_id: str) -> str:
    """Return the public tenant id for provider/org identity material."""

    org_id = (org_id or "").strip()
    if org_id:
        return org_id
    provider = _safe_component(auth_provider, fallback="unknown-provider")
    return f"{_PERSONAL_TENANT_PREFIX}:{provider}"


def tenant_id_from_session(session: Session) -> str:
    """Return the stable tenant id for a verified dashboard-auth session.

    Org-backed identities keep the historical org id. Personal identities are
    provider-scoped so two providers with tenant-local user ids do not share the
    same public tenant label. Owner-key HMAC material intentionally keeps the
    legacy no-org tenant value for ok1 stability; see _owner_key_tenant_material.
    """

    return tenant_id_from_provider_org(session.provider, session.org_id)


def _owner_key_tenant_material(tenant_id: str) -> str:
    """Return tenant material for the ok1 owner-key compatibility contract.

    Public personal tenants are provider-scoped (``personal:<provider>``) so
    HTTP/WS payloads are explicit about the tenant shape.  ok1 owner keys were
    originally minted with the literal ``personal`` tenant material for no-org
    users; keep that material here so existing personal users do not silently
    move to a new owner home.  Provider separation for ok1 still comes from
    ``auth_provider`` being a separate HMAC input in :func:`_derive_owner_key`.
    A future ok2 key format can switch to the public tenant material with an
    explicit migration/dual-home lookup.
    """

    if tenant_id.startswith(f"{_PERSONAL_TENANT_PREFIX}:"):
        return _PERSONAL_TENANT_PREFIX
    return tenant_id


def _keyring_path() -> Path:
    return get_hermes_home() / _KEYRING_PATH


def owner_keyring_backup_paths() -> tuple[Path, ...]:
    """Return every persistent file operators must back up for owner keys."""

    return (_keyring_path(),)


def _read_keyring() -> dict[str, Any] | None:
    path = _keyring_path()
    try:
        st = path.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise RuntimeError(f"owner keyring must be a regular file: {path}")
    if os.name != "nt" and st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise RuntimeError(f"owner keyring has unsafe permissions: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"owner keyring is unreadable: {path}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != _KEYRING_SCHEMA_VERSION:
        raise RuntimeError(f"owner keyring has unsupported schema: {path}")
    active = payload.get("active")
    if not isinstance(active, dict) or not str(active.get("secret") or "").strip():
        raise RuntimeError(f"owner keyring has no active secret: {path}")
    pending = payload.get("pending")
    if pending is not None and (not isinstance(pending, dict) or not str(pending.get("secret") or "").strip()):
        raise RuntimeError(f"owner keyring has invalid pending rotation: {path}")
    return payload


def _write_keyring(payload: Mapping[str, Any]) -> None:
    path = _keyring_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.is_symlink():
        raise RuntimeError(f"owner keyring must not be a symlink: {path}")
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
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _new_keyring(secret: str) -> dict[str, Any]:
    return {
        "schema_version": _KEYRING_SCHEMA_VERSION,
        "active": {"version": _OWNER_KEY_VERSION, "secret": secret, "created_at": int(time.time())},
        "retained": [],
        "pending": None,
    }


def _owner_secret() -> bytes:
    """Return the active owner key secret, preserving the legacy file format.

    Existing installations continue to read ``control-plane/owner_secret`` until
    an operator explicitly starts rotation. Once a keyring exists, it is the
    sole source of active-version state and an environment override must match
    it exactly rather than silently remapping owner homes.
    """

    env_secret = os.environ.get(_SECRET_ENV, "").strip()
    keyring = _read_keyring()
    if keyring is not None:
        active_secret = str(keyring["active"]["secret"])
        if env_secret and not hmac.compare_digest(env_secret, active_secret):
            raise RuntimeError(
                f"{_SECRET_ENV} does not match persisted active owner keyring secret; "
                "refusing to silently remap owner homes"
            )
        return active_secret.encode("utf-8")

    global_home = get_hermes_home()
    secret_path = global_home / _SECRET_PATH
    try:
        secret_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

    try:
        existing = secret_path.read_text(encoding="utf-8").strip()
        if existing:
            try:
                secret_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass
            if env_secret and not hmac.compare_digest(env_secret, existing):
                raise RuntimeError(
                    f"{_SECRET_ENV} does not match persisted owner secret at {secret_path}; "
                    "refusing to silently remap owner homes"
                )
            return existing.encode("utf-8")
    except FileNotFoundError:
        pass

    if env_secret:
        return env_secret.encode("utf-8")

    secret = secrets.token_urlsafe(48)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        fd = os.open(secret_path, flags, 0o600)
    except FileExistsError:
        deadline = time.monotonic() + 2.0
        while True:
            existing = secret_path.read_text(encoding="utf-8").strip()
            if existing:
                return existing.encode("utf-8")
            if time.monotonic() >= deadline:
                raise RuntimeError(f"owner secret exists but is empty: {secret_path}")
            time.sleep(0.02)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(secret + "\n")
    try:
        secret_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    return secret.encode("utf-8")


def _derive_owner_key_with_secret(
    *, auth_provider: str, tenant_id: str, owner_user_id: str, secret: bytes
) -> str:
    material = "\x1f".join((auth_provider, tenant_id, owner_user_id)).encode("utf-8")
    digest = hmac.new(secret, material, hashlib.sha256).hexdigest()
    return f"{_OWNER_KEY_VERSION}_{digest[: _OWNER_KEY_DIGEST_BYTES * 2]}"


def _derive_owner_key(*, auth_provider: str, tenant_id: str, owner_user_id: str) -> str:
    return _derive_owner_key_with_secret(
        auth_provider=auth_provider,
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        secret=_owner_secret(),
    )


def begin_owner_key_rotation(next_secret: str) -> None:
    """Persist a pending replacement secret without changing active owner keys.

    Rotation is deliberately two-phase: callers must migrate every affected
    owner home and then call :func:`complete_owner_key_rotation`. This prevents
    an implicit login-time change from orphaning an existing owner runtime.
    """

    next_secret = str(next_secret or "").strip()
    if not next_secret:
        raise ValueError("next owner secret must be non-empty")
    current = _owner_secret().decode("utf-8")
    keyring = _read_keyring() or _new_keyring(current)
    active_secret = str(keyring["active"]["secret"])
    if not hmac.compare_digest(active_secret, current):
        raise RuntimeError("persisted active owner secret does not match the current owner secret")
    pending = keyring.get("pending")
    if pending is not None:
        if hmac.compare_digest(str(pending["secret"]), next_secret):
            return
        raise RuntimeError("a different owner key rotation is already pending")
    if hmac.compare_digest(active_secret, next_secret):
        raise ValueError("next owner secret must differ from the active secret")
    keyring["pending"] = {
        "version": _OWNER_KEY_VERSION,
        "secret": next_secret,
        "created_at": int(time.time()),
        "migrated_owner_keys": [],
        "migrated_destination_keys": [],
    }
    _write_keyring(keyring)


def migrate_owner_home_for_rotation(
    owner: OwnerContext, *, destination_owner_key: str | None = None
) -> Path:
    """Move one admitted owner home to the pending key's destination.

    The operation is intentionally explicit and fail-closed. It does not create
    a target home, merge a collision, or switch the active signing secret.
    """

    keyring = _read_keyring()
    if keyring is None or keyring.get("pending") is None:
        raise RuntimeError("no owner key rotation is pending")
    expected_source = _derive_owner_key_with_secret(
        auth_provider=owner.auth_provider,
        tenant_id=_owner_key_tenant_material(owner.tenant_id),
        owner_user_id=owner.owner_user_id,
        secret=str(keyring["active"]["secret"]).encode("utf-8"),
    )
    if owner.owner_key != expected_source:
        raise RuntimeError("owner context does not match the active owner secret")
    target_key = destination_owner_key or _derive_owner_key_with_secret(
        auth_provider=owner.auth_provider,
        tenant_id=_owner_key_tenant_material(owner.tenant_id),
        owner_user_id=owner.owner_user_id,
        secret=str(keyring["pending"]["secret"]).encode("utf-8"),
    )
    if not _OWNER_KEY_RE.match(target_key):
        raise ValueError("invalid destination owner key")
    source = owner.owner_home
    target = _host_owner_home(target_key, host_global_home=owner.host_global_home)
    if source == target:
        return target
    if not source.is_dir() or source.is_symlink():
        raise RuntimeError("source owner home is missing or unsafe")
    if target.exists() or target.is_symlink():
        raise RuntimeError("destination owner home already exists")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(str(source), str(target))
    except OSError as exc:
        raise RuntimeError("owner home migration failed") from exc
    migrated = keyring["pending"].setdefault("migrated_owner_keys", [])
    destinations = keyring["pending"].setdefault("migrated_destination_keys", [])
    changed = False
    if owner.owner_key not in migrated:
        migrated.append(owner.owner_key)
        changed = True
    if target_key not in destinations:
        destinations.append(target_key)
        changed = True
    if changed:
        _write_keyring(keyring)
    return target


def complete_owner_key_rotation() -> None:
    """Atomically activate a pending owner-key secret after all homes move."""

    keyring = _read_keyring()
    if keyring is None or keyring.get("pending") is None:
        raise RuntimeError("no owner key rotation is pending")
    global_home = get_hermes_home()
    users_root = global_home / "users"
    migrated_sources = set(keyring["pending"].get("migrated_owner_keys", []))
    migrated_destinations = set(keyring["pending"].get("migrated_destination_keys", []))
    if users_root.exists():
        for source in users_root.iterdir():
            if (
                source.is_dir()
                and source.name.startswith(f"{_OWNER_KEY_VERSION}_")
                and source.name not in migrated_sources
                and source.name not in migrated_destinations
            ):
                raise RuntimeError("owner home migration is incomplete")
    old_active = dict(keyring["active"])
    old_active["retired_at"] = int(time.time())
    keyring.setdefault("retained", []).append(old_active)
    keyring["active"] = {
        key: value
        for key, value in keyring["pending"].items()
        if key not in {"migrated_owner_keys", "migrated_destination_keys"}
    }
    keyring["pending"] = None
    _write_keyring(keyring)


def _host_owner_home(owner_key: str, *, host_global_home: Path | None = None) -> Path:
    return (host_global_home or get_hermes_home()) / "users" / owner_key


def owner_context_from_session(session: Session) -> OwnerContext:
    """Derive an owner context from a verified dashboard-auth session."""

    auth_provider = _safe_component(session.provider, fallback="unknown-provider")
    tenant_id = tenant_id_from_session(session)
    owner_user_id = session.user_id.strip()
    if not owner_user_id:
        raise ValueError("session.user_id is required to derive owner context")
    owner_key = _derive_owner_key(
        auth_provider=auth_provider,
        tenant_id=_owner_key_tenant_material(tenant_id),
        owner_user_id=owner_user_id,
    )
    host_global_home = get_hermes_home()
    return OwnerContext(
        auth_provider=auth_provider,
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        owner_key=owner_key,
        host_global_home=host_global_home,
        host_owner_home=_host_owner_home(owner_key, host_global_home=host_global_home),
    )


def owner_context_from_owner_key(owner_key: str, *, global_home: str | Path | None = None) -> OwnerContext:
    """Reconstruct a Control Plane owner context from a signed owner key.

    This is only for server-minted internal owner-worker tokens whose signature
    was already validated by the Control Plane. Browser/front-end owner fields
    must continue to use :func:`owner_context_from_session` or
    :func:`owner_context_from_ticket_payload` instead.  Owner workers must not
    call this without an explicit Control Plane/global home: their ``HERMES_HOME``
    is already ``<global>/users/<owner_key>``, so a blind reconstruction would
    incorrectly derive ``<owner_home>/users/<owner_key>``.
    """

    owner_key = str(owner_key or "").strip()
    if not _OWNER_KEY_RE.match(owner_key):
        raise ValueError("invalid owner_key")
    if global_home is None and os.environ.get("HERMES_OWNER_KEY", "").strip():
        raise RuntimeError("owner_context_from_owner_key requires explicit global_home inside owner workers")
    base_home = Path(global_home).expanduser().resolve() if global_home is not None else get_hermes_home()
    return OwnerContext(
        auth_provider="internal-owner-token",
        tenant_id="",
        owner_user_id="",
        owner_key=owner_key,
        host_global_home=base_home,
        host_owner_home=_host_owner_home(owner_key, host_global_home=base_home),
    )


def owner_context_from_ticket_payload(payload: Mapping[str, Any]) -> OwnerContext:
    """Reconstruct and verify owner context from a server-minted WS payload."""

    auth_provider = _safe_component(str(payload.get("provider") or ""), fallback="unknown-provider")
    owner_user_id = str(payload.get("user_id") or "").strip()
    org_id = str(payload.get("org_id") or "")
    tenant_id = str(payload.get("tenant_id") or "").strip() or tenant_id_from_provider_org(auth_provider, org_id)
    if not owner_user_id:
        raise ValueError("ticket payload missing user_id")
    expected_tenant_id = tenant_id_from_provider_org(auth_provider, org_id)
    if tenant_id != expected_tenant_id:
        raise ValueError("ticket payload tenant_id mismatch")
    owner_key = _derive_owner_key(
        auth_provider=auth_provider,
        tenant_id=_owner_key_tenant_material(tenant_id),
        owner_user_id=owner_user_id,
    )
    payload_owner_key = str(payload.get("owner_key") or "").strip()
    if payload_owner_key and payload_owner_key != owner_key:
        raise ValueError("ticket payload owner_key mismatch")
    host_global_home = get_hermes_home()
    return OwnerContext(
        auth_provider=auth_provider,
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        owner_key=owner_key,
        host_global_home=host_global_home,
        host_owner_home=_host_owner_home(owner_key, host_global_home=host_global_home),
    )


def owner_public_summary(owner: OwnerContext) -> dict[str, str]:
    """Return non-host-sensitive owner fields safe for API responses."""

    return {
        "tenant_id": owner.tenant_id,
        "owner_key": owner.owner_key,
    }


def _admit_host_owner_home(owner: OwnerContext) -> Path:
    """Create and verify the Control Plane's trusted owner-home path.

    The path is derived solely from the canonical owner key. This is a host-root
    admission check, not the descriptor-relative filesystem protocol planned
    for iteration 7.
    """

    global_home = owner.host_global_home
    users_root = global_home / "users"
    expected = users_root / owner.owner_key
    if owner.host_owner_home != expected:
        raise RuntimeError("host_owner_home does not match canonical owner key")

    for path, label in ((global_home, "host global home"), (users_root, "host users root")):
        try:
            info = path.lstat()
        except FileNotFoundError:
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
            info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise RuntimeError(f"{label} must not be a symlink")
        if not stat.S_ISDIR(info.st_mode):
            raise RuntimeError(f"{label} must be a directory")
        if os.name != "nt" and info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise RuntimeError(f"{label} has unsafe permissions")

    try:
        before = expected.lstat()
    except FileNotFoundError:
        expected.mkdir(mode=0o700)
        before = expected.lstat()
    if stat.S_ISLNK(before.st_mode):
        raise RuntimeError("host owner home must not be a symlink")
    if not stat.S_ISDIR(before.st_mode):
        raise RuntimeError("host owner home must be a directory")
    if os.name != "nt" and before.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise RuntimeError("host owner home has unsafe permissions")
    if hasattr(os, "getuid") and before.st_uid != os.getuid():
        raise RuntimeError("host owner home has unexpected ownership")
    users_info = users_root.lstat()
    if before.st_dev != users_info.st_dev:
        raise RuntimeError("host owner home is on an unexpected mount")
    after = expected.lstat()
    if (before.st_dev, before.st_ino, stat.S_IFMT(before.st_mode)) != (
        after.st_dev,
        after.st_ino,
        stat.S_IFMT(after.st_mode),
    ):
        raise RuntimeError("host owner home changed during admission")
    return expected


def ensure_owner_home(owner: OwnerContext) -> Path:
    """Create the admitted host owner home and runtime/data subdirectories."""

    return ensure_owner_runtime_dirs(_admit_host_owner_home(owner))


def owner_worker_env(owner: OwnerContext) -> dict[str, str]:
    """Return environment variables for a future owner worker process."""

    return owner_worker_env_for(
        owner_key=owner.owner_key,
        owner_home=owner.owner_home,
        tenant_id=owner.tenant_id,
        owner_user_id=owner.owner_user_id,
        auth_provider=owner.auth_provider,
    )
