"""Map durable channel bindings to Owner Worker gateway sessions."""

from __future__ import annotations

import time

from hermes_cli.channel_identity import resolve_employee_profile
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
    conversation_kind: str | None = None,
    conversation_id: str | None = None,
    thread_id: str = "",
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
            exact_kind = str(conversation_kind or "").strip()
            exact_conversation = str(conversation_id or "").strip()
            exact_thread = str(thread_id or "")
            if exact_kind not in {"direct", "group"} or not exact_conversation:
                raise RuntimeError("verified Feishu conversation metadata is required")
            with store.read() as conn:
                account = conn.execute(
                    "SELECT b.account_id AS connector_account_id, b.peer_lookup_hash, "
                    "eb.employee_id FROM channel_bindings b "
                    "JOIN connector_accounts a ON a.account_id=b.account_id "
                    "JOIN employee_channel_bindings eb "
                    "ON eb.connector_account_id=a.account_id AND eb.provider=a.provider "
                    "JOIN employees e ON e.employee_id=eb.employee_id "
                    "JOIN external_identities x "
                    "ON x.external_identity_id=b.external_identity_id "
                    "AND x.provider=a.provider AND x.canonical_user_id=e.canonical_user_id "
                    "JOIN owner_bindings o ON o.canonical_user_id=e.canonical_user_id "
                    "WHERE b.binding_id=? AND b.status='active' AND a.status='active' "
                    "AND a.provider='feishu' AND eb.lifecycle_status='active' "
                    "AND e.lifecycle_status='active' AND x.status='active' "
                    "AND o.owner_key=?",
                    (binding_id, client.owner.owner_key),
                ).fetchone()
            if account is None:
                raise RuntimeError("employee Feishu binding is unavailable")
            expected_peer = store.crypto.lookup_hash("conversation:feishu", exact_conversation)
            if account["peer_lookup_hash"] != expected_peer:
                raise RuntimeError("Feishu conversation does not match binding")
            employee = resolve_employee_profile(
                store,
                owner=client.owner,
                employee_id=str(account["employee_id"]),
                revision=profile_revision,
            )
            create_params["employee_policy"] = {
                "employee_id": employee.employee_id,
                "profile_revision": employee.revision,
                "profile_fingerprint": employee.fingerprint,
                "source_policy": normalize_employee_source_policy(employee.profile),
            }
            create_params["retained_source_context"] = {
                "provider": "feishu",
                "source_kind": (
                    "feishu_direct" if exact_kind == "direct" else "feishu_group"
                ),
                "employee_id": employee.employee_id,
                "connector_account_id": str(account["connector_account_id"]),
                "binding_id": binding_id,
                "conversation_id": exact_conversation,
                "thread_id": exact_thread,
                "dispatch_scope": exact_scope,
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
