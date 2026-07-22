"""Behavioral contract tests for the bundled PowerPoint skill."""

from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_DIR = REPO_ROOT / "skills" / "productivity" / "powerpoint"


def _read(name: str) -> str:
    return (SKILL_DIR / name).read_text(encoding="utf-8")


def test_powerpoint_frontmatter_and_routing() -> None:
    content = _read("SKILL.md")
    _, raw, _ = content.split("---", 2)
    frontmatter = yaml.safe_load(raw)

    assert frontmatter["name"] == "powerpoint"
    assert set(frontmatter["platforms"]) == {"linux", "macos", "windows"}
    assert "pptx" in frontmatter["description"]
    assert "editing.md" in content
    assert "pptxgenjs.md" in content


def test_default_workflow_is_fast_and_deterministic() -> None:
    content = _read("SKILL.md")
    fast = content.split("## Default Fast Workflow", 1)[1].split("## Reading Content", 1)[0]
    validation = content.split("## Default Validation", 1)[1].split(
        "## Thorough Visual QA", 1
    )[0]

    assert "2-4 substantive model decisions" in fast
    assert "Do not use `delegate_task`, `pdftoppm`, or web research" in fast
    assert "python -m markitdown" in validation
    assert "grep -iE" in validation
    assert "soffice.py --headless --convert-to pdf" in validation
    assert "Fix and repeat only a check that found a concrete defect" in validation


def test_visual_qa_is_opt_in_and_uses_validated_artifacts() -> None:
    content = _read("SKILL.md")
    thorough = content.split("## Thorough Visual QA (Opt-in)", 1)[1]

    assert "only when the user explicitly asks" in thorough
    assert "delegate_task.artifact_paths" in thorough
    assert "Never synthesize `/workspace/<name>`" in thorough
    assert "Pass those real files in `artifact_paths`" in thorough


def test_supporting_guides_preserve_the_fast_default() -> None:
    creation = _read("pptxgenjs.md")
    editing = _read("editing.md")

    assert "## Fast Creation Contract" in creation
    assert "one PptxGenJS program" in creation
    assert "Do not split slide generation across subagents" in creation
    assert "For ordinary decks, edit the slides directly without delegation" in editing
    assert "Large/thorough workflows only" in editing
    assert "delegate_task.artifact_paths" in editing
