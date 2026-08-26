from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / ".claude" / "hooks" / "auto-submit-pr.py"

spec = importlib.util.spec_from_file_location("auto_submit_pr", HOOK)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


def _git(repo: Path, *arguments: str, check: bool = True) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=False,
        capture_output=True,
        text=True,
    )
    if check and completed.returncode != 0:
        raise AssertionError(completed.stderr)
    return completed.stdout.strip()


def _repositories(tmp_path: Path, session_id: str = "task-session") -> tuple[Path, Path, Path]:
    origin = tmp_path / "origin.git"
    repo = tmp_path / "repo"
    worktree = repo / ".claude" / "worktrees" / "task"
    origin.mkdir()
    repo.mkdir()
    _git(origin, "init", "--bare")
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Hook Test")
    _git(repo, "config", "user.email", "hook@example.invalid")
    (repo / "tracked.txt").write_text("initial\n")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "initial")
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-u", "origin", "main")
    worktree.parent.mkdir(parents=True)
    _git(repo, "worktree", "add", "-b", "worktree-task", str(worktree))
    git_dir = Path(_git(worktree, "rev-parse", "--git-dir"))
    if not git_dir.is_absolute():
        git_dir = worktree / git_dir
    (git_dir.resolve() / "claude-task-owner.json").write_text(
        json.dumps({"version": 1, "task_id": session_id}) + "\n"
    )
    return origin, repo, worktree


def _env(tmp_path: Path) -> dict[str, str]:
    return {
        "CLAUDE_AUTO_PR_STATE_DIR": str(tmp_path / "auto-pr"),
        "CLAUDE_DEVELOPMENT_WORKFLOW_STATE_DIR": str(tmp_path / "workflow"),
    }


def _approve(session_id: str, worktree: Path, env: dict[str, str]) -> None:
    module.workflow.record_plan_approved(
        {"session_id": session_id, "cwd": str(worktree)}, env
    )


def test_skips_missing_session_id(tmp_path):
    result = module.process({}, _env(tmp_path))
    assert "缺少 session_id" in result["systemMessage"]


def test_result_does_not_rewake_model():
    result = module._result("message")
    assert result["systemMessage"] == "message"
    assert "hookSpecificOutput" not in result
    json.dumps(result, ensure_ascii=False)


def test_main_accepts_utf8_bom_payload(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO("﻿{}"))

    assert module.main() == 0

    output = json.loads(capsys.readouterr().out)
    assert "缺少 session_id" in output["systemMessage"]


def test_missing_plan_does_not_block_worktree_checks(tmp_path):
    session_id = "task-session"
    _origin, repo, _worktree = _repositories(tmp_path, session_id)
    env = _env(tmp_path)

    result = module.process({"session_id": session_id, "cwd": str(repo)}, env)

    assert result["decision"] == "block"
    assert "primary checkout" in result["reason"]


def test_approved_session_in_primary_checkout_is_blocked_with_bound(tmp_path):
    session_id = "task-session"
    _origin, repo, _worktree = _repositories(tmp_path, session_id)
    env = _env(tmp_path)
    _approve(session_id, repo, env)
    payload = {"session_id": session_id, "cwd": str(repo)}

    first = module.process(payload, env)
    second = module.process(payload, env)
    third = module.process(payload, env)

    assert first["decision"] == "block"
    assert second["decision"] == "block"
    assert "decision" not in third
    assert "自动继续上限" in third["systemMessage"]


def test_stop_hook_active_never_blocks_again(tmp_path):
    session_id = "task-session"
    _origin, repo, _worktree = _repositories(tmp_path, session_id)
    env = _env(tmp_path)
    _approve(session_id, repo, env)

    result = module.process(
        {"session_id": session_id, "cwd": str(repo), "stop_hook_active": True}, env
    )

    assert "decision" not in result
    assert "primary checkout" in result["systemMessage"]


def test_owner_mismatch_blocks_before_git_mutation(tmp_path):
    session_id = "task-session"
    _origin, _repo, worktree = _repositories(tmp_path, "other-session")
    env = _env(tmp_path)
    _approve(session_id, worktree, env)
    (worktree / "change.txt").write_text("change\n")

    result = module.process({"session_id": session_id, "cwd": str(worktree)}, env)

    assert result["decision"] == "block"
    assert "不属于本会话" in result["reason"]
    assert "?? change.txt" in _git(worktree, "status", "--porcelain")


def test_missing_and_stale_verification_block_submission(tmp_path):
    session_id = "task-session"
    _origin, _repo, worktree = _repositories(tmp_path, session_id)
    env = _env(tmp_path)
    _approve(session_id, worktree, env)
    (worktree / "tracked.txt").write_text("change\n")

    missing = module.process({"session_id": session_id, "cwd": str(worktree)}, env)
    assert missing["decision"] == "block"
    assert "no successful verification" in missing["reason"]

    module.workflow.write_verification(worktree, session_id, ["tests"], env)
    (worktree / "tracked.txt").write_text("changed again\n")
    stale = module.process({"session_id": session_id, "cwd": str(worktree)}, env)
    assert stale["decision"] == "block"
    assert "changed after verification" in stale["reason"]


def test_verified_stop_commits_pushes_creates_pr_and_is_idempotent(tmp_path, monkeypatch):
    session_id = "task-session"
    origin, _repo, worktree = _repositories(tmp_path, session_id)
    env = _env(tmp_path)
    (worktree / "tracked.txt").write_text("implemented\n")
    module.workflow.write_verification(worktree, session_id, ["pytest"], env)
    gh_calls: list[tuple[str, ...]] = []

    def fake_gh(_cwd: Path, *arguments: str):
        gh_calls.append(arguments)
        if arguments[:2] == ("auth", "status"):
            return True, "", ""
        if arguments[:2] == ("pr", "view"):
            return False, "", "not found"
        if arguments[:2] == ("pr", "create"):
            return True, "https://example.invalid/pr/1\n", ""
        raise AssertionError(arguments)

    monkeypatch.setattr(module.shutil, "which", lambda _name: "gh")
    monkeypatch.setattr(module, "_gh", fake_gh)

    result = module.process({"session_id": session_id, "cwd": str(worktree)}, env)
    again = module.process({"session_id": session_id, "cwd": str(worktree)}, env)

    assert "https://example.invalid/pr/1" in result["systemMessage"]
    branch_head = _git(origin, "rev-parse", "refs/heads/worktree-task")
    assert branch_head == _git(worktree, "rev-parse", "HEAD")
    assert _git(worktree, "show", "HEAD:tracked.txt") == "implemented"
    assert gh_calls.count(("pr", "create", "--fill")) == 1
    assert "已经处理过" in again["systemMessage"]


def test_open_pr_is_updated_but_terminal_pr_is_not_pushed(tmp_path, monkeypatch):
    for state in ("OPEN", "CLOSED", "MERGED"):
        case = tmp_path / state.lower()
        case.mkdir()
        session_id = f"session-{state.lower()}"
        origin, _repo, worktree = _repositories(case, session_id)
        env = _env(case)
        _approve(session_id, worktree, env)
        (worktree / "tracked.txt").write_text(f"{state}\n")
        module.workflow.write_verification(worktree, session_id, ["pytest"], env)

        def fake_gh(_cwd: Path, *arguments: str, current_state=state):
            if arguments[:2] == ("auth", "status"):
                return True, "", ""
            if arguments[:2] == ("pr", "view"):
                return True, json.dumps(
                    {"url": f"https://example.invalid/{current_state}", "state": current_state}
                ), ""
            raise AssertionError(arguments)

        monkeypatch.setattr(module.shutil, "which", lambda _name: "gh")
        monkeypatch.setattr(module, "_gh", fake_gh)
        before = _git(origin, "rev-parse", "refs/heads/main")
        result = module.process({"session_id": session_id, "cwd": str(worktree)}, env)

        if state == "OPEN":
            assert "已更新现有 PR" in result["systemMessage"]
            assert _git(origin, "rev-parse", "refs/heads/worktree-task") != before
        else:
            assert state in result["systemMessage"]
            _git(origin, "rev-parse", "refs/heads/worktree-task", check=False)
            assert _git(worktree, "status", "--porcelain")


def test_missing_gh_and_auth_failure_are_passive(tmp_path, monkeypatch):
    session_id = "task-session"
    _origin, _repo, worktree = _repositories(tmp_path, session_id)
    env = _env(tmp_path)
    _approve(session_id, worktree, env)
    (worktree / "tracked.txt").write_text("change\n")
    module.workflow.write_verification(worktree, session_id, ["pytest"], env)

    monkeypatch.setattr(module.shutil, "which", lambda _name: None)
    missing = module.process({"session_id": session_id, "cwd": str(worktree)}, env)
    assert "decision" not in missing
    assert "未找到 gh" in missing["systemMessage"]

    monkeypatch.setattr(module.shutil, "which", lambda _name: "gh")
    monkeypatch.setattr(module, "_gh", lambda _cwd, *_args: (False, "", "no auth"))
    unauthenticated = module.process(
        {"session_id": session_id, "cwd": str(worktree)}, env
    )
    assert "decision" not in unauthenticated
    assert "未认证" in unauthenticated["systemMessage"]


def test_conflict_detection():
    assert module._has_conflicts("UU file.py\n")
    assert not module._has_conflicts(" M file.py\n?? new.py\n")


def test_branch_commit_detection_ignores_origin_main_history(tmp_path):
    _origin, _repo, worktree = _repositories(tmp_path)

    assert module._has_branch_commits(worktree) is False

    (worktree / "branch.txt").write_text("branch\n")
    _git(worktree, "add", "branch.txt")
    _git(worktree, "commit", "-m", "branch")
    assert module._has_branch_commits(worktree) is True


def test_commit_message_is_bounded():
    message = module._commit_message("feature/" + "x" * 200)
    assert message.startswith("Auto-submit: ")
    assert len(message) <= len("Auto-submit: ") + 70
