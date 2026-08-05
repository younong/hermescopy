"""Provider-fenced connector polling and atomic inbox commits."""

from __future__ import annotations

import time
from dataclasses import dataclass

from hermes_cli.channel_identity.owner_resolution import resolve_connector_account
from hermes_cli.channel_identity.store import (
    ACCOUNT_CREDENTIAL_AAD_TABLE,
    ChannelIdentityStore,
)

from .contracts import InboundBatch
from .inbox import CanonicalInbox


class StalePollLeaseError(RuntimeError):
    """The account lease was replaced or its credentials changed."""


@dataclass(frozen=True)
class PollLease:
    provider: str
    account_id: str
    holder: str
    generation: int
    credential_version: int


@dataclass(frozen=True)
class ResolvedPollAccount:
    provider: str
    account_id: str
    provider_account_id: str
    credentials: dict
    credential_version: int
    cursor: str


def acquire_poll_lease(
    store: ChannelIdentityStore,
    *,
    provider: str,
    account_id: str,
    holder: str,
) -> PollLease:
    exact_provider = str(provider or "").strip()
    exact_account = str(account_id or "").strip()
    exact_holder = str(holder or "").strip()
    if not exact_provider or not exact_account or not exact_holder:
        raise ValueError("provider, account_id, and holder are required")
    with store.write() as conn:
        changed = conn.execute(
            """
            UPDATE connector_accounts SET poll_generation=poll_generation+1,
                poll_holder=?, poll_health='starting', updated_at=?
            WHERE account_id=? AND provider=? AND status='active'
            """,
            (exact_holder, time.time(), exact_account, exact_provider),
        ).rowcount
        if changed != 1:
            raise RuntimeError("active connector account not found")
        row = conn.execute(
            "SELECT poll_generation, credential_version FROM connector_accounts "
            "WHERE account_id=? AND provider=?",
            (exact_account, exact_provider),
        ).fetchone()
    return PollLease(
        provider=exact_provider,
        account_id=exact_account,
        holder=exact_holder,
        generation=row["poll_generation"],
        credential_version=row["credential_version"],
    )


def load_poll_account(
    store: ChannelIdentityStore,
    lease: PollLease,
) -> ResolvedPollAccount:
    with store.read() as conn:
        row = conn.execute(
            """
            SELECT cursor_ciphertext, cursor_key_version
            FROM connector_accounts
            WHERE account_id=? AND provider=? AND status='active' AND poll_holder=?
              AND poll_generation=? AND credential_version=?
            """,
            (
                lease.account_id,
                lease.provider,
                lease.holder,
                lease.generation,
                lease.credential_version,
            ),
        ).fetchone()
    if row is None:
        raise StalePollLeaseError("connector poll lease is stale")
    account = resolve_connector_account(
        store,
        provider=lease.provider,
        account_id=lease.account_id,
        credential_version=lease.credential_version,
    )
    cursor = ""
    if row["cursor_ciphertext"] is not None:
        cursor = store.crypto.decrypt_text(
            row["cursor_ciphertext"],
            table=ACCOUNT_CREDENTIAL_AAD_TABLE,
            record_id=lease.account_id,
            field="cursor",
            version=row["cursor_key_version"],
        )
    return ResolvedPollAccount(
        provider=account.provider,
        account_id=account.account_id,
        provider_account_id=account.provider_account_id,
        credentials=account.credentials,
        credential_version=account.credential_version,
        cursor=cursor,
    )


def commit_inbound_batch(
    store: ChannelIdentityStore,
    lease: PollLease,
    *,
    batch: InboundBatch,
) -> int:
    """Commit normalized messages and their provider cursor atomically."""
    now = time.time()
    cursor_ciphertext, cursor_version = store.crypto.encrypt_text(
        batch.cursor,
        table=ACCOUNT_CREDENTIAL_AAD_TABLE,
        record_id=lease.account_id,
        field="cursor",
    )
    inbox = CanonicalInbox(store, provider=lease.provider)
    with store.write() as conn:
        account = conn.execute(
            """
            SELECT 1 FROM connector_accounts
            WHERE account_id=? AND provider=? AND status='active' AND poll_holder=?
              AND poll_generation=? AND credential_version=?
            """,
            (
                lease.account_id,
                lease.provider,
                lease.holder,
                lease.generation,
                lease.credential_version,
            ),
        ).fetchone()
        if account is None:
            raise StalePollLeaseError("connector poll lease is stale")
        inserted = sum(
            inbox.commit(conn, account_id=lease.account_id, envelope=envelope)
            for envelope in batch.messages
        )
        changed = conn.execute(
            """
            UPDATE connector_accounts SET cursor_ciphertext=?, cursor_key_version=?,
                poll_health='healthy', updated_at=?
            WHERE account_id=? AND provider=? AND status='active' AND poll_holder=?
              AND poll_generation=? AND credential_version=?
            """,
            (
                cursor_ciphertext,
                cursor_version,
                now,
                lease.account_id,
                lease.provider,
                lease.holder,
                lease.generation,
                lease.credential_version,
            ),
        ).rowcount
        if changed != 1:
            raise StalePollLeaseError("connector poll lease became stale")
    return inserted
