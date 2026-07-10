"""Shared DB-backed dashboard session API helpers.

This module is intentionally import-light and has no dependency on
``hermes_cli.web_server`` or any FastAPI app globals.  Control Plane and Owner
Worker routes keep their own auth/profile/proxy decisions, then call these
helpers with an already-open owner/profile-local ``SessionDB``.
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from fastapi import HTTPException


def _row_get(row: Any, key: str, index: int) -> Any:
    if isinstance(row, dict):
        return row.get(key)
    try:
        return row[key]
    except Exception:
        try:
            return row[index]
        except Exception:
            return None


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
) -> dict[str, Any]:
    if archived not in ("exclude", "only", "include"):
        raise HTTPException(status_code=400, detail="archived must be one of: exclude, only, include")
    if order not in ("created", "recent"):
        raise HTTPException(status_code=400, detail="order must be one of: created, recent")
    exclude_list = [s for s in (exclude_sources or "").split(",") if s.strip()]
    min_message_count = max(0, min_messages)
    archived_only = archived == "only"
    include_archived = archived == "include"
    sessions = db.list_sessions_rich(
        source=source or None,
        exclude_sources=exclude_list or None,
        cwd_prefix=(cwd_prefix or None),
        limit=limit,
        offset=offset,
        min_message_count=min_message_count,
        include_archived=include_archived,
        archived_only=archived_only,
        order_by_last_active=order == "recent",
    )
    total = db.session_count(
        source=source or None,
        cwd_prefix=(cwd_prefix or None),
        exclude_sources=exclude_list or None,
        min_message_count=min_message_count,
        include_archived=include_archived,
        archived_only=archived_only,
        exclude_children=True,
    )
    now = time.time()
    for session in sessions:
        session["is_active"] = (
            session.get("ended_at") is None
            and (now - session.get("last_active", session.get("started_at", 0))) < 300
        )
        session["archived"] = bool(session.get("archived"))
        if profile_name:
            session["profile"] = profile_name
            session["is_default_profile"] = profile_name == "default"
    return {"sessions": sessions, "total": total, "limit": limit, "offset": offset}


def _compression_root(db: Any, session_id: str, root_cache: dict[str, str]) -> str:
    if not session_id:
        return session_id
    if session_id in root_cache:
        return root_cache[session_id]
    chain: list[str] = []
    cur = session_id
    visited: set[str] = set()
    root = session_id
    while cur and cur not in visited:
        visited.add(cur)
        chain.append(cur)
        if cur in root_cache:
            root = root_cache[cur]
            break
        try:
            session = db.get_session(cur)
        except Exception:
            session = None
        if not session:
            root = cur
            break
        parent = session.get("parent_session_id") if isinstance(session, dict) else None
        if not parent:
            root = cur
            break
        try:
            parent_session = db.get_session(parent)
        except Exception:
            parent_session = None
        if not parent_session:
            root = cur
            break
        parent_ended_at = parent_session.get("ended_at")
        started_at = session.get("started_at")
        is_compression_edge = (
            parent_session.get("end_reason") == "compression"
            and parent_ended_at is not None
            and started_at is not None
            and started_at >= parent_ended_at
        )
        if not is_compression_edge:
            root = cur
            break
        cur = parent
    for node in chain:
        root_cache[node] = root
    return root


def _lineage_tip(db: Any, root_id: str, tip_cache: dict[str, str]) -> str:
    if root_id in tip_cache:
        return tip_cache[root_id]
    tip = root_id
    try:
        resolved = db.get_compression_tip(root_id)
        if resolved:
            tip = resolved
    except Exception:
        pass
    tip_cache[root_id] = tip
    return tip


def search_sessions_payload(db: Any, *, q: str = "", limit: int = 20) -> dict[str, Any]:
    if not q or not q.strip():
        return {"results": []}
    safe_limit = max(1, min(int(limit or 20), 100))
    root_cache: dict[str, str] = {}
    tip_cache: dict[str, str] = {}
    seen: dict[str, dict[str, Any]] = {}

    def add_lineage_result(raw_sid: str, payload: dict[str, Any]) -> None:
        if not raw_sid:
            return
        root = _compression_root(db, raw_sid, root_cache)
        if root in seen or len(seen) >= safe_limit:
            return
        payload = dict(payload)
        payload["session_id"] = _lineage_tip(db, root, tip_cache)
        payload["lineage_root"] = root
        seen[root] = payload

    for row in db.search_sessions_by_id(q, limit=safe_limit, include_archived=True):
        sid = row.get("id")
        preview = (row.get("preview") or "").strip()
        add_lineage_result(
            sid,
            {
                "snippet": preview or f"Session ID: {sid}",
                "role": None,
                "source": row.get("source"),
                "model": row.get("model"),
                "session_started": row.get("started_at"),
            },
        )

    terms = [token if token.startswith('"') or token.endswith("*") else token + "*" for token in re.findall(r'"[^"]*"|\S+', q.strip())]
    for match in db.search_messages(query=" ".join(terms), limit=max(safe_limit * 5, 50)):
        if len(seen) >= safe_limit:
            break
        add_lineage_result(
            match["session_id"],
            {
                "snippet": match.get("snippet", ""),
                "role": match.get("role"),
                "source": match.get("source"),
                "model": match.get("model"),
                "session_started": match.get("session_started"),
            },
        )
    return {"results": list(seen.values())}


def session_latest_descendant(db: Any, session_id: str) -> tuple[str | None, list[str]]:
    sid = db.resolve_session_id(session_id)
    if not sid or not db.get_session(sid):
        return None, []
    conn = (
        getattr(db, "conn", None)
        or getattr(db, "_conn", None)
        or getattr(db, "connection", None)
        or getattr(db, "_connection", None)
    )
    rows: list[dict[str, Any]] = []
    if conn is not None:
        raw_rows = conn.execute("SELECT id, parent_session_id, started_at FROM sessions").fetchall()
        rows = [
            {
                "id": _row_get(row, "id", 0),
                "parent_session_id": _row_get(row, "parent_session_id", 1),
                "started_at": _row_get(row, "started_at", 2),
            }
            for row in raw_rows
        ]
    else:
        rows = db.list_sessions_rich(limit=10000, offset=0)
    children: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        rid = row.get("id")
        parent = row.get("parent_session_id")
        if rid and parent:
            children.setdefault(parent, []).append(row)

    def started(row: dict[str, Any]) -> float:
        try:
            return float(row.get("started_at") or 0)
        except Exception:
            return 0.0

    current = sid
    path = [sid]
    seen = {sid}
    while children.get(current):
        candidates = [row for row in children[current] if row.get("id") not in seen]
        if not candidates:
            break
        candidates.sort(key=started, reverse=True)
        current = candidates[0]["id"]
        path.append(current)
        seen.add(current)
    return current, path


def latest_descendant_payload(db: Any, session_id: str) -> dict[str, Any]:
    latest, path = session_latest_descendant(db, session_id)
    if not latest:
        raise HTTPException(status_code=404, detail="Session not found")
    return {
        "requested_session_id": path[0] if path else session_id,
        "session_id": latest,
        "path": path,
        "changed": bool(path and latest != path[0]),
    }


def session_detail_payload(db: Any, session_id: str, *, profile_name: str | None = None) -> dict[str, Any]:
    sid = db.resolve_session_id(session_id)
    session = db.get_session(sid) if sid else None
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if profile_name:
        session["profile"] = profile_name
    return session


def session_messages_payload(db: Any, session_id: str) -> dict[str, Any]:
    sid = db.resolve_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=404, detail="Session not found")
    sid = db.resolve_resume_session_id(sid)
    return {"session_id": sid, "messages": db.get_messages(sid)}


def export_session_payload(db: Any, session_id: str) -> dict[str, Any]:
    sid = db.resolve_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=404, detail="Session not found")
    data = db.export_session(sid)
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


def empty_count_payload(db: Any) -> dict[str, Any]:
    return {"count": db.count_empty_sessions()}


def delete_empty_payload(db: Any) -> dict[str, Any]:
    return {"ok": True, "deleted": db.delete_empty_sessions()}


def stats_payload(db: Any) -> dict[str, Any]:
    by_source: dict[str, int] = {}
    try:
        for session in db.list_sessions_rich(limit=10000, include_archived=True):
            source = str(session.get("source") or "cli")
            by_source[source] = by_source.get(source, 0) + 1
    except Exception:
        pass
    return {
        "total": db.session_count(include_archived=True),
        "active_store": db.session_count(include_archived=False),
        "archived": db.session_count(archived_only=True),
        "messages": db.message_count(),
        "by_source": by_source,
    }


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
