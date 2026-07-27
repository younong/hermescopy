#!/usr/bin/env python3
"""Enforce the Claude Code subagent budget for code reviews."""

import json
from pathlib import Path
import re
import sys
from typing import Any


MAX_REVIEW_AGENT_CALLS = 5
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


def _searchable(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def is_review_tool_input(tool_input: dict[str, Any]) -> bool:
    searchable = "\n".join(
        _searchable(tool_input.get(key, ""))
        for key in (
            "description",
            "prompt",
            "subagent_type",
            "name",
            "script",
            "args",
        )
    )
    return bool(REVIEW_PATTERN.search(searchable))


def _user_request(record: dict[str, Any]) -> str | None:
    if record.get("isSidechain"):
        return None
    message = record.get("message")
    if not isinstance(message, dict) or message.get("role") != "user":
        return None
    content = message.get("content")
    return content if isinstance(content, str) else None


def review_context_before(
    transcript_path: Path, tool_use_id: str
) -> tuple[bool, int]:
    request_is_review = False
    count = 0
    with transcript_path.open(encoding="utf-8", errors="replace") as transcript:
        for line in transcript:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            user_request = _user_request(record)
            if user_request is not None:
                request_is_review = bool(REVIEW_PATTERN.search(user_request))
                count = 0
                continue

            message = record.get("message")
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                if block.get("id") == tool_use_id:
                    return request_is_review, count
                if block.get("name") == "Agent":
                    count += 1
    return request_is_review, count


def deny(reason: str) -> None:
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


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        return 0

    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return 0

    tool_name = payload.get("tool_name")
    input_is_review = is_review_tool_input(tool_input)
    if tool_name == "Workflow" and input_is_review:
        deny(
            "Blocked review workflow: Hermes code reviews may use at most 5 "
            "direct Agent calls from the main conversation; Workflow orchestration "
            "is not allowed."
        )
        return 0
    if tool_name != "Agent":
        return 0

    transcript_path = payload.get("transcript_path")
    if not isinstance(transcript_path, str) or not transcript_path:
        if input_is_review:
            deny(
                "Blocked review subagent because the hook could not determine the "
                "current review's Agent usage from transcript_path."
            )
        return 0

    try:
        request_is_review, calls = review_context_before(
            Path(transcript_path), str(payload.get("tool_use_id", ""))
        )
    except OSError:
        deny(
            "Blocked review subagent because the hook could not read the session "
            "transcript to enforce the 5-call review Agent budget."
        )
        return 0

    if request_is_review and calls >= MAX_REVIEW_AGENT_CALLS:
        deny(
            "Blocked review subagent: Hermes code reviews allow at most 5 Agent "
            "calls per user review request, and that budget is exhausted. Verify "
            "and synthesize the remaining work in the main conversation."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
