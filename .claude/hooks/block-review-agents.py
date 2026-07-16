#!/usr/bin/env python3
"""Block Claude Code subagents used to fan out code reviews."""

import json
import re
import sys


REVIEW_PATTERN = re.compile(
    r"(?:"
    r"\bcode[ -]?review\b|"
    r"\breview(?:ing)?\b.{0,80}\b(?:diff|change|commit|branch|pr|pull request|finding|candidate)\b|"
    r"\b(?:diff|change|commit|branch|pr|pull request|finding|candidate)\b.{0,80}\breview(?:ing)?\b|"
    r"\bscan\b.{0,30}\bdiff\b|"
    r"\baudit\b.{0,30}\bremoved\b|"
    r"\b(?:verify|verifier|refute)\b.{0,80}\b(?:finding|candidate|defect|bug)\b|"
    r"\b(?:finding|candidate|defect|bug)\b.{0,80}\b(?:verify|verifier|refute)\b|"
    r"CONFIRMED\s*/\s*PLAUSIBLE\s*/\s*REFUTED"
    r")",
    re.IGNORECASE | re.DOTALL,
)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        return 0

    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return 0

    searchable = "\n".join(
        str(tool_input.get(key, ""))
        for key in ("description", "prompt", "subagent_type")
    )
    if not REVIEW_PATTERN.search(searchable):
        return 0

    reason = (
        "Blocked review subagent: Hermes code reviews have a global Agent "
        "budget of 0. Review and verify serially in the main conversation."
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
