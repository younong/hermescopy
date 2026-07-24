"""Resolve trusted external bindings into Owner and channel credentials."""

from __future__ import annotations

from hermes_cli.dashboard_auth.owner_context import OwnerContext, owner_context_from_registry

from .models import ResolvedChannelOwner
from .store import ChannelIdentityStore


def resolve_binding(
    store: ChannelIdentityStore,
    *,
    binding_id: str,
    allow_pending: bool = False,
) -> tuple[OwnerContext, ResolvedChannelOwner]:
    binding_status = "pending" if allow_pending else "active"
    owner_statuses = ("pending", "active") if allow_pending else ("active", "active")
    with store.read() as conn:
        row = conn.execute(
            """
            SELECT b.binding_id, b.peer_ciphertext, b.peer_key_version,
                   e.external_identity_id, e.canonical_user_id,
                   o.auth_provider, o.tenant_id, o.owner_user_id, o.owner_key,
                   a.account_id, a.base_url, a.bot_id_ciphertext, a.bot_id_key_version,
                   a.bot_token_ciphertext, a.bot_token_key_version, a.credential_version
            FROM channel_bindings b
            JOIN external_identities e ON e.external_identity_id=b.external_identity_id
            JOIN canonical_users u ON u.canonical_user_id=e.canonical_user_id
            JOIN owner_bindings o ON o.canonical_user_id=u.canonical_user_id
            JOIN ilink_accounts a ON a.account_id=b.account_id
            WHERE b.binding_id=? AND b.status=? AND e.status='active'
              AND u.status IN (?, ?) AND a.status=?
            """,
            (binding_id, binding_status, *owner_statuses, binding_status),
        ).fetchone()
    if row is None:
        raise RuntimeError("channel binding is unavailable")
    owner = owner_context_from_registry(
        auth_provider=row["auth_provider"],
        tenant_id=row["tenant_id"],
        canonical_user_id=row["owner_user_id"],
        expected_owner_key=row["owner_key"],
    )
    bot_id = store.crypto.decrypt_text(
        row["bot_id_ciphertext"],
        table="ilink_accounts",
        record_id=row["account_id"],
        field="bot_id",
        version=row["bot_id_key_version"],
    )
    bot_token = store.crypto.decrypt_text(
        row["bot_token_ciphertext"],
        table="ilink_accounts",
        record_id=row["account_id"],
        field="bot_token",
        version=row["bot_token_key_version"],
    )
    peer_id = store.crypto.decrypt_text(
        row["peer_ciphertext"],
        table="channel_bindings",
        record_id=row["binding_id"],
        field="peer",
        version=row["peer_key_version"],
    )
    return owner, ResolvedChannelOwner(
        canonical_user_id=row["canonical_user_id"],
        owner_key=row["owner_key"],
        external_identity_id=row["external_identity_id"],
        account_id=row["account_id"],
        binding_id=row["binding_id"],
        account_base_url=row["base_url"],
        bot_id=bot_id,
        bot_token=bot_token,
        peer_id=peer_id,
        credential_version=row["credential_version"],
    )
