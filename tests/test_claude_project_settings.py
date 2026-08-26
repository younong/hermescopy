"""Tests for repository-scoped Claude Code configuration."""

import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_project_limits_concurrent_agent_subagents_to_two():
    settings = json.loads((REPO_ROOT / ".claude" / "settings.json").read_text())

    assert settings["env"]["CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS"] == "2"


def test_claude_entrypoint_imports_canonical_agent_guide():
    instructions = (REPO_ROOT / "CLAUDE.md").read_text().splitlines()

    assert "@AGENTS.md" in (line.strip() for line in instructions)
