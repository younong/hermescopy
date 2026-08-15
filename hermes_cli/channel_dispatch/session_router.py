"""Map durable channel bindings to Owner Worker gateway sessions."""

from __future__ import annotations

import time

from hermes_cli.channel_identity import resolve_employee_profile
from hermes_cli.channel_identity.store import ChannelIdentityStore
from hermes_cli.employee_policy import normalize_employee_source_policy

# Bound on create→fence rotation rounds: a lost race re-reads and resumes (or
# re-rotates against) the winner's mapping; more than one retry means something
# other than ordinary concurrency is wrong.
_MAX_OPEN_ATTEMPTS = 2


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
    """Open (or resume) the gateway session for one channel binding scope.

    Profile revision drift — an employee profile update landed after this
    scope's mapping was written — ROTATES the mapping to a fresh session
    created from the inbound revision instead of failing the message. The
    superseded stored session is left intact for audit and is reclaimed by the
    gateway's detached-session cap once nothing references it.
    """
    generation = int(client.handle.worker_generation)
    if client.owner is None:
        raise RuntimeError("channel session requires a resolved Owner")
    owner_key = client.owner.owner_key
    exact_scope = str(dispatch_scope or "")

    def _create_params() -> dict:
        params = {
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
                    (binding_id, owner_key),
                ).fetchone()
            if account is None:
                raise RuntimeError("employee Feishu binding is unavailable")
            expected_peer = store.crypto.lookup_hash("conversation:feishu", exact_conversation)
            if account["peer_lookup_hash"] != expected_peer:
                raise RuntimeError("Feishu conversation does not match binding")
            # The inbound revision was stamped at enqueue, so a mismatch means
            # the inbound carries the newer profile; resolving that exact
            # revision is fingerprint-verified by resolve_employee_profile.
            employee = resolve_employee_profile(
                store,
                owner=client.owner,
                employee_id=str(account["employee_id"]),
                revision=profile_revision,
            )
            params["employee_policy"] = {
                "employee_id": employee.employee_id,
                "profile_revision": employee.revision,
                "profile_fingerprint": employee.fingerprint,
                "source_policy": normalize_employee_source_policy(employee.profile),
            }
            params["retained_source_context"] = {
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
        return params

    for _attempt in range(_MAX_OPEN_ATTEMPTS):
        with store.read() as conn:
            row = conn.execute(
                "SELECT owner_key, stored_session_id, worker_generation, profile_revision "
                "FROM channel_sessions WHERE binding_id=? AND dispatch_scope=?",
                (binding_id, exact_scope),
            ).fetchone()
        if row is None:
            result = await client.call("session.create", _create_params())
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
                        owner_key,
                        stored_id,
                        generation,
                        profile_revision,
                        time.time(),
                    ),
                )
            return live_id, stored_id
        if row["owner_key"] != owner_key:
            raise RuntimeError("channel session Owner does not match binding")
        if row["profile_revision"] != profile_revision:
            if source != "feishu":
                raise RuntimeError(
                    "channel session profile revision does not match inbound scope"
                )
            # Rotate: create the successor session from the inbound (newer)
            # profile revision, then fence the mapping swap on the revision we
            # read so a concurrent rotation or manual rollover can't be
            # clobbered.
            result = await client.call("session.create", _create_params())
            new_live_id = str(result["session_id"])
            new_stored_id = str(result["stored_session_id"])
            with store.write() as conn:
                rotated = conn.execute(
                    """
                    UPDATE channel_sessions
                       SET stored_session_id=?, worker_generation=?,
                           profile_revision=?, updated_at=?
                     WHERE binding_id=? AND dispatch_scope=? AND owner_key=?
                       AND profile_revision=?
                    """,
                    (
                        new_stored_id,
                        generation,
                        profile_revision,
                        time.time(),
                        binding_id,
                        exact_scope,
                        owner_key,
                        row["profile_revision"],
                    ),
                ).rowcount
            if rotated == 1:
                return new_live_id, new_stored_id
            # Lost the race: another open rotated first, or a rollover deleted
            # the row. Close the duplicate live session (best-effort — an
            # orphaned detached session is trimmed by the gateway's session
            # cap) and loop to resume/rotate against the winning mapping.
            try:
                await client.call("session.close", {"session_id": new_live_id})
            except Exception:
                pass
            continue
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
                (generation, time.time(), binding_id, exact_scope, owner_key),
            )
        return live_id, str(row["stored_session_id"])
    raise RuntimeError("channel session profile revision does not match inbound scope")
