"""Contracts for the bundled Volcengine Agent Plan embedding skill."""

from __future__ import annotations

import re
from pathlib import Path

import yaml


SKILL_DIR = (
    Path(__file__).resolve().parents[2]
    / "skills"
    / "mlops"
    / "inference"
    / "volcengine-agent-plan"
)


def test_skill_metadata_and_script_are_bundled():
    text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    match = re.match(r"---\n(.*?)\n---", text, re.DOTALL)
    assert match is not None
    metadata = yaml.safe_load(match.group(1))
    assert metadata["name"] == "volcengine-agent-plan"
    assert len(metadata["description"]) <= 60
    assert metadata["platforms"] == ["linux", "macos", "windows"]
    assert (SKILL_DIR / "scripts" / "embed.py").is_file()


def test_skill_does_not_tell_users_to_pass_secret_on_command_line():
    text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert "--api-key" not in text
    assert "VOLCENGINE_AGENT_PLAN_API_KEY" in text
    assert "/embeddings/multimodal" not in text
