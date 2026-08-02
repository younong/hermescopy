"""Canonical live session context-window breakdown for UI surfaces."""

from __future__ import annotations

from typing import Any, Dict

from agent.prepared_model_request import prepared_context_payload

_CATEGORY_COLORS = {
    "system_prompt": "var(--context-usage-system)",
    "tool_definitions": "var(--context-usage-tools)",
    "rules": "var(--context-usage-rules)",
    "skills": "var(--context-usage-skills)",
    "mcp": "var(--context-usage-mcp)",
    "subagent_definitions": "var(--context-usage-subagents)",
    "memory": "var(--context-usage-memory)",
    "conversation": "var(--context-usage-conversation)",
    "provider_overhead": "var(--ui-text-tertiary)",
}


def compute_session_context_breakdown(agent: Any) -> Dict[str, Any]:
    """Format the last dispatch-ready provider request for first-party UIs."""
    payload = prepared_context_payload(
        getattr(agent, "_prepared_model_request", None)
    )
    if payload is None:
        return {
            "categories": [],
            "context_max": 0,
            "context_percent": 0,
            "context_used": 0,
            "estimated_total": 0,
            "model": getattr(agent, "model", "") or "",
            "accounting_source": "unknown",
        }

    payload["categories"] = [
        {
            **category,
            "color": _CATEGORY_COLORS.get(
                category["id"], "var(--ui-text-tertiary)"
            ),
        }
        for category in payload["categories"]
    ]
    return payload
