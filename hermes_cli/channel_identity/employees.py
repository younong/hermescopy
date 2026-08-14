"""Owner-scoped employees, immutable profiles, and optional channel bindings."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import time
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from hermes_cli.dashboard_auth.owner_context import (
    OwnerContext,
    ensure_owner_home,
    owner_context_from_owner_key,
)
from hermes_cli.employee_policy import (
    LEGACY_EMPLOYEE_WORKSPACE,
    effective_employee_workspace,
    employee_policy_with_workspace,
    employee_workspace_relative_path,
    normalize_employee_source_policy,
)

from .credentials import decrypt_account_credentials, encrypt_account_credentials
from .models import (
    Employee,
    EmployeeChannelBinding,
    EmployeeCollaborationPolicy,
    EmployeeProfile,
    RegisteredChannel,
)
from .registration import ensure_owner_binding
from .store import EMPLOYEE_PROFILE_AAD_TABLE, ChannelIdentityStore

_FEISHU_PROVIDER = "feishu"


class EmployeeProfileRevisionConflict(RuntimeError):
    """The requested profile revision is no longer current."""


class FeishuCredentialRevisionConflict(RuntimeError):
    """The requested Feishu credential version is no longer current."""


class BuiltinEmployeeProtected(RuntimeError):
    """A mutation targeted the Owner's protected built-in assistant."""


def _employee_id(value: str) -> str:
    employee_id = str(value or "").strip()
    if not employee_id.startswith("emp_") or len(employee_id) <= 4:
        raise ValueError("employee ID is invalid")
    return employee_id


def _canonical_profile(
    profile: Mapping[str, Any],
) -> tuple[dict[str, Any], str, str]:
    if not isinstance(profile, Mapping) or not profile:
        raise ValueError("employee profile is required")
    try:
        payload = json.dumps(
            dict(profile),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("employee profile must be JSON serializable") from exc
    normalized = json.loads(payload)
    if not isinstance(normalized, dict) or any(
        not isinstance(key, str) or not key for key in normalized
    ):
        raise ValueError("employee profile must be a JSON object with named fields")
    fingerprint = "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return normalized, payload, fingerprint


def employee_profile_fingerprint(profile: Mapping[str, Any]) -> str:
    """Return a byte-stable fingerprint of a canonical JSON profile."""
    return _canonical_profile(profile)[2]


def create_employee(
    store: ChannelIdentityStore,
    *,
    owner: OwnerContext,
    profile: Mapping[str, Any],
    activate: bool = True,
    employee_id: str | None = None,
) -> Employee:
    """Create an Owner-scoped employee with one backend-owned workspace."""
    employee_id = _employee_id(employee_id) if employee_id else f"emp_{uuid.uuid4().hex}"
    source_policy = employee_policy_with_workspace(profile, employee_id=employee_id)
    _, profile_payload, profile_fingerprint = _canonical_profile(source_policy)
    workspace = _provision_employee_workspace(owner, employee_id, require_new=True)
    committed = False
    now = time.time()
    try:
        with store.write() as conn:
            canonical_user_id = ensure_owner_binding(store, owner, conn=conn)
            conn.execute(
                """
                INSERT INTO employees
                  (employee_id, canonical_user_id, employee_kind, lifecycle_status,
                   created_at, updated_at)
                VALUES (?, ?, 'managed', ?, ?, ?)
                """,
                (
                    employee_id,
                    canonical_user_id,
                    "active" if activate else "suspended",
                    now,
                    now,
                ),
            )
            _insert_profile_revision(
                store,
                conn,
                employee_id=employee_id,
                revision=1,
                profile_payload=profile_payload,
                profile_fingerprint=profile_fingerprint,
                now=now,
            )
        committed = True
    finally:
        if not committed:
            _remove_empty_workspace(workspace)
    return resolve_employee(store, owner=owner, employee_id=employee_id)


def list_employees(
    store: ChannelIdentityStore,
    *,
    owner: OwnerContext,
) -> tuple[Employee, ...]:
    """List employees owned by the authenticated Owner, repairing its built-in."""
    with store.write() as conn:
        ensure_owner_binding(store, owner, conn=conn)
        rows = conn.execute(
            _EMPLOYEE_SELECT +
            " WHERE o.owner_key=? "
            "ORDER BY e.employee_kind='builtin_assistant' DESC, "
            "e.created_at, e.employee_id",
            (owner.owner_key,),
        ).fetchall()
    return tuple(_employee_from_row(row) for row in rows)


def resolve_employee(
    store: ChannelIdentityStore,
    *,
    owner: OwnerContext,
    employee_id: str,
) -> Employee:
    """Resolve one employee after an exact Owner authorization check."""
    employee_id = _employee_id(employee_id)
    with store.read() as conn:
        row = conn.execute(
            _EMPLOYEE_SELECT + " WHERE e.employee_id=? AND o.owner_key=?",
            (employee_id, owner.owner_key),
        ).fetchone()
    if row is None:
        raise RuntimeError("employee is unavailable")
    return _employee_from_row(row)


def set_employee_status(
    store: ChannelIdentityStore,
    *,
    owner: OwnerContext,
    employee_id: str,
    status: str,
) -> Employee:
    """Set employee lifecycle without changing any channel binding lifecycle."""
    employee_id = _employee_id(employee_id)
    status = str(status or "").strip()
    if status not in {"active", "suspended", "revoked"}:
        raise ValueError("employee status is invalid")
    with store.write() as conn:
        row = _owned_employee_row(conn, owner=owner, employee_id=employee_id)
        if row is None:
            raise RuntimeError("employee is unavailable")
        _reject_builtin_mutation(row)
        if row["lifecycle_status"] == "revoked" and status != "revoked":
            raise RuntimeError("revoked employee cannot be reactivated")
        conn.execute(
            "UPDATE employees SET lifecycle_status=?, updated_at=? WHERE employee_id=?",
            (status, time.time(), employee_id),
        )
    return resolve_employee(store, owner=owner, employee_id=employee_id)


def update_employee_profile(
    store: ChannelIdentityStore,
    *,
    owner: OwnerContext,
    employee_id: str,
    profile: Mapping[str, Any],
    expected_revision: int,
) -> EmployeeProfile:
    """Create the next encrypted profile revision behind an optimistic fence."""
    employee_id = _employee_id(employee_id)
    try:
        expected_revision = int(expected_revision)
    except (TypeError, ValueError) as exc:
        raise ValueError("expected revision must be an integer") from exc
    if expected_revision < 0:
        raise ValueError("expected revision must not be negative")
    now = time.time()
    with store.write() as conn:
        employee = _owned_employee_row(conn, owner=owner, employee_id=employee_id)
        if employee is None:
            raise RuntimeError("employee is unavailable")
        _reject_builtin_mutation(employee)
        if employee["lifecycle_status"] != "active":
            raise RuntimeError("employee is unavailable")
        current = conn.execute(
            "SELECT * FROM employee_profiles "
            "WHERE employee_id=? AND lifecycle_status='active'",
            (employee_id,),
        ).fetchone()
        current_revision = int(current["revision"]) if current is not None else 0
        if current_revision != expected_revision:
            raise EmployeeProfileRevisionConflict(
                f"employee profile revision changed from {expected_revision} to {current_revision}"
            )
        current_workspace = employee_workspace_relative_path(employee_id)
        if current is not None:
            current_profile = _profile_from_row(store, current)
            current_workspace = effective_employee_workspace(
                employee_id,
                current_profile["workspace_relative_path"],
            )
        source_policy = employee_policy_with_workspace(
            profile,
            employee_id=employee_id,
            current_workspace=current_workspace,
        )
        normalized_profile, profile_payload, fingerprint = _canonical_profile(source_policy)
        revision = current_revision + 1
        if current is not None:
            conn.execute(
                "UPDATE employee_profiles SET lifecycle_status='superseded', updated_at=? "
                "WHERE employee_id=? AND revision=?",
                (now, employee_id, current_revision),
            )
        _insert_profile_revision(
            store,
            conn,
            employee_id=employee_id,
            revision=revision,
            profile_payload=profile_payload,
            profile_fingerprint=fingerprint,
            now=now,
        )
    return EmployeeProfile(
        employee_id=employee_id,
        revision=revision,
        fingerprint=fingerprint,
        lifecycle_status="active",
        profile=normalized_profile,
    )


def resolve_employee_profile(
    store: ChannelIdentityStore,
    *,
    owner: OwnerContext,
    employee_id: str,
    revision: int | None = None,
) -> EmployeeProfile:
    """Resolve an Owner-authorized profile, optionally at an exact revision."""
    employee_id = _employee_id(employee_id)
    with store.read() as conn:
        if _owned_employee_row(conn, owner=owner, employee_id=employee_id) is None:
            raise RuntimeError("employee is unavailable")
        if revision is None:
            row = conn.execute(
                "SELECT * FROM employee_profiles "
                "WHERE employee_id=? AND lifecycle_status='active'",
                (employee_id,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM employee_profiles WHERE employee_id=? AND revision=?",
                (employee_id, int(revision)),
            ).fetchone()
    if row is None:
        raise RuntimeError("employee profile is unavailable")
    profile = _profile_from_row(store, row)
    _, _, fingerprint = _canonical_profile(profile)
    if fingerprint != row["profile_fingerprint"]:
        raise RuntimeError("employee profile fingerprint is inconsistent")
    return EmployeeProfile(
        employee_id=employee_id,
        revision=int(row["revision"]),
        fingerprint=fingerprint,
        lifecycle_status=row["lifecycle_status"],
        profile=profile,
    )


def resolve_employee_collaboration_policy(
    store: ChannelIdentityStore,
    *,
    owner: OwnerContext,
    employee_id: str,
) -> EmployeeCollaborationPolicy:
    """Resolve internal collaboration permissions for one employee."""
    return resolve_employee(
        store, owner=owner, employee_id=employee_id
    ).collaboration_policy


def update_employee_collaboration_policy(
    store: ChannelIdentityStore,
    *,
    owner: OwnerContext,
    employee_id: str,
    may_participate: bool,
    may_create_groups: bool,
    invite_quota: int | None,
) -> EmployeeCollaborationPolicy:
    """Replace internal collaboration permissions for one employee."""
    employee_id = _employee_id(employee_id)
    if not isinstance(may_participate, bool) or not isinstance(may_create_groups, bool):
        raise ValueError("collaboration policy flags must be booleans")
    if invite_quota is not None and (
        isinstance(invite_quota, bool)
        or not isinstance(invite_quota, int)
        or invite_quota < 0
    ):
        raise ValueError("invite quota must be a non-negative integer or null")
    now = time.time()
    with store.write() as conn:
        employee = _owned_employee_row(conn, owner=owner, employee_id=employee_id)
        if employee is None:
            raise RuntimeError("employee is unavailable")
        _reject_builtin_mutation(employee)
        conn.execute(
            """
            INSERT INTO employee_collaboration_policies
              (employee_id, may_participate, may_create_groups, invite_quota,
               updated_by_owner_key, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(employee_id) DO UPDATE SET
                may_participate=excluded.may_participate,
                may_create_groups=excluded.may_create_groups,
                invite_quota=excluded.invite_quota,
                updated_by_owner_key=excluded.updated_by_owner_key,
                updated_at=excluded.updated_at
            """,
            (
                employee_id,
                int(may_participate),
                int(may_create_groups),
                invite_quota,
                owner.owner_key,
                now,
                now,
            ),
        )
    return EmployeeCollaborationPolicy(
        employee_id=employee_id,
        may_participate=may_participate,
        may_create_groups=may_create_groups,
        invite_quota=invite_quota,
    )


def ensure_employee_feishu_conversation_binding(
    store: ChannelIdentityStore,
    *,
    employee_id: str,
    connector_account_id: str,
    conversation_id: str,
    actor_id: str,
    conversation_kind: str,
) -> RegisteredChannel:
    """Create one conversation binding under an active employee Feishu account."""
    employee_id = _employee_id(employee_id)
    connector_account_id = str(connector_account_id or "").strip()
    conversation_id = str(conversation_id or "").strip()
    actor_id = str(actor_id or "").strip()
    if not connector_account_id:
        raise ValueError("connector account ID is required")
    if not conversation_id:
        raise ValueError("conversation ID is required")
    if not actor_id:
        raise ValueError("Feishu actor ID is required")
    if conversation_kind not in {"direct", "group"}:
        raise ValueError("Feishu conversation kind is invalid")
    conversation_hash = store.crypto.lookup_hash(
        f"conversation:{_FEISHU_PROVIDER}", conversation_id
    )
    actor_hash = store.crypto.lookup_hash(
        f"external-subject:{_FEISHU_PROVIDER}", actor_id
    )
    now = time.time()
    with store.write() as conn:
        kind = conn.execute(
            "SELECT employee_kind FROM employees WHERE employee_id=?",
            (employee_id,),
        ).fetchone()
        if kind is not None:
            _reject_builtin_mutation(kind)
        row = conn.execute(
            """
            SELECT e.canonical_user_id, e.employee_kind, o.owner_key
            FROM employees e
            JOIN employee_channel_bindings eb ON eb.employee_id=e.employee_id
            JOIN connector_accounts a ON a.account_id=eb.connector_account_id
                                     AND a.provider=eb.provider
            JOIN owner_bindings o ON o.canonical_user_id=e.canonical_user_id
            WHERE e.employee_id=? AND eb.connector_account_id=?
              AND eb.provider='feishu' AND eb.lifecycle_status='active'
              AND e.lifecycle_status='active' AND a.status='active'
            """,
            (employee_id, connector_account_id),
        ).fetchone()
        if row is None:
            raise RuntimeError("employee Feishu binding is unavailable")
        _reject_builtin_mutation(row)
        existing = conn.execute(
            "SELECT binding_id, external_identity_id FROM channel_bindings "
            "WHERE account_id=? AND peer_lookup_hash=?",
            (connector_account_id, conversation_hash),
        ).fetchone()
        created = existing is None
        if existing is not None:
            binding_id = str(existing["binding_id"])
            external_identity_id = str(existing["external_identity_id"])
        else:
            identity = conn.execute(
                "SELECT external_identity_id, canonical_user_id FROM external_identities "
                "WHERE provider=? AND subject_lookup_hash=?",
                (_FEISHU_PROVIDER, actor_hash),
            ).fetchone()
            if identity is not None and identity["canonical_user_id"] != row["canonical_user_id"]:
                from .registration import ChannelIdentityOwnershipConflict

                raise ChannelIdentityOwnershipConflict(
                    "confirmed identity belongs to another Owner"
                )
            if identity is None:
                external_identity_id = f"ei_{uuid.uuid4().hex}"
                subject_ciphertext, subject_version = store.crypto.encrypt_text(
                    actor_id,
                    table="external_identities",
                    record_id=external_identity_id,
                    field="subject",
                )
                conn.execute(
                    """
                    INSERT INTO external_identities
                      (external_identity_id, provider, subject_lookup_hash,
                       subject_ciphertext, subject_key_version, canonical_user_id,
                       status, created_at, updated_at)
                    VALUES (?, 'feishu', ?, ?, ?, ?, 'active', ?, ?)
                    """,
                    (
                        external_identity_id,
                        actor_hash,
                        subject_ciphertext,
                        subject_version,
                        row["canonical_user_id"],
                        now,
                        now,
                    ),
                )
            else:
                external_identity_id = str(identity["external_identity_id"])
            binding_id = f"cb_{uuid.uuid4().hex}"
            ciphertext, key_version = store.crypto.encrypt_text(
                conversation_id,
                table="channel_bindings",
                record_id=binding_id,
                field="peer",
            )
            conn.execute(
                """
                INSERT INTO channel_bindings
                  (binding_id, external_identity_id, account_id, peer_lookup_hash,
                   peer_ciphertext, peer_key_version, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?)
                """,
                (
                    binding_id,
                    external_identity_id,
                    connector_account_id,
                    conversation_hash,
                    ciphertext,
                    key_version,
                    now,
                    now,
                ),
            )
    return RegisteredChannel(
        canonical_user_id=row["canonical_user_id"],
        owner_key=row["owner_key"],
        external_identity_id=external_identity_id,
        account_id=connector_account_id,
        binding_id=binding_id,
        created=created,
    )


def register_employee_feishu_binding(
    store: ChannelIdentityStore,
    *,
    owner: OwnerContext,
    employee_id: str,
    provider_account_id: str,
    credentials: Mapping[str, Any],
    activate: bool = True,
) -> EmployeeChannelBinding:
    """Create a Feishu connector account and bind it to an existing employee."""
    employee_id = _employee_id(employee_id)
    provider_account_id = str(provider_account_id or "").strip()
    if not provider_account_id:
        raise ValueError("provider account ID is required")
    if not isinstance(credentials, Mapping) or not credentials:
        raise ValueError("connector credentials are required")
    now = time.time()
    account_id = f"ca_{uuid.uuid4().hex}"
    binding_id = f"ecb_{uuid.uuid4().hex}"
    account_hash = store.crypto.lookup_hash(
        f"provider-account:{_FEISHU_PROVIDER}", provider_account_id
    )
    ciphertext, key_version = encrypt_account_credentials(
        store, account_id=account_id, credentials=credentials
    )
    lifecycle_status = "active" if activate else "suspended"
    account_status = "active" if activate else "pending"
    with store.write() as conn:
        employee = _owned_employee_row(conn, owner=owner, employee_id=employee_id)
        if employee is None:
            raise RuntimeError("employee is unavailable")
        _reject_builtin_mutation(employee)
        if employee["lifecycle_status"] == "revoked":
            raise RuntimeError("employee is unavailable")
        if conn.execute(
            "SELECT 1 FROM employee_channel_bindings "
            "WHERE employee_id=? AND provider='feishu' AND lifecycle_status<>'revoked'",
            (employee_id,),
        ).fetchone() is not None:
            raise RuntimeError("employee already has a current Feishu binding")
        conn.execute(
            """
            INSERT INTO connector_accounts
              (account_id, provider, provider_account_id, account_lookup_hash,
               credentials_ciphertext, credentials_key_version,
               credential_version, status, created_at, updated_at)
            VALUES (?, 'feishu', ?, ?, ?, ?, 1, ?, ?, ?)
            """,
            (
                account_id,
                provider_account_id,
                account_hash,
                ciphertext,
                key_version,
                account_status,
                now,
                now,
            ),
        )
        conn.execute(
            """
            INSERT INTO employee_channel_bindings
              (binding_id, employee_id, provider, connector_account_id,
               lifecycle_status, created_at, updated_at)
            VALUES (?, ?, 'feishu', ?, ?, ?, ?)
            """,
            (binding_id, employee_id, account_id, lifecycle_status, now, now),
        )
    return resolve_employee_feishu_binding(
        store, owner=owner, employee_id=employee_id
    )


def resolve_employee_feishu_binding(
    store: ChannelIdentityStore,
    *,
    owner: OwnerContext,
    employee_id: str,
    include_revoked: bool = False,
) -> EmployeeChannelBinding:
    """Resolve the current, or most recent, Feishu binding for one employee."""
    employee_id = _employee_id(employee_id)
    lifecycle_filter = "" if include_revoked else " AND eb.lifecycle_status<>'revoked'"
    with store.read() as conn:
        row = conn.execute(
            _BINDING_SELECT
            + " WHERE eb.employee_id=? AND o.owner_key=? AND eb.provider='feishu'"
            + lifecycle_filter
            + " ORDER BY eb.created_at DESC LIMIT 1",
            (employee_id, owner.owner_key),
        ).fetchone()
    if row is None:
        raise RuntimeError("employee Feishu binding is unavailable")
    return _binding_from_row(row)


def resolve_employee_feishu_credentials(
    store: ChannelIdentityStore,
    *,
    owner: OwnerContext,
    employee_id: str,
) -> tuple[dict[str, Any], int]:
    """Decrypt current Feishu credentials after exact employee Owner authorization."""
    employee_id = _employee_id(employee_id)
    with store.read() as conn:
        employee = _owned_employee_row(conn, owner=owner, employee_id=employee_id)
        if employee is None:
            raise RuntimeError("employee is unavailable")
        _reject_builtin_mutation(employee)
        row = _owned_feishu_binding_row(
            conn, owner=owner, employee_id=employee_id
        )
    if row is None:
        raise RuntimeError("employee Feishu binding is unavailable")
    return decrypt_account_credentials(
        store,
        account_id=row["connector_account_id"],
        ciphertext=row["credentials_ciphertext"],
        key_version=row["credentials_key_version"],
    ), int(row["credential_version"])


def rotate_employee_feishu_credentials(
    store: ChannelIdentityStore,
    *,
    owner: OwnerContext,
    employee_id: str,
    credentials: Mapping[str, Any],
    expected_credential_version: int,
) -> EmployeeChannelBinding:
    """Rotate current Feishu credentials behind Owner and version fences."""
    employee_id = _employee_id(employee_id)
    try:
        expected_credential_version = int(expected_credential_version)
    except (TypeError, ValueError) as exc:
        raise ValueError("expected credential version must be an integer") from exc
    if expected_credential_version < 1:
        raise ValueError("expected credential version must be positive")
    if not isinstance(credentials, Mapping) or not credentials:
        raise ValueError("connector credentials are required")
    now = time.time()
    with store.write() as conn:
        employee = _owned_employee_row(conn, owner=owner, employee_id=employee_id)
        if employee is None:
            raise RuntimeError("employee is unavailable")
        _reject_builtin_mutation(employee)
        row = _owned_feishu_binding_row(
            conn, owner=owner, employee_id=employee_id
        )
        if row is None or row["lifecycle_status"] == "revoked":
            raise RuntimeError("employee Feishu binding is unavailable")
        current_version = int(row["credential_version"])
        if current_version != expected_credential_version:
            raise FeishuCredentialRevisionConflict(
                "Feishu credential version changed from "
                f"{expected_credential_version} to {current_version}"
            )
        ciphertext, key_version = encrypt_account_credentials(
            store,
            account_id=row["connector_account_id"],
            credentials=credentials,
        )
        conn.execute(
            """
            UPDATE connector_accounts
            SET credentials_ciphertext=?, credentials_key_version=?,
                credential_version=credential_version+1, updated_at=?
            WHERE account_id=?
            """,
            (ciphertext, key_version, now, row["connector_account_id"]),
        )
    return resolve_employee_feishu_binding(
        store, owner=owner, employee_id=employee_id
    )


def rollover_employee_sessions(
    store: ChannelIdentityStore,
    *,
    owner: OwnerContext,
    employee_id: str,
) -> int:
    """Retire idle channel-session mappings across one employee's bindings."""
    employee_id = _employee_id(employee_id)
    with store.write() as conn:
        employee = _owned_employee_row(conn, owner=owner, employee_id=employee_id)
        if employee is None:
            raise RuntimeError("employee is unavailable")
        _reject_builtin_mutation(employee)
        if employee["lifecycle_status"] != "active":
            raise RuntimeError("employee is unavailable")
        active = conn.execute(
            """
            SELECT 1 FROM inbound_messages i
            JOIN employee_channel_bindings eb
              ON eb.connector_account_id=i.account_id
            WHERE eb.employee_id=?
              AND i.status IN ('queued', 'processing', 'outbound_pending')
            LIMIT 1
            """,
            (employee_id,),
        ).fetchone()
        if active is not None:
            raise RuntimeError("employee has active conversations")
        cursor = conn.execute(
            """
            DELETE FROM channel_sessions
            WHERE binding_id IN (
                SELECT cb.binding_id
                FROM channel_bindings cb
                JOIN employee_channel_bindings eb
                  ON eb.connector_account_id=cb.account_id
                WHERE eb.employee_id=?
            )
            """,
            (employee_id,),
        )
        return int(cursor.rowcount)


def set_employee_feishu_binding_status(
    store: ChannelIdentityStore,
    *,
    owner: OwnerContext,
    employee_id: str,
    status: str,
) -> EmployeeChannelBinding:
    """Set Feishu binding lifecycle without changing employee lifecycle."""
    employee_id = _employee_id(employee_id)
    status = str(status or "").strip()
    if status not in {"active", "suspended", "revoked"}:
        raise ValueError("employee Feishu binding status is invalid")
    now = time.time()
    with store.write() as conn:
        employee = _owned_employee_row(conn, owner=owner, employee_id=employee_id)
        if employee is None:
            raise RuntimeError("employee is unavailable")
        _reject_builtin_mutation(employee)
        row = _owned_feishu_binding_row(
            conn,
            owner=owner,
            employee_id=employee_id,
            include_revoked=True,
        )
        if row is None:
            raise RuntimeError("employee Feishu binding is unavailable")
        if row["lifecycle_status"] == "revoked" and status != "revoked":
            raise RuntimeError("revoked employee Feishu binding cannot be reactivated")
        conn.execute(
            "UPDATE employee_channel_bindings SET lifecycle_status=?, updated_at=? "
            "WHERE binding_id=?",
            (status, now, row["binding_id"]),
        )
        conn.execute(
            "UPDATE connector_accounts SET status=?, updated_at=? WHERE account_id=?",
            (status, now, row["connector_account_id"]),
        )
    return resolve_employee_feishu_binding(
        store,
        owner=owner,
        employee_id=employee_id,
        include_revoked=status == "revoked",
    )


def _profile_from_row(store: ChannelIdentityStore, row) -> dict[str, Any]:
    payload = store.crypto.decrypt_text(
        row["profile_ciphertext"],
        table=EMPLOYEE_PROFILE_AAD_TABLE,
        record_id=f"{row['employee_id']}:{row['revision']}",
        field="profile",
        version=row["profile_key_version"],
    )
    try:
        profile = json.loads(payload)
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("employee profile is invalid") from exc
    if not isinstance(profile, dict):
        raise RuntimeError("employee profile is invalid")
    return profile


def _provision_employee_workspace(
    owner: OwnerContext,
    employee_id: str,
    *,
    require_new: bool,
) -> Path:
    owner_home = ensure_owner_home(owner)
    employees = owner_home / "workspaces" / "default" / "employees"
    try:
        employees.mkdir(mode=0o700)
    except FileExistsError:
        pass
    parent_info = employees.lstat()
    if stat.S_ISLNK(parent_info.st_mode) or not stat.S_ISDIR(parent_info.st_mode):
        raise RuntimeError("employee workspace parent must be a directory")
    if os.name != "nt" and parent_info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise RuntimeError("employee workspace parent has unsafe permissions")
    workspace = owner_home / "workspaces" / "default" / employee_workspace_relative_path(employee_id)
    try:
        workspace.mkdir(mode=0o700)
        created = True
    except FileExistsError:
        created = False
    info = workspace.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("employee workspace must be a directory")
    if os.name != "nt" and info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise RuntimeError("employee workspace has unsafe permissions")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise RuntimeError("employee workspace has unexpected ownership")
    if require_new and not created:
        raise RuntimeError("employee workspace already exists")
    return workspace


def _remove_empty_workspace(workspace: Path) -> None:
    try:
        workspace.rmdir()
    except (FileNotFoundError, OSError):
        pass


def reconcile_employee_workspaces(store: ChannelIdentityStore) -> int:
    """Provision canonical workspaces and revise only the retired UI placeholder."""
    with store.read() as conn:
        rows = conn.execute(
            "SELECT p.*, o.owner_key FROM employee_profiles p "
            "JOIN employees e ON e.employee_id=p.employee_id "
            "JOIN owner_bindings o ON o.canonical_user_id=e.canonical_user_id "
            "WHERE p.lifecycle_status='active' AND e.employee_kind='managed'"
        ).fetchall()
    repaired = 0
    for row in rows:
        profile = _profile_from_row(store, row)
        if profile.get("workspace_relative_path") != LEGACY_EMPLOYEE_WORKSPACE:
            continue
        owner = owner_context_from_owner_key(
            str(row["owner_key"]),
            global_home=store.global_home,
        )
        employee_id = str(row["employee_id"])
        _provision_employee_workspace(owner, employee_id, require_new=False)
        now = time.time()
        with store.write() as conn:
            current = conn.execute(
                "SELECT * FROM employee_profiles WHERE employee_id=? "
                "AND lifecycle_status='active'",
                (employee_id,),
            ).fetchone()
            if current is None:
                continue
            current_profile = _profile_from_row(store, current)
            if current_profile.get("workspace_relative_path") != LEGACY_EMPLOYEE_WORKSPACE:
                continue
            source_policy = normalize_employee_source_policy(current_profile)
            replacement = {
                **source_policy,
                "workspace_relative_path": employee_workspace_relative_path(employee_id),
            }
            _, payload, fingerprint = _canonical_profile(replacement)
            revision = int(current["revision"]) + 1
            conn.execute(
                "UPDATE employee_profiles SET lifecycle_status='superseded', updated_at=? "
                "WHERE employee_id=? AND revision=?",
                (now, employee_id, current["revision"]),
            )
            _insert_profile_revision(
                store,
                conn,
                employee_id=employee_id,
                revision=revision,
                profile_payload=payload,
                profile_fingerprint=fingerprint,
                now=now,
            )
        repaired += 1
    return repaired


def _insert_profile_revision(
    store: ChannelIdentityStore,
    conn,
    *,
    employee_id: str,
    revision: int,
    profile_payload: str,
    profile_fingerprint: str,
    now: float,
) -> None:
    ciphertext, key_version = store.crypto.encrypt_text(
        profile_payload,
        table=EMPLOYEE_PROFILE_AAD_TABLE,
        record_id=f"{employee_id}:{revision}",
        field="profile",
    )
    conn.execute(
        """
        INSERT INTO employee_profiles
          (employee_id, revision, profile_ciphertext, profile_key_version,
           profile_fingerprint, lifecycle_status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, 'active', ?, ?)
        """,
        (
            employee_id,
            revision,
            ciphertext,
            key_version,
            profile_fingerprint,
            now,
            now,
        ),
    )


def reject_builtin_employee_mutation(employee: Employee) -> None:
    """Reject a mutation against a resolved protected built-in employee."""
    if employee.protected:
        raise BuiltinEmployeeProtected("built-in assistant employee is protected")


def _reject_builtin_mutation(row) -> None:
    if row["employee_kind"] == "builtin_assistant":
        raise BuiltinEmployeeProtected("built-in assistant employee is protected")


def _owned_employee_row(conn, *, owner: OwnerContext, employee_id: str):
    return conn.execute(
        """
        SELECT e.canonical_user_id, e.employee_kind, e.lifecycle_status
        FROM employees e
        JOIN owner_bindings o ON o.canonical_user_id=e.canonical_user_id
        WHERE e.employee_id=? AND o.owner_key=?
        """,
        (employee_id, owner.owner_key),
    ).fetchone()


def _owned_feishu_binding_row(
    conn,
    *,
    owner: OwnerContext,
    employee_id: str,
    include_revoked: bool = False,
):
    lifecycle_filter = "" if include_revoked else " AND eb.lifecycle_status<>'revoked'"
    return conn.execute(
        """
        SELECT eb.binding_id, eb.connector_account_id, eb.lifecycle_status,
               a.credentials_ciphertext, a.credentials_key_version,
               a.credential_version
        FROM employee_channel_bindings eb
        JOIN employees e ON e.employee_id=eb.employee_id
        JOIN owner_bindings o ON o.canonical_user_id=e.canonical_user_id
        JOIN connector_accounts a ON a.account_id=eb.connector_account_id
                                 AND a.provider=eb.provider
        WHERE eb.employee_id=? AND eb.provider='feishu'
          AND o.owner_key=?
        """
        + lifecycle_filter
        + " ORDER BY eb.created_at DESC LIMIT 1",
        (employee_id, owner.owner_key),
    ).fetchone()


def _binding_from_row(row) -> EmployeeChannelBinding:
    return EmployeeChannelBinding(
        binding_id=row["binding_id"],
        employee_id=row["employee_id"],
        provider=row["provider"],
        connector_account_id=row["connector_account_id"],
        provider_account_id=row["provider_account_id"],
        credential_version=int(row["credential_version"]),
        account_status=row["account_status"],
        lifecycle_status=row["binding_status"],
    )


def _employee_from_row(row) -> Employee:
    binding = None
    if row["binding_id"] is not None:
        binding = _binding_from_row(row)
    is_builtin = row["employee_kind"] == "builtin_assistant"
    return Employee(
        employee_id=row["employee_id"],
        canonical_user_id=row["canonical_user_id"],
        owner_key=row["owner_key"],
        employee_kind=row["employee_kind"],
        lifecycle_status=row["employee_status"],
        profile_revision=(
            int(row["profile_revision"])
            if row["profile_revision"] is not None
            else None
        ),
        profile_fingerprint=row["profile_fingerprint"],
        collaboration_policy=EmployeeCollaborationPolicy(
            employee_id=row["employee_id"],
            may_participate=True if is_builtin else bool(row["may_participate"]),
            may_create_groups=True if is_builtin else bool(row["may_create_groups"]),
            invite_quota=(
                None
                if is_builtin or row["invite_quota"] is None
                else int(row["invite_quota"])
            ),
        ),
        feishu_binding=binding,
    )


_BINDING_SELECT = """
SELECT eb.binding_id, eb.employee_id, eb.provider, eb.connector_account_id,
       eb.lifecycle_status AS binding_status, a.provider_account_id,
       a.credential_version, a.status AS account_status
FROM employee_channel_bindings eb
JOIN employees e ON e.employee_id=eb.employee_id
JOIN owner_bindings o ON o.canonical_user_id=e.canonical_user_id
JOIN connector_accounts a ON a.account_id=eb.connector_account_id
                         AND a.provider=eb.provider
"""

_EMPLOYEE_SELECT = """
SELECT e.employee_id, e.canonical_user_id, e.employee_kind,
       e.lifecycle_status AS employee_status, o.owner_key,
       p.revision AS profile_revision, p.profile_fingerprint,
       COALESCE(cp.may_participate, 1) AS may_participate,
       COALESCE(cp.may_create_groups, 0) AS may_create_groups,
       CASE WHEN cp.employee_id IS NULL THEN 5 ELSE cp.invite_quota END AS invite_quota,
       eb.binding_id, eb.provider, eb.connector_account_id,
       eb.lifecycle_status AS binding_status, a.provider_account_id,
       a.credential_version, a.status AS account_status
FROM employees e
JOIN owner_bindings o ON o.canonical_user_id=e.canonical_user_id
LEFT JOIN employee_profiles p ON p.employee_id=e.employee_id
                             AND p.lifecycle_status='active'
LEFT JOIN employee_collaboration_policies cp ON cp.employee_id=e.employee_id
LEFT JOIN employee_channel_bindings eb ON eb.employee_id=e.employee_id
                                      AND eb.provider='feishu'
                                      AND eb.lifecycle_status<>'revoked'
LEFT JOIN connector_accounts a ON a.account_id=eb.connector_account_id
                              AND a.provider=eb.provider
"""
