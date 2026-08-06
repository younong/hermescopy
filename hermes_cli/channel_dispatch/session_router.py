"""Map durable channel bindings to Owner Worker gateway sessions."""

from __future__ import annotations

import time

from hermes_cli.channel_identity.employee_profiles import resolve_employee_profile
from hermes_cli.channel_identity.store import ChannelIdentityStore
from hermes_cli.employee_policy import normalize_employee_source_policy


async def open_binding_session(
    client,
    store: ChannelIdentityStore,
    *,
    binding_id: str,
    source: str,
    title: str,
    dispatch_scope: str = "",
    profile_revision: int | None = None,
) -> tuple[str, str]:
    generation = int(client.handle.worker_generation)
    if client.owner is None:
        raise RuntimeError("channel session requires a resolved Owner")
    exact_scope = str(dispatch_scope or "")
    with store.read() as conn:
        row = conn.execute(
            "SELECT owner_key, stored_session_id, worker_generation, profile_revision "
            "FROM channel_sessions WHERE binding_id=? AND dispatch_scope=?",
            (binding_id, exact_scope),
        ).fetchone()
    if row is None:
        create_params = {
            "source": source,
            "title": title,
            "close_on_disconnect": False,
        }
        if source == "feishu":
            if profile_revision is None:
                raise RuntimeError("managed Feishu session requires a profile revision")
            with store.read() as conn:
                account = conn.execute(
                    "SELECT b.account_id FROM channel_bindings b "
                    "JOIN managed_feishu_accounts m ON m.account_id=b.account_id "
                    "WHERE b.binding_id=? AND m.lifecycle_status='active'",
                    (binding_id,),
                ).fetchone()
            if account is None:
                raise RuntimeError("managed Feishu binding is unavailable")
            employee = resolve_employee_profile(
                store,
                owner=client.owner,
                account_id=str(account["account_id"]),
                revision=profile_revision,
            )
            create_params["employee_policy"] = {
                "account_id": employee.account_id,
                "profile_revision": employee.revision,
                "profile_fingerprint": employee.fingerprint,
                "source_policy": normalize_employee_source_policy(employee.profile),
            }
        result = await client.call("session.create", create_params)
        live_id = str(result["session_id"])
        stored_id = str(result["stored_session_id"])
        with store.write() as conn:
            conn.execute(
                """
                INSERT INTO channel_sessions
                  (binding_id, dispatch_scope, owner_key, stored_session_id,
                   worker_generation, profile_revision, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    binding_id,
                    exact_scope,
                    client.owner.owner_key,
                    stored_id,
                    generation,
                    profile_revision,
                    time.time(),
                ),
            )
        return live_id, stored_id
    if row["owner_key"] != client.owner.owner_key:
        raise RuntimeError("channel session Owner does not match binding")
    if row["profile_revision"] != profile_revision:
        raise RuntimeError("channel session profile revision does not match inbound scope")
    result = await client.call(
        "session.resume",
        {
            "session_id": row["stored_session_id"],
            "source": source,
        },
    )
    live_id = str(result["session_id"])
    with store.write() as conn:
        conn.execute(
            "UPDATE channel_sessions SET worker_generation=?, updated_at=? "
            "WHERE binding_id=? AND dispatch_scope=? AND owner_key=?",
            (generation, time.time(), binding_id, exact_scope, client.owner.owner_key),
        )
    return live_id, str(row["stored_session_id"])
