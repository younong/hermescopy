#!/usr/bin/env python3
"""Block repository edits from the primary checkout."""

import json
import os
from pathlib import Path
import subprocess
import sys


def git_path(project_dir: Path, argument: str) -> Path | None:
    try:
        output = subprocess.check_output(
            ["git", "-C", str(project_dir), "rev-parse", argument],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None

    path = Path(output)
    if not path.is_absolute():
        path = project_dir / path
    return path.resolve()


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        return 0

    project_dir_value = os.environ.get("CLAUDE_PROJECT_DIR")
    if not project_dir_value:
        return 0
    project_dir = Path(project_dir_value).resolve()

    git_dir = git_path(project_dir, "--git-dir")
    common_dir = git_path(project_dir, "--git-common-dir")
    if git_dir is None or common_dir is None or git_dir != common_dir:
        return 0

    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return 0
    target_value = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not isinstance(target_value, str) or not target_value:
        return 0

    target = Path(target_value)
    if not target.is_absolute():
        target = project_dir / target
    target = target.resolve()
    try:
        target.relative_to(project_dir)
    except ValueError:
        return 0

    reason = (
        "Repository edits must be made in a dedicated Claude Code worktree "
        "created from fresh origin/main. Call EnterWorktree before editing."
    )
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
