"""Tests for the bounded code-review Agent hook."""

import json
import os
from pathlib import Path
import subprocess
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK_PATH = REPO_ROOT / ".claude" / "hooks" / "block-review-agents.py"


def _tool_use(tool_use_id: str, *, review: bool = True) -> dict:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": tool_use_id,
                    "name": "Agent",
                    "input": {
                        "description": "Review changed code" if review else "Plan feature",
                        "prompt": "Review this diff" if review else "Design this feature",
                    },
                }
            ],
        },
    }


def _write_transcript(path: Path, records: list[dict]) -> None:
    path.write_text("".join(f"{json.dumps(record)}\n" for record in records))


def _run_hook(
    transcript_path: Path,
    *,
    tool_name: str = "Agent",
    tool_use_id: str = "current",
    review: bool = True,
    include_transcript: bool = True,
) -> subprocess.CompletedProcess[str]:
    tool_input = {
        "description": "Review changed code" if review else "Plan feature",
        "prompt": "Review this diff" if review else "Design this feature",
    }
    payload = {
        "tool_name": tool_name,
        "tool_use_id": tool_use_id,
        "tool_input": tool_input,
    }
    if include_transcript:
        payload["transcript_path"] = str(transcript_path)
    env = os.environ.copy()
    env["CLAUDE_PROJECT_DIR"] = str(REPO_ROOT)
    return subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        input=json.dumps(payload),
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )


def _denial_reason(result: subprocess.CompletedProcess[str]) -> str:
    output = json.loads(result.stdout)
    return output["hookSpecificOutput"]["permissionDecisionReason"]


def test_first_five_review_agent_calls_are_allowed(tmp_path):
    transcript = tmp_path / "session.jsonl"
    records = [
        {
            "type": "user",
            "message": {"role": "user", "content": "Please review this PR"},
        },
        *[_tool_use(f"review-{index}") for index in range(4)],
        _tool_use("current"),
    ]
    _write_transcript(transcript, records)

    result = _run_hook(transcript)

    assert result.stdout == ""


def test_sixth_review_agent_call_is_blocked(tmp_path):
    transcript = tmp_path / "session.jsonl"
    records = [
        {
            "type": "user",
            "message": {"role": "user", "content": "Please review this PR"},
        },
        *[_tool_use(f"review-{index}") for index in range(5)],
        _tool_use("current"),
    ]
    _write_transcript(transcript, records)

    reason = _denial_reason(_run_hook(transcript))

    assert "at most 5 Agent calls" in reason
    assert "budget is exhausted" in reason


def test_new_user_request_resets_the_count(tmp_path):
    transcript = tmp_path / "session.jsonl"
    records = [
        {
            "type": "user",
            "message": {"role": "user", "content": "Review the first PR"},
        },
        *[_tool_use(f"old-{index}") for index in range(5)],
        {
            "type": "user",
            "message": {"role": "user", "content": "Continue with another PR"},
        },
        _tool_use("current"),
    ]
    _write_transcript(transcript, records)

    result = _run_hook(transcript)

    assert result.stdout == ""


def test_tool_results_do_not_reset_the_count(tmp_path):
    transcript = tmp_path / "session.jsonl"
    records = [
        {
            "type": "user",
            "message": {"role": "user", "content": "Please review this PR"},
        },
        *[
            entry
            for index in range(5)
            for entry in (
                _tool_use(f"review-{index}"),
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": f"review-{index}",
                                "content": "done",
                            }
                        ],
                    },
                },
            )
        ],
        _tool_use("current"),
    ]
    _write_transcript(transcript, records)

    reason = _denial_reason(_run_hook(transcript))

    assert "budget is exhausted" in reason


def test_all_agents_in_review_request_consume_budget(tmp_path):
    transcript = tmp_path / "session.jsonl"
    records = [
        {
            "type": "user",
            "message": {"role": "user", "content": "Please review this PR"},
        },
        *[_tool_use(f"plan-{index}", review=False) for index in range(5)],
        _tool_use("current"),
    ]
    _write_transcript(transcript, records)

    reason = _denial_reason(_run_hook(transcript))

    assert "budget is exhausted" in reason


def test_non_review_request_agents_do_not_consume_review_budget(tmp_path):
    transcript = tmp_path / "session.jsonl"
    records = [
        {
            "type": "user",
            "message": {"role": "user", "content": "Plan this feature"},
        },
        *[_tool_use(f"plan-{index}", review=False) for index in range(6)],
        _tool_use("current"),
    ]
    _write_transcript(transcript, records)

    result = _run_hook(transcript)

    assert result.stdout == ""


def test_review_workflow_is_always_blocked(tmp_path):
    transcript = tmp_path / "session.jsonl"
    _write_transcript(transcript, [])

    reason = _denial_reason(_run_hook(transcript, tool_name="Workflow"))

    assert "Workflow orchestration is not allowed" in reason


def test_missing_transcript_fails_closed_for_review_agent(tmp_path):
    reason = _denial_reason(
        _run_hook(tmp_path / "missing.jsonl", include_transcript=False)
    )

    assert "could not determine" in reason


def test_non_review_agent_is_not_intercepted(tmp_path):
    result = _run_hook(
        tmp_path / "missing.jsonl", review=False, include_transcript=False
    )

    assert result.stdout == ""


def test_settings_apply_hook_to_agents_and_workflows():
    settings = json.loads((REPO_ROOT / ".claude" / "settings.json").read_text())
    review_hooks = [
        entry
        for entry in settings["hooks"]["PreToolUse"]
        if entry.get("matcher") == "Agent|Workflow"
    ]

    assert len(review_hooks) == 1
    hook = review_hooks[0]["hooks"][0]
    assert hook["command"] == "python"
    assert hook["args"][0] == "-c"
    assert "block-review-agents.py" in " ".join(hook["args"])
