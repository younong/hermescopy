#!/usr/bin/env python3
"""Automatically approve only Claude Code's ExitPlanMode permission request."""
from __future__ import annotations

import json
import sys
from typing import Any


ALLOW_DECISION = {
    "hookSpecificOutput": {
        "hookEventName": "PermissionRequest",
        "decision": {"behavior": "allow"},
    }
}


def main() -> int:
    try:
        payload: Any = json.loads((sys.stdin.read() or "").lstrip("﻿"))
    except json.JSONDecodeError as exc:
        print(json.dumps({"systemMessage": f"ExitPlanMode approval hook skipped: {exc}"}))
        return 0

    if (
        isinstance(payload, dict)
        and payload.get("hook_event_name") == "PermissionRequest"
        and payload.get("tool_name") == "ExitPlanMode"
    ):
        print(json.dumps(ALLOW_DECISION))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
