#!/usr/bin/env python3
"""Submit a completed, verified Claude Code worktree as a GitHub pull request."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Any

COMMAND_TIMEOUT_SECONDS = 45
LOCK_TIMEOUT_SECONDS = 10 * 60
MAX_STOP_RETRIES = 2
OWNER_FILENAME = "claude-task-owner.json"
OWNER_VERSION = 1
TRUNK_BRANCHES = {"main", "master"}


def _load_workflow_module():
    path = Path(__file__).with_name("development-workflow.py")
    spec = importlib.util.spec_from_file_location("claude_development_workflow", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load development workflow hook: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


workflow = _load_workflow_module()


def _result(message: str) -> dict[str, Any]:
    return {
        "systemMessage": message,
        "suppressOutput": False,
    }


def _block(message: str) -> dict[str, Any]:
    return {
        "decision": "block",
        "reason": message,
        "systemMessage": message,
        "suppressOutput": False,
    }


def _run(command: list[str], cwd: Path) -> tuple[bool, str, str]:
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=COMMAND_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, "", str(exc)
    return completed.returncode == 0, completed.stdout.strip(), completed.stderr.strip()


def _git(cwd: Path, *arguments: str) -> tuple[bool, str, str]:
    return _run(["git", *arguments], cwd)


def _gh(cwd: Path, *arguments: str) -> tuple[bool, str, str]:
    return _run(["gh", *arguments], cwd)


def _state_root(environ: dict[str, str]) -> Path:
    return Path(environ.get("CLAUDE_AUTO_PR_STATE_DIR", "~/.claude/auto-submit-pr")).expanduser()


def _state_paths(cwd: Path, session_id: str, environ: dict[str, str]) -> tuple[Path, Path]:
    key = hashlib.sha256(f"{cwd.resolve()}\0{session_id}".encode()).hexdigest()
    root = _state_root(environ)
    return root / f"{key}.done", root / f"{key}.lock"


def _acquire_lock(lock_path: Path) -> bool:
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("x", encoding="utf-8")
    except FileExistsError:
        try:
            if time.time() - lock_path.stat().st_mtime <= LOCK_TIMEOUT_SECONDS:
                return False
            lock_path.unlink()
            handle = lock_path.open("x", encoding="utf-8")
        except OSError:
            return False
    except OSError:
        return False
    handle.write(str(os.getpid()))
    handle.close()
    return True


def _release_lock(lock_path: Path) -> None:
    try:
        lock_path.unlink()
    except OSError:
        pass


def _mark_done(done_path: Path, message: str) -> None:
    done_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = done_path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"message": message}), encoding="utf-8")
    temporary.replace(done_path)


def _current_worktree(payload: dict[str, Any], environ: dict[str, str]) -> Path | None:
    raw = payload.get("cwd") or environ.get("CLAUDE_PROJECT_DIR")
    if not raw:
        return None
    candidate = Path(str(raw)).expanduser()
    if not candidate.is_dir():
        return None
    ok, top_level, _ = _git(candidate, "rev-parse", "--show-toplevel")
    return Path(top_level).resolve() if ok and top_level else None


def _repository_paths(cwd: Path) -> tuple[Path, Path, Path] | None:
    ok, output, _ = _git(
        cwd,
        "rev-parse",
        "--path-format=absolute",
        "--show-toplevel",
        "--git-dir",
        "--git-common-dir",
    )
    lines = output.splitlines() if ok else []
    if len(lines) != 3:
        return None
    return tuple(Path(value).resolve() for value in lines)  # type: ignore[return-value]


def _owner_matches(git_dir: Path, session_id: str) -> bool:
    try:
        owner = json.loads((git_dir / OWNER_FILENAME).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return False
    return (
        isinstance(owner, dict)
        and owner.get("version") == OWNER_VERSION
        and owner.get("task_id") == session_id
    )


def _existing_pr(cwd: Path) -> tuple[str | None, str | None]:
    ok, output, _ = _gh(cwd, "pr", "view", "--json", "url,state,number")
    if not ok:
        return None, None
    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        return None, "INVALID METADATA"
    return str(data.get("url") or "").strip() or None, str(data.get("state") or "").strip().upper() or None


def _has_conflicts(status: str) -> bool:
    return any(line[:2] in {"AA", "AU", "DD", "DU", "UA", "UD", "UU"} for line in status.splitlines() if len(line) >= 2)


def _has_unpushed_commits(cwd: Path) -> bool:
    # ``--not`` is sticky. Keep the positive HEAD ref before excluding origin/*.
    ok, count, _ = _git(cwd, "rev-list", "--count", "HEAD", "--not", "--remotes=origin")
    return ok and count.isdigit() and int(count) > 0


def _has_branch_commits(cwd: Path) -> bool:
    for trunk in ("origin/main", "origin/master"):
        exists, _, _ = _git(cwd, "rev-parse", "--verify", trunk)
        if not exists:
            continue
        ok, count, _ = _git(cwd, "rev-list", "--count", f"{trunk}..HEAD")
        return ok and count.isdigit() and int(count) > 0
    return _has_unpushed_commits(cwd)


def _commit_message(branch: str) -> str:
    readable = branch.replace("/", ": ").replace("-", " ").strip()
    return f"Auto-submit: {readable[:70]}"


def _retry_or_result(
    payload: dict[str, Any],
    cwd: Path,
    session_id: str,
    environ: dict[str, str],
    message: str,
) -> dict[str, Any]:
    if payload.get("stop_hook_active") is True:
        return _result(f"自动提交 PR 暂停：{message}")
    retry_path = workflow.stop_retry_path(cwd, session_id, environ)
    try:
        current = workflow._read_json(retry_path)
        attempts = int(current.get("attempts", 0)) if current else 0
        attempts += 1
        workflow._write_json(
            retry_path,
            {
                "version": workflow.STATE_VERSION,
                "session_id": session_id,
                "worktree": str(cwd),
                "attempts": attempts,
                "reason": message,
            },
        )
    except (ValueError, TypeError, workflow.WorkflowError, OSError):
        return _result(f"自动提交 PR 暂停：{message}")
    if attempts > MAX_STOP_RETRIES:
        return _result(f"自动提交 PR 暂停：{message}（已达到自动继续上限。）")
    return _block(message)


def _clear_retries(cwd: Path, session_id: str, environ: dict[str, str]) -> None:
    workflow.stop_retry_path(cwd, session_id, environ).unlink(missing_ok=True)


def process(payload: dict[str, Any], environ: dict[str, str] | None = None) -> dict[str, Any]:
    env = dict(os.environ if environ is None else environ)
    session_id = str(payload.get("session_id") or "").strip()
    if not session_id:
        return _result("自动提交 PR 已跳过：Stop 事件缺少 session_id。")
    cwd = _current_worktree(payload, env)
    if cwd is None:
        fallback = Path(str(payload.get("cwd") or env.get("CLAUDE_PROJECT_DIR") or ".")).resolve()
        return _retry_or_result(
            payload, fallback, session_id, env,
            "当前目录不是有效的 Git worktree；请进入本会话专属 worktree 后继续实现。",
        )
    paths = _repository_paths(cwd)
    if paths is None or paths[1] == paths[2]:
        return _retry_or_result(
            payload, cwd, session_id, env,
            "计划已批准，但当前仍是 primary checkout；请调用 EnterWorktree 后继续实现。",
        )
    _worktree, git_dir, _common_dir = paths
    if not _owner_matches(git_dir, session_id):
        return _retry_or_result(
            payload, cwd, session_id, env,
            "当前 linked worktree 不属于本会话；请进入本会话拥有的 worktree。",
        )
    done_path, lock_path = _state_paths(cwd, session_id, env)
    if done_path.exists():
        return _result("自动提交 PR 已跳过：本次会话已经处理过该 worktree。")
    if not _acquire_lock(lock_path):
        return _result("自动提交 PR 已跳过：另一个 Stop hook 正在处理该 worktree。")
    try:
        ok, branch, _ = _git(cwd, "branch", "--show-current")
        if not ok or not branch:
            return _result("自动提交 PR 已跳过：当前处于 detached HEAD。")
        if branch in TRUNK_BRANCHES:
            return _result(f"自动提交 PR 已跳过：不会直接操作保护分支 {branch}。")
        ok, status, _ = _git(cwd, "status", "--porcelain=v1")
        if not ok:
            return _result("自动提交 PR 已跳过：无法读取 Git 状态。")
        if _has_conflicts(status):
            return _result("自动提交 PR 已跳过：工作树存在合并冲突，请先解决冲突。")
        if not status.strip() and not _has_branch_commits(cwd):
            return _result("自动提交 PR 已跳过：工作树没有待提交或待推送变更。")
        try:
            verified, verification_reason, _record = workflow.verification_status(
                cwd, session_id, env
            )
        except workflow.WorkflowError as exc:
            verified, verification_reason = False, str(exc)
        if not verified:
            return _retry_or_result(
                payload,
                cwd,
                session_id,
                env,
                "提交前验证不完整："
                f"{verification_reason}。请运行 `.claude/hooks/development-workflow.py "
                "verify -- <verification command and arguments>` 后再结束。",
            )
        _clear_retries(cwd, session_id, env)
        if shutil.which("gh") is None:
            return _result("自动提交 PR 已跳过：未找到 gh，请安装 GitHub CLI。")
        auth_ok, _, _ = _gh(cwd, "auth", "status")
        if not auth_ok:
            return _result("自动提交 PR 已跳过：GitHub CLI 未认证，请先运行 gh auth login。")
        existing_url, existing_state = _existing_pr(cwd)
        if existing_state in {"CLOSED", "MERGED"}:
            return _result(
                f"自动提交 PR 已跳过：当前分支已有终态 PR "
                f"{existing_url or '(无链接)'}（{existing_state}）。"
            )
        if status.strip():
            ok, _, error = _git(cwd, "add", "-A")
            if not ok:
                return _result(f"自动提交 PR 失败：git add 未成功（{error or 'unknown error'}）。")
            ok, _, error = _git(cwd, "commit", "-m", _commit_message(branch))
            if not ok:
                return _result(f"自动提交 PR 失败：git commit 未成功（{error or 'unknown error'}）。")
        ok, _, error = _git(cwd, "push", "-u", "origin", branch)
        if not ok:
            return _result(f"自动提交 PR 失败：git push 未成功（{error or 'unknown error'}）。")
        if existing_url:
            message = f"自动提交 PR 完成：已更新现有 PR {existing_url}。"
        else:
            created, output, error = _gh(cwd, "pr", "create", "--fill")
            if not created:
                return _result(f"自动提交 PR 失败：gh pr create 未成功（{error or 'unknown error'}）。")
            url = next((line.strip() for line in reversed(output.splitlines()) if line.strip()), "")
            message = f"自动提交 PR 完成：{url or 'PR 已创建。'}"
        _mark_done(done_path, message)
        return _result(message)
    finally:
        _release_lock(lock_path)


def main() -> int:
    try:
        raw = sys.stdin.read().lstrip("﻿")
        payload = json.loads(raw) if raw.strip() else {}
        output = process(payload if isinstance(payload, dict) else {})
    except Exception as exc:
        output = _result(f"自动提交 PR 未执行：hook 内部错误（{exc}）。")
    print(json.dumps(output, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
