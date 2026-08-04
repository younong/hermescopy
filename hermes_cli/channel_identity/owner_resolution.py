"""Resolve trusted external bindings into Owner and channel credentials."""

from __future__ import annotations

from hermes_cli.dashboard_auth.owner_context import OwnerContext, owner_context_from_registry

from .credentials import decrypt_account_credentials
from .models import ResolvedChannelOwner, ResolvedConnectorAccount
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
                   a.provider, a.account_id, a.provider_account_id,
                   a.credential_version
            FROM channel_bindings b
            JOIN external_identities e ON e.external_identity_id=b.external_identity_id
            JOIN canonical_users u ON u.canonical_user_id=e.canonical_user_id
            JOIN owner_bindings o ON o.canonical_user_id=u.canonical_user_id
            JOIN connector_accounts a ON a.account_id=b.account_id
            WHERE b.binding_id=? AND b.status=? AND e.status='active'
              AND u.status IN (?, ?) AND a.status=?
              AND a.provider=e.provider
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
        global_home=store.global_home,
    )
    conversation_id = store.crypto.decrypt_text(
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
        provider=row["provider"],
        account_id=row["account_id"],
        provider_account_id=row["provider_account_id"],
        binding_id=row["binding_id"],
        conversation_id=conversation_id,
        credential_version=row["credential_version"],
    )


def resolve_connector_account(
    store: ChannelIdentityStore,
    *,
    provider: str,
    account_id: str,
    credential_version: int | None = None,
) -> ResolvedConnectorAccount:
    """Resolve one active account through an exact provider/version fence."""
    exact_provider = str(provider or "").strip()
    exact_account = str(account_id or "").strip()
    if not exact_provider or not exact_account:
        raise ValueError("provider and account_id are required")
    with store.read() as conn:
        row = conn.execute(
            """
            SELECT provider, account_id, provider_account_id,
                   credentials_ciphertext, credentials_key_version,
                   credential_version
            FROM connector_accounts
            WHERE provider=? AND account_id=? AND status='active'
            """,
            (exact_provider, exact_account),
        ).fetchone()
    if row is None or (
        credential_version is not None
        and int(row["credential_version"]) != int(credential_version)
    ):
        raise RuntimeError("connector account is unavailable")
    credentials = decrypt_account_credentials(
        store,
        account_id=row["account_id"],
        ciphertext=row["credentials_ciphertext"],
        key_version=row["credentials_key_version"],
    )
    return ResolvedConnectorAccount(
        provider=row["provider"],
        account_id=row["account_id"],
        provider_account_id=row["provider_account_id"],
        credentials=credentials,
        credential_version=row["credential_version"],
    )
