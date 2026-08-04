"""Map durable channel bindings to Owner Worker gateway sessions."""

from __future__ import annotations

import time

from hermes_cli.channel_identity.store import ChannelIdentityStore


async def open_binding_session(
    client,
    store: ChannelIdentityStore,
    *,
    binding_id: str,
    source: str,
    title: str,
) -> tuple[str, str]:
    generation = int(client.handle.worker_generation)
    if client.owner is None:
        raise RuntimeError("channel session requires a resolved Owner")
    with store.read() as conn:
        row = conn.execute(
            "SELECT owner_key, stored_session_id, worker_generation "
            "FROM channel_sessions WHERE binding_id=?",
            (binding_id,),
        ).fetchone()
    if row is None:
        result = await client.call(
            "session.create",
            {
                "source": source,
                "title": title,
                "close_on_disconnect": False,
            },
        )
        live_id = str(result["session_id"])
        stored_id = str(result["stored_session_id"])
        with store.write() as conn:
            conn.execute(
                "INSERT INTO channel_sessions VALUES (?, ?, ?, ?, ?)",
                (binding_id, client.owner.owner_key, stored_id, generation, time.time()),
            )
        return live_id, stored_id
    if row["owner_key"] != client.owner.owner_key:
        raise RuntimeError("channel session Owner does not match binding")
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
            "UPDATE channel_sessions SET worker_generation=?, updated_at=? WHERE binding_id=? AND owner_key=?",
            (generation, time.time(), binding_id, client.owner.owner_key),
        )
    return live_id, str(row["stored_session_id"])
