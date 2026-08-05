"""Session sources retained by authenticated owner execution surfaces."""

from __future__ import annotations

from typing import Any


RETAINED_SESSION_SOURCES = frozenset(
    {
        "cron",
        "dashboard-gui",
        "feishu",
        "openai-api",
        "webhook",
        "weixin-ilink",
    }
)


def is_retained_session_source(value: Any) -> bool:
    return str(value or "").strip().lower() in RETAINED_SESSION_SOURCES


def retained_recovery_scope(scope: dict[str, Any]) -> dict[str, Any]:
    return {**scope, "source_filter": sorted(RETAINED_SESSION_SOURCES)}
