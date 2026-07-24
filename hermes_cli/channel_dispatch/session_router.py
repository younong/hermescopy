"""Map durable channel bindings to Owner Worker gateway sessions."""

from __future__ import annotations

import time

from hermes_cli.channel_identity.store import ChannelIdentityStore


async def open_binding_session(client, store: ChannelIdentityStore, *, binding_id: str) -> tuple[str, str]:
    generation = int(client.handle.worker_generation)
    with store.read() as conn:
        row = conn.execute(
            "SELECT stored_session_id, worker_generation FROM channel_sessions WHERE binding_id=?",
            (binding_id,),
        ).fetchone()
    if row is None:
        result = await client.call(
            "session.create",
            {
                "source": "weixin-ilink",
                "title": "WeChat",
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
    result = await client.call(
        "session.resume",
        {
            "session_id": row["stored_session_id"],
            "source": "weixin-ilink",
        },
    )
    live_id = str(result["session_id"])
    with store.write() as conn:
        conn.execute(
            "UPDATE channel_sessions SET worker_generation=?, updated_at=? WHERE binding_id=? AND owner_key=?",
            (generation, time.time(), binding_id, client.owner.owner_key),
        )
    return live_id, str(row["stored_session_id"])
