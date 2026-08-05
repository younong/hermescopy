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
    active_from: float | None = None,
    active_before: float | None = None,
    profile_name: str | None = None,
    recovery_scope: dict[str, Any] | None = None,
    compact: bool = False,
    allowed_sources: list[str] | None = None,
    latency_trace_id: str = "",
) -> dict[str, Any]:
    if archived not in ("exclude", "only", "include"):
        raise HTTPException(status_code=400, detail="archived must be one of: exclude, only, include")
    if order not in ("created", "recent"):
        raise HTTPException(status_code=400, detail="order must be one of: created, recent")
    if active_from is not None and active_before is not None and active_from >= active_before:
        raise HTTPException(status_code=400, detail="active_from must be less than active_before")
    exclude_list = [s for s in (exclude_sources or "").split(",") if s.strip()]
    min_message_count = max(0, min_messages)
    archived_only = archived == "only"
    include_archived = archived == "include"
    stage_started_at = time.monotonic()
    sessions, total = db.list_sessions_page(
        source=source or None,
        source_filter=allowed_sources,
        exclude_sources=exclude_list or None,
        cwd_prefix=(cwd_prefix or None),
        limit=limit,
        offset=offset,
        min_message_count=min_message_count,
        include_archived=include_archived,
        archived_only=archived_only,
        order_by_last_active=order == "recent",
        active_from=active_from,
        active_before=active_before,
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


def _composition_ids(raw_ids: list[str] | str) -> list[str]:
    ids = raw_ids if isinstance(raw_ids, list) else str(raw_ids).split(",")
    if not ids:
        raise HTTPException(status_code=400, detail="ids is required")
    if len(ids) > 50:
        raise HTTPException(status_code=400, detail="ids must contain at most 50 entries")
    normalized: list[str] = []
    total_chars = 0
    seen: set[str] = set()
    for raw in ids:
        sid = str(raw)
        if not sid.strip():
            raise HTTPException(status_code=400, detail="ids must not contain blanks")
        if len(sid) > 256:
            raise HTTPException(status_code=400, detail="each id must be at most 256 characters")
        total_chars += len(sid)
        if total_chars > 4096:
            raise HTTPException(status_code=400, detail="ids must total at most 4096 characters")
        if sid in seen:
            raise HTTPException(status_code=400, detail="ids must not contain duplicates")
        seen.add(sid)
        normalized.append(sid)
    return normalized


def _composition_segment(
    segment_id: str,
    label: str,
    value: int | None,
    *,
    unit: str,
    status: str,
    known_total: int,
) -> dict[str, Any]:
    return {
        "id": segment_id,
        "label": label,
        "value": value,
        "percentage": (
            round(value / known_total * 100, 1)
            if value is not None and known_total > 0
            else None
        ),
        "unit": unit,
        "status": status,
    }


def session_composition_payload(
    db: Any,
    *,
    ids: list[str] | str,
    recovery_scope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return exact DB and partial request/compression composition charts."""
    requested = _composition_ids(ids)
    canonical = db.resolve_canonical_session_ids(
        requested, recovery_scope=recovery_scope
    )
    missing = [sid for sid in requested if sid not in canonical]
    if missing:
        raise HTTPException(status_code=404, detail="Session not found")

    roots = list(dict.fromkeys(canonical[sid]["root"] for sid in requested))
    tips = list(dict.fromkeys(canonical[sid]["tip"] for sid in requested))
    role_counts = db.display_message_role_counts(
        tips, recovery_scope=recovery_scope
    )
    combined_roles: dict[str, int] = {}
    for tip in tips:
        for role, count in role_counts.get(tip, {}).items():
            combined_roles[role] = combined_roles.get(role, 0) + count
    exact_total = sum(combined_roles.values())
    other_count = sum(
        count
        for role, count in combined_roles.items()
        if role not in {"user", "assistant", "tool"}
    )
    exact_role_values = [
        ("user", "User", combined_roles.get("user", 0)),
        ("assistant", "Assistant", combined_roles.get("assistant", 0)),
        ("tool", "Tool", combined_roles.get("tool", 0)),
    ]
    if other_count:
        exact_role_values.append(("other", "Other", other_count))
    exact_segments = [
        _composition_segment(
            role,
            label,
            count,
            unit="messages",
            status="exact",
            known_total=exact_total,
        )
        for role, label, count in exact_role_values
    ]

    from agent.context_compressor import ContextCompressor
    from agent.model_metadata import estimate_messages_tokens_rough, estimate_tokens_rough

    request_totals = {
        "system_prompt": 0,
        "user": 0,
        "assistant": 0,
        "tool": 0,
        "other": 0,
    }
    compression_totals = {
        "instructions_and_framing": 0,
        "previous_summary": 0,
        "user": 0,
        "assistant": 0,
        "tool": 0,
        "other": 0,
    }
    available_compressions = 0
    unavailable_compressions = 0
    omitted_chunks = 0
    for tip in tips:
        session = db.get_session_for_recovery(tip, recovery_scope=recovery_scope)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        request_totals["system_prompt"] += estimate_tokens_rough(
            session.get("system_prompt") or ""
        )
        replay = db.get_messages_as_conversation(
            tip, include_ancestors=True, recovery_scope=recovery_scope
        )
        for message in replay:
            role = str(message.get("role") or "other")
            bucket = role if role in {"user", "assistant", "tool"} else "other"
            request_totals[bucket] += estimate_messages_tokens_rough([message])

        model = str(session.get("model") or "unknown")
        compressor = ContextCompressor(model=model, quiet_mode=True)
        compressor.compression_count = max(
            0,
            len(
                db._session_lineage_root_to_tip(
                    tip, recovery_scope=recovery_scope
                )
            )
            - 1,
        )
        reconstruction = compressor.reconstruct_next_compression_request(replay)
        if reconstruction["availability"] == "available":
            available_compressions += 1
            prompt_tokens = estimate_tokens_rough(
                str(reconstruction.get("prompt") or "")
            )
            known_component_tokens = 0
            for role, serialized in reconstruction.get("serialized_components", {}).items():
                bucket = role if role in {"user", "assistant", "tool"} else "other"
                value = estimate_tokens_rough(str(serialized or ""))
                compression_totals[bucket] += value
                known_component_tokens += value
            previous_summary_tokens = estimate_tokens_rough(
                str(reconstruction.get("previous_summary") or "")
            )
            compression_totals["previous_summary"] += previous_summary_tokens
            known_component_tokens += previous_summary_tokens
            compression_totals["instructions_and_framing"] += max(
                0, prompt_tokens - known_component_tokens
            )
            omitted_chunks += int(reconstruction.get("omitted_follow_up_chunks") or 0)
        else:
            unavailable_compressions += 1

    request_known_total = sum(request_totals.values())
    request_segments = [
        _composition_segment(key, label, request_totals[key], unit="rough_tokens", status="estimated", known_total=request_known_total)
        for key, label in (
            ("system_prompt", "Stored system prompt"),
            ("user", "Active user replay"),
            ("assistant", "Active assistant replay"),
            ("tool", "Active tool replay"),
            ("other", "Other active replay"),
        )
        if request_totals[key] > 0
    ]
    request_segments.append(
        _composition_segment("tool_definitions", "Tool definitions", None, unit="rough_tokens", status="unavailable", known_total=request_known_total)
    )
    coverage = {
        "requested_sessions": len(requested),
        "included_sessions": len(tips),
        "available_sessions": len(tips),
        "unavailable_sessions": 0,
    }
    request_limitations = [
        {"code": "tool_definitions_unavailable", "message": "Stored sessions do not retain the tool definitions for the next runtime request."},
        {"code": "runtime_injections_unavailable", "message": "Runtime-only rules, memory, skills, MCP data, and provider transformations are not stored with the session."},
    ]
    compression_limitations = []
    if unavailable_compressions:
        compression_limitations.append({
            "code": "no_next_compression_window",
            "message": f"No next compression request is currently available for {unavailable_compressions} session(s).",
        })
    if omitted_chunks:
        compression_limitations.append({
            "code": "follow_up_chunks_omitted",
            "message": f"{omitted_chunks} follow-up compression chunk(s) depend on earlier model output and are omitted.",
        })
    compression_availability = (
        "available" if available_compressions == len(tips)
        else "partial" if available_compressions else "unavailable"
    )
    compression_known_total = sum(compression_totals.values())
    compression_segments = [
        _composition_segment(
            key,
            label,
            compression_totals[key],
            unit="rough_tokens",
            status="estimated",
            known_total=compression_known_total,
        )
        for key, label in (
            ("instructions_and_framing", "Instructions and request framing"),
            ("previous_summary", "Previous summary"),
            ("user", "Serialized user turns"),
            ("assistant", "Serialized assistant turns"),
            ("tool", "Serialized tool results"),
            ("other", "Other serialized turns"),
        )
    ] if available_compressions else []
    charts = [
        {
            "id": "db_messages", "label": "Stored session messages", "availability": "available",
            "accuracy": "exact_count", "unit": "messages", "total": exact_total,
            "known_total": exact_total, "segments": exact_segments, "limitations": [], "coverage": coverage,
        },
        {
            "id": "main_model_request", "label": "Next main-model request", "availability": "partial",
            "accuracy": "rough_heuristic", "unit": "rough_tokens", "total": None,
            "known_total": request_known_total, "segments": request_segments,
            "limitations": request_limitations, "coverage": coverage,
        },
        {
            "id": "compression_request", "label": "Next compression request", "availability": compression_availability,
            "accuracy": "rough_heuristic" if available_compressions else "unavailable", "unit": "rough_tokens",
            "total": compression_known_total if available_compressions == len(tips) else None,
            "known_total": compression_known_total,
            "segments": compression_segments,
            "limitations": compression_limitations, "coverage": {
                **coverage, "available_sessions": available_compressions,
                "unavailable_sessions": unavailable_compressions,
            },
        },
    ]
    return {
        "schema_version": 1,
        "scope": {
            "requested_ids": requested,
            "canonical_session_count": len(tips),
            "canonical_root_ids": roots,
            "canonical_tip_ids": tips,
            "aggregation": "full_compression_lineage",
            "date_truncation": False,
        },
        "charts": charts,
        "limitations": request_limitations + compression_limitations,
        "coverage": coverage,
    }


def search_sessions_payload(
    db: Any,
    *,
    q: str = "",
    limit: int = 20,
    recovery_scope: dict[str, Any] | None = None,
    allowed_sources: list[str] | None = None,
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
        source_filter=allowed_sources,
        **search_scope,
    )
    terms = [token if token.startswith('"') or token.endswith("*") else token + "*" for token in re.findall(r'"[^"]*"|\S+', q.strip())]
    message_rows = db.search_messages(
        query=" ".join(terms),
        limit=max(safe_limit * 5, 50),
        source_filter=allowed_sources,
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


def rename_session_payload(
    db: Any,
    session_id: str,
    *,
    title: str | None = None,
    archived: bool | None = None,
    recovery_scope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    sid = db.resolve_session_id(session_id, recovery_scope=recovery_scope)
    if not sid:
        raise HTTPException(status_code=404, detail="Session not found")
    if title is None and archived is None:
        raise HTTPException(status_code=400, detail="Nothing to update; provide 'title' and/or 'archived'.")
    if title is not None:
        try:
            db.set_session_title(
                sid,
                title or "",
                recovery_scope=recovery_scope,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    if archived is not None:
        db.set_session_archived(
            sid,
            archived,
            recovery_scope=recovery_scope,
        )
    result: dict[str, Any] = {
        "ok": True,
        "title": (db.get_session_for_recovery(sid, recovery_scope=recovery_scope) or {}).get("title") or "",
    }
    if archived is not None:
        result["archived"] = bool(archived)
    return result


def delete_session_payload(
    db: Any,
    session_id: str,
    *,
    recovery_scope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    sid = db.resolve_session_id(session_id, recovery_scope=recovery_scope)
    if not sid:
        return {"ok": True, "already_absent": True}
    db.delete_session(sid, recovery_scope=recovery_scope)
    return {"ok": True}


def bulk_delete_payload(
    db: Any,
    ids: list[str],
    *,
    recovery_scope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if len(ids) > 500:
        raise HTTPException(status_code=400, detail="ids must contain at most 500 entries")
    scoped_ids = [
        sid
        for raw in ids
        if (sid := db.resolve_session_id(raw, recovery_scope=recovery_scope))
    ]
    return {
        "ok": True,
        "deleted": db.delete_sessions(
            scoped_ids,
            recovery_scope=recovery_scope,
        ),
    }


def empty_count_payload(
    db: Any,
    *,
    recovery_scope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if recovery_scope is None:
        return {"count": db.count_empty_sessions()}
    return {"count": db.count_empty_sessions(recovery_scope=recovery_scope)}


def delete_empty_payload(
    db: Any,
    *,
    recovery_scope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "ok": True,
        "deleted": db.delete_empty_sessions(recovery_scope=recovery_scope),
    }


def stats_payload(
    db: Any,
    *,
    recovery_scope: dict[str, Any] | None = None,
    allowed_sources: list[str] | None = None,
) -> dict[str, Any]:
    return db.session_stats(
        recovery_scope=recovery_scope,
        source_filter=allowed_sources,
    )


def prune_sessions_payload(
    db: Any,
    *,
    older_than_days: int = 90,
    source: str | None = None,
    sessions_dir: Path | None = None,
    recovery_scope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if older_than_days < 1:
        raise HTTPException(status_code=400, detail="older_than_days must be >= 1")
    return {
        "ok": True,
        "removed": db.prune_sessions(
            older_than_days=older_than_days,
            source=source or None,
            sessions_dir=sessions_dir if sessions_dir is not None and sessions_dir.exists() else None,
            recovery_scope=recovery_scope,
        ),
    }
