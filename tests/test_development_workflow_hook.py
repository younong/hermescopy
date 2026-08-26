from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / ".claude" / "hooks" / "development-workflow.py"

spec = importlib.util.spec_from_file_location("development_workflow", HOOK)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


def _git(repo: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _repositories(tmp_path: Path, session_id: str = "task-session") -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    worktree = repo / ".claude" / "worktrees" / "task"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Hook Test")
    _git(repo, "config", "user.email", "hook@example.invalid")
    (repo / "tracked.txt").write_text("initial\n")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "initial")
    worktree.parent.mkdir(parents=True)
    _git(repo, "worktree", "add", "-b", "worktree-task", str(worktree))
    git_dir = Path(_git(worktree, "rev-parse", "--git-dir"))
    if not git_dir.is_absolute():
        git_dir = worktree / git_dir
    (git_dir.resolve() / "claude-task-owner.json").write_text(
        json.dumps({"version": 1, "task_id": session_id}) + "\n"
    )
    return repo, worktree


def test_permission_request_allows_exit_plan_mode():
    output = module.process_hook(
        {
            "hook_event_name": "PermissionRequest",
            "tool_name": "ExitPlanMode",
            "session_id": "task-session",
        }
    )

    decision = output["hookSpecificOutput"]["decision"]
    assert decision == {"behavior": "allow"}
    assert "updatedInput" not in decision


def test_single_permission_suggestion_is_preserved():
    suggestion = {"type": "addRules", "rules": [{"toolName": "Bash"}]}

    output = module.permission_output({"permission_suggestions": [suggestion]})

    assert output["hookSpecificOutput"]["decision"]["updatedPermissions"] == [suggestion]


def test_multiple_permission_suggestions_are_not_applied_blindly():
    output = module.permission_output({"permission_suggestions": [{"one": 1}, {"two": 2}]})

    assert "updatedPermissions" not in output["hookSpecificOutput"]["decision"]


def test_post_exit_plan_records_state_and_requests_continuation(tmp_path):
    env = {"CLAUDE_DEVELOPMENT_WORKFLOW_STATE_DIR": str(tmp_path / "state")}
    payload = {
        "hook_event_name": "PostToolUse",
        "tool_name": "ExitPlanMode",
        "session_id": "task-session",
        "cwd": str(tmp_path),
    }

    output = module.process_hook(payload, env)

    context = output["hookSpecificOutput"]["additionalContext"]
    assert "EnterWorktree" in context
    assert "Continue implementation now" in context
    assert "development-workflow.py verify" in context
    assert "Stop hook" in context
    assert module.read_plan("task-session", env)["approved"] is True


def test_successful_verification_matches_exact_snapshot(tmp_path):
    session_id = "task-session"
    _repo, worktree = _repositories(tmp_path, session_id)
    env = {"CLAUDE_DEVELOPMENT_WORKFLOW_STATE_DIR": str(tmp_path / "state")}
    module.record_plan_approved({"session_id": session_id, "cwd": str(worktree)}, env)
    (worktree / "tracked.txt").write_text("changed\n")
    (worktree / "untracked.txt").write_text("new\n")

    record = module.write_verification(worktree, session_id, ["python", "-m", "pytest"], env)
    valid, reason, loaded = module.verification_status(worktree, session_id, env)

    assert valid is True
    assert "matches" in reason
    assert loaded == record


def test_tracked_or_untracked_change_makes_verification_stale(tmp_path):
    session_id = "task-session"
    _repo, worktree = _repositories(tmp_path, session_id)
    env = {"CLAUDE_DEVELOPMENT_WORKFLOW_STATE_DIR": str(tmp_path / "state")}
    module.write_verification(worktree, session_id, ["true"], env)

    (worktree / "tracked.txt").write_text("changed\n")
    assert module.verification_status(worktree, session_id, env)[0] is False

    (worktree / "tracked.txt").write_text("initial\n")
    module.write_verification(worktree, session_id, ["true"], env)
    (worktree / "untracked.txt").write_text("new\n")
    valid, reason, _record = module.verification_status(worktree, session_id, env)
    assert valid is False
    assert "changed after verification" in reason


def test_verify_cli_records_only_success(tmp_path):
    session_id = "task-session"
    _repo, worktree = _repositories(tmp_path, session_id)
    state = tmp_path / "state"
    env = os.environ.copy()
    env["CLAUDE_CODE_SESSION_ID"] = session_id
    env["CLAUDE_DEVELOPMENT_WORKFLOW_STATE_DIR"] = str(state)

    failed = subprocess.run(
        [sys.executable, str(HOOK), "verify", "--", sys.executable, "-c", "raise SystemExit(7)"],
        cwd=worktree,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert failed.returncode == 7
    assert module.read_verification(worktree, session_id, env) is None

    passed = subprocess.run(
        [sys.executable, str(HOOK), "verify", "--", sys.executable, "-c", "print('ok')"],
        cwd=worktree,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert passed.returncode == 0, passed.stderr
    assert "Recorded successful verification" in passed.stdout
    assert module.verification_status(worktree, session_id, env)[0] is True


def test_settings_wire_one_portable_sequential_workflow():
    settings = json.loads((ROOT / ".claude" / "settings.json").read_text())
    hooks = settings["hooks"]

    plan_permissions = [
        entry for entry in hooks["PermissionRequest"]
        if entry.get("matcher") == "ExitPlanMode"
    ]
    plan_continuations = [
        entry for entry in hooks["PostToolUse"]
        if entry.get("matcher") == "ExitPlanMode"
    ]
    assert len(plan_permissions) == 1
    assert len(plan_continuations) == 1
    assert len(hooks["Stop"]) == 1
    assert len(hooks["Stop"][0]["hooks"]) == 1
    assert hooks["Stop"][0]["hooks"][0].get("async") is not True

    command_hooks = [
        hook
        for entries in hooks.values()
        for entry in entries
        for hook in entry["hooks"]
        if hook["type"] == "command"
    ]
    assert all(hook["command"] == "bash" for hook in command_hooks)
    assert all(hook["args"][0] == "-lc" for hook in command_hooks)
    python_hooks = [
        hook for hook in command_hooks if "run-python-hook.sh" in " ".join(hook["args"])
    ]
    assert len(python_hooks) == len(command_hooks) - 1  # CODEX_ENV_FILE setup is shell-only.
    assert not any("Write-Output" in " ".join(hook["args"]) for hook in command_hooks)


def test_malformed_hook_payload_fails_safely(tmp_path):
    env = os.environ.copy()
    env["CLAUDE_DEVELOPMENT_WORKFLOW_STATE_DIR"] = str(tmp_path / "state")

    completed = subprocess.run(
        [sys.executable, str(HOOK)],
        input="not json",
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )

    assert "hook skipped" in json.loads(completed.stdout)["systemMessage"]
