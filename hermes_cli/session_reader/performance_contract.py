"""Deterministic performance contract shared by Reader tests and release smoke."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SessionReaderPerformanceStandards:
    schema_version: int = 1
    visible_sessions: int = 3_000
    page_size: int = 30
    messages_per_session: int = 3
    compression_chain_interval: int = 10
    compression_chain_length: int = 3
    list_sql_max: int = 6
    stats_sql_exact: int = 3
    search_sql_max: int = 7
    local_list_max_ms: float = 300.0
    reader_cold_max_ms: float = 300.0
    reader_warm_max_ms: float = 300.0
    db_pool_size: int = 4
    server_workers: int = 8
    server_in_flight: int = 16
    client_connections: int = 8
    client_keepalive_connections: int = 4

    def payload(self) -> dict[str, int | float]:
        return asdict(self)


STANDARDS = SessionReaderPerformanceStandards()
SEARCH_MARKER = "session-reader-performance-marker"


def expected_latest_session_id(visible_sessions: int = STANDARDS.visible_sessions) -> str:
    return f"session-{visible_sessions - 1}-root"


def populate_large_session_history(
    db: Any,
    *,
    owner_key: str | None = None,
    owner_home: Path | None = None,
    visible_sessions: int = STANDARDS.visible_sessions,
) -> dict[str, int]:
    """Populate deterministic logical histories with periodic compression chains."""
    if (owner_key is None) != (owner_home is None):
        raise ValueError("owner_key and owner_home must be provided together")
    workspace_root = (
        str((Path(owner_home) / "workspaces").resolve())
        if owner_home is not None
        else None
    )
    base = 1_700_000_000.0
    sessions = []
    messages = []
    message_id = 1
    compression_chains = 0
    for index in range(visible_sessions):
        chain_length = (
            STANDARDS.compression_chain_length
            if index % STANDARDS.compression_chain_interval == 0
            else 1
        )
        if chain_length > 1:
            compression_chains += 1
        parent_id = None
        for chain_index in range(chain_length):
            session_id = (
                f"session-{index}-root"
                if chain_index == 0
                else f"session-{index}-tip-{chain_index}"
            )
            started_at = base + index * 10 + chain_index
            compressed = chain_index < chain_length - 1
            sessions.append(
                (
                    session_id,
                    "gui",
                    parent_id,
                    started_at,
                    started_at + 0.5 if compressed else None,
                    "compression" if compressed else None,
                    STANDARDS.messages_per_session,
                    0,
                    owner_key,
                    workspace_root,
                    1 if owner_key is not None else None,
                )
            )
            for message_index in range(STANDARDS.messages_per_session):
                marker = f" {SEARCH_MARKER}" if index < 12 and message_index == 0 else ""
                messages.append(
                    (
                        message_id,
                        session_id,
                        "user" if message_index == 0 else "assistant",
                        f"message {index} {chain_index} {message_index}{marker}",
                        started_at + message_index / 10,
                    )
                )
                message_id += 1
            parent_id = session_id
    db._conn.executemany(
        """INSERT INTO sessions (
               id, source, parent_session_id, started_at, ended_at,
               end_reason, message_count, archived, owner_key,
               workspace_root, worker_generation
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        sessions,
    )
    db._conn.executemany(
        """INSERT INTO messages (id, session_id, role, content, timestamp)
           VALUES (?, ?, ?, ?, ?)""",
        messages,
    )
    db._conn.commit()
    return {
        "visibleSessions": visible_sessions,
        "physicalSessions": len(sessions),
        "messages": len(messages),
        "compressionChains": compression_chains,
    }
