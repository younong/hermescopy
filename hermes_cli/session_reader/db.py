"""Import-light read-only SQLite adapter for Session Reader list queries."""
from __future__ import annotations

import json
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any


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
    """The exact SessionDB read surface needed by ``list_sessions_payload``."""

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

    def close(self) -> None:
        self._conn.close()

    @staticmethod
    def _recovery_scope_clause(
        recovery_scope: dict[str, Any] | None,
        *,
        alias: str = "sessions",
    ) -> tuple[str, list[Any]]:
        if not recovery_scope:
            return "", []
        required = ("owner_key", "workspace_root", "worker_generation")
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
        return (
            f" AND {alias}.owner_key = ? AND {alias}.workspace_root = ? "
            f"AND {alias}.worker_generation = ?",
            [
                str(recovery_scope["owner_key"]),
                str(recovery_scope["workspace_root"]),
                int(recovery_scope["worker_generation"]),
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
