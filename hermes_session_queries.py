"""Shared SQLite read queries for session stores."""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import sqlite3
from typing import Any, Dict, List, Tuple


logger = logging.getLogger(__name__)
MAX_FTS5_QUERY_CHARS = 2_048

_BACKGROUND_REVIEW_HARNESS_PREFIXES = (
    "Review the conversation above and update the skill library",
    "Review the conversation above and consider saving to memory",
)
_LEGACY_NETWORK_CONTINUATION_PROMPT = (
    "[System: The previous response was cut off by a network error mid-stream. "
    "Continue exactly where you left off. Do not restart or repeat prior text. "
    "Finish the answer directly.]"
)


def is_network_continuation_prompt(role: Any, content: Any) -> bool:
    """Return whether role/content are the internal network-recovery marker."""
    return (
        role in {"user", "system"}
        and isinstance(content, str)
        and content.strip() == _LEGACY_NETWORK_CONTINUATION_PROMPT
    )


def _legacy_synthetic_message_kind(message: Any) -> str | None:
    """Classify synthetic user/system turns persisted by older builds."""
    if not isinstance(message, dict):
        return None
    if message.get("role") not in {"user", "system"}:
        return None
    content = message.get("content")
    if not isinstance(content, str):
        return None
    stripped = content.strip()
    if stripped == _LEGACY_NETWORK_CONTINUATION_PROMPT:
        return "network_continuation"
    if any(
        stripped.startswith(prefix) for prefix in _BACKGROUND_REVIEW_HARNESS_PREFIXES
    ):
        return "background_review"
    return None


def strip_legacy_synthetic_messages(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Remove synthetic turns persisted by older recovery/review paths."""
    result: list[dict[str, Any]] = []
    skip_next_assistant = False
    for message in messages:
        synthetic_kind = _legacy_synthetic_message_kind(message)
        if synthetic_kind:
            skip_next_assistant = synthetic_kind == "background_review"
            continue
        if skip_next_assistant:
            skip_next_assistant = False
            if message.get("role") == "assistant":
                continue
        result.append(message)
    return result


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


class SessionQueryMixin:
    """Read behavior shared by writable and process-local read-only stores."""

    _CONTENT_JSON_PREFIX = _CONTENT_JSON_PREFIX
    _IMAGE_EXT_RE = _IMAGE_EXT_RE
    _MARKDOWN_IMAGE_RE = _MARKDOWN_IMAGE_RE
    _MARKDOWN_LINK_RE = _MARKDOWN_LINK_RE
    _DATA_IMAGE_RE = _DATA_IMAGE_RE
    _URL_RE = _URL_RE
    _ABS_IMAGE_PATH_RE = _ABS_IMAGE_PATH_RE
    _CONVERSATION_PAGE_CURSOR_VERSION = 2
    _CONVERSATION_PAGE_DEFAULT_LIMIT = 100
    _CONVERSATION_PAGE_MAX_LIMIT = 200
    _CONVERSATION_PAGE_CONTEXT_ROWS = 32
    _CONVERSATION_PAGE_MAX_TEXT_CHARS = 120_000
    _CONVERSATION_PAGE_MAX_ATTACHMENTS = 64
    _CONVERSATION_PAGE_MAX_SERIALIZED_BYTES = 2 * 1024 * 1024

    @classmethod
    def _decode_content(cls, content: Any) -> Any:
        """Reverse :meth:`_encode_content`; returns scalars unchanged."""
        if isinstance(content, str) and content.startswith(cls._CONTENT_JSON_PREFIX):
            try:
                return json.loads(content[len(cls._CONTENT_JSON_PREFIX):])
            except (json.JSONDecodeError, TypeError):
                logger.warning(
                    "Failed to decode JSON-encoded message content; "
                    "returning raw string"
                )
                return content
        return content

    @staticmethod
    def _is_duplicate_replayed_user_message(messages: List[Dict[str, Any]], msg: Dict[str, Any]) -> bool:
        if msg.get("role") != "user":
            return False
        content = msg.get("content")
        if not isinstance(content, str) or not content:
            return False
        for prev in reversed(messages):
            if prev.get("role") == "user" and prev.get("content") == content:
                return True
            if prev.get("role") == "assistant" and (prev.get("content") or prev.get("tool_calls")):
                return False
        return False

    @classmethod
    def _encode_conversation_page_cursor(cls, payload: Dict[str, Any]) -> str:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
        return base64.urlsafe_b64encode(encoded).decode("ascii").rstrip("=")

    @classmethod
    def _decode_conversation_page_cursor(cls, cursor: str) -> Dict[str, Any]:
        if not isinstance(cursor, str) or not cursor or len(cursor) > 2048:
            raise ValueError("invalid conversation history cursor")
        try:
            padded = cursor + "=" * (-len(cursor) % 4)
            body = json.loads(base64.b64decode(padded, altchars=b"-_", validate=True))
        except (ValueError, TypeError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid conversation history cursor") from exc
        required = {"v", "tip", "lineage", "snapshot", "before"}
        if (
            not isinstance(body, dict)
            or set(body) != required
            or body.get("v") != cls._CONVERSATION_PAGE_CURSOR_VERSION
        ):
            raise ValueError("unsupported conversation history cursor")
        if not isinstance(body.get("tip"), str) or not isinstance(body.get("lineage"), str):
            raise ValueError("invalid conversation history cursor")
        for key in ("snapshot", "before"):
            if not isinstance(body.get(key), int) or body[key] < 0:
                raise ValueError("invalid conversation history cursor")
        if body["before"] > body["snapshot"] + 1:
            raise ValueError("invalid conversation history cursor")
        return body

    @staticmethod
    def _conversation_lineage_fingerprint(session_ids: List[str]) -> str:
        joined = "\x00".join(session_ids).encode("utf-8")
        return hashlib.sha256(joined).hexdigest()[:24]

    @classmethod
    def _conversation_message_from_row(
        cls,
        row: Any,
        *,
        include_row_identity: bool = False,
    ) -> Dict[str, Any]:
        content = cls._decode_content(row["content"])
        if row["role"] in {"user", "assistant"} and isinstance(content, str):
            from agent.memory_manager import sanitize_context

            content = sanitize_context(content).strip()
        msg: Dict[str, Any] = {"role": row["role"], "content": content}
        if include_row_identity:
            msg["_row_id"] = int(row["id"])
            msg["_session_id"] = str(row["session_id"])
        if row["attachments"]:
            try:
                attachments = json.loads(row["attachments"])
                if isinstance(attachments, list):
                    msg["attachments"] = attachments
            except (json.JSONDecodeError, TypeError):
                logger.warning("Failed to deserialize attachments in conversation replay")
        if row["timestamp"]:
            msg["timestamp"] = row["timestamp"]
        if row["tool_call_id"]:
            msg["tool_call_id"] = row["tool_call_id"]
        if row["tool_name"]:
            msg["tool_name"] = row["tool_name"]
        if row["tool_calls"]:
            try:
                msg["tool_calls"] = json.loads(row["tool_calls"])
            except (json.JSONDecodeError, TypeError):
                logger.warning(
                    "Failed to deserialize tool_calls in conversation replay, falling back to []"
                )
                msg["tool_calls"] = []
        if row["platform_message_id"]:
            msg["message_id"] = row["platform_message_id"]
        if row["observed"]:
            msg["observed"] = True
        if row["role"] == "assistant":
            if row["finish_reason"]:
                msg["finish_reason"] = row["finish_reason"]
            if row["reasoning"]:
                msg["reasoning"] = row["reasoning"]
            if row["reasoning_content"] is not None:
                msg["reasoning_content"] = row["reasoning_content"]
            for key in (
                "reasoning_details",
                "codex_reasoning_items",
                "codex_message_items",
            ):
                if not row[key]:
                    continue
                try:
                    msg[key] = json.loads(row[key])
                except (json.JSONDecodeError, TypeError):
                    logger.warning("Failed to deserialize %s, falling back to None", key)
                    msg[key] = None
        return msg

    @classmethod
    def _sanitize_conversation_page_message(
        cls, message: Dict[str, Any]
    ) -> Dict[str, Any]:
        sanitized = dict(message)
        for key in ("content", "reasoning", "reasoning_content"):
            value = sanitized.get(key)
            if isinstance(value, str) and len(value) > cls._CONVERSATION_PAGE_MAX_TEXT_CHARS:
                sanitized[key] = value[: cls._CONVERSATION_PAGE_MAX_TEXT_CHARS]
        attachments = sanitized.get("attachments")
        if isinstance(attachments, list):
            sanitized["attachments"] = attachments[
                : cls._CONVERSATION_PAGE_MAX_ATTACHMENTS
            ]
        return sanitized

    @classmethod
    def _bound_conversation_page_messages(
        cls, messages: List[Dict[str, Any]]
    ) -> Tuple[List[Dict[str, Any]], int]:
        bounded_reversed: List[Dict[str, Any]] = []
        serialized_bytes = 2
        for offset, message in enumerate(reversed(messages)):
            sanitized = cls._sanitize_conversation_page_message(message)
            encoded_size = len(
                json.dumps(sanitized, ensure_ascii=False, default=str).encode("utf-8")
            )
            separator_size = 1 if bounded_reversed else 0
            if serialized_bytes + separator_size + encoded_size > cls._CONVERSATION_PAGE_MAX_SERIALIZED_BYTES:
                omitted = len(messages) - offset
                bounded_reversed.reverse()
                return bounded_reversed, omitted
            bounded_reversed.append(sanitized)
            serialized_bytes += separator_size + encoded_size
        bounded_reversed.reverse()
        return bounded_reversed, 0

    @staticmethod
    def _sanitize_fts5_query(query: str) -> str:
        """Sanitize user input for safe use in FTS5 MATCH queries.

        FTS5 has its own query syntax where characters like ``"``, ``(``, ``)``,
        ``+``, ``*``, ``{``, ``}``, the column-filter operator ``:`` and bare
        boolean operators (``AND``, ``OR``, ``NOT``) have special meaning.
        Passing raw user input directly to MATCH can cause
        ``sqlite3.OperationalError``.

        Strategy:
        - Preserve properly paired quoted phrases (``"exact phrase"``)
        - Strip unmatched FTS5-special characters that would cause errors
        - Wrap unquoted hyphenated and dotted terms in quotes so FTS5
          matches them as exact phrases instead of splitting on the
          hyphen/dot (e.g. ``chat-send``, ``P2.2``, ``my-app.config.ts``)
        """
        # Cap user-controlled FTS input before any regex processing. Search
        # queries do not need to be arbitrarily large, and bounding them keeps
        # sanitizer/runtime behavior predictable under adversarial input.
        query = query[:MAX_FTS5_QUERY_CHARS]

        # Step 1: Extract balanced double-quoted phrases and protect them
        # from further processing via numbered placeholders. Do this with a
        # single linear scan rather than a regex so pathological quote runs
        # cannot induce backtracking.
        _quoted_parts: list = []
        pieces: list[str] = []
        i = 0
        while i < len(query):
            ch = query[i]
            if ch != '"':
                pieces.append(ch)
                i += 1
                continue
            end = query.find('"', i + 1)
            if end == -1:
                # Unmatched quote: replace with whitespace like the old
                # sanitizer's special-char stripping step.
                pieces.append(" ")
                i += 1
                continue
            _quoted_parts.append(query[i:end + 1])
            pieces.append(f"\x00Q{len(_quoted_parts) - 1}\x00")
            i = end + 1

        sanitized = "".join(pieces)

        # Step 2: Strip remaining (unmatched) FTS5-special characters.  ``:`` is
        # FTS5's column-filter operator (``col:term``); since the FTS table has a
        # single ``content`` column, an unquoted colon query like ``TODO: fix``
        # parses as ``column:term`` and raises "no such column" — swallowed at
        # the execute site into zero results.  Strip it like the others.
        sanitized = re.sub(r'[+{}():\"^]', " ", sanitized)

        # Step 3: Collapse repeated * (e.g. "***") into a single one,
        # and remove leading * (prefix-only needs at least one char before *)
        sanitized = re.sub(r"\*+", "*", sanitized)
        sanitized = re.sub(r"(^|\s)\*", r"\1", sanitized)

        # Step 4: Remove dangling boolean operators at start/end that would
        # cause syntax errors (e.g. "hello AND" or "OR world")
        sanitized = re.sub(r"(?i)^(AND|OR|NOT)\b\s*", "", sanitized.strip())
        sanitized = re.sub(r"(?i)\s+(AND|OR|NOT)\s*$", "", sanitized.strip())

        # Step 5: Wrap unquoted dotted and/or hyphenated terms in double
        # quotes.  FTS5's tokenizer splits on dots and hyphens, turning
        # ``chat-send`` into ``chat AND send`` and ``P2.2`` into ``p2 AND 2``.
        # Quoting preserves phrase semantics.  A single pass avoids the
        # double-quoting bug that would occur if dotted, hyphenated and underscored
        # patterns were applied sequentially (e.g. ``my-app.config``).
        sanitized = re.sub(r"\b(\w+(?:[._-]\w+)+)\b", r'"\1"', sanitized)

        # Step 6: Restore preserved quoted phrases
        for i, quoted in enumerate(_quoted_parts):
            sanitized = sanitized.replace(f"\x00Q{i}\x00", quoted)

        return sanitized.strip()

    @staticmethod
    def _is_cjk_codepoint(cp: int) -> bool:
        return (0x4E00 <= cp <= 0x9FFF or    # CJK Unified Ideographs
                0x3400 <= cp <= 0x4DBF or    # CJK Extension A
                0x20000 <= cp <= 0x2A6DF or  # CJK Extension B
                0x3000 <= cp <= 0x303F or    # CJK Symbols
                0x3040 <= cp <= 0x309F or    # Hiragana
                0x30A0 <= cp <= 0x30FF or    # Katakana
                0xAC00 <= cp <= 0xD7AF)      # Hangul Syllables

    @staticmethod
    def _contains_cjk(text: str) -> bool:
        """Check if text contains CJK (Chinese, Japanese, Korean) characters."""
        for ch in text:
            cp = ord(ch)
            if (0x4E00 <= cp <= 0x9FFF or    # CJK Unified Ideographs
                0x3400 <= cp <= 0x4DBF or    # CJK Extension A
                0x20000 <= cp <= 0x2A6DF or  # CJK Extension B
                0x3000 <= cp <= 0x303F or    # CJK Symbols
                0x3040 <= cp <= 0x309F or    # Hiragana
                0x30A0 <= cp <= 0x30FF or    # Katakana
                0xAC00 <= cp <= 0xD7AF):     # Hangul Syllables
                return True
        return False

    @classmethod
    def _count_cjk(cls, text: str) -> int:
        """Count CJK characters in text."""
        return sum(1 for ch in text if cls._is_cjk_codepoint(ord(ch)))

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

    def _content_for_preview(self, session_id: str, raw: Any) -> Any:
        if isinstance(raw, str) and (
            not raw or raw.startswith(self._CONTENT_JSON_PREFIX)
        ):
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
        active_from: float | None = None,
        active_before: float | None = None,
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
        if order_by_last_active or active_from is not None or active_before is not None:
            chain_child_scope = scope_clause.replace("s.", "child.")
            activity_clauses: list[str] = []
            activity_params: list[Any] = []
            if active_from is not None:
                activity_clauses.append("COALESCE(cm.effective_last_active, s.started_at) >= ?")
                activity_params.append(active_from)
            if active_before is not None:
                activity_clauses.append("COALESCE(cm.effective_last_active, s.started_at) < ?")
                activity_params.append(active_before)
            activity_where = (
                " AND " + " AND ".join(activity_clauses) if activity_clauses else ""
            )
            activity_order = (
                "_effective_last_active DESC, s.started_at DESC, s.id DESC"
                if order_by_last_active else "s.started_at DESC"
            )
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
                    COALESCE((SELECT m.content FROM messages m
                              WHERE m.session_id = s.id AND m.role = 'user'
                                AND m.content IS NOT NULL ORDER BY m.timestamp, m.id LIMIT 1), '') AS _preview_raw,
                    COALESCE((SELECT MAX(m2.timestamp) FROM messages m2 WHERE m2.session_id = s.id),
                             s.started_at) AS last_active,
                    COALESCE(cm.effective_last_active, s.started_at) AS _effective_last_active
                FROM sessions s LEFT JOIN chain_max cm ON cm.root_id = s.id
                {where_sql}{activity_where}
                ORDER BY {activity_order}
                LIMIT ? OFFSET ?
            """
            query_params = params + scope_params + params + activity_params + [limit, offset]
        else:
            query = f"""
                SELECT s.*,
                    COALESCE((SELECT m.content FROM messages m
                              WHERE m.session_id = s.id AND m.role = 'user'
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
                session.pop("_preview_raw", ""), 60
            )
            session.pop("_effective_last_active", None)
            sessions.append(session)
        if project_compression_tips and not include_children:
            sessions = self._project_compression_tips(
                sessions, recovery_scope=recovery_scope
            )
        return sessions

    def compression_lineage(
        self,
        session_ids: list[str],
        *,
        recovery_scope: dict[str, Any] | None = None,
    ) -> dict[str, dict[str, str]]:
        requested = list(dict.fromkeys(str(sid) for sid in session_ids if sid))
        if not requested:
            return {}
        values_sql = ",".join("(?, ?, 0)" for _ in requested)
        requested_params = [value for sid in requested for value in (sid, sid)]
        child_scope, child_scope_params = self._recovery_scope_clause(
            recovery_scope, alias="child"
        )
        parent_scope, parent_scope_params = self._recovery_scope_clause(
            recovery_scope, alias="parent"
        )
        reverse_sql = f"""
            WITH RECURSIVE ancestry(request_id, id, depth) AS (
                VALUES {values_sql}
                UNION ALL
                SELECT ancestry.request_id, parent.id, ancestry.depth + 1
                FROM ancestry
                JOIN sessions child ON child.id = ancestry.id
                JOIN sessions parent ON parent.id = child.parent_session_id
                WHERE ancestry.depth < 100
                  AND parent.end_reason = 'compression'
                  AND json_extract(COALESCE(child.model_config, '{{}}'), '$._branched_from') IS NULL
                  AND json_extract(COALESCE(child.model_config, '{{}}'), '$._delegate_from') IS NULL
                  AND COALESCE(child.source, '') != 'tool'
                  AND parent.ended_at IS NOT NULL
                  AND child.started_at IS NOT NULL
                  AND child.started_at >= parent.ended_at
                  {child_scope} {parent_scope}
            )
            SELECT request_id, id, depth FROM ancestry
            ORDER BY request_id, depth DESC
        """
        with self._lock:
            rows = self._conn.execute(
                reverse_sql,
                (*requested_params, *child_scope_params, *parent_scope_params),
            ).fetchall()
        roots: dict[str, str] = {}
        for row in rows:
            roots.setdefault(str(row["request_id"]), str(row["id"]))

        unique_roots = list(dict.fromkeys(roots.values()))
        root_values = ",".join("(?, ?, 0)" for _ in unique_roots)
        root_params = [value for sid in unique_roots for value in (sid, sid)]
        edge_parent_scope, edge_parent_params = self._recovery_scope_clause(
            recovery_scope, alias="parent"
        )
        edge_child_scope, edge_child_params = self._recovery_scope_clause(
            recovery_scope, alias="child"
        )
        forward_sql = f"""
            WITH RECURSIVE chain(root_id, id, depth) AS (
                VALUES {root_values}
                UNION ALL
                SELECT chain.root_id, child.id, chain.depth + 1
                FROM chain
                JOIN sessions parent ON parent.id = chain.id
                JOIN sessions child ON child.id = (
                    SELECT candidate.id
                    FROM sessions candidate
                    WHERE candidate.parent_session_id = chain.id
                      AND json_extract(COALESCE(candidate.model_config, '{{}}'), '$._branched_from') IS NULL
                      AND json_extract(COALESCE(candidate.model_config, '{{}}'), '$._delegate_from') IS NULL
                      AND COALESCE(candidate.source, '') != 'tool'
                      {edge_child_scope.replace('child.', 'candidate.')}
                    ORDER BY CASE WHEN candidate.end_reason = 'compression' THEN 0
                                  WHEN candidate.ended_at IS NULL THEN 1 ELSE 2 END,
                             COALESCE((SELECT MAX(m.timestamp) FROM messages m
                                       WHERE m.session_id = candidate.id),
                                      candidate.started_at) DESC,
                             candidate.started_at DESC, candidate.id DESC
                    LIMIT 1
                )
                WHERE chain.depth < 100
                  AND parent.end_reason = 'compression'{edge_parent_scope}
            )
            SELECT root_id, id, depth FROM chain
            ORDER BY root_id, depth DESC
        """
        with self._lock:
            tip_rows = self._conn.execute(
                forward_sql,
                (*root_params, *edge_child_params, *edge_parent_params),
            ).fetchall()
        tips: dict[str, str] = {}
        for row in tip_rows:
            tips.setdefault(str(row["root_id"]), str(row["id"]))
        return {
            sid: {"root": roots.get(sid, sid), "tip": tips.get(roots.get(sid, sid), roots.get(sid, sid))}
            for sid in requested
        }

    def _session_rich_rows(
        self,
        session_ids: list[str],
        *,
        recovery_scope: dict[str, Any] | None = None,
    ) -> dict[str, dict[str, Any]]:
        requested = list(dict.fromkeys(str(sid) for sid in session_ids if sid))
        if not requested:
            return {}
        placeholders = ",".join("?" for _ in requested)
        scope_clause, scope_params = self._recovery_scope_clause(
            recovery_scope, alias="s"
        )
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT s.*,
                    COALESCE((SELECT m.content FROM messages m
                              WHERE m.session_id = s.id AND m.role = 'user'
                                AND m.content IS NOT NULL
                              ORDER BY m.timestamp, m.id LIMIT 1), '') AS _preview_raw,
                    COALESCE((SELECT MAX(m2.timestamp) FROM messages m2
                              WHERE m2.session_id = s.id), s.started_at) AS last_active
                FROM sessions s
                WHERE s.id IN ({placeholders}){scope_clause}
                """,
                (*requested, *scope_params),
            ).fetchall()
        result = {}
        for row in rows:
            session = dict(row)
            session["preview"] = self._build_message_preview(
                session.pop("_preview_raw", ""), 60
            )
            result[str(session["id"])] = session
        return result

    def resolve_canonical_session_ids(
        self,
        session_ids: list[str],
        *,
        recovery_scope: dict[str, Any] | None = None,
    ) -> dict[str, dict[str, str]]:
        """Resolve exact visible IDs to their canonical compression root and tip."""
        requested = list(dict.fromkeys(str(sid) for sid in session_ids if sid))
        if not requested:
            return {}
        placeholders = ",".join("?" for _ in requested)
        scope_clause, scope_params = self._recovery_scope_clause(
            recovery_scope, alias="s"
        )
        with self._lock:
            rows = self._conn.execute(
                f"SELECT s.id FROM sessions s WHERE s.id IN ({placeholders}){scope_clause}",
                (*requested, *scope_params),
            ).fetchall()
        visible = {str(row["id"]) for row in rows}
        lineage = self.compression_lineage(
            [sid for sid in requested if sid in visible], recovery_scope=recovery_scope
        )
        return {sid: lineage[sid] for sid in requested if sid in lineage}

    def display_message_role_counts(
        self,
        session_ids: list[str],
        *,
        recovery_scope: dict[str, Any] | None = None,
    ) -> dict[str, dict[str, int]]:
        """Count canonical display rows by role over each requested full lineage."""
        requested = list(dict.fromkeys(str(sid) for sid in session_ids if sid))
        if not requested:
            return {}
        values_sql = ",".join("(?, ?, 0)" for _ in requested)
        values_params = [value for sid in requested for value in (sid, sid)]
        scope_clause, scope_params = self._recovery_scope_clause(
            recovery_scope, alias="s"
        )
        sql = f"""
            WITH RECURSIVE lineage(request_id, id, depth) AS (
                VALUES {values_sql}
                UNION ALL
                SELECT lineage.request_id, s.parent_session_id, lineage.depth + 1
                FROM lineage JOIN sessions s ON s.id = lineage.id
                WHERE lineage.depth < 100 AND s.parent_session_id IS NOT NULL{scope_clause}
            )
            SELECT lineage.request_id, m.role, COUNT(m.id) AS message_count
            FROM lineage
            JOIN sessions s ON s.id = lineage.id{scope_clause}
            JOIN messages m ON m.session_id = lineage.id
              AND m.context_projection = 0
              AND (m.active = 1 OR m.compacted = 1)
            GROUP BY lineage.request_id, m.role
        """
        with self._lock:
            rows = self._conn.execute(
                sql, (*values_params, *scope_params, *scope_params)
            ).fetchall()
        result = {sid: {} for sid in requested}
        for row in rows:
            result[str(row["request_id"])][str(row["role"] or "unknown")] = int(
                row["message_count"]
            )
        return result

    def display_message_counts(
        self,
        session_ids: list[str],
        *,
        include_ancestors: bool = True,
        recovery_scope: dict[str, Any] | None = None,
    ) -> dict[str, int]:
        requested = list(dict.fromkeys(str(sid) for sid in session_ids if sid))
        if not requested:
            return {}
        values_sql = ",".join("(?, ?, 0)" for _ in requested)
        values_params = [value for sid in requested for value in (sid, sid)]
        scope_clause, scope_params = self._recovery_scope_clause(
            recovery_scope, alias="s"
        )
        if include_ancestors:
            lineage_sql = f"""
                WITH RECURSIVE lineage(request_id, id, depth) AS (
                    VALUES {values_sql}
                    UNION ALL
                    SELECT lineage.request_id, s.parent_session_id, lineage.depth + 1
                    FROM lineage
                    JOIN sessions s ON s.id = lineage.id
                    WHERE lineage.depth < 100
                      AND s.parent_session_id IS NOT NULL{scope_clause}
                )
                SELECT lineage.request_id, COUNT(m.id) AS message_count
                FROM lineage
                JOIN sessions s ON s.id = lineage.id{scope_clause}
                LEFT JOIN messages m ON m.session_id = lineage.id
                    AND m.context_projection = 0
                    AND (m.active = 1 OR m.compacted = 1)
                GROUP BY lineage.request_id
            """
            params = (*values_params, *scope_params, *scope_params)
        else:
            placeholders = ",".join("?" for _ in requested)
            lineage_sql = f"""
                SELECT s.id AS request_id, COUNT(m.id) AS message_count
                FROM sessions s
                LEFT JOIN messages m ON m.session_id = s.id
                    AND m.context_projection = 0
                    AND (m.active = 1 OR m.compacted = 1)
                WHERE s.id IN ({placeholders}){scope_clause}
                GROUP BY s.id
            """
            params = (*requested, *scope_params)
        with self._lock:
            rows = self._conn.execute(lineage_sql, params).fetchall()
        counts = {str(row["request_id"]): int(row["message_count"]) for row in rows}
        return {sid: counts.get(sid, 0) for sid in requested}

    def list_sessions_page(
        self,
        *,
        include_display_counts: bool = True,
        **options: Any,
    ) -> tuple[list[dict[str, Any]], int]:
        sessions = self.list_sessions_rich(**options)
        count_options = {
            key: options[key]
            for key in (
                "source", "cwd_prefix", "min_message_count", "include_archived",
                "archived_only", "exclude_sources", "active_from", "active_before",
                "recovery_scope",
            )
            if key in options
        }
        total = self.session_count(exclude_children=True, **count_options)
        if include_display_counts:
            recovery_scope = options.get("recovery_scope")
            counts = self.display_message_counts(
                [str(session["id"]) for session in sessions],
                include_ancestors=True,
                recovery_scope=recovery_scope,
            )
            for session in sessions:
                session["message_count"] = counts.get(str(session["id"]), 0)
        return sessions, total

    def _project_compression_tips(
        self,
        sessions: list[dict[str, Any]],
        *,
        recovery_scope: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        roots = [
            str(session["id"])
            for session in sessions
            if session.get("end_reason") == "compression"
        ]
        if not roots:
            return sessions
        lineage = self.compression_lineage(roots, recovery_scope=recovery_scope)
        tips = self._session_rich_rows(
            [str(item["tip"]) for item in lineage.values()],
            recovery_scope=recovery_scope,
        )
        projected = []
        for session in sessions:
            root_id = str(session["id"])
            tip_id = lineage.get(root_id, {}).get("tip", root_id)
            if tip_id == root_id:
                projected.append(session)
                continue
            tip = tips.get(str(tip_id))
            if not tip:
                projected.append(session)
                continue
            merged = dict(session)
            for key in (
                "id", "ended_at", "end_reason", "message_count", "tool_call_count",
                "title", "last_active", "preview", "model", "system_prompt", "cwd",
                "git_branch", "git_repo_root",
            ):
                if key in tip:
                    merged[key] = tip[key]
            merged["_lineage_root_id"] = root_id
            projected.append(merged)
        return projected

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

    def session_count(
        self,
        source: str | None = None,
        cwd_prefix: str | None = None,
        min_message_count: int = 0,
        include_archived: bool = False,
        archived_only: bool = False,
        exclude_children: bool = False,
        exclude_sources: list[str] | None = None,
        active_from: float | None = None,
        active_before: float | None = None,
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
        if active_from is None and active_before is None:
            with self._lock:
                row = self._conn.execute(
                    f"SELECT COUNT(*) FROM sessions s {where_sql}", params
                ).fetchone()
            return int(row[0])

        scope_clause, scope_params = self._recovery_scope_clause(
            recovery_scope, alias="s"
        )
        child_scope = scope_clause.replace("s.", "child.")
        activity_clauses: list[str] = []
        activity_params: list[Any] = []
        if active_from is not None:
            activity_clauses.append("COALESCE(cm.effective_last_active, s.started_at) >= ?")
            activity_params.append(active_from)
        if active_before is not None:
            activity_clauses.append("COALESCE(cm.effective_last_active, s.started_at) < ?")
            activity_params.append(active_before)
        activity_where = " AND ".join(activity_clauses)
        query = f"""
            WITH RECURSIVE chain(root_id, cur_id) AS (
                SELECT s.id, s.id FROM sessions s {where_sql}
                UNION ALL
                SELECT chain.root_id, child.id FROM chain
                JOIN sessions parent ON parent.id = chain.cur_id
                JOIN sessions child INDEXED BY idx_sessions_parent
                  ON child.parent_session_id = chain.cur_id
                WHERE parent.end_reason = 'compression'
                  AND json_extract(COALESCE(child.model_config, '{{}}'), '$._branched_from') IS NULL
                  AND json_extract(COALESCE(child.model_config, '{{}}'), '$._delegate_from') IS NULL
                  AND COALESCE(child.source, '') != 'tool'{child_scope}
            ), chain_max AS (
                SELECT root_id, MAX(COALESCE(
                    (SELECT MAX(m.timestamp) FROM messages m WHERE m.session_id = cur_id),
                    (SELECT started_at FROM sessions ss WHERE ss.id = cur_id)
                )) AS effective_last_active
                FROM chain GROUP BY root_id
            )
            SELECT COUNT(*) FROM sessions s
            LEFT JOIN chain_max cm ON cm.root_id = s.id
            {where_sql} AND {activity_where}
        """
        with self._lock:
            row = self._conn.execute(
                query, (*params, *scope_params, *params, *activity_params)
            ).fetchone()
        return int(row[0])

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

    def get_messages_as_conversation(
        self,
        session_id: str,
        include_ancestors: bool = False,
        include_inactive: bool = False,
        *,
        recovery_scope: dict[str, Any] | None = None,
    ) -> List[Dict[str, Any]]:
        """Load active model replay in chronological insertion order."""
        session_ids = [session_id]
        if include_ancestors:
            session_ids = self._session_lineage_root_to_tip(
                session_id, recovery_scope=recovery_scope
            )

        active_clause = "" if include_inactive else " AND m.active = 1"
        scope_clause, scope_params = self._recovery_scope_clause(
            recovery_scope, alias="s"
        )
        placeholders = ",".join("?" for _ in session_ids)
        with self._lock:
            rows = self._conn.execute(
                "SELECT m.role, m.content, m.attachments, m.tool_call_id, "
                "m.tool_calls, m.tool_name, m.finish_reason, m.reasoning, "
                "m.reasoning_content, m.reasoning_details, "
                "m.codex_reasoning_items, m.codex_message_items, "
                "m.platform_message_id, m.observed, m.timestamp "
                "FROM messages m JOIN sessions s ON s.id = m.session_id "
                f"WHERE m.session_id IN ({placeholders}){scope_clause}"
                f"{active_clause} ORDER BY m.id",
                (*session_ids, *scope_params),
            ).fetchall()

        messages: List[Dict[str, Any]] = []
        for row in rows:
            message = self._conversation_message_from_row(row)
            if include_ancestors and self._is_duplicate_replayed_user_message(
                messages, message
            ):
                continue
            messages.append(message)
        return strip_legacy_synthetic_messages(messages)

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
        hydrated = strip_legacy_synthetic_messages(hydrated)
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

        contexts: dict[int, list[dict[str, Any]]] = {
            int(match["id"]): [] for match in matches
        }
        if contexts:
            values_sql = ",".join("(?)" for _ in contexts)
            try:
                with self._lock:
                    context_rows = self._conn.execute(
                        f"""
                        WITH targets(target_id) AS (VALUES {values_sql}),
                        ranked AS (
                            SELECT t.target_id, m.role, m.content,
                                   CASE WHEN m.id = t.target_id THEN 1
                                        WHEN (m.timestamp < target.timestamp)
                                          OR (m.timestamp = target.timestamp AND m.id < target.id)
                                        THEN 0 ELSE 2 END AS position,
                                   ROW_NUMBER() OVER (
                                       PARTITION BY t.target_id,
                                           CASE WHEN m.id = t.target_id THEN 1
                                                WHEN (m.timestamp < target.timestamp)
                                                  OR (m.timestamp = target.timestamp AND m.id < target.id)
                                                THEN 0 ELSE 2 END
                                       ORDER BY CASE WHEN m.id = t.target_id THEN 0
                                                     WHEN (m.timestamp < target.timestamp)
                                                       OR (m.timestamp = target.timestamp AND m.id < target.id)
                                                     THEN -m.timestamp ELSE m.timestamp END,
                                                CASE WHEN m.id < target.id THEN -m.id ELSE m.id END
                                   ) AS rank
                            FROM targets t
                            JOIN messages target ON target.id = t.target_id
                            JOIN messages m ON m.session_id = target.session_id
                            WHERE m.id = t.target_id
                               OR (m.timestamp < target.timestamp)
                               OR (m.timestamp = target.timestamp AND m.id < target.id)
                               OR (m.timestamp > target.timestamp)
                               OR (m.timestamp = target.timestamp AND m.id > target.id)
                        )
                        SELECT target_id, role, content FROM ranked
                        WHERE rank = 1
                        ORDER BY target_id, position
                        """,
                        tuple(contexts),
                    ).fetchall()
                for row in context_rows:
                    contexts[int(row["target_id"])].append(
                        {
                            "role": row["role"],
                            "content": self._build_message_preview(
                                row["content"], 200
                            ),
                        }
                    )
            except Exception:
                pass
        for match in matches:
            match["context"] = contexts.get(int(match["id"]), [])

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

    def session_stats(
        self,
        *,
        recovery_scope: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        scope_clause, scope_params = self._recovery_scope_clause(
            recovery_scope, alias="s"
        )
        with self._lock:
            totals = self._conn.execute(
                f"""
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN s.archived = 0 THEN 1 ELSE 0 END) AS active_store,
                    SUM(CASE WHEN s.archived = 1 THEN 1 ELSE 0 END) AS archived
                FROM sessions s
                WHERE 1 = 1{scope_clause}
                """,
                scope_params,
            ).fetchone()
            source_rows = self._conn.execute(
                f"""
                SELECT COALESCE(s.source, 'cli') AS source, COUNT(*) AS count
                FROM sessions s
                WHERE 1 = 1{scope_clause}
                GROUP BY COALESCE(s.source, 'cli')
                """,
                scope_params,
            ).fetchall()
            messages = self._conn.execute(
                "SELECT COUNT(*) FROM messages m JOIN sessions s ON s.id = m.session_id "
                f"WHERE 1 = 1{scope_clause}",
                scope_params,
            ).fetchone()
        return {
            "total": int(totals["total"] or 0),
            "active_store": int(totals["active_store"] or 0),
            "archived": int(totals["archived"] or 0),
            "messages": int(messages[0] if messages else 0),
            "by_source": {
                str(row["source"]): int(row["count"]) for row in source_rows
            },
        }

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
