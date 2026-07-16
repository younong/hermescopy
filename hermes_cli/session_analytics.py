"""Session analytics helpers shared by Control Plane and Owner Workers."""
from __future__ import annotations

import time
from typing import Any, Dict, List


def usage_analytics_from_db(db: Any, days: int = 30) -> dict[str, Any]:
    from agent.insights import InsightsEngine

    cutoff = time.time() - (days * 86400)
    cur = db._conn.execute("""
        SELECT date(started_at, 'unixepoch') as day,
               SUM(input_tokens) as input_tokens,
               SUM(output_tokens) as output_tokens,
               SUM(cache_read_tokens) as cache_read_tokens,
               SUM(reasoning_tokens) as reasoning_tokens,
               COALESCE(SUM(estimated_cost_usd), 0) as estimated_cost,
               COALESCE(SUM(actual_cost_usd), 0) as actual_cost,
               COUNT(*) as sessions,
               SUM(COALESCE(api_call_count, 0)) as api_calls
        FROM sessions WHERE started_at > ?
        GROUP BY day ORDER BY day
    """, (cutoff,))
    daily = [dict(r) for r in cur.fetchall()]

    cur2 = db._conn.execute("""
        SELECT model,
               SUM(input_tokens) as input_tokens,
               SUM(output_tokens) as output_tokens,
               COALESCE(SUM(estimated_cost_usd), 0) as estimated_cost,
               COUNT(*) as sessions,
               SUM(COALESCE(api_call_count, 0)) as api_calls
        FROM sessions WHERE started_at > ? AND model IS NOT NULL
        GROUP BY model ORDER BY SUM(input_tokens) + SUM(output_tokens) DESC
    """, (cutoff,))
    by_model = [dict(r) for r in cur2.fetchall()]

    cur3 = db._conn.execute("""
        SELECT SUM(input_tokens) as total_input,
               SUM(output_tokens) as total_output,
               SUM(cache_read_tokens) as total_cache_read,
               SUM(reasoning_tokens) as total_reasoning,
               COALESCE(SUM(estimated_cost_usd), 0) as total_estimated_cost,
               COALESCE(SUM(actual_cost_usd), 0) as total_actual_cost,
               COUNT(*) as total_sessions,
               SUM(COALESCE(api_call_count, 0)) as total_api_calls
        FROM sessions WHERE started_at > ?
    """, (cutoff,))
    totals = dict(cur3.fetchone())
    insights_report = InsightsEngine(db).generate(days=days)
    skills = insights_report.get("skills", {
        "summary": {
            "total_skill_loads": 0,
            "total_skill_edits": 0,
            "total_skill_actions": 0,
            "distinct_skills_used": 0,
        },
        "top_skills": [],
    })

    return {
        "daily": daily,
        "by_model": by_model,
        "totals": totals,
        "period_days": days,
        "skills": skills,
    }


def models_analytics_from_db(db: Any, days: int = 30) -> dict[str, Any]:
    cutoff = time.time() - (days * 86400)

    cur = db._conn.execute("""
        SELECT model,
               billing_provider,
               SUM(input_tokens) as input_tokens,
               SUM(output_tokens) as output_tokens,
               SUM(cache_read_tokens) as cache_read_tokens,
               SUM(reasoning_tokens) as reasoning_tokens,
               COALESCE(SUM(estimated_cost_usd), 0) as estimated_cost,
               COALESCE(SUM(actual_cost_usd), 0) as actual_cost,
               COUNT(*) as sessions,
               SUM(COALESCE(api_call_count, 0)) as api_calls,
               SUM(tool_call_count) as tool_calls,
               MAX(started_at) as last_used_at,
               AVG(input_tokens + output_tokens) as avg_tokens_per_session
        FROM sessions WHERE started_at > ? AND model IS NOT NULL AND model != ''
        GROUP BY model, billing_provider
        ORDER BY SUM(input_tokens) + SUM(output_tokens) DESC
    """, (cutoff,))
    raw_rows = [dict(r) for r in cur.fetchall()]

    rows_by_model: Dict[str, List[Dict[str, Any]]] = {}
    for row in raw_rows:
        rows_by_model.setdefault(row.get("model") or "", []).append(row)

    rows: List[Dict[str, Any]] = []
    for model_rows in rows_by_model.values():
        provider_rows = [r for r in model_rows if r.get("billing_provider")]
        if len(provider_rows) == 1:
            target = provider_rows[0]
            for row in model_rows:
                if row is target or row.get("billing_provider"):
                    continue
                has_usage = any(
                    (row.get(key) or 0) != 0
                    for key in (
                        "input_tokens",
                        "output_tokens",
                        "cache_read_tokens",
                        "reasoning_tokens",
                        "estimated_cost",
                        "actual_cost",
                        "api_calls",
                        "tool_calls",
                    )
                )
                if has_usage:
                    continue
                target["sessions"] = (target.get("sessions") or 0) + (row.get("sessions") or 0)
                target["last_used_at"] = max(target.get("last_used_at") or 0, row.get("last_used_at") or 0)
                total_tokens = (target.get("input_tokens") or 0) + (target.get("output_tokens") or 0)
                sessions = target.get("sessions") or 0
                target["avg_tokens_per_session"] = total_tokens / sessions if sessions else 0
            rows.append(target)
            rows.extend(
                r for r in model_rows
                if r is not target
                and (r.get("billing_provider") or any(
                    (r.get(key) or 0) != 0
                    for key in (
                        "input_tokens",
                        "output_tokens",
                        "cache_read_tokens",
                        "reasoning_tokens",
                        "estimated_cost",
                        "actual_cost",
                        "api_calls",
                        "tool_calls",
                    )
                ))
            )
        else:
            rows.extend(model_rows)

    rows.sort(
        key=lambda r: (r.get("input_tokens") or 0) + (r.get("output_tokens") or 0),
        reverse=True,
    )

    models = []
    for row in rows:
        provider = row.get("billing_provider") or ""
        model_name = row["model"]
        caps = {}
        try:
            from agent.models_dev import get_model_capabilities

            mc = get_model_capabilities(provider=provider, model=model_name)
            if mc is not None:
                caps = {
                    "supports_tools": mc.supports_tools,
                    "supports_vision": mc.supports_vision,
                    "supports_reasoning": mc.supports_reasoning,
                    "context_window": mc.context_window,
                    "max_output_tokens": mc.max_output_tokens,
                    "model_family": mc.model_family,
                }
        except Exception:
            pass

        models.append({
            "model": model_name,
            "provider": provider,
            "input_tokens": row["input_tokens"],
            "output_tokens": row["output_tokens"],
            "cache_read_tokens": row["cache_read_tokens"],
            "reasoning_tokens": row["reasoning_tokens"],
            "estimated_cost": row["estimated_cost"],
            "actual_cost": row["actual_cost"],
            "sessions": row["sessions"],
            "api_calls": row["api_calls"],
            "tool_calls": row["tool_calls"],
            "last_used_at": row["last_used_at"],
            "avg_tokens_per_session": row["avg_tokens_per_session"],
            "capabilities": caps,
        })

    totals_cur = db._conn.execute("""
        SELECT COUNT(DISTINCT model) as distinct_models,
               SUM(input_tokens) as total_input,
               SUM(output_tokens) as total_output,
               SUM(cache_read_tokens) as total_cache_read,
               SUM(reasoning_tokens) as total_reasoning,
               COALESCE(SUM(estimated_cost_usd), 0) as total_estimated_cost,
               COALESCE(SUM(actual_cost_usd), 0) as total_actual_cost,
               COUNT(*) as total_sessions,
               SUM(COALESCE(api_call_count, 0)) as total_api_calls
        FROM sessions WHERE started_at > ? AND model IS NOT NULL AND model != ''
    """, (cutoff,))
    totals = dict(totals_cur.fetchone())

    return {
        "models": models,
        "totals": totals,
        "period_days": days,
    }
