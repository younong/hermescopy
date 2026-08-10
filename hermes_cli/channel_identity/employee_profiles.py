"""Owner-scoped managed Feishu accounts and encrypted employee profiles."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections.abc import Mapping
from typing import Any

from hermes_cli.dashboard_auth.owner_context import OwnerContext

from .credentials import decrypt_account_credentials, encrypt_account_credentials
from .models import (
    EmployeeCollaborationPolicy,
    EmployeeProfile,
    ManagedFeishuAccount,
    RegisteredChannel,
)
from .registration import ChannelIdentityOwnershipConflict, ensure_owner_binding
from .store import EMPLOYEE_PROFILE_AAD_TABLE, ChannelIdentityStore

_PROVIDER = "feishu"


def _account_id(value: str) -> str:
    account_id = str(value or "").strip()
    if not account_id:
        raise ValueError("account ID is required")
    return account_id


class EmployeeProfileRevisionConflict(RuntimeError):
    """The requested profile revision is no longer current."""


class FeishuCredentialRevisionConflict(RuntimeError):
    """The requested credential version is no longer current."""


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


def register_managed_feishu_account_for_owner(
    store: ChannelIdentityStore,
    *,
    owner: OwnerContext,
    provider_account_id: str,
    external_subject: str,
    conversation_id: str | None,
    credentials: Mapping[str, Any],
    employee_profile: Mapping[str, Any],
    activate: bool = True,
) -> RegisteredChannel:
    """Register a new Feishu account and immutably assign it to one Owner."""
    provider_account_id = str(provider_account_id or "").strip()
    external_subject = str(external_subject or "").strip()
    conversation_id = str(conversation_id or "").strip()
    if not provider_account_id:
        raise ValueError("provider account ID is required")
    if not external_subject:
        raise ValueError("external subject is required")
    if not isinstance(credentials, Mapping) or not credentials:
        raise ValueError("connector credentials are required")
    normalized_profile, _, _ = _canonical_profile(employee_profile)
    now = time.time()
    requested_status = "active" if activate else "pending"
    subject_hash = store.crypto.lookup_hash(
        f"external-subject:{_PROVIDER}", external_subject
    )
    account_hash = store.crypto.lookup_hash(
        f"provider-account:{_PROVIDER}", provider_account_id
    )
    conversation_hash = (
        store.crypto.lookup_hash(f"conversation:{_PROVIDER}", conversation_id)
        if conversation_id
        else ""
    )

    with store.write() as conn:
        if conn.execute(
            "SELECT 1 FROM connector_accounts WHERE provider=? "
            "AND provider_account_id=? AND account_lookup_hash=?",
            (_PROVIDER, provider_account_id, account_hash),
        ).fetchone() is not None:
            raise RuntimeError("Feishu account is already registered")
        canonical_user_id = ensure_owner_binding(store, owner, conn=conn)
        identity = conn.execute(
            "SELECT external_identity_id, canonical_user_id "
            "FROM external_identities WHERE provider=? AND subject_lookup_hash=?",
            (_PROVIDER, subject_hash),
        ).fetchone()
        if identity is not None and identity["canonical_user_id"] != canonical_user_id:
            raise ChannelIdentityOwnershipConflict(
                "confirmed identity belongs to another Owner"
            )
        if identity is None:
            external_identity_id = f"ei_{uuid.uuid4().hex}"
            subject_ciphertext, subject_version = store.crypto.encrypt_text(
                external_subject,
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
                VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?)
                """,
                (
                    external_identity_id,
                    _PROVIDER,
                    subject_hash,
                    subject_ciphertext,
                    subject_version,
                    canonical_user_id,
                    now,
                    now,
                ),
            )
        else:
            external_identity_id = str(identity["external_identity_id"])

        account_id = f"ca_{uuid.uuid4().hex}"
        credentials_ciphertext, credentials_version = encrypt_account_credentials(
            store,
            account_id=account_id,
            credentials=credentials,
        )
        conn.execute(
            """
            INSERT INTO connector_accounts
              (account_id, provider, provider_account_id, account_lookup_hash,
               credentials_ciphertext, credentials_key_version,
               credential_version, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
            """,
            (
                account_id,
                _PROVIDER,
                provider_account_id,
                account_hash,
                credentials_ciphertext,
                credentials_version,
                requested_status,
                now,
                now,
            ),
        )
        conn.execute(
            """
            INSERT INTO managed_feishu_accounts
              (account_id, canonical_user_id, lifecycle_status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                account_id,
                canonical_user_id,
                "active" if activate else "suspended",
                now,
                now,
            ),
        )
        _insert_profile_revision(
            store,
            conn,
            account_id=account_id,
            revision=1,
            profile=normalized_profile,
            now=now,
        )
        binding_id = ""
        if conversation_id:
            binding_id = f"cb_{uuid.uuid4().hex}"
            conversation_ciphertext, conversation_version = store.crypto.encrypt_text(
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
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    binding_id,
                    external_identity_id,
                    account_id,
                    conversation_hash,
                    conversation_ciphertext,
                    conversation_version,
                    requested_status,
                    now,
                    now,
                ),
            )
    return RegisteredChannel(
        canonical_user_id=canonical_user_id,
        owner_key=owner.owner_key,
        external_identity_id=external_identity_id,
        account_id=account_id,
        binding_id=binding_id,
        created=True,
    )


def claim_existing_feishu_account_for_owner(
    store: ChannelIdentityStore,
    *,
    owner: OwnerContext,
    account_id: str,
    employee_profile: Mapping[str, Any],
) -> ManagedFeishuAccount:
    """Claim one unowned legacy account only when every binding proves one Owner."""
    account_id = _account_id(account_id)
    normalized_profile, _, _ = _canonical_profile(employee_profile)
    now = time.time()
    with store.write() as conn:
        canonical_user_id = ensure_owner_binding(store, owner, conn=conn)
        account = conn.execute(
            "SELECT provider FROM connector_accounts WHERE account_id=?",
            (account_id,),
        ).fetchone()
        if account is None or account["provider"] != _PROVIDER:
            raise RuntimeError("Feishu account is unavailable")
        existing = conn.execute(
            "SELECT canonical_user_id FROM managed_feishu_accounts WHERE account_id=?",
            (account_id,),
        ).fetchone()
        if existing is not None:
            if existing["canonical_user_id"] != canonical_user_id:
                raise ChannelIdentityOwnershipConflict(
                    "managed Feishu account belongs to another Owner"
                )
            raise RuntimeError("Feishu account is already managed")
        owners = conn.execute(
            """
            SELECT DISTINCT e.canonical_user_id
            FROM channel_bindings b
            JOIN external_identities e
              ON e.external_identity_id=b.external_identity_id
            WHERE b.account_id=?
            """,
            (account_id,),
        ).fetchall()
        if len(owners) != 1 or owners[0]["canonical_user_id"] != canonical_user_id:
            raise RuntimeError("Feishu account ownership is ambiguous")
        conn.execute(
            """
            INSERT INTO managed_feishu_accounts
              (account_id, canonical_user_id, lifecycle_status, created_at, updated_at)
            VALUES (?, ?, 'active', ?, ?)
            """,
            (account_id, canonical_user_id, now, now),
        )
        _insert_profile_revision(
            store,
            conn,
            account_id=account_id,
            revision=1,
            profile=normalized_profile,
            now=now,
        )
    return resolve_managed_feishu_account(store, owner=owner, account_id=account_id)


def ensure_managed_feishu_conversation_binding(
    store: ChannelIdentityStore,
    *,
    account_id: str,
    conversation_id: str,
    actor_id: str,
    conversation_kind: str,
) -> RegisteredChannel:
    """Create one real chat binding under the managed account's immutable Owner."""
    account_id = _account_id(account_id)
    conversation_id = str(conversation_id or "").strip()
    actor_id = str(actor_id or "").strip()
    if not conversation_id:
        raise ValueError("conversation ID is required")
    if not actor_id:
        raise ValueError("Feishu actor ID is required")
    if conversation_kind not in {"direct", "group"}:
        raise ValueError("Feishu conversation kind is invalid")
    conversation_hash = store.crypto.lookup_hash(
        f"conversation:{_PROVIDER}", conversation_id
    )
    actor_hash = store.crypto.lookup_hash(
        f"external-subject:{_PROVIDER}", actor_id
    )
    now = time.time()
    with store.write() as conn:
        row = conn.execute(
            """
            SELECT m.canonical_user_id, o.owner_key
            FROM managed_feishu_accounts m
            JOIN connector_accounts a ON a.account_id=m.account_id
                                      AND a.provider='feishu'
                                      AND a.status='active'
            JOIN owner_bindings o ON o.canonical_user_id=m.canonical_user_id
            WHERE m.account_id=? AND m.lifecycle_status='active'
            """,
            (account_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError("managed Feishu account is unavailable")
        existing = conn.execute(
            "SELECT binding_id, external_identity_id FROM channel_bindings "
            "WHERE account_id=? AND peer_lookup_hash=?",
            (account_id, conversation_hash),
        ).fetchone()
        created = existing is None
        if existing is not None:
            binding_id = str(existing["binding_id"])
            external_identity_id = str(existing["external_identity_id"])
        else:
            identity = conn.execute(
                "SELECT external_identity_id, canonical_user_id "
                "FROM external_identities WHERE provider=? AND subject_lookup_hash=?",
                (_PROVIDER, actor_hash),
            ).fetchone()
            if identity is not None and identity["canonical_user_id"] != row["canonical_user_id"]:
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
                    VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?)
                    """,
                    (
                        external_identity_id,
                        _PROVIDER,
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
                    account_id,
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
        account_id=account_id,
        binding_id=binding_id,
        created=created,
    )


def update_employee_profile(
    store: ChannelIdentityStore,
    *,
    owner: OwnerContext,
    account_id: str,
    profile: Mapping[str, Any],
    expected_revision: int,
) -> EmployeeProfile:
    """Create the next encrypted profile revision behind an optimistic fence."""
    account_id = _account_id(account_id)
    try:
        expected_revision = int(expected_revision)
    except (TypeError, ValueError) as exc:
        raise ValueError("expected revision must be an integer") from exc
    if expected_revision < 0:
        raise ValueError("expected revision must not be negative")
    normalized_profile, _, fingerprint = _canonical_profile(profile)
    now = time.time()
    with store.write() as conn:
        row = _owned_account_row(conn, owner=owner, account_id=account_id)
        if row is None or row["lifecycle_status"] != "active":
            raise RuntimeError("managed Feishu account is unavailable")
        current = conn.execute(
            "SELECT revision FROM feishu_employee_profiles "
            "WHERE account_id=? AND lifecycle_status='active'",
            (account_id,),
        ).fetchone()
        current_revision = int(current["revision"]) if current is not None else 0
        if current_revision != expected_revision:
            raise EmployeeProfileRevisionConflict(
                f"employee profile revision changed from {expected_revision} to {current_revision}"
            )
        revision = current_revision + 1
        if current is not None:
            conn.execute(
                "UPDATE feishu_employee_profiles "
                "SET lifecycle_status='superseded', updated_at=? "
                "WHERE account_id=? AND revision=?",
                (now, account_id, current_revision),
            )
        _insert_profile_revision(
            store,
            conn,
            account_id=account_id,
            revision=revision,
            profile=normalized_profile,
            now=now,
        )
    return EmployeeProfile(
        account_id=account_id,
        revision=revision,
        fingerprint=fingerprint,
        lifecycle_status="active",
        profile=normalized_profile,
    )


def resolve_employee_profile(
    store: ChannelIdentityStore,
    *,
    owner: OwnerContext,
    account_id: str,
    revision: int | None = None,
) -> EmployeeProfile:
    """Resolve an Owner-authorized profile, optionally at an exact revision."""
    account_id = _account_id(account_id)
    with store.read() as conn:
        account = _owned_account_row(conn, owner=owner, account_id=account_id)
        if account is None:
            raise RuntimeError("managed Feishu account is unavailable")
        if revision is None:
            row = conn.execute(
                "SELECT * FROM feishu_employee_profiles "
                "WHERE account_id=? AND lifecycle_status='active'",
                (account_id,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM feishu_employee_profiles "
                "WHERE account_id=? AND revision=?",
                (account_id, int(revision)),
            ).fetchone()
    if row is None:
        raise RuntimeError("employee profile is unavailable")
    payload = store.crypto.decrypt_text(
        row["profile_ciphertext"],
        table=EMPLOYEE_PROFILE_AAD_TABLE,
        record_id=f"{account_id}:{row['revision']}",
        field="profile",
        version=row["profile_key_version"],
    )
    try:
        profile = json.loads(payload)
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("employee profile is invalid") from exc
    if not isinstance(profile, dict):
        raise RuntimeError("employee profile is invalid")
    _, _, fingerprint = _canonical_profile(profile)
    if fingerprint != row["profile_fingerprint"]:
        raise RuntimeError("employee profile fingerprint is inconsistent")
    return EmployeeProfile(
        account_id=account_id,
        revision=int(row["revision"]),
        fingerprint=fingerprint,
        lifecycle_status=row["lifecycle_status"],
        profile=profile,
    )


def list_managed_feishu_accounts(
    store: ChannelIdentityStore,
    *,
    owner: OwnerContext,
) -> tuple[ManagedFeishuAccount, ...]:
    """List managed Feishu accounts visible to exactly one Owner."""
    with store.read() as conn:
        rows = conn.execute(
            """
            SELECT a.account_id, a.provider_account_id, a.credential_version,
                   a.status AS account_status, m.canonical_user_id,
                   m.lifecycle_status, o.owner_key, p.revision,
                   p.profile_fingerprint,
                   COALESCE(cp.may_participate, 1) AS may_participate,
                   COALESCE(cp.may_create_groups, 0) AS may_create_groups,
                   CASE WHEN cp.account_id IS NULL THEN 5
                        ELSE cp.invite_quota END AS invite_quota
            FROM managed_feishu_accounts m
            JOIN connector_accounts a ON a.account_id=m.account_id
                                      AND a.provider='feishu'
            JOIN owner_bindings o ON o.canonical_user_id=m.canonical_user_id
            LEFT JOIN feishu_employee_profiles p ON p.account_id=m.account_id
                                               AND p.lifecycle_status='active'
            LEFT JOIN feishu_employee_collaboration_policies cp
              ON cp.account_id=m.account_id
            WHERE o.owner_key=?
            ORDER BY m.created_at, a.account_id
            """,
            (owner.owner_key,),
        ).fetchall()
    return tuple(_managed_account_from_row(row) for row in rows)


def resolve_managed_feishu_account(
    store: ChannelIdentityStore,
    *,
    owner: OwnerContext,
    account_id: str,
) -> ManagedFeishuAccount:
    """Resolve account and current-profile lifecycle metadata for one Owner."""
    account_id = _account_id(account_id)
    with store.read() as conn:
        row = conn.execute(
            """
            SELECT a.account_id, a.provider_account_id, a.credential_version,
                   a.status AS account_status, m.canonical_user_id,
                   m.lifecycle_status, o.owner_key, p.revision,
                   p.profile_fingerprint,
                   COALESCE(cp.may_participate, 1) AS may_participate,
                   COALESCE(cp.may_create_groups, 0) AS may_create_groups,
                   CASE WHEN cp.account_id IS NULL THEN 5
                        ELSE cp.invite_quota END AS invite_quota
            FROM managed_feishu_accounts m
            JOIN connector_accounts a ON a.account_id=m.account_id
                                      AND a.provider='feishu'
            JOIN owner_bindings o ON o.canonical_user_id=m.canonical_user_id
            LEFT JOIN feishu_employee_profiles p ON p.account_id=m.account_id
                                               AND p.lifecycle_status='active'
            LEFT JOIN feishu_employee_collaboration_policies cp
              ON cp.account_id=m.account_id
            WHERE m.account_id=? AND o.owner_key=?
            """,
            (account_id, owner.owner_key),
        ).fetchone()
    if row is None:
        raise RuntimeError("managed Feishu account is unavailable")
    return _managed_account_from_row(row)


def resolve_employee_collaboration_policy(
    store: ChannelIdentityStore,
    *,
    owner: OwnerContext,
    account_id: str,
) -> EmployeeCollaborationPolicy:
    """Resolve collaboration permissions after exact Owner authorization."""
    return resolve_managed_feishu_account(
        store, owner=owner, account_id=account_id
    ).collaboration_policy


def update_employee_collaboration_policy(
    store: ChannelIdentityStore,
    *,
    owner: OwnerContext,
    account_id: str,
    may_participate: bool,
    may_create_groups: bool,
    invite_quota: int | None,
) -> EmployeeCollaborationPolicy:
    """Replace collaboration permissions for one Owner-scoped employee."""
    account_id = _account_id(account_id)
    if not isinstance(may_participate, bool) or not isinstance(may_create_groups, bool):
        raise ValueError("collaboration policy flags must be booleans")
    if invite_quota is not None:
        if isinstance(invite_quota, bool) or not isinstance(invite_quota, int):
            raise ValueError("invite quota must be a non-negative integer or null")
        if invite_quota < 0:
            raise ValueError("invite quota must be a non-negative integer or null")
    now = time.time()
    with store.write() as conn:
        if _owned_account_row(conn, owner=owner, account_id=account_id) is None:
            raise RuntimeError("managed Feishu account is unavailable")
        conn.execute(
            """
            INSERT INTO feishu_employee_collaboration_policies
              (account_id, may_participate, may_create_groups, invite_quota,
               updated_by_owner_key, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(account_id) DO UPDATE SET
                may_participate=excluded.may_participate,
                may_create_groups=excluded.may_create_groups,
                invite_quota=excluded.invite_quota,
                updated_by_owner_key=excluded.updated_by_owner_key,
                updated_at=excluded.updated_at
            """,
            (
                account_id,
                1 if may_participate else 0,
                1 if may_create_groups else 0,
                invite_quota,
                owner.owner_key,
                now,
                now,
            ),
        )
    return EmployeeCollaborationPolicy(
        account_id=account_id,
        may_participate=may_participate,
        may_create_groups=may_create_groups,
        invite_quota=invite_quota,
    )


def resolve_managed_feishu_credentials(
    store: ChannelIdentityStore,
    *,
    owner: OwnerContext,
    account_id: str,
) -> tuple[dict[str, Any], int]:
    """Resolve encrypted credentials after an exact Owner authorization check."""
    account_id = _account_id(account_id)
    with store.read() as conn:
        row = conn.execute(
            """
            SELECT a.credentials_ciphertext, a.credentials_key_version,
                   a.credential_version
            FROM managed_feishu_accounts m
            JOIN connector_accounts a ON a.account_id=m.account_id
                                      AND a.provider='feishu'
            JOIN owner_bindings o ON o.canonical_user_id=m.canonical_user_id
            WHERE m.account_id=? AND o.owner_key=?
            """,
            (account_id, owner.owner_key),
        ).fetchone()
    if row is None:
        raise RuntimeError("managed Feishu account is unavailable")
    return decrypt_account_credentials(
        store,
        account_id=account_id,
        ciphertext=row["credentials_ciphertext"],
        key_version=row["credentials_key_version"],
    ), int(row["credential_version"])


def rotate_managed_feishu_credentials(
    store: ChannelIdentityStore,
    *,
    owner: OwnerContext,
    account_id: str,
    credentials: Mapping[str, Any],
    expected_credential_version: int,
) -> ManagedFeishuAccount:
    """Replace encrypted credentials behind Owner and optimistic-version fences."""
    account_id = _account_id(account_id)
    try:
        expected_credential_version = int(expected_credential_version)
    except (TypeError, ValueError) as exc:
        raise ValueError("expected credential version must be an integer") from exc
    if expected_credential_version < 1:
        raise ValueError("expected credential version must be positive")
    if not isinstance(credentials, Mapping) or not credentials:
        raise ValueError("connector credentials are required")
    ciphertext, key_version = encrypt_account_credentials(
        store,
        account_id=account_id,
        credentials=credentials,
    )
    now = time.time()
    with store.write() as conn:
        row = _owned_account_row(conn, owner=owner, account_id=account_id)
        if row is None or row["lifecycle_status"] == "revoked":
            raise RuntimeError("managed Feishu account is unavailable")
        current = conn.execute(
            "SELECT credential_version FROM connector_accounts WHERE account_id=?",
            (account_id,),
        ).fetchone()
        if current is None:
            raise RuntimeError("managed Feishu account is unavailable")
        current_version = int(current["credential_version"])
        if current_version != expected_credential_version:
            raise FeishuCredentialRevisionConflict(
                "Feishu credential version changed from "
                f"{expected_credential_version} to {current_version}"
            )
        conn.execute(
            """
            UPDATE connector_accounts
            SET credentials_ciphertext=?, credentials_key_version=?,
                credential_version=credential_version+1, updated_at=?
            WHERE account_id=?
            """,
            (ciphertext, key_version, now, account_id),
        )
    return resolve_managed_feishu_account(store, owner=owner, account_id=account_id)


def rollover_managed_feishu_sessions(
    store: ChannelIdentityStore,
    *,
    owner: OwnerContext,
    account_id: str,
) -> int:
    """Retire only idle channel-session mappings for one managed account."""
    account_id = _account_id(account_id)
    with store.write() as conn:
        row = _owned_account_row(conn, owner=owner, account_id=account_id)
        if row is None or row["lifecycle_status"] != "active":
            raise RuntimeError("managed Feishu account is unavailable")
        active = conn.execute(
            """
            SELECT 1 FROM inbound_messages
            WHERE account_id=? AND status IN ('queued', 'processing', 'outbound_pending')
            LIMIT 1
            """,
            (account_id,),
        ).fetchone()
        if active is not None:
            raise RuntimeError("managed Feishu account has active conversations")
        cursor = conn.execute(
            """
            DELETE FROM channel_sessions
            WHERE binding_id IN (
                SELECT binding_id FROM channel_bindings WHERE account_id=?
            )
            """,
            (account_id,),
        )
        return int(cursor.rowcount)


def set_managed_feishu_account_status(
    store: ChannelIdentityStore,
    *,
    owner: OwnerContext,
    account_id: str,
    status: str,
) -> ManagedFeishuAccount:
    """Set managed account lifecycle without changing its immutable Owner."""
    account_id = _account_id(account_id)
    status = str(status or "").strip()
    if not status:
        raise ValueError("managed Feishu account status is required")
    if status not in {"active", "suspended", "revoked"}:
        raise ValueError("managed Feishu account status is invalid")
    now = time.time()
    with store.write() as conn:
        row = _owned_account_row(conn, owner=owner, account_id=account_id)
        if row is None:
            raise RuntimeError("managed Feishu account is unavailable")
        if row["lifecycle_status"] == "revoked" and status != "revoked":
            raise RuntimeError("revoked managed Feishu account cannot be reactivated")
        conn.execute(
            "UPDATE managed_feishu_accounts SET lifecycle_status=?, updated_at=? "
            "WHERE account_id=?",
            (status, now, account_id),
        )
        conn.execute(
            "UPDATE connector_accounts SET status=?, updated_at=? WHERE account_id=?",
            (status, now, account_id),
        )
    return resolve_managed_feishu_account(store, owner=owner, account_id=account_id)


def _managed_account_from_row(row) -> ManagedFeishuAccount:
    return ManagedFeishuAccount(
        account_id=row["account_id"],
        canonical_user_id=row["canonical_user_id"],
        owner_key=row["owner_key"],
        provider_account_id=row["provider_account_id"],
        credential_version=int(row["credential_version"]),
        account_status=row["account_status"],
        lifecycle_status=row["lifecycle_status"],
        profile_revision=int(row["revision"]) if row["revision"] is not None else None,
        profile_fingerprint=row["profile_fingerprint"],
        collaboration_policy=EmployeeCollaborationPolicy(
            account_id=row["account_id"],
            may_participate=bool(row["may_participate"]),
            may_create_groups=bool(row["may_create_groups"]),
            invite_quota=(
                int(row["invite_quota"])
                if row["invite_quota"] is not None
                else None
            ),
        ),
    )


def _insert_profile_revision(
    store: ChannelIdentityStore,
    conn,
    *,
    account_id: str,
    revision: int,
    profile: Mapping[str, Any],
    now: float,
) -> None:
    _, payload, fingerprint = _canonical_profile(profile)
    ciphertext, key_version = store.crypto.encrypt_text(
        payload,
        table=EMPLOYEE_PROFILE_AAD_TABLE,
        record_id=f"{account_id}:{revision}",
        field="profile",
    )
    conn.execute(
        """
        INSERT INTO feishu_employee_profiles
          (account_id, revision, profile_ciphertext, profile_key_version,
           profile_fingerprint, lifecycle_status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, 'active', ?, ?)
        """,
        (account_id, revision, ciphertext, key_version, fingerprint, now, now),
    )


def _owned_account_row(conn, *, owner: OwnerContext, account_id: str):
    return conn.execute(
        """
        SELECT m.canonical_user_id, m.lifecycle_status
        FROM managed_feishu_accounts m
        JOIN owner_bindings o ON o.canonical_user_id=m.canonical_user_id
        JOIN connector_accounts a ON a.account_id=m.account_id
                                 AND a.provider='feishu'
        WHERE m.account_id=? AND o.owner_key=?
        """,
        (account_id, owner.owner_key),
    ).fetchone()
