"""Read-only SQLite adapter for authenticated Session Reader queries."""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from hermes_session_queries import SessionQueryMixin


class ReadOnlySessionDB(SessionQueryMixin):
    """The exact SessionDB read surface used by authenticated session GETs."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            f"file:{self.db_path}?mode=ro",
            uri=True,
            check_same_thread=False,
            timeout=1.0,
            isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA query_only=ON")
        self._fts_enabled = self._table_exists("messages_fts")
        self._trigram_available = self._table_exists("messages_fts_trigram")

    def _table_exists(self, table: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        return row is not None

    def close(self) -> None:
        self._conn.close()
