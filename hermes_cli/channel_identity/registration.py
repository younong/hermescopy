"""Idempotent registration of external identities and immutable owners."""

from __future__ import annotations

import hmac
import time
import uuid
from collections.abc import Mapping
from typing import Any

from hermes_cli.dashboard_auth.owner_context import OwnerContext, owner_context_from_registry

from .credentials import encrypt_account_credentials
from .models import RegisteredChannel
from .store import ChannelIdentityStore, _builtin_assistant_employee_id

_PROVIDER = "weixin_ilink"
_AUTH_PROVIDER = "channel-weixin-ilink"
_TENANT_ID = "personal:channel-weixin-ilink"


def _required_text(value: str, *, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} is required")
    return text


def _provider_slug(provider: str) -> str:
    value = _required_text(provider, field="provider")
    if any(not (char.isalnum() or char in "_-") for char in value):
        raise ValueError("provider contains unsupported characters")
    return value


class ChannelIdentityOwnershipConflict(RuntimeError):
    """The confirmed external identity belongs to another immutable Owner."""


def ensure_owner_binding(
    store: ChannelIdentityStore,
    owner: OwnerContext,
    *,
    conn=None,
) -> str:
    """Return the random channel-registry identity for one trusted Owner."""
    now = time.time()
    if conn is not None:
        canonical_user_id = _ensure_owner_binding(conn, owner=owner, now=now)
        _ensure_builtin_personalization(store, conn, canonical_user_id, now=now)
        return canonical_user_id
    with store.write() as write_conn:
        canonical_user_id = _ensure_owner_binding(write_conn, owner=owner, now=now)
        _ensure_builtin_personalization(
            store, write_conn, canonical_user_id, now=now
        )
        return canonical_user_id


def _ensure_builtin_personalization(
    store: ChannelIdentityStore, conn, canonical_user_id: str, *, now: float
) -> None:
    from .builtin_assistant import ensure_builtin_assistant_personalization

    ensure_builtin_assistant_personalization(
        store,
        conn,
        employee_id=_builtin_assistant_employee_id(canonical_user_id),
        now=now,
    )


def _ensure_owner_binding(conn, *, owner: OwnerContext, now: float) -> str:
    existing = conn.execute(
        """
        SELECT o.canonical_user_id, o.auth_provider, o.tenant_id,
               o.owner_user_id, o.owner_key, u.status
        FROM owner_bindings o
        JOIN canonical_users u ON u.canonical_user_id=o.canonical_user_id
        WHERE o.owner_key=?
        """,
        (owner.owner_key,),
    ).fetchone()
    if existing is not None:
        _validate_owner_binding(existing, owner=owner)
        if existing["status"] != "active":
            raise RuntimeError("channel owner binding is unavailable")
        _ensure_builtin_assistant(
            conn,
            canonical_user_id=existing["canonical_user_id"],
            now=now,
        )
        return existing["canonical_user_id"]

    canonical_user_id = f"cu_{uuid.uuid4().hex}"
    conn.execute(
        "INSERT INTO canonical_users VALUES (?, 'active', ?, ?)",
        (canonical_user_id, now, now),
    )
    conn.execute(
        "INSERT INTO owner_bindings VALUES (?, ?, ?, ?, ?, ?)",
        (
            canonical_user_id,
            owner.auth_provider,
            owner.tenant_id,
            owner.owner_user_id,
            owner.owner_key,
            now,
        ),
    )
    _ensure_builtin_assistant(conn, canonical_user_id=canonical_user_id, now=now)
    return canonical_user_id


def _ensure_builtin_assistant(conn, *, canonical_user_id: str, now: float) -> str:
    employee_id = _builtin_assistant_employee_id(canonical_user_id)
    conn.execute(
        """
        INSERT INTO employees
          (employee_id, canonical_user_id, employee_kind, lifecycle_status,
           created_at, updated_at)
        VALUES (?, ?, 'builtin_assistant', 'active', ?, ?)
        ON CONFLICT(employee_id) DO NOTHING
        """,
        (employee_id, canonical_user_id, now, now),
    )
    row = conn.execute(
        "SELECT canonical_user_id, employee_kind, lifecycle_status "
        "FROM employees WHERE employee_id=?",
        (employee_id,),
    ).fetchone()
    if (
        row is None
        or row["canonical_user_id"] != canonical_user_id
        or row["employee_kind"] != "builtin_assistant"
        or row["lifecycle_status"] != "active"
    ):
        raise RuntimeError("builtin assistant employee is inconsistent")
    return employee_id


def _validate_owner_binding(row, *, owner: OwnerContext) -> None:
    values = (
        (row["auth_provider"], owner.auth_provider),
        (row["tenant_id"], owner.tenant_id),
        (row["owner_user_id"], owner.owner_user_id),
        (row["owner_key"], owner.owner_key),
    )
    if any(not hmac.compare_digest(str(stored), str(expected)) for stored, expected in values):
        raise RuntimeError("channel owner binding is inconsistent")
    owner_context_from_registry(
        auth_provider=row["auth_provider"],
        tenant_id=row["tenant_id"],
        canonical_user_id=row["owner_user_id"],
        expected_owner_key=row["owner_key"],
        global_home=owner.host_global_home,
    )


def register_connector_binding_for_owner(
    store: ChannelIdentityStore,
    *,
    owner: OwnerContext,
    provider: str,
    provider_account_id: str,
    external_subject: str,
    conversation_id: str,
    credentials: Mapping[str, Any],
    activate: bool = True,
) -> RegisteredChannel:
    """Register one verified connector account and immutable Owner conversation."""
    provider = _provider_slug(provider)
    provider_account_id = _required_text(
        provider_account_id, field="provider account ID"
    )
    external_subject = _required_text(
        external_subject, field="external subject"
    )
    conversation_id = _required_text(
        conversation_id, field="conversation ID"
    )
    if not isinstance(credentials, Mapping) or not credentials:
        raise ValueError("connector credentials are required")

    now = time.time()
    requested_status = "active" if activate else "pending"
    subject_hash = store.crypto.lookup_hash(
        f"external-subject:{provider}", external_subject
    )
    account_hash = store.crypto.lookup_hash(
        f"provider-account:{provider}", provider_account_id
    )
    conversation_hash = store.crypto.lookup_hash(
        f"conversation:{provider}", conversation_id
    )

    with store.write() as conn:
        canonical_user_id = ensure_owner_binding(store, owner, conn=conn)
        existing_identity = conn.execute(
            """
            SELECT external_identity_id, canonical_user_id
            FROM external_identities
            WHERE provider=? AND subject_lookup_hash=?
            """,
            (provider, subject_hash),
        ).fetchone()
        if existing_identity is not None:
            if not hmac.compare_digest(
                str(existing_identity["canonical_user_id"]), canonical_user_id
            ):
                raise ChannelIdentityOwnershipConflict(
                    "confirmed identity belongs to another Owner"
                )
            external_identity_id = str(
                existing_identity["external_identity_id"]
            )
        else:
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
                    provider,
                    subject_hash,
                    subject_ciphertext,
                    subject_version,
                    canonical_user_id,
                    now,
                    now,
                ),
            )

        account = conn.execute(
            """
            SELECT a.account_id, a.credential_version,
                   e.canonical_user_id AS employee_canonical_user_id
            FROM connector_accounts a
            LEFT JOIN employee_channel_bindings eb
              ON eb.connector_account_id=a.account_id
             AND eb.lifecycle_status<>'revoked'
            LEFT JOIN employees e ON e.employee_id=eb.employee_id
            WHERE a.provider=? AND a.provider_account_id=?
              AND a.account_lookup_hash=?
            """,
            (provider, provider_account_id, account_hash),
        ).fetchone()
        if account is None:
            account_id = f"ca_{uuid.uuid4().hex}"
            credentials_ciphertext, credentials_version = (
                encrypt_account_credentials(
                    store,
                    account_id=account_id,
                    credentials=credentials,
                )
            )
            conn.execute(
                """
                INSERT INTO connector_accounts
                  (account_id, provider, provider_account_id,
                   account_lookup_hash, credentials_ciphertext,
                   credentials_key_version, credential_version, status,
                   created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                """,
                (
                    account_id,
                    provider,
                    provider_account_id,
                    account_hash,
                    credentials_ciphertext,
                    credentials_version,
                    requested_status,
                    now,
                    now,
                ),
            )
        else:
            account_id = str(account["account_id"])
            employee_owner = account["employee_canonical_user_id"]
            if employee_owner is not None and not hmac.compare_digest(
                str(employee_owner), canonical_user_id
            ):
                raise ChannelIdentityOwnershipConflict(
                    "employee channel account belongs to another Owner"
                )
            existing_binding = conn.execute(
                """
                SELECT e.canonical_user_id
                FROM channel_bindings b
                JOIN external_identities e
                  ON e.external_identity_id=b.external_identity_id
                WHERE b.account_id=? AND b.peer_lookup_hash=?
                """,
                (account_id, conversation_hash),
            ).fetchone()
            if existing_binding is not None and not hmac.compare_digest(
                str(existing_binding["canonical_user_id"]), canonical_user_id
            ):
                raise ChannelIdentityOwnershipConflict(
                    "confirmed conversation belongs to another Owner"
                )
            credentials_ciphertext, credentials_version = (
                encrypt_account_credentials(
                    store,
                    account_id=account_id,
                    credentials=credentials,
                )
            )
            conn.execute(
                """
                UPDATE connector_accounts
                SET credentials_ciphertext=?, credentials_key_version=?,
                    credential_version=credential_version+1,
                    status=CASE WHEN status='active' THEN status ELSE ? END,
                    updated_at=?
                WHERE account_id=?
                """,
                (
                    credentials_ciphertext,
                    credentials_version,
                    requested_status,
                    now,
                    account_id,
                ),
            )

        binding = conn.execute(
            """
            SELECT b.binding_id, e.canonical_user_id, b.external_identity_id
            FROM channel_bindings b
            JOIN external_identities e
              ON e.external_identity_id=b.external_identity_id
            WHERE b.account_id=? AND b.peer_lookup_hash=?
            """,
            (account_id, conversation_hash),
        ).fetchone()
        created = binding is None
        if binding is not None:
            if (
                not hmac.compare_digest(
                    str(binding["canonical_user_id"]), canonical_user_id
                )
                or not hmac.compare_digest(
                    str(binding["external_identity_id"]), external_identity_id
                )
            ):
                raise ChannelIdentityOwnershipConflict(
                    "confirmed conversation belongs to another Owner"
                )
            binding_id = str(binding["binding_id"])
            conn.execute(
                "UPDATE channel_bindings SET status=?, updated_at=? "
                "WHERE binding_id=?",
                (requested_status, now, binding_id),
            )
        else:
            binding_id = f"cb_{uuid.uuid4().hex}"
            conversation_ciphertext, conversation_version = (
                store.crypto.encrypt_text(
                    conversation_id,
                    table="channel_bindings",
                    record_id=binding_id,
                    field="peer",
                )
            )
            conn.execute(
                """
                INSERT INTO channel_bindings
                  (binding_id, external_identity_id, account_id,
                   peer_lookup_hash, peer_ciphertext, peer_key_version,
                   status, created_at, updated_at)
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
            created=created,
        )


def register_weixin_identity(
    store: ChannelIdentityStore,
    *,
    subject: str,
    bot_id: str,
    bot_token: str,
    base_url: str,
    peer_id: str,
    activate: bool = True,
) -> RegisteredChannel:
    """Get or create one external identity and its independent Owner binding."""
    return _register_weixin_identity(
        store,
        target_canonical_user_id=None,
        subject=subject,
        bot_id=bot_id,
        bot_token=bot_token,
        base_url=base_url,
        peer_id=peer_id,
        activate=activate,
    )


def register_weixin_identity_for_owner(
    store: ChannelIdentityStore,
    *,
    target_canonical_user_id: str,
    subject: str,
    bot_id: str,
    bot_token: str,
    base_url: str,
    peer_id: str,
    activate: bool = True,
) -> RegisteredChannel:
    """Bind one confirmed identity to a pre-materialized trusted Owner."""
    target = str(target_canonical_user_id or "").strip()
    if not target:
        raise ValueError("target canonical user is required")
    return _register_weixin_identity(
        store,
        target_canonical_user_id=target,
        subject=subject,
        bot_id=bot_id,
        bot_token=bot_token,
        base_url=base_url,
        peer_id=peer_id,
        activate=activate,
    )


def _register_weixin_identity(
    store: ChannelIdentityStore,
    *,
    target_canonical_user_id: str | None,
    subject: str,
    bot_id: str,
    bot_token: str,
    base_url: str,
    peer_id: str,
    activate: bool,
) -> RegisteredChannel:
    if not all(str(value or "").strip() for value in (subject, bot_id, bot_token, base_url, peer_id)):
        raise ValueError("confirmed iLink identity and credentials must be complete")
    subject_hash = store.crypto.lookup_hash(
        f"external-subject:{_PROVIDER}", subject
    )
    bot_hash = store.crypto.lookup_hash(
        f"provider-account:{_PROVIDER}", bot_id
    )
    peer_hash = store.crypto.lookup_hash(
        f"conversation:{_PROVIDER}", peer_id
    )
    now = time.time()
    requested_status = "active" if activate else "pending"

    # BEGIN IMMEDIATE serializes registration writers before this lookup, so a
    # concurrent waiter rereads the committed winner instead of racing inserts.
    with store.write() as conn:
        if target_canonical_user_id is not None:
            target_owner = conn.execute(
                "SELECT owner_key FROM owner_bindings WHERE canonical_user_id=?",
                (target_canonical_user_id,),
            ).fetchone()
            if target_owner is None:
                raise RuntimeError("target channel owner binding is unavailable")

        existing = conn.execute(
            """
            SELECT e.external_identity_id, e.canonical_user_id, o.owner_key,
                   a.account_id, b.binding_id
            FROM external_identities e
            JOIN owner_bindings o ON o.canonical_user_id=e.canonical_user_id
            JOIN channel_bindings b ON b.external_identity_id=e.external_identity_id
            JOIN connector_accounts a ON a.account_id=b.account_id
                                     AND a.provider=e.provider
            WHERE e.provider=? AND e.subject_lookup_hash=?
            """,
            (_PROVIDER, subject_hash),
        ).fetchone()
        if existing is not None:
            if (
                target_canonical_user_id is not None
                and not hmac.compare_digest(
                    existing["canonical_user_id"], target_canonical_user_id
                )
            ):
                raise ChannelIdentityOwnershipConflict(
                    "confirmed identity belongs to another Owner"
                )
            _validate_existing_registration(
                conn,
                existing=existing,
                bot_hash=bot_hash,
                peer_hash=peer_hash,
            )
            _update_credentials(
                store,
                conn,
                account_id=existing["account_id"],
                binding_id=existing["binding_id"],
                bot_id=bot_id,
                bot_token=bot_token,
                base_url=base_url,
                status=requested_status,
                update_owner_status=target_canonical_user_id is None,
                now=now,
            )
            return RegisteredChannel(
                canonical_user_id=existing["canonical_user_id"],
                owner_key=existing["owner_key"],
                external_identity_id=existing["external_identity_id"],
                account_id=existing["account_id"],
                binding_id=existing["binding_id"],
                created=False,
            )

        canonical_user_id = target_canonical_user_id or f"cu_{uuid.uuid4().hex}"
        external_identity_id = f"ei_{uuid.uuid4().hex}"
        account_id = f"ia_{uuid.uuid4().hex}"
        binding_id = f"cb_{uuid.uuid4().hex}"
        if target_canonical_user_id is None:
            owner = owner_context_from_registry(
                auth_provider=_AUTH_PROVIDER,
                tenant_id=_TENANT_ID,
                canonical_user_id=canonical_user_id,
                global_home=store.global_home,
            )
        else:
            owner_row = conn.execute(
                """
                SELECT auth_provider, tenant_id, owner_user_id, owner_key
                FROM owner_bindings WHERE canonical_user_id=?
                """,
                (canonical_user_id,),
            ).fetchone()
            if owner_row is None:
                raise RuntimeError("target channel owner binding is unavailable")
            owner = owner_context_from_registry(
                auth_provider=owner_row["auth_provider"],
                tenant_id=owner_row["tenant_id"],
                canonical_user_id=owner_row["owner_user_id"],
                expected_owner_key=owner_row["owner_key"],
                global_home=store.global_home,
            )
        subject_ciphertext, subject_version = store.crypto.encrypt_text(
            subject,
            table="external_identities",
            record_id=external_identity_id,
            field="subject",
        )
        credentials_ciphertext, credentials_version = encrypt_account_credentials(
            store,
            account_id=account_id,
            credentials={
                "base_url": base_url.rstrip("/"),
                "bot_id": bot_id,
                "bot_token": bot_token,
            },
        )
        peer_ciphertext, peer_version = store.crypto.encrypt_text(
            peer_id,
            table="channel_bindings",
            record_id=binding_id,
            field="peer",
        )
        if target_canonical_user_id is None:
            conn.execute(
                "INSERT INTO canonical_users VALUES (?, ?, ?, ?)",
                (canonical_user_id, requested_status, now, now),
            )
            conn.execute(
                "INSERT INTO owner_bindings VALUES (?, ?, ?, ?, ?, ?)",
                (
                    canonical_user_id,
                    owner.auth_provider,
                    owner.tenant_id,
                    owner.owner_user_id,
                    owner.owner_key,
                    now,
                ),
            )
            _ensure_builtin_assistant(
                conn, canonical_user_id=canonical_user_id, now=now
            )
        conn.execute(
            """
            INSERT INTO external_identities
              (external_identity_id, provider, subject_lookup_hash, subject_ciphertext,
               subject_key_version, canonical_user_id, status, created_at, updated_at)
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
                bot_hash,
                bot_hash,
                credentials_ciphertext,
                credentials_version,
                requested_status,
                now,
                now,
            ),
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
                peer_hash,
                peer_ciphertext,
                peer_version,
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


def activate_weixin_identity(
    store: ChannelIdentityStore,
    *,
    registered: RegisteredChannel,
) -> None:
    """Activate a provisioned identity after its Owner home is ready."""
    now = time.time()
    with store.write() as conn:
        row = conn.execute(
            """
            SELECT e.canonical_user_id, o.owner_key, b.external_identity_id
            FROM connector_accounts a
            JOIN channel_bindings b ON b.account_id=a.account_id
            JOIN external_identities e ON e.external_identity_id=b.external_identity_id
                                      AND e.provider=a.provider
            JOIN owner_bindings o ON o.canonical_user_id=e.canonical_user_id
            WHERE a.account_id=? AND b.binding_id=?
            """,
            (registered.account_id, registered.binding_id),
        ).fetchone()
        if (
            row is None
            or row["canonical_user_id"] != registered.canonical_user_id
            or row["external_identity_id"] != registered.external_identity_id
            or row["owner_key"] != registered.owner_key
        ):
            raise RuntimeError("pending channel registration changed during provisioning")
        conn.execute(
            """
            UPDATE canonical_users SET status='active', updated_at=?
            WHERE canonical_user_id=? AND status='pending'
            """,
            (now, registered.canonical_user_id),
        )
        conn.execute(
            "UPDATE connector_accounts SET status='active', updated_at=? WHERE account_id=?",
            (now, registered.account_id),
        )
        conn.execute(
            "UPDATE channel_bindings SET status='active', updated_at=? WHERE binding_id=?",
            (now, registered.binding_id),
        )


def _validate_existing_registration(conn, *, existing, bot_hash: str, peer_hash: str) -> None:
    account = conn.execute(
        "SELECT account_lookup_hash FROM connector_accounts WHERE account_id=?",
        (existing["account_id"],),
    ).fetchone()
    binding = conn.execute(
        "SELECT peer_lookup_hash FROM channel_bindings WHERE binding_id=?",
        (existing["binding_id"],),
    ).fetchone()
    if account is None or binding is None:
        raise RuntimeError("existing channel identity is incomplete")
    if account["account_lookup_hash"] != bot_hash or binding["peer_lookup_hash"] != peer_hash:
        raise RuntimeError("confirmed identity conflicts with existing channel binding")


def _update_credentials(
    store: ChannelIdentityStore,
    conn,
    *,
    account_id: str,
    binding_id: str,
    bot_id: str,
    bot_token: str,
    base_url: str,
    status: str,
    update_owner_status: bool,
    now: float,
) -> None:
    credentials_ciphertext, credentials_version = encrypt_account_credentials(
        store,
        account_id=account_id,
        credentials={
            "base_url": base_url.rstrip("/"),
            "bot_id": bot_id,
            "bot_token": bot_token,
        },
    )
    conn.execute(
        """
        UPDATE connector_accounts
        SET credentials_ciphertext=?, credentials_key_version=?,
            credential_version=credential_version+1, status=?, updated_at=?
        WHERE account_id=?
        """,
        (
            credentials_ciphertext,
            credentials_version,
            status,
            now,
            account_id,
        ),
    )
    conn.execute(
        "UPDATE channel_bindings SET status=?, updated_at=? WHERE binding_id=?",
        (status, now, binding_id),
    )
    if update_owner_status:
        conn.execute(
            """
            UPDATE canonical_users SET status=?, updated_at=?
            WHERE canonical_user_id=(
                SELECT e.canonical_user_id
                FROM channel_bindings b
                JOIN external_identities e
                  ON e.external_identity_id=b.external_identity_id
                WHERE b.account_id=? AND b.binding_id=?
            )
            """,
            (status, now, account_id, binding_id),
        )
