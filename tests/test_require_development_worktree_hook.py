"""Tests for the Claude Code primary-checkout edit guard."""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK_PATH = REPO_ROOT / ".claude" / "hooks" / "require-development-worktree.py"


def _git(repo: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )


def _new_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "tracked.txt").write_text("initial\n")
    _git(repo, "add", "tracked.txt")
    _git(
        repo,
        "-c",
        "user.name=Hook Test",
        "-c",
        "user.email=hook@example.invalid",
        "commit",
        "-m",
        "Initial commit",
    )
    return repo


def _add_worktree(
    repo: Path,
    name: str,
    *,
    location: Path | None = None,
    detach: bool = False,
) -> Path:
    worktree = location or repo / ".claude" / "worktrees" / name
    worktree.parent.mkdir(parents=True, exist_ok=True)
    if detach:
        _git(repo, "worktree", "add", "--detach", str(worktree), "HEAD")
    else:
        branch_name = f"worktree-{name.replace(' ', '-')}"
        _git(repo, "worktree", "add", "-b", branch_name, str(worktree))
    return worktree


def _run_hook(
    project_dir: Path,
    target: Path | str,
    *,
    raw_payload: str | None = None,
    path_prefix: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    payload = raw_payload
    if payload is None:
        payload = json.dumps(
            {
                "tool_name": "Write",
                "tool_input": {"file_path": str(target)},
            }
        )
    env = os.environ.copy()
    env["CLAUDE_PROJECT_DIR"] = str(project_dir)
    if path_prefix is not None:
        env["PATH"] = f"{path_prefix}{os.pathsep}{env.get('PATH', '')}"
    return subprocess.run(
        ["python3", str(HOOK_PATH)],
        input=payload,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )


def _denial_reason(result: subprocess.CompletedProcess[str]) -> str:
    output = json.loads(result.stdout)
    hook_output = output["hookSpecificOutput"]
    assert hook_output["permissionDecision"] == "deny"
    return hook_output["permissionDecisionReason"]


def _load_hook_module():
    spec = importlib.util.spec_from_file_location("worktree_hook", HOOK_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_primary_checkout_denial_lists_existing_worktree(tmp_path):
    repo = _new_repo(tmp_path)
    worktree = _add_worktree(repo, "recover-task")

    reason = _denial_reason(_run_hook(repo, repo / "new-file.txt"))

    assert str(worktree) in reason
    assert "worktree-recover-task" in reason
    assert 'EnterWorktree(path="<exact path>")' in reason
    assert "Context compaction or session resumption" in reason


def test_primary_checkout_denial_requires_ambiguous_candidates_to_be_resolved(
    tmp_path,
):
    repo = _new_repo(tmp_path)
    first = _add_worktree(repo, "first-task")
    second = _add_worktree(repo, "second-task")

    reason = _denial_reason(_run_hook(repo, repo / "tracked.txt"))

    assert str(first) in reason
    assert str(second) in reason
    assert "If candidates are ambiguous, do not create another worktree" in reason
    assert "ask the user if it is still unclear" in reason


def test_primary_checkout_without_candidates_remains_blocked(tmp_path):
    repo = _new_repo(tmp_path)

    reason = _denial_reason(_run_hook(repo, repo / "tracked.txt"))

    assert "No registered Claude Code worktree candidates were found" in reason
    assert "Only after confirming" in reason


def test_edit_targeting_linked_worktree_is_allowed(tmp_path):
    repo = _new_repo(tmp_path)
    worktree = _add_worktree(repo, "active-task")

    result = _run_hook(repo, worktree / "new-file.txt")

    assert result.stdout == ""


def test_outside_target_is_not_intercepted(tmp_path):
    repo = _new_repo(tmp_path)

    result = _run_hook(repo, tmp_path / "outside.txt")

    assert result.stdout == ""


def test_only_registered_claude_worktrees_are_listed(tmp_path):
    repo = _new_repo(tmp_path)
    listed = _add_worktree(repo, "listed")
    unregistered = repo / ".claude" / "worktrees" / "unregistered"
    unregistered.mkdir(parents=True)
    external = _add_worktree(
        repo,
        "external",
        location=tmp_path / "external-worktree",
    )

    reason = _denial_reason(_run_hook(repo, repo / "tracked.txt"))

    assert str(listed) in reason
    assert str(unregistered) not in reason
    assert str(external) not in reason


def test_detached_and_locked_worktrees_are_described(tmp_path):
    repo = _new_repo(tmp_path)
    worktree = _add_worktree(repo, "detached task", detach=True)
    _git(repo, "worktree", "lock", "--reason", "test lock", str(worktree))

    reason = _denial_reason(_run_hook(repo, repo / "tracked.txt"))

    assert str(worktree) in reason
    assert 'branch: "detached"; locked' in reason


def test_worktree_path_is_json_quoted(tmp_path):
    repo = _new_repo(tmp_path)
    worktree = _add_worktree(repo, "task with spaces")

    reason = _denial_reason(_run_hook(repo, repo / "tracked.txt"))

    assert json.dumps(str(worktree)) in reason


def test_missing_registered_worktree_is_not_recommended(tmp_path):
    repo = _new_repo(tmp_path)
    worktree = _add_worktree(repo, "missing")
    shutil.rmtree(worktree)

    reason = _denial_reason(_run_hook(repo, repo / "tracked.txt"))

    assert str(worktree) not in reason
    assert "No registered Claude Code worktree candidates were found" in reason


def test_worktree_discovery_failure_keeps_edit_blocked(tmp_path):
    repo = _new_repo(tmp_path)
    wrapper_dir = tmp_path / "bin"
    wrapper_dir.mkdir()
    wrapper = wrapper_dir / "git"
    real_git = shutil.which("git")
    assert real_git is not None
    wrapper.write_text(
        "#!/bin/sh\n"
        "case \"$*\" in\n"
        "  *'worktree list'*) exit 1 ;;\n"
        f"  *) exec {json.dumps(real_git)} \"$@\" ;;\n"
        "esac\n"
    )
    wrapper.chmod(0o755)

    reason = _denial_reason(
        _run_hook(repo, repo / "tracked.txt", path_prefix=wrapper_dir)
    )

    assert "Registered worktree discovery failed" in reason


def test_unverifiable_repository_target_fails_closed(tmp_path):
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    reason = _denial_reason(_run_hook(project_dir, project_dir / "file.txt"))

    assert "could not verify" in reason


def test_malformed_payload_does_not_crash(tmp_path):
    repo = _new_repo(tmp_path)

    result = _run_hook(repo, repo / "tracked.txt", raw_payload="not json")

    assert result.stdout == ""


def test_parser_handles_porcelain_flags_and_final_record():
    hook = _load_hook_module()
    output = (
        "worktree /repo\0HEAD abc\0branch refs/heads/main\0\0"
        "worktree /repo/task\0HEAD def\0detached\0locked test lock\0"
    )

    assert hook.parse_worktrees(output) == [
        {
            "worktree": "/repo",
            "HEAD": "abc",
            "branch": "refs/heads/main",
        },
        {
            "worktree": "/repo/task",
            "HEAD": "def",
            "detached": True,
            "locked": "test lock",
        },
    ]


def test_candidate_output_is_bounded():
    hook = _load_hook_module()
    candidates = [
        hook.WorktreeCandidate(Path(f"/repo/.claude/worktrees/task-{index}"), "main")
        for index in range(hook.MAX_CANDIDATES + 3)
    ]

    reason = hook.denial_reason(candidates)

    assert "3 additional candidate(s) omitted" in reason
    assert "task-19" in reason
    assert "task-20" not in reason


def test_settings_keep_edit_hook_and_fresh_base():
    settings = json.loads((REPO_ROOT / ".claude" / "settings.json").read_text())

    assert settings["worktree"]["baseRef"] == "fresh"
    edit_hooks = [
        entry
        for entry in settings["hooks"]["PreToolUse"]
        if entry.get("matcher") == "Write|Edit|NotebookEdit"
    ]
    assert len(edit_hooks) == 1
    assert "require-development-worktree.py" in edit_hooks[0]["hooks"][0]["command"]
