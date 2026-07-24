"""Idempotent registration of external identities and immutable owners."""

from __future__ import annotations

import time
import uuid

from hermes_cli.dashboard_auth.owner_context import owner_context_from_registry

from .models import RegisteredChannel
from .store import ChannelIdentityStore

_PROVIDER = "weixin_ilink"
_AUTH_PROVIDER = "channel-weixin-ilink"
_TENANT_ID = "personal:channel-weixin-ilink"


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
    """Get or create one external identity and its immutable Owner binding."""
    if not all(str(value or "").strip() for value in (subject, bot_id, bot_token, base_url, peer_id)):
        raise ValueError("confirmed iLink identity and credentials must be complete")
    subject_hash = store.crypto.lookup_hash("external-subject", subject)
    bot_hash = store.crypto.lookup_hash("bot-id", bot_id)
    peer_hash = store.crypto.lookup_hash("peer-id", peer_id)
    now = time.time()
    requested_status = "active" if activate else "pending"

    # BEGIN IMMEDIATE serializes registration writers before this lookup, so a
    # concurrent waiter rereads the committed winner instead of racing inserts.
    with store.write() as conn:
        existing = conn.execute(
            """
            SELECT e.external_identity_id, e.canonical_user_id, o.owner_key,
                   a.account_id, b.binding_id
            FROM external_identities e
            JOIN owner_bindings o ON o.canonical_user_id=e.canonical_user_id
            JOIN ilink_accounts a ON a.external_identity_id=e.external_identity_id
            JOIN channel_bindings b ON b.external_identity_id=e.external_identity_id
                                   AND b.account_id=a.account_id
            WHERE e.provider=? AND e.subject_lookup_hash=?
            """,
            (_PROVIDER, subject_hash),
        ).fetchone()
        if existing is not None:
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

        canonical_user_id = f"cu_{uuid.uuid4().hex}"
        external_identity_id = f"ei_{uuid.uuid4().hex}"
        account_id = f"ia_{uuid.uuid4().hex}"
        binding_id = f"cb_{uuid.uuid4().hex}"
        owner = owner_context_from_registry(
            auth_provider=_AUTH_PROVIDER,
            tenant_id=_TENANT_ID,
            canonical_user_id=canonical_user_id,
        )
        subject_ciphertext, subject_version = store.crypto.encrypt_text(
            subject,
            table="external_identities",
            record_id=external_identity_id,
            field="subject",
        )
        bot_id_ciphertext, bot_id_version = store.crypto.encrypt_text(
            bot_id,
            table="ilink_accounts",
            record_id=account_id,
            field="bot_id",
        )
        bot_token_ciphertext, bot_token_version = store.crypto.encrypt_text(
            bot_token,
            table="ilink_accounts",
            record_id=account_id,
            field="bot_token",
        )
        peer_ciphertext, peer_version = store.crypto.encrypt_text(
            peer_id,
            table="channel_bindings",
            record_id=binding_id,
            field="peer",
        )
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
            INSERT INTO ilink_accounts
              (account_id, external_identity_id, bot_id_lookup_hash, bot_id_ciphertext,
               bot_id_key_version, bot_token_ciphertext, bot_token_key_version,
               base_url, credential_version, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
            """,
            (
                account_id,
                external_identity_id,
                bot_hash,
                bot_id_ciphertext,
                bot_id_version,
                bot_token_ciphertext,
                bot_token_version,
                base_url.rstrip("/"),
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
            SELECT e.canonical_user_id, o.owner_key, a.external_identity_id
            FROM ilink_accounts a
            JOIN channel_bindings b ON b.account_id=a.account_id
            JOIN external_identities e ON e.external_identity_id=a.external_identity_id
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
            "UPDATE canonical_users SET status='active', updated_at=? WHERE canonical_user_id=?",
            (now, registered.canonical_user_id),
        )
        conn.execute(
            "UPDATE ilink_accounts SET status='active', updated_at=? WHERE account_id=?",
            (now, registered.account_id),
        )
        conn.execute(
            "UPDATE channel_bindings SET status='active', updated_at=? WHERE binding_id=?",
            (now, registered.binding_id),
        )


def _validate_existing_registration(conn, *, existing, bot_hash: str, peer_hash: str) -> None:
    account = conn.execute(
        "SELECT bot_id_lookup_hash FROM ilink_accounts WHERE account_id=?",
        (existing["account_id"],),
    ).fetchone()
    binding = conn.execute(
        "SELECT peer_lookup_hash FROM channel_bindings WHERE binding_id=?",
        (existing["binding_id"],),
    ).fetchone()
    if account is None or binding is None:
        raise RuntimeError("existing channel identity is incomplete")
    if account["bot_id_lookup_hash"] != bot_hash or binding["peer_lookup_hash"] != peer_hash:
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
    now: float,
) -> None:
    bot_id_ciphertext, bot_id_version = store.crypto.encrypt_text(
        bot_id,
        table="ilink_accounts",
        record_id=account_id,
        field="bot_id",
    )
    token_ciphertext, token_version = store.crypto.encrypt_text(
        bot_token,
        table="ilink_accounts",
        record_id=account_id,
        field="bot_token",
    )
    conn.execute(
        """
        UPDATE ilink_accounts
        SET bot_id_ciphertext=?, bot_id_key_version=?, bot_token_ciphertext=?,
            bot_token_key_version=?, base_url=?, credential_version=credential_version+1,
            status=?, updated_at=?
        WHERE account_id=?
        """,
        (
            bot_id_ciphertext,
            bot_id_version,
            token_ciphertext,
            token_version,
            base_url.rstrip("/"),
            status,
            now,
            account_id,
        ),
    )
    conn.execute(
        "UPDATE channel_bindings SET status=?, updated_at=? WHERE binding_id=?",
        (status, now, binding_id),
    )
    conn.execute(
        """
        UPDATE canonical_users SET status=?, updated_at=?
        WHERE canonical_user_id=(
            SELECT canonical_user_id FROM external_identities
            WHERE external_identity_id=(
                SELECT external_identity_id FROM ilink_accounts WHERE account_id=?
            )
        )
        """,
        (status, now, account_id),
    )
