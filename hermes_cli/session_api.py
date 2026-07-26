"""Shared DB-backed dashboard session API helpers.

This module is intentionally import-light and has no dependency on
``hermes_cli.web_server`` or any FastAPI app globals.  Control Plane and Owner
Worker routes keep their own auth/profile/proxy decisions, then call these
helpers with an already-open owner/profile-local ``SessionDB``.
"""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Any

from starlette.exceptions import HTTPException

from hermes_cli.display_transcript import format_display_transcript
from hermes_cli.latency_trace import log_latency_stage


_log = logging.getLogger(__name__)

_COMPACT_SESSION_FIELDS = (
    "id",
    "source",
    "model",
    "title",
    "started_at",
    "ended_at",
    "last_active",
    "is_active",
    "message_count",
    "tool_call_count",
    "input_tokens",
    "output_tokens",
    "preview",
    "parent_session_id",
    "archived",
    "_lineage_root_id",
)


def list_sessions_payload(
    db: Any,
    *,
    limit: int = 20,
    offset: int = 0,
    min_messages: int = 0,
    archived: str = "exclude",
    order: str = "created",
    source: str | None = None,
    exclude_sources: str | None = None,
    cwd_prefix: str | None = None,
    profile_name: str | None = None,
    recovery_scope: dict[str, Any] | None = None,
    compact: bool = False,
    latency_trace_id: str = "",
) -> dict[str, Any]:
    if archived not in ("exclude", "only", "include"):
        raise HTTPException(status_code=400, detail="archived must be one of: exclude, only, include")
    if order not in ("created", "recent"):
        raise HTTPException(status_code=400, detail="order must be one of: created, recent")
    exclude_list = [s for s in (exclude_sources or "").split(",") if s.strip()]
    min_message_count = max(0, min_messages)
    archived_only = archived == "only"
    include_archived = archived == "include"
    stage_started_at = time.monotonic()
    sessions, total = db.list_sessions_page(
        source=source or None,
        exclude_sources=exclude_list or None,
        cwd_prefix=(cwd_prefix or None),
        limit=limit,
        offset=offset,
        min_message_count=min_message_count,
        include_archived=include_archived,
        archived_only=archived_only,
        order_by_last_active=order == "recent",
        recovery_scope=recovery_scope,
        include_display_counts=not compact,
    )
    log_latency_stage(
        _log,
        trace_id=latency_trace_id,
        surface="session-list",
        stage="sessions.queried",
        started_at=stage_started_at,
    )
    stage_started_at = time.monotonic()
    log_latency_stage(
        _log,
        trace_id=latency_trace_id,
        surface="session-list",
        stage="sessions.counted",
        started_at=stage_started_at,
    )
    now = time.time()
    stage_started_at = time.monotonic()
    for session in sessions:
        session["is_active"] = (
            session.get("ended_at") is None
            and (now - session.get("last_active", session.get("started_at", 0))) < 300
        )
        session["archived"] = bool(session.get("archived"))
        if profile_name:
            session["profile"] = profile_name
            session["is_default_profile"] = profile_name == "default"
    if compact:
        sessions = [
            {key: session[key] for key in _COMPACT_SESSION_FIELDS if key in session}
            for session in sessions
        ]
    log_latency_stage(
        _log,
        trace_id=latency_trace_id,
        surface="session-list",
        stage="sessions.enriched" if not compact else "sessions.compact",
        started_at=stage_started_at,
    )
    return {"sessions": sessions, "total": total, "limit": limit, "offset": offset}


def _resolve_session_id(
    db: Any,
    session_id: str,
    recovery_scope: dict[str, Any] | None = None,
) -> str | None:
    return db.resolve_session_id(session_id, recovery_scope=recovery_scope)


def search_sessions_payload(
    db: Any,
    *,
    q: str = "",
    limit: int = 20,
    recovery_scope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not q or not q.strip():
        return {"results": []}
    safe_limit = max(1, min(int(limit or 20), 100))
    seen: dict[str, dict[str, Any]] = {}

    search_scope = (
        {"recovery_scope": recovery_scope} if recovery_scope is not None else {}
    )
    id_rows = db.search_sessions_by_id(
        q,
        limit=safe_limit,
        include_archived=True,
        **search_scope,
    )
    terms = [token if token.startswith('"') or token.endswith("*") else token + "*" for token in re.findall(r'"[^"]*"|\S+', q.strip())]
    message_rows = db.search_messages(
        query=" ".join(terms),
        limit=max(safe_limit * 5, 50),
        **search_scope,
    )
    candidates: list[tuple[str, dict[str, Any]]] = []
    for row in id_rows:
        sid = str(row.get("id") or "")
        preview = (row.get("preview") or "").strip()
        candidates.append(
            (
                sid,
                {
                    "snippet": preview or f"Session ID: {sid}",
                    "role": None,
                    "source": row.get("source"),
                    "model": row.get("model"),
                    "session_started": row.get("started_at"),
                },
            )
        )
    candidates.extend(
        (
            str(match.get("session_id") or ""),
            {
                "snippet": match.get("snippet", ""),
                "role": match.get("role"),
                "source": match.get("source"),
                "model": match.get("model"),
                "session_started": match.get("session_started"),
            },
        )
        for match in message_rows
    )
    lineage = db.compression_lineage(
        [sid for sid, _payload in candidates], recovery_scope=recovery_scope
    )
    for sid, payload in candidates:
        item = lineage.get(sid, {"root": sid, "tip": sid})
        root = item["root"]
        if not sid or root in seen or len(seen) >= safe_limit:
            continue
        result = dict(payload)
        result["session_id"] = item["tip"]
        result["lineage_root"] = root
        seen[root] = result
    return {"results": list(seen.values())}


def session_latest_descendant(
    db: Any,
    session_id: str,
    *,
    recovery_scope: dict[str, Any] | None = None,
) -> tuple[str | None, list[str]]:
    """Return the canonical compression continuation for a resumable session."""
    if recovery_scope is None:
        sid = db.resolve_session_id(session_id)
        row = db.get_session(sid) if sid else None
    else:
        sid = _resolve_session_id(db, session_id, recovery_scope)
        row = (
            db.get_session_for_recovery(sid, recovery_scope=recovery_scope)
            if sid
            else None
        )
    if not sid:
        return None, []
    if not row:
        return None, []
    latest = db.resolve_resume_session_id(
        sid, recovery_scope=recovery_scope
    )
    if not latest:
        return None, []
    return latest, [sid] if latest == sid else [sid, latest]


def latest_descendant_payload(
    db: Any,
    session_id: str,
    *,
    recovery_scope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    latest, path = session_latest_descendant(db, session_id, recovery_scope=recovery_scope)
    if not latest:
        raise HTTPException(status_code=404, detail="Session not found")
    return {
        "requested_session_id": path[0] if path else session_id,
        "session_id": latest,
        "path": path,
        "changed": bool(path and latest != path[0]),
    }


def session_detail_payload(
    db: Any,
    session_id: str,
    *,
    profile_name: str | None = None,
    recovery_scope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if recovery_scope is None:
        sid = db.resolve_session_id(session_id)
        session = db.get_session(sid) if sid else None
    else:
        sid = _resolve_session_id(db, session_id, recovery_scope)
        session = (
            db.get_session_for_recovery(sid, recovery_scope=recovery_scope)
            if sid
            else None
        )
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if profile_name:
        session["profile"] = profile_name
    return session


def session_messages_payload(
    db: Any,
    session_id: str,
    *,
    limit: int | None = None,
    before: str | None = None,
    recovery_scope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if recovery_scope is None:
        sid = db.resolve_session_id(session_id)
    else:
        sid = _resolve_session_id(db, session_id, recovery_scope)
    if not sid:
        raise HTTPException(status_code=404, detail="Session not found")
    if recovery_scope is not None and not db.get_session_for_recovery(
        sid, recovery_scope=recovery_scope
    ):
        raise HTTPException(status_code=404, detail="Session not found")
    sid = db.resolve_resume_session_id(sid, recovery_scope=recovery_scope)
    if limit is None and before is None:
        history = db.get_display_messages(
            sid,
            include_ancestors=True,
            recovery_scope=recovery_scope,
        )
        return {
            "session_id": sid,
            "messages": format_display_transcript(history),
        }
    try:
        page = db.get_conversation_page(
            sid,
            before_cursor=before,
            limit=limit or 100,
            include_ancestors=True,
            recovery_scope=recovery_scope,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "session_id": sid,
        "messages": format_display_transcript(page["messages"]),
        "history_page": {
            "cursor": page["next_cursor"],
            "has_more": page["has_more"],
            "returned_count": page["returned_count"],
            "truncated_count": page["filtered_count"],
        },
    }


def export_session_payload(
    db: Any,
    session_id: str,
    *,
    recovery_scope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if recovery_scope is None:
        sid = db.resolve_session_id(session_id)
        data = db.export_session(sid) if sid else None
    else:
        sid = _resolve_session_id(db, session_id, recovery_scope)
        data = (
            db.export_session(sid, recovery_scope=recovery_scope)
            if sid
            else None
        )
    if not sid:
        raise HTTPException(status_code=404, detail="Session not found")
    if data is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return data


def rename_session_payload(db: Any, session_id: str, *, title: str | None = None, archived: bool | None = None) -> dict[str, Any]:
    sid = db.resolve_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=404, detail="Session not found")
    if title is None and archived is None:
        raise HTTPException(status_code=400, detail="Nothing to update; provide 'title' and/or 'archived'.")
    if title is not None:
        try:
            db.set_session_title(sid, title or "")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    if archived is not None:
        db.set_session_archived(sid, archived)
    result: dict[str, Any] = {"ok": True, "title": db.get_session_title(sid) or ""}
    if archived is not None:
        result["archived"] = bool(archived)
    return result


def delete_session_payload(db: Any, session_id: str) -> dict[str, Any]:
    sid = db.resolve_session_id(session_id)
    if not sid:
        return {"ok": True, "already_absent": True}
    db.delete_session(sid)
    return {"ok": True}


def bulk_delete_payload(db: Any, ids: list[str]) -> dict[str, Any]:
    if len(ids) > 500:
        raise HTTPException(status_code=400, detail="ids must contain at most 500 entries")
    return {"ok": True, "deleted": db.delete_sessions(ids)}


def empty_count_payload(
    db: Any,
    *,
    recovery_scope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if recovery_scope is None:
        return {"count": db.count_empty_sessions()}
    return {"count": db.count_empty_sessions(recovery_scope=recovery_scope)}


def delete_empty_payload(db: Any) -> dict[str, Any]:
    return {"ok": True, "deleted": db.delete_empty_sessions()}


def stats_payload(
    db: Any,
    *,
    recovery_scope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return db.session_stats(recovery_scope=recovery_scope)


def prune_sessions_payload(
    db: Any,
    *,
    older_than_days: int = 90,
    source: str | None = None,
    sessions_dir: Path | None = None,
) -> dict[str, Any]:
    if older_than_days < 1:
        raise HTTPException(status_code=400, detail="older_than_days must be >= 1")
    return {
        "ok": True,
        "removed": db.prune_sessions(
            older_than_days=older_than_days,
            source=(source or None),
            sessions_dir=sessions_dir if sessions_dir is not None and sessions_dir.exists() else None,
        ),
    }
