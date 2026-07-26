"""Read-only SQLite adapter for authenticated Session Reader queries."""
from __future__ import annotations

import json
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any

from hermes_state import SessionDB


def _delegate_from_json(column: str = "model_config") -> str:
    return f"json_extract(COALESCE({column}, '{{}}'), '$._delegate_from')"


def _cwd_prefix_clause(cwd_prefix: str) -> tuple[str, list[str]]:
    prefix = cwd_prefix.rstrip("/\\") or cwd_prefix
    return "(s.cwd = ? OR s.cwd LIKE ? OR s.cwd LIKE ?)", [
        prefix,
        f"{prefix}/%",
        f"{prefix}\\%",
    ]


_BRANCH_CHILD_SQL = (
    "json_extract(COALESCE({a}.model_config, '{{}}'), '$._branched_from') IS NOT NULL"
    " OR EXISTS (SELECT 1 FROM sessions p"
    "            WHERE p.id = {a}.parent_session_id"
    "            AND p.end_reason = 'branched'"
    "            AND {a}.started_at >= p.ended_at)"
)
_LISTABLE_CHILD_SQL = (
    f"(s.parent_session_id IS NULL OR {_BRANCH_CHILD_SQL.format(a='s')})"
)
_CONTENT_JSON_PREFIX = "\x00json:"
_IMAGE_EXT_RE = re.compile(
    r"(?i)\.(?:png|jpe?g|gif|webp|bmp|svg|heic|heif)(?:[?#][^\s)]*)?$"
)
_MARKDOWN_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_DATA_IMAGE_RE = re.compile(r"(?i)data:image/[\w.+-]+;base64,[^\s)]+")
_URL_RE = re.compile(r"(?i)\b(?:https?|file)://[^\s)]+")
_ABS_IMAGE_PATH_RE = re.compile(
    r"(?i)(?<!\S)(?:/[\w .@%+=:,~\-]+)+\.(?:png|jpe?g|gif|webp|bmp|svg|heic|heif)(?:[?#][^\s)]*)?"
)


class ReadOnlySessionDB:
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

    _decode_content = SessionDB._decode_content
    _is_duplicate_replayed_user_message = staticmethod(
        SessionDB._is_duplicate_replayed_user_message
    )
    _encode_conversation_page_cursor = SessionDB._encode_conversation_page_cursor
    _decode_conversation_page_cursor = SessionDB._decode_conversation_page_cursor
    _conversation_lineage_fingerprint = staticmethod(
        SessionDB._conversation_lineage_fingerprint
    )
    _conversation_message_from_row = SessionDB._conversation_message_from_row
    _sanitize_conversation_page_message = SessionDB._sanitize_conversation_page_message
    _bound_conversation_page_messages = SessionDB._bound_conversation_page_messages
    _sanitize_fts5_query = staticmethod(SessionDB._sanitize_fts5_query)
    _is_cjk_codepoint = staticmethod(SessionDB._is_cjk_codepoint)
    _contains_cjk = staticmethod(SessionDB._contains_cjk)
    _count_cjk = SessionDB._count_cjk
    _CONVERSATION_PAGE_CURSOR_VERSION = SessionDB._CONVERSATION_PAGE_CURSOR_VERSION
    _CONVERSATION_PAGE_DEFAULT_LIMIT = SessionDB._CONVERSATION_PAGE_DEFAULT_LIMIT
    _CONVERSATION_PAGE_MAX_LIMIT = SessionDB._CONVERSATION_PAGE_MAX_LIMIT
    _CONVERSATION_PAGE_CONTEXT_ROWS = SessionDB._CONVERSATION_PAGE_CONTEXT_ROWS
    _CONVERSATION_PAGE_MAX_TEXT_CHARS = SessionDB._CONVERSATION_PAGE_MAX_TEXT_CHARS
    _CONVERSATION_PAGE_MAX_ATTACHMENTS = SessionDB._CONVERSATION_PAGE_MAX_ATTACHMENTS
    _CONVERSATION_PAGE_MAX_SERIALIZED_BYTES = (
        SessionDB._CONVERSATION_PAGE_MAX_SERIALIZED_BYTES
    )

    @staticmethod
    def _recovery_scope_clause(
        recovery_scope: dict[str, Any] | None,
        *,
        alias: str = "sessions",
    ) -> tuple[str, list[Any]]:
        if not recovery_scope:
            return "", []
        required = ("owner_key", "workspace_root")
        if any(recovery_scope.get(key) in (None, "") for key in required):
            return " AND 1 = 0", []
        if recovery_scope.get("historical_resume") is True:
            return (
                f" AND {alias}.owner_key = ? AND {alias}.workspace_root = ? "
                f"AND typeof({alias}.worker_generation) = 'integer' "
                f"AND {alias}.worker_generation > 0",
                [
                    str(recovery_scope["owner_key"]),
                    str(recovery_scope["workspace_root"]),
                ],
            )
        worker_generation = recovery_scope.get("worker_generation")
        if worker_generation in (None, ""):
            return " AND 1 = 0", []
        return (
            f" AND {alias}.owner_key = ? AND {alias}.workspace_root = ? "
            f"AND {alias}.worker_generation = ?",
            [
                str(recovery_scope["owner_key"]),
                str(recovery_scope["workspace_root"]),
                int(worker_generation),
            ],
        )

    @classmethod
    def _looks_like_image_reference(cls, value: str) -> bool:
        lower = value.strip().strip("'\"<>").lower()
        return (
            lower.startswith("data:image/")
            or "/api/fs/read-data-url" in lower
            or "/api/artifacts" in lower
            or bool(_IMAGE_EXT_RE.search(lower))
        )

    @classmethod
    def _strip_image_references(cls, text: str) -> str:
        def replace_image(match: re.Match[str]) -> str:
            return match.group(1).strip() or " "

        def replace_link(match: re.Match[str]) -> str:
            label = match.group(1).strip()
            target = match.group(2).strip().strip("'\"")
            return label or " " if cls._looks_like_image_reference(target) else match.group(0)

        text = _MARKDOWN_IMAGE_RE.sub(replace_image, text)
        text = _MARKDOWN_LINK_RE.sub(replace_link, text)
        text = _DATA_IMAGE_RE.sub(" ", text)
        text = _URL_RE.sub(
            lambda match: " " if cls._looks_like_image_reference(match.group(0)) else match.group(0),
            text,
        )
        return _ABS_IMAGE_PATH_RE.sub(" ", text)

    @classmethod
    def _build_message_preview(cls, content: Any, max_chars: int = 60) -> str:
        decoded = content
        if isinstance(content, str) and content.startswith(_CONTENT_JSON_PREFIX):
            try:
                decoded = json.loads(content[len(_CONTENT_JSON_PREFIX) :])
            except (json.JSONDecodeError, TypeError):
                decoded = content
        if isinstance(decoded, list):
            parts = [
                part.get("text", "")
                for part in decoded
                if isinstance(part, dict) and part.get("type") == "text"
            ]
            preview = " ".join(part for part in parts if part).strip() or "[multimodal content]"
        elif isinstance(decoded, str):
            preview = "" if decoded.startswith(_CONTENT_JSON_PREFIX) else cls._strip_image_references(decoded)
        else:
            preview = ""
        preview = " ".join(preview.split())
        return preview[:max_chars] + "..." if len(preview) > max_chars else preview

    def _content_for_preview(self, session_id: str, raw: Any) -> Any:
        if isinstance(raw, str) and (not raw or raw.startswith(_CONTENT_JSON_PREFIX)):
            with self._lock:
                row = self._conn.execute(
                    "SELECT content FROM messages "
                    "WHERE session_id = ? AND role = 'user' AND content IS NOT NULL "
                    "ORDER BY timestamp, id LIMIT 1",
                    (session_id,),
                ).fetchone()
            if row:
                return row["content"]
        return raw

    def _where(
        self,
        *,
        source: str | None,
        exclude_sources: list[str] | None,
        cwd_prefix: str | None,
        include_children: bool,
        min_message_count: int,
        include_archived: bool,
        archived_only: bool,
        recovery_scope: dict[str, Any] | None,
    ) -> tuple[str, list[Any], str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        scope_clause, scope_params = self._recovery_scope_clause(recovery_scope, alias="s")
        if scope_clause:
            clauses.append(scope_clause.removeprefix(" AND "))
            params.extend(scope_params)
        if not include_children:
            clauses.extend((_LISTABLE_CHILD_SQL, f"{_delegate_from_json('s.model_config')} IS NULL"))
        if source:
            clauses.append("s.source = ?")
            params.append(source)
        if exclude_sources:
            clauses.append(f"s.source NOT IN ({','.join('?' for _ in exclude_sources)})")
            params.extend(exclude_sources)
        if cwd_prefix:
            clause, values = _cwd_prefix_clause(cwd_prefix)
            clauses.append(clause)
            params.extend(values)
        if min_message_count > 0:
            clauses.append("s.message_count >= ?")
            params.append(min_message_count)
        if archived_only:
            clauses.append("s.archived = 1")
        elif not include_archived:
            clauses.append("s.archived = 0")
        return (
            f"WHERE {' AND '.join(clauses)}" if clauses else "",
            params,
            scope_clause,
            scope_params,
        )

    def list_sessions_rich(
        self,
        source: str | None = None,
        exclude_sources: list[str] | None = None,
        cwd_prefix: str | None = None,
        limit: int = 20,
        offset: int = 0,
        include_children: bool = False,
        min_message_count: int = 0,
        project_compression_tips: bool = True,
        order_by_last_active: bool = False,
        include_archived: bool = False,
        archived_only: bool = False,
        id_query: str | None = None,
        recovery_scope: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        where_sql, params, scope_clause, scope_params = self._where(
            source=source,
            exclude_sources=exclude_sources,
            cwd_prefix=cwd_prefix,
            include_children=include_children,
            min_message_count=min_message_count,
            include_archived=include_archived,
            archived_only=archived_only,
            recovery_scope=recovery_scope,
        )
        if id_query:
            escaped = (
                str(id_query).replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            clause = (
                "(LOWER(s.id) LIKE ? ESCAPE '\\' OR EXISTS ("
                "WITH RECURSIVE id_chain(id) AS ("
                "SELECT s.id UNION ALL SELECT child.id FROM sessions child "
                "JOIN id_chain parent ON child.parent_session_id = parent.id "
                "WHERE child.id != parent.id) "
                "SELECT 1 FROM id_chain WHERE LOWER(id) LIKE ? ESCAPE '\\'))"
            )
            where_sql += (" AND " if where_sql else "WHERE ") + clause
            params.extend((f"%{escaped.lower()}%", f"%{escaped.lower()}%"))
        if order_by_last_active:
            chain_child_scope = scope_clause.replace("s.", "child.")
            query = f"""
                WITH RECURSIVE chain(root_id, cur_id) AS (
                    SELECT s.id, s.id FROM sessions s {where_sql}
                    UNION ALL
                    SELECT c.root_id, child.id
                    FROM chain c
                    JOIN sessions parent ON parent.id = c.cur_id
                    JOIN sessions child INDEXED BY idx_sessions_parent
                      ON child.parent_session_id = c.cur_id
                    WHERE parent.end_reason = 'compression'
                      AND json_extract(COALESCE(child.model_config, '{{}}'), '$._branched_from') IS NULL
                      AND json_extract(COALESCE(child.model_config, '{{}}'), '$._delegate_from') IS NULL
                      AND COALESCE(child.source, '') != 'tool'
                      {chain_child_scope}
                ),
                chain_max AS (
                    SELECT root_id,
                        MAX(COALESCE(
                            (SELECT MAX(m.timestamp) FROM messages m WHERE m.session_id = cur_id),
                            (SELECT started_at FROM sessions ss WHERE ss.id = cur_id)
                        )) AS effective_last_active
                    FROM chain GROUP BY root_id
                )
                SELECT s.*,
                    COALESCE((SELECT SUBSTR(REPLACE(REPLACE(m.content, X'0A', ' '), X'0D', ' '), 1, 63)
                              FROM messages m WHERE m.session_id = s.id AND m.role = 'user'
                                AND m.content IS NOT NULL ORDER BY m.timestamp, m.id LIMIT 1), '') AS _preview_raw,
                    COALESCE((SELECT MAX(m2.timestamp) FROM messages m2 WHERE m2.session_id = s.id),
                             s.started_at) AS last_active,
                    COALESCE(cm.effective_last_active, s.started_at) AS _effective_last_active
                FROM sessions s LEFT JOIN chain_max cm ON cm.root_id = s.id
                {where_sql}
                ORDER BY _effective_last_active DESC, s.started_at DESC, s.id DESC
                LIMIT ? OFFSET ?
            """
            query_params = params + scope_params + params + [limit, offset]
        else:
            query = f"""
                SELECT s.*,
                    COALESCE((SELECT SUBSTR(REPLACE(REPLACE(m.content, X'0A', ' '), X'0D', ' '), 1, 63)
                              FROM messages m WHERE m.session_id = s.id AND m.role = 'user'
                                AND m.content IS NOT NULL ORDER BY m.timestamp, m.id LIMIT 1), '') AS _preview_raw,
                    COALESCE((SELECT MAX(m2.timestamp) FROM messages m2 WHERE m2.session_id = s.id),
                             s.started_at) AS last_active
                FROM sessions s {where_sql}
                ORDER BY s.started_at DESC LIMIT ? OFFSET ?
            """
            query_params = params + [limit, offset]
        with self._lock:
            rows = self._conn.execute(query, query_params).fetchall()
        sessions = []
        for row in rows:
            session = dict(row)
            session["preview"] = self._build_message_preview(
                self._content_for_preview(session["id"], session.pop("_preview_raw", "")), 60
            )
            session.pop("_effective_last_active", None)
            sessions.append(session)
        if project_compression_tips and not include_children:
            sessions = [self._project_compression_tip(session, recovery_scope) for session in sessions]
        return sessions

    def _project_compression_tip(
        self,
        session: dict[str, Any],
        recovery_scope: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if session.get("end_reason") != "compression":
            return session
        tip_id = self.get_compression_tip(session["id"], recovery_scope=recovery_scope)
        if tip_id == session["id"]:
            return session
        tip = self._get_session_rich_row(tip_id, recovery_scope=recovery_scope)
        if not tip:
            return session
        merged = dict(session)
        for key in (
            "id", "ended_at", "end_reason", "message_count", "tool_call_count",
            "title", "last_active", "preview", "model", "system_prompt", "cwd",
            "git_branch", "git_repo_root",
        ):
            if key in tip:
                merged[key] = tip[key]
        merged["_lineage_root_id"] = session["id"]
        return merged

    def get_compression_tip(
        self,
        session_id: str,
        *,
        recovery_scope: dict[str, Any] | None = None,
    ) -> str | None:
        scope_clause, scope_params = self._recovery_scope_clause(recovery_scope, alias="parent")
        lineage_scope = (
            " AND child.owner_key = parent.owner_key AND child.workspace_root = parent.workspace_root "
            + (
                "AND typeof(child.worker_generation) = 'integer' AND child.worker_generation > 0"
                if recovery_scope and recovery_scope.get("historical_resume")
                else "AND child.worker_generation = parent.worker_generation"
            )
            if recovery_scope
            else ""
        )
        current = session_id
        seen = {current} if current else set()
        for _ in range(100):
            with self._lock:
                row = self._conn.execute(
                    f"""
                    SELECT child.id FROM sessions parent
                    JOIN sessions child ON child.parent_session_id = parent.id
                    WHERE parent.id = ? AND parent.end_reason = 'compression'
                      {scope_clause} {lineage_scope}
                      AND json_extract(COALESCE(child.model_config, '{{}}'), '$._branched_from') IS NULL
                      AND json_extract(COALESCE(child.model_config, '{{}}'), '$._delegate_from') IS NULL
                      AND COALESCE(child.source, '') != 'tool'
                    ORDER BY CASE WHEN child.end_reason = 'compression' THEN 0
                                  WHEN child.ended_at IS NULL THEN 1 ELSE 2 END,
                      COALESCE((SELECT MAX(m.timestamp) FROM messages m WHERE m.session_id = child.id),
                               child.started_at) DESC,
                      child.started_at DESC, child.id DESC LIMIT 1
                    """,
                    (current, *scope_params),
                ).fetchone()
            if row is None or not row["id"] or row["id"] in seen:
                return current
            current = row["id"]
            seen.add(current)
        return current

    def _get_session_rich_row(
        self,
        session_id: str,
        *,
        recovery_scope: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        scope_clause, scope_params = self._recovery_scope_clause(recovery_scope, alias="s")
        with self._lock:
            row = self._conn.execute(
                f"""
                SELECT s.*,
                    COALESCE((SELECT SUBSTR(REPLACE(REPLACE(m.content, X'0A', ' '), X'0D', ' '), 1, 63)
                              FROM messages m WHERE m.session_id = s.id AND m.role = 'user'
                                AND m.content IS NOT NULL ORDER BY m.timestamp, m.id LIMIT 1), '') AS _preview_raw,
                    COALESCE((SELECT MAX(m2.timestamp) FROM messages m2 WHERE m2.session_id = s.id),
                             s.started_at) AS last_active
                FROM sessions s WHERE s.id = ?{scope_clause}
                """,
                (session_id, *scope_params),
            ).fetchone()
        if not row:
            return None
        session = dict(row)
        session["preview"] = self._build_message_preview(
            self._content_for_preview(session["id"], session.pop("_preview_raw", "")), 60
        )
        return session

    def session_count(
        self,
        source: str | None = None,
        cwd_prefix: str | None = None,
        min_message_count: int = 0,
        include_archived: bool = False,
        archived_only: bool = False,
        exclude_children: bool = False,
        exclude_sources: list[str] | None = None,
        recovery_scope: dict[str, Any] | None = None,
    ) -> int:
        where_sql, params, _scope_clause, _scope_params = self._where(
            source=source,
            exclude_sources=exclude_sources,
            cwd_prefix=cwd_prefix,
            include_children=not exclude_children,
            min_message_count=min_message_count,
            include_archived=include_archived,
            archived_only=archived_only,
            recovery_scope=recovery_scope,
        )
        with self._lock:
            row = self._conn.execute(
                f"SELECT COUNT(*) FROM sessions s {where_sql}", params
            ).fetchone()
        return int(row[0])

    @staticmethod
    def _strip_background_review_harness(
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        prefixes = (
            "Review the conversation above and update the skill library",
            "Review the conversation above and consider saving to memory",
        )
        result: list[dict[str, Any]] = []
        skip_next_assistant = False
        for message in messages:
            content = message.get("content")
            is_harness = (
                message.get("role") in {"user", "system"}
                and isinstance(content, str)
                and any(content.lstrip().startswith(prefix) for prefix in prefixes)
            )
            if is_harness:
                skip_next_assistant = True
                continue
            if skip_next_assistant:
                skip_next_assistant = False
                if message.get("role") == "assistant":
                    continue
            result.append(message)
        return result

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_session_for_recovery(
        self,
        session_id: str,
        *,
        recovery_scope: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        scope_clause, scope_params = self._recovery_scope_clause(recovery_scope)
        with self._lock:
            row = self._conn.execute(
                f"SELECT * FROM sessions WHERE id = ?{scope_clause}",
                (session_id, *scope_params),
            ).fetchone()
        return dict(row) if row else None

    def resolve_session_id(
        self,
        session_id_or_prefix: str,
        *,
        recovery_scope: dict[str, Any] | None = None,
    ) -> str | None:
        scope_clause, scope_params = self._recovery_scope_clause(recovery_scope)
        with self._lock:
            exact = self._conn.execute(
                f"SELECT id FROM sessions WHERE id = ?{scope_clause}",
                (session_id_or_prefix, *scope_params),
            ).fetchone()
        if exact:
            return str(exact["id"])
        escaped = (
            session_id_or_prefix.replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )
        with self._lock:
            rows = self._conn.execute(
                f"SELECT id FROM sessions WHERE id LIKE ? ESCAPE '\\'{scope_clause} "
                "ORDER BY started_at DESC LIMIT 2",
                (f"{escaped}%", *scope_params),
            ).fetchall()
        return str(rows[0]["id"]) if len(rows) == 1 else None

    def resolve_resume_session_id(
        self,
        session_id: str,
        *,
        recovery_scope: dict[str, Any] | None = None,
    ) -> str:
        """Redirect a resume target to the descendant session that holds the messages.

        Context compression ends the current session and forks a new child session
        (linked via ``parent_session_id``). The flush cursor is reset, so the
        child is where new messages actually land — the parent ends up with
        ``message_count = 0`` rows unless messages had already been flushed to
        it before compression. See #15000.

        This helper walks ``parent_session_id`` forward from ``session_id`` and
        returns the descendant in the chain that has the **most recent** messages.
        Unlike the original logic, it does NOT short-circuit when the starting
        session already has messages — a descendant that was created by
        compression may hold the continuation content and should be preferred
        by the WebUI and gateway for ``--resume`` and session loading.

        If no descendant (including the starting session) has any messages,
        the original ``session_id`` is returned unchanged.

        The chain is always walked via the child whose ``started_at`` is
        latest; that matches the single-chain shape that compression creates.
        A depth cap (32) guards against accidental loops in malformed data.
        """
        if not session_id:
            return session_id

        # Follow the compression-continuation chain forward to the live tip
        # FIRST. Auto-compression ends the current session and forks a
        # continuation child, but a long-lived parent keeps its own flushed
        # message rows — so the empty-head walk below never redirects it, and
        # resuming the parent id reloads the pre-compression transcript while
        # the turns generated *after* compression (and their responses) sit in
        # the continuation. ``get_compression_tip`` is lineage-aware: it only
        # follows children whose parent ended with ``end_reason='compression'``
        # (created after the parent was ended), so delegation / branch children
        # never hijack the resume. This is the fix for the desktop "I came back
        # and the reply isn't there" report on large sessions.
        try:
            tip = self.get_compression_tip(session_id, recovery_scope=recovery_scope)
        except Exception:
            tip = session_id
        if tip and tip != session_id:
            session_id = tip

        scope_clause, scope_params = self._recovery_scope_clause(recovery_scope)
        lineage_scope_clause = (
            "  AND parent.owner_key = child.owner_key "
            "AND parent.workspace_root = child.workspace_root "
            + (
                "AND typeof(child.worker_generation) = 'integer' "
                "AND child.worker_generation > 0 "
                if recovery_scope.get("historical_resume")
                else "AND parent.worker_generation = child.worker_generation "
            )
            if recovery_scope
            else ""
        )
        with self._lock:
            current = session_id
            seen = {current}
            best = None  # tracks the last (deepest) node with messages

            for _ in range(32):
                # Check if the current node remains in the validated durable scope
                # before any message read. This is deliberately a sessions-table
                # predicate rather than trusting lineage IDs from a previous hop.
                try:
                    row = self._conn.execute(
                        f"SELECT 1 FROM sessions WHERE id = ?{scope_clause} "
                        "AND EXISTS (SELECT 1 FROM messages WHERE session_id = sessions.id LIMIT 1)",
                        (current, *scope_params),
                    ).fetchone()
                except Exception:
                    return session_id
                if row is not None:
                    best = current

                # Walk to the most-recently-started child — but skip explicit
                # branch (`_branched_from`), delegate/subagent (`_delegate_from`),
                # and tool children. They also carry a ``parent_session_id`` yet
                # are NOT compression continuations; following them would hijack
                # the resume target to an unrelated session (e.g. a subagent
                # run). This mirrors the child-exclusion in ``get_compression_tip``.
                try:
                    child_row = self._conn.execute(
                        f"SELECT child.id FROM sessions child "
                        "JOIN sessions parent ON parent.id = child.parent_session_id "
                        "WHERE child.parent_session_id = ? "
                        f"  {scope_clause.replace('sessions.', 'child.')} "
                        f"{lineage_scope_clause}"
                        "  AND json_extract(COALESCE(child.model_config, '{}'), '$._branched_from') IS NULL "
                        "  AND json_extract(COALESCE(child.model_config, '{}'), '$._delegate_from') IS NULL "
                        "  AND COALESCE(child.source, '') != 'tool' "
                        "ORDER BY child.started_at DESC, child.id DESC LIMIT 1",
                        (current, *scope_params),
                    ).fetchone()
                except Exception:
                    return session_id
                if child_row is None:
                    break
                child_id = child_row["id"] if hasattr(child_row, "keys") else child_row[0]
                if not child_id or child_id in seen:
                    break
                seen.add(child_id)
                current = child_id

            return best if best is not None else session_id

    _CONVERSATION_PAGE_CURSOR_VERSION = 2
    _CONVERSATION_PAGE_DEFAULT_LIMIT = 100
    _CONVERSATION_PAGE_MAX_LIMIT = 200
    _CONVERSATION_PAGE_CONTEXT_ROWS = 32
    _CONVERSATION_PAGE_MAX_TEXT_CHARS = 120_000
    _CONVERSATION_PAGE_MAX_ATTACHMENTS = 64
    _CONVERSATION_PAGE_MAX_SERIALIZED_BYTES = 2 * 1024 * 1024

    def get_conversation_page(
        self,
        session_id: str,
        *,
        before_cursor: str | None = None,
        limit: int = _CONVERSATION_PAGE_DEFAULT_LIMIT,
        include_ancestors: bool = True,
        recovery_scope: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return canonical user-visible history in chronological order.

        Display history includes live canonical turns plus turns archived by
        in-place compaction, while excluding model-only compaction projections
        and user-rewound rows. Model replay uses :meth:`get_messages_as_conversation`
        and remains active-only.

        The opaque cursor binds the canonical tip, its root-to-tip lineage, and
        the maximum row id visible to the first request. It is not an
        authorization credential: every page rebuilds the lineage under
        ``recovery_scope`` before reading message rows.
        """
        try:
            safe_limit = int(limit)
        except (TypeError, ValueError):
            safe_limit = self._CONVERSATION_PAGE_DEFAULT_LIMIT
        safe_limit = max(1, min(safe_limit, self._CONVERSATION_PAGE_MAX_LIMIT))

        session_ids = [session_id]
        if include_ancestors:
            session_ids = self._session_lineage_root_to_tip(
                session_id, recovery_scope=recovery_scope
            )
        scope_clause, scope_params = self._recovery_scope_clause(
            recovery_scope, alias="s"
        )
        placeholders = ",".join("?" for _ in session_ids)
        lineage_fingerprint = self._conversation_lineage_fingerprint(session_ids)
        display_predicate = (
            "m.context_projection = 0 AND (m.active = 1 OR m.compacted = 1)"
        )

        with self._lock:
            tip_row = self._conn.execute(
                f"SELECT 1 FROM sessions s WHERE s.id = ?{scope_clause} LIMIT 1",
                (session_id, *scope_params),
            ).fetchone()
            if tip_row is None:
                raise ValueError("conversation session not found in recovery scope")

            if before_cursor:
                decoded = self._decode_conversation_page_cursor(before_cursor)
                if (
                    decoded["tip"] != session_id
                    or decoded["lineage"] != lineage_fingerprint
                ):
                    raise ValueError("conversation history cursor does not match session")
                snapshot_id = decoded["snapshot"]
                before_id = decoded["before"]
            else:
                snapshot_row = self._conn.execute(
                    "SELECT COALESCE(MAX(m.id), 0) FROM messages m "
                    "JOIN sessions s ON s.id = m.session_id "
                    f"WHERE m.session_id IN ({placeholders}) AND {display_predicate}"
                    f"{scope_clause}",
                    (*session_ids, *scope_params),
                ).fetchone()
                snapshot_id = int(snapshot_row[0] if snapshot_row else 0)
                before_id = snapshot_id + 1

            select_columns = (
                "m.id, m.session_id, m.role, m.content, m.attachments, "
                "m.tool_call_id, m.tool_calls, m.tool_name, m.finish_reason, "
                "m.reasoning, m.reasoning_content, m.reasoning_details, "
                "m.codex_reasoning_items, m.codex_message_items, "
                "m.platform_message_id, m.observed, m.timestamp"
            )
            rows_desc = self._conn.execute(
                f"SELECT {select_columns} FROM messages m "
                "JOIN sessions s ON s.id = m.session_id "
                f"WHERE m.session_id IN ({placeholders}) AND {display_predicate} "
                "AND m.id <= ? AND m.id < ?"
                f"{scope_clause} ORDER BY m.id DESC LIMIT ?",
                (
                    *session_ids,
                    snapshot_id,
                    before_id,
                    *scope_params,
                    safe_limit + 1,
                ),
            ).fetchall()
            page_rows_desc = rows_desc[:safe_limit]
            page_rows = list(reversed(page_rows_desc))
            oldest_id = int(page_rows[0]["id"]) if page_rows else before_id
            context_rows = self._conn.execute(
                f"SELECT {select_columns} FROM messages m "
                "JOIN sessions s ON s.id = m.session_id "
                f"WHERE m.session_id IN ({placeholders}) AND {display_predicate} "
                "AND m.id <= ? AND m.id < ?"
                f"{scope_clause} ORDER BY m.id DESC LIMIT ?",
                (
                    *session_ids,
                    snapshot_id,
                    oldest_id,
                    *scope_params,
                    self._CONVERSATION_PAGE_CONTEXT_ROWS,
                ),
            ).fetchall()

        context_rows = list(reversed(context_rows))
        page_ids = {int(row["id"]) for row in page_rows}
        hydrated: List[dict[str, Any]] = []
        for row in [*context_rows, *page_rows]:
            msg = self._conversation_message_from_row(row, include_row_identity=True)
            if include_ancestors and self._is_duplicate_replayed_user_message(hydrated, msg):
                continue
            hydrated.append(msg)
        hydrated = self._strip_background_review_harness(hydrated)
        messages = [msg for msg in hydrated if msg.get("_row_id") in page_ids]

        tool_calls: Dict[str, tuple[str, Any]] = {}
        for msg in hydrated:
            for call in msg.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                function = call.get("function") or {}
                call_id = call.get("id")
                name = function.get("name") if isinstance(function, dict) else None
                if call_id and name:
                    arguments: Any = {}
                    try:
                        arguments = json.loads(function.get("arguments") or "{}")
                    except (json.JSONDecodeError, TypeError):
                        pass
                    tool_calls[str(call_id)] = (str(name), arguments)
        for msg in messages:
            call_id = msg.get("tool_call_id")
            if msg.get("role") == "tool" and call_id in tool_calls:
                msg["_display_tool_name"], msg["_display_tool_args"] = tool_calls[call_id]

        filtered_count = len(page_rows) - len(messages)
        messages, budget_omitted_count = self._bound_conversation_page_messages(messages)
        filtered_count += budget_omitted_count
        raw_has_more = len(rows_desc) > safe_limit
        has_more = raw_has_more or filtered_count > 0
        next_cursor = None
        if has_more and page_rows:
            cursor_before_id = oldest_id
            if budget_omitted_count and messages:
                cursor_before_id = int(messages[0]["_row_id"])
            next_cursor = self._encode_conversation_page_cursor(
                {
                    "v": self._CONVERSATION_PAGE_CURSOR_VERSION,
                    "tip": session_id,
                    "lineage": lineage_fingerprint,
                    "snapshot": snapshot_id,
                    "before": cursor_before_id,
                }
            )
        return {
            "session_id": session_id,
            "messages": messages,
            "next_cursor": next_cursor,
            "has_more": has_more,
            "returned_count": len(messages),
            "raw_count": len(page_rows),
            "filtered_count": filtered_count,
            "snapshot_id": snapshot_id,
        }


    def get_display_messages(
        self,
        session_id: str,
        *,
        include_ancestors: bool = True,
        recovery_scope: dict[str, Any] | None = None,
    ) -> List[dict[str, Any]]:
        """Load the complete canonical transcript intended for user display."""
        messages: List[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            page = self.get_conversation_page(
                session_id,
                before_cursor=cursor,
                limit=self._CONVERSATION_PAGE_MAX_LIMIT,
                include_ancestors=include_ancestors,
                recovery_scope=recovery_scope,
            )
            messages = page["messages"] + messages
            if not page["has_more"]:
                return messages
            cursor = page["next_cursor"]
            if not cursor:
                return messages


    def search_messages(
        self,
        query: str,
        source_filter: list[str] = None,
        exclude_sources: list[str] = None,
        role_filter: list[str] = None,
        limit: int = 20,
        offset: int = 0,
        sort: str = None,
        include_inactive: bool = False,
        recovery_scope: dict[str, Any] | None = None,
    ) -> List[dict[str, Any]]:
        """
        Full-text search across session messages using FTS5.

        Supports FTS5 query syntax:
          - Simple keywords: "docker deployment"
          - Phrases: '"exact phrase"'
          - Boolean: "docker OR kubernetes", "python NOT java"
          - Prefix: "deploy*"

        Returns matching messages with session metadata, content snippet,
        and surrounding context (1 message before and after the match).

        ``sort`` controls temporal ordering:
          - ``None`` (default): FTS5 BM25 relevance only. Time-neutral.
          - ``"newest"``: order by message timestamp DESC, then by rank.
          - ``"oldest"``: order by message timestamp ASC, then by rank.

        The short-CJK LIKE fallback already orders by timestamp DESC and
        ignores ``sort``. The trigram CJK path honours ``sort`` like the main
        FTS5 path.

        Rewound (``active=0``, ``compacted=0``) rows are excluded by default —
        the user took those back. Compaction-archived rows (``active=0``,
        ``compacted=1``) ARE included by default: they were summarized away from
        the live context but remain part of the conversation's record, so the
        pre-compaction transcript stays discoverable after in-place compaction
        (#38763). Pass ``include_inactive=True`` to search every row regardless.
        """
        if not self._fts_enabled:
            return []

        if not query or not query.strip():
            return []

        query = self._sanitize_fts5_query(query)
        if not query:
            return []

        # Normalise sort. Anything not in the allowed set falls back to None
        # (FTS5 rank-only) so callers can pass through user input without
        # validation.
        if isinstance(sort, str):
            sort_norm = sort.strip().lower()
            if sort_norm not in ("newest", "oldest"):
                sort_norm = None
        else:
            sort_norm = None

        # ORDER BY shared across the main FTS5 path and trigram CJK path.
        # With sort set, timestamp is primary and rank is the tiebreaker.
        if sort_norm == "newest":
            order_by_sql = "ORDER BY m.timestamp DESC, rank"
        elif sort_norm == "oldest":
            order_by_sql = "ORDER BY m.timestamp ASC, rank"
        else:
            order_by_sql = "ORDER BY rank"

        # Build WHERE clauses dynamically
        scope_clause, scope_params = self._recovery_scope_clause(
            recovery_scope, alias="s"
        )
        where_clauses = ["messages_fts MATCH ?"]
        params: list = [query]
        if scope_clause:
            where_clauses.append(scope_clause.removeprefix(" AND "))
            params.extend(scope_params)
        if not include_inactive:
            # Live rows (active=1) AND compaction-archived rows (compacted=1)
            # are discoverable; only rewind/undo rows (active=0, compacted=0)
            # are hidden. See archive_and_compact() / #38763.
            where_clauses.append("(m.active = 1 OR m.compacted = 1)")

        if source_filter is not None:
            source_placeholders = ",".join("?" for _ in source_filter)
            where_clauses.append(f"s.source IN ({source_placeholders})")
            params.extend(source_filter)

        if exclude_sources is not None:
            exclude_placeholders = ",".join("?" for _ in exclude_sources)
            where_clauses.append(f"s.source NOT IN ({exclude_placeholders})")
            params.extend(exclude_sources)

        if role_filter:
            role_placeholders = ",".join("?" for _ in role_filter)
            where_clauses.append(f"m.role IN ({role_placeholders})")
            params.extend(role_filter)

        where_sql = " AND ".join(where_clauses)
        params.extend([limit, offset])

        sql = f"""
            SELECT
                m.id,
                m.session_id,
                m.role,
                snippet(messages_fts, 0, '>>>', '<<<', '...', 40) AS snippet,
                m.content,
                m.timestamp,
                m.tool_name,
                s.source,
                s.model,
                s.started_at AS session_started
            FROM messages_fts
            JOIN messages m ON m.id = messages_fts.rowid
            JOIN sessions s ON s.id = m.session_id
            WHERE {where_sql}
            {order_by_sql}
            LIMIT ? OFFSET ?
        """

        # CJK queries bypass the unicode61 FTS5 table.  The default tokenizer
        # splits CJK characters into individual tokens, so "大别山项目" becomes
        # "大 AND 别 AND 山 AND 项 AND 目" — producing false positives and
        # missing exact phrase matches.
        #
        # For queries with 3+ CJK characters, we use the trigram FTS5 table
        # (indexed substring matching with ranking and snippets).  For shorter
        # CJK queries (1-2 chars), trigram can't match (it needs ≥9 UTF-8
        # bytes = 3 CJK chars), so we fall back to LIKE.
        is_cjk = self._contains_cjk(query)
        if is_cjk:
            raw_query = query.strip('"').strip()
            cjk_count = self._count_cjk(raw_query)

            # Per-token CJK length check (#20494): trigram needs >=3 CJK chars
            # per token. A query like "广西 OR 桂林 OR 漓江" has cjk_count=6
            # (>=3) but each individual token is only 2 chars — trigram returns 0.
            # Route to LIKE when any non-operator CJK token is <3 CJK chars.
            _tokens_for_check = [
                t for t in raw_query.split()
                if t.upper() not in {"AND", "OR", "NOT"} and self._contains_cjk(t)
            ]
            _any_short_cjk = any(
                self._count_cjk(t) < 3 for t in _tokens_for_check
            )

            _trigram_succeeded = False
            if cjk_count >= 3 and not _any_short_cjk and self._trigram_available:
                # Trigram FTS5 path — quote each non-operator token to handle
                # FTS5 special chars (%, *, etc.) while preserving boolean
                # operators (AND, OR, NOT) for multi-term queries.
                tokens = raw_query.split()
                parts = []
                for tok in tokens:
                    if tok.upper() in {"AND", "OR", "NOT"}:
                        parts.append(tok)
                    else:
                        parts.append('"' + tok.replace('"', '""') + '"')
                trigram_query = " ".join(parts)
                tri_where = ["messages_fts_trigram MATCH ?"]
                tri_params: list = [trigram_query]
                if scope_clause:
                    tri_where.append(scope_clause.removeprefix(" AND "))
                    tri_params.extend(scope_params)
                if not include_inactive:
                    tri_where.append("(m.active = 1 OR m.compacted = 1)")
                if source_filter is not None:
                    tri_where.append(f"s.source IN ({','.join('?' for _ in source_filter)})")
                    tri_params.extend(source_filter)
                if exclude_sources is not None:
                    tri_where.append(f"s.source NOT IN ({','.join('?' for _ in exclude_sources)})")
                    tri_params.extend(exclude_sources)
                if role_filter:
                    tri_where.append(f"m.role IN ({','.join('?' for _ in role_filter)})")
                    tri_params.extend(role_filter)
                tri_sql = f"""
                    SELECT
                        m.id,
                        m.session_id,
                        m.role,
                        snippet(messages_fts_trigram, 0, '>>>', '<<<', '...', 40) AS snippet,
                        m.content,
                        m.timestamp,
                        m.tool_name,
                        s.source,
                        s.model,
                        s.started_at AS session_started
                    FROM messages_fts_trigram
                    JOIN messages m ON m.id = messages_fts_trigram.rowid
                    JOIN sessions s ON s.id = m.session_id
                    WHERE {' AND '.join(tri_where)}
                    {order_by_sql}
                    LIMIT ? OFFSET ?
                """
                tri_params.extend([limit, offset])
                with self._lock:
                    try:
                        tri_cursor = self._conn.execute(tri_sql, tri_params)
                    except sqlite3.OperationalError:
                        # Trigram query failed at runtime — fall through to LIKE.
                        pass
                    else:
                        matches = [dict(row) for row in tri_cursor.fetchall()]
                        _trigram_succeeded = True
            if not _trigram_succeeded:
                # Short / mixed CJK query, trigram unavailable, or trigram
                # <3 CJK chars. Fall back to LIKE substring search.
                # For multi-token OR queries (e.g. "广西 OR 桂林 OR 漓江"),
                # build one LIKE condition per non-operator token so each term
                # is matched independently (#20494).
                non_op_tokens = [
                    t for t in raw_query.split()
                    if t.upper() not in {"AND", "OR", "NOT"}
                ] or [raw_query]
                token_clauses = []
                like_params: list = []
                for tok in non_op_tokens:
                    esc = tok.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                    token_clauses.append(
                        "(m.content LIKE ? ESCAPE '\\' OR m.tool_name LIKE ? ESCAPE '\\' OR m.tool_calls LIKE ? ESCAPE '\\')"
                    )
                    like_params += [f"%{esc}%", f"%{esc}%", f"%{esc}%"]
                like_where = [f"({' OR '.join(token_clauses)})"]
                if scope_clause:
                    like_where.append(scope_clause.removeprefix(" AND "))
                    like_params.extend(scope_params)
                if source_filter is not None:
                    like_where.append(f"s.source IN ({','.join('?' for _ in source_filter)})")
                    like_params.extend(source_filter)
                if exclude_sources is not None:
                    like_where.append(f"s.source NOT IN ({','.join('?' for _ in exclude_sources)})")
                    like_params.extend(exclude_sources)
                if role_filter:
                    like_where.append(f"m.role IN ({','.join('?' for _ in role_filter)})")
                    like_params.extend(role_filter)
                like_sql = f"""
                    SELECT m.id, m.session_id, m.role,
                           substr(m.content,
                                  max(1, instr(m.content, ?) - 40),
                                  120) AS snippet,
                           m.content, m.timestamp, m.tool_name,
                           s.source, s.model, s.started_at AS session_started
                    FROM messages m
                    JOIN sessions s ON s.id = m.session_id
                    WHERE {' AND '.join(like_where)}
                    ORDER BY m.timestamp DESC
                    LIMIT ? OFFSET ?
                """
                like_params.extend([limit, offset])
                # instr() for snippet uses first search token
                like_params = [non_op_tokens[0]] + like_params
                with self._lock:
                    like_cursor = self._conn.execute(like_sql, like_params)
                    matches = [dict(row) for row in like_cursor.fetchall()]
        else:
            with self._lock:
                try:
                    cursor = self._conn.execute(sql, params)
                except sqlite3.OperationalError:
                    # FTS5 query syntax error despite sanitization — return empty
                    return []
                else:
                    matches = [dict(row) for row in cursor.fetchall()]

        # Add surrounding context (1 message before + after each match).
        # Done outside the lock so we don't hold it across N sequential queries.
        for match in matches:
            try:
                with self._lock:
                    ctx_cursor = self._conn.execute(
                        """WITH target AS (
                               SELECT session_id, timestamp, id
                               FROM messages
                               WHERE id = ?
                           )
                           SELECT role, content
                           FROM (
                               SELECT m.id, m.timestamp, m.role, m.content
                               FROM messages m
                               JOIN target t ON t.session_id = m.session_id
                               WHERE (m.timestamp < t.timestamp)
                                  OR (m.timestamp = t.timestamp AND m.id < t.id)
                               ORDER BY m.timestamp DESC, m.id DESC
                               LIMIT 1
                           )
                           UNION ALL
                           SELECT role, content
                           FROM messages
                           WHERE id = ?
                           UNION ALL
                           SELECT role, content
                           FROM (
                               SELECT m.id, m.timestamp, m.role, m.content
                               FROM messages m
                               JOIN target t ON t.session_id = m.session_id
                               WHERE (m.timestamp > t.timestamp)
                                  OR (m.timestamp = t.timestamp AND m.id > t.id)
                               ORDER BY m.timestamp ASC, m.id ASC
                               LIMIT 1
                           )""",
                        (match["id"], match["id"]),
                    )
                    context_msgs = []
                    for r in ctx_cursor.fetchall():
                        context_msgs.append(
                            {"role": r["role"], "content": self._build_message_preview(r["content"], 200)}
                        )
                match["context"] = context_msgs
            except Exception:
                match["context"] = []

        # Remove full content from result (snippet is enough, saves tokens)
        for match in matches:
            match.pop("content", None)

        return matches


    def search_sessions_by_id(
        self,
        query: str,
        limit: int = 20,
        include_archived: bool = True,
        recovery_scope: dict[str, Any] | None = None,
    ) -> List[dict[str, Any]]:
        """Search surfaced sessions by exact/prefix/substring session id.

        Desktop search uses this alongside FTS message search so users can paste
        a session id from logs, CLI output, or another Hermes surface and jump
        straight to that conversation.  Matching also checks ``_lineage_root_id``
        for projected compression-chain tips, so an old root id still resolves to
        the live continuation row.
        """
        needle = (query or "").strip().lower()
        if not needle or limit <= 0:
            return []

        # SQL-bounded: list_sessions_rich pushes the id LIKE filter into the
        # query (matching the row's own id AND any id in its forward
        # compression chain), so we only materialize matching rows instead of
        # scanning every session. Fetch a small multiple of `limit` so the
        # in-Python exact/prefix/substring ranking below has enough candidates
        # to order, then truncate.
        candidates = self.list_sessions_rich(
            limit=max(limit * 4, limit),
            offset=0,
            include_archived=include_archived,
            order_by_last_active=True,
            id_query=needle,
            recovery_scope=recovery_scope,
        )

        def score(row: dict[str, Any]) -> int:
            ids = [str(row.get("id") or ""), str(row.get("_lineage_root_id") or "")]
            normalized = [value.lower() for value in ids if value]
            if any(value == needle for value in normalized):
                return 0
            if any(value.startswith(needle) for value in normalized):
                return 1
            return 2

        ranked = sorted(
            enumerate(candidates),
            key=lambda item: (score(item[1]), item[0]),
        )
        return [row for _, row in ranked[:limit]]


    def export_session(
        self,
        session_id: str,
        *,
        recovery_scope: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        session = self.get_session_for_recovery(
            session_id, recovery_scope=recovery_scope
        )
        if not session:
            return None
        return {
            **session,
            "messages": self.get_display_messages(
                session_id,
                include_ancestors=True,
                recovery_scope=recovery_scope,
            ),
        }

    def message_count(
        self,
        session_id: str | None = None,
        *,
        recovery_scope: dict[str, Any] | None = None,
    ) -> int:
        scope_clause, scope_params = self._recovery_scope_clause(
            recovery_scope, alias="s"
        )
        with self._lock:
            if session_id:
                row = self._conn.execute(
                    "SELECT COUNT(*) FROM messages m JOIN sessions s ON s.id = m.session_id "
                    f"WHERE m.session_id = ?{scope_clause}",
                    (session_id, *scope_params),
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT COUNT(*) FROM messages m JOIN sessions s ON s.id = m.session_id "
                    f"WHERE 1 = 1{scope_clause}",
                    scope_params,
                ).fetchone()
        return int(row[0] if row else 0)

    def count_empty_sessions(
        self,
        *,
        recovery_scope: dict[str, Any] | None = None,
    ) -> int:
        scope_clause, scope_params = self._recovery_scope_clause(
            recovery_scope, alias="s"
        )
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM sessions s "
                "WHERE s.message_count = 0 AND s.ended_at IS NOT NULL AND s.archived = 0"
                f"{scope_clause}",
                scope_params,
            ).fetchone()
        return int(row[0] if row else 0)

    def _session_lineage_root_to_tip(
        self,
        session_id: str,
        *,
        recovery_scope: dict[str, Any] | None = None,
    ) -> list[str]:
        scope_clause, scope_params = self._recovery_scope_clause(recovery_scope)
        chain: list[str] = []
        current = session_id
        seen: set[str] = set()
        with self._lock:
            for _ in range(100):
                if not current or current in seen:
                    break
                row = self._conn.execute(
                    f"SELECT parent_session_id FROM sessions WHERE id = ?{scope_clause}",
                    (current, *scope_params),
                ).fetchone()
                if row is None:
                    break
                seen.add(current)
                chain.append(current)
                current = row["parent_session_id"]
        return list(reversed(chain)) or [session_id]

    def display_message_count(
        self,
        session_id: str,
        *,
        include_ancestors: bool = True,
        recovery_scope: dict[str, Any] | None = None,
    ) -> int:
        session_ids = (
            self._session_lineage_root_to_tip(session_id, recovery_scope=recovery_scope)
            if include_ancestors
            else [session_id]
        )
        placeholders = ",".join("?" for _ in session_ids)
        scope_clause, scope_params = self._recovery_scope_clause(recovery_scope, alias="s")
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM messages m JOIN sessions s ON s.id = m.session_id "
                f"WHERE m.session_id IN ({placeholders}) AND m.context_projection = 0 "
                "AND (m.active = 1 OR m.compacted = 1)"
                f"{scope_clause}",
                (*session_ids, *scope_params),
            ).fetchone()
        return int(row[0] if row else 0)
