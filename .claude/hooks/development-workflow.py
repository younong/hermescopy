#!/usr/bin/env python3
"""Coordinate plan continuation and verified Claude Code development snapshots."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time
from typing import Any, Sequence

COMMAND_TIMEOUT_SECONDS = 10
OWNER_FILENAME = "claude-task-owner.json"
OWNER_VERSION = 1
STATE_VERSION = 1


class WorkflowError(RuntimeError):
    """Raised when development workflow state cannot be established safely."""


def _run(
    command: Sequence[str],
    cwd: Path,
    *,
    binary: bool = False,
    timeout: int | None = COMMAND_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[Any]:
    try:
        return subprocess.run(
            list(command),
            cwd=cwd,
            capture_output=True,
            text=not binary,
            encoding=None if binary else "utf-8",
            errors=None if binary else "replace",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise WorkflowError(f"command failed to start: {' '.join(command)}: {exc}") from exc


def _git(cwd: Path, *arguments: str, binary: bool = False) -> bytes | str:
    completed = _run(["git", *arguments], cwd, binary=binary)
    if completed.returncode != 0:
        error = completed.stderr
        if isinstance(error, bytes):
            error = error.decode(errors="replace")
        raise WorkflowError(
            f"git {' '.join(arguments)} failed: {str(error).strip() or 'unknown error'}"
        )
    return completed.stdout


def _state_root(environ: dict[str, str]) -> Path:
    configured = environ.get("CLAUDE_DEVELOPMENT_WORKFLOW_STATE_DIR")
    return Path(configured or "~/.claude/development-workflow").expanduser()


def _state_key(*values: object) -> str:
    material = "\0".join(str(value) for value in values)
    return hashlib.sha256(material.encode()).hexdigest()


def plan_path(session_id: str, environ: dict[str, str]) -> Path:
    return _state_root(environ) / "plans" / f"{_state_key(session_id)}.json"


def verification_path(cwd: Path, session_id: str, environ: dict[str, str]) -> Path:
    return (
        _state_root(environ)
        / "verifications"
        / f"{_state_key(cwd.resolve(), session_id)}.json"
    )


def stop_retry_path(cwd: Path, session_id: str, environ: dict[str, str]) -> Path:
    return (
        _state_root(environ)
        / "stop-retries"
        / f"{_state_key(cwd.resolve(), session_id)}.json"
    )


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError) as exc:
        raise WorkflowError(f"workflow state is unreadable: {path}: {exc}") from exc
    if not isinstance(value, dict) or value.get("version") != STATE_VERSION:
        raise WorkflowError(f"workflow state has an unsupported format: {path}")
    return value


def payload_session_id(payload: dict[str, Any]) -> str:
    value = payload.get("session_id")
    if not isinstance(value, str) or not value.strip():
        raise WorkflowError("hook payload has no session_id")
    return value.strip()


def record_plan_approved(
    payload: dict[str, Any], environ: dict[str, str] | None = None
) -> dict[str, Any]:
    env = dict(os.environ if environ is None else environ)
    session_id = payload_session_id(payload)
    record = {
        "version": STATE_VERSION,
        "session_id": session_id,
        "approved": True,
        "project_dir": str(payload.get("cwd") or env.get("CLAUDE_PROJECT_DIR") or ""),
        "recorded_at": int(time.time()),
    }
    _write_json(plan_path(session_id, env), record)
    return record


def read_plan(
    session_id: str, environ: dict[str, str] | None = None
) -> dict[str, Any] | None:
    env = dict(os.environ if environ is None else environ)
    return _read_json(plan_path(session_id, env))


def repository_paths(cwd: Path) -> tuple[Path, Path, Path]:
    output = str(
        _git(
            cwd,
            "rev-parse",
            "--path-format=absolute",
            "--show-toplevel",
            "--git-dir",
            "--git-common-dir",
        )
    ).splitlines()
    if len(output) != 3:
        raise WorkflowError("current directory is not a verifiable Git worktree")
    return tuple(Path(value).resolve() for value in output)  # type: ignore[return-value]


def verify_worktree_owner(cwd: Path, session_id: str) -> Path:
    worktree, git_dir, common_dir = repository_paths(cwd)
    if git_dir == common_dir:
        raise WorkflowError("verification must run in a linked Claude Code worktree")
    owner_path = git_dir / OWNER_FILENAME
    try:
        owner = json.loads(owner_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
        raise WorkflowError(f"worktree ownership cannot be verified: {owner_path}: {exc}") from exc
    if (
        not isinstance(owner, dict)
        or owner.get("version") != OWNER_VERSION
        or owner.get("task_id") != session_id
    ):
        raise WorkflowError(
            f"worktree is not owned by the current session {session_id}: {worktree}"
        )
    return worktree


def _hash_untracked_file(hasher: Any, cwd: Path, relative: bytes) -> None:
    relative_text = os.fsdecode(relative)
    target = cwd / relative_text
    try:
        metadata = target.lstat()
    except OSError as exc:
        raise WorkflowError(f"cannot inspect untracked path {relative_text}: {exc}") from exc
    hasher.update(relative)
    hasher.update(b"\0")
    hasher.update(str(stat.S_IFMT(metadata.st_mode)).encode())
    hasher.update(b"\0")
    if stat.S_ISLNK(metadata.st_mode):
        hasher.update(os.fsencode(os.readlink(target)))
    elif stat.S_ISREG(metadata.st_mode):
        try:
            with target.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    hasher.update(chunk)
        except OSError as exc:
            raise WorkflowError(f"cannot read untracked file {relative_text}: {exc}") from exc
    else:
        raise WorkflowError(f"unsupported untracked path type: {relative_text}")
    hasher.update(b"\0")


def repository_snapshot(cwd: Path) -> dict[str, str]:
    worktree, _git_dir, _common_dir = repository_paths(cwd)
    head = str(_git(worktree, "rev-parse", "HEAD")).strip()
    tracked_diff = bytes(_git(worktree, "diff", "--binary", "HEAD", "--", binary=True))
    untracked_output = bytes(
        _git(
            worktree,
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
            binary=True,
        )
    )
    hasher = hashlib.sha256()
    hasher.update(b"head\0")
    hasher.update(head.encode())
    hasher.update(b"\0tracked\0")
    hasher.update(tracked_diff)
    hasher.update(b"\0untracked\0")
    for relative in sorted(value for value in untracked_output.split(b"\0") if value):
        _hash_untracked_file(hasher, worktree, relative)
    return {"head": head, "digest": hasher.hexdigest()}


def write_verification(
    cwd: Path,
    session_id: str,
    argv: Sequence[str],
    environ: dict[str, str] | None = None,
) -> dict[str, Any]:
    env = dict(os.environ if environ is None else environ)
    worktree = verify_worktree_owner(cwd, session_id)
    snapshot = repository_snapshot(worktree)
    record: dict[str, Any] = {
        "version": STATE_VERSION,
        "session_id": session_id,
        "worktree": str(worktree),
        "argv": list(argv),
        "head": snapshot["head"],
        "snapshot": snapshot["digest"],
        "recorded_at": int(time.time()),
    }
    _write_json(verification_path(worktree, session_id, env), record)
    return record


def read_verification(
    cwd: Path, session_id: str, environ: dict[str, str] | None = None
) -> dict[str, Any] | None:
    env = dict(os.environ if environ is None else environ)
    return _read_json(verification_path(cwd, session_id, env))


def verification_status(
    cwd: Path, session_id: str, environ: dict[str, str] | None = None
) -> tuple[bool, str, dict[str, Any] | None]:
    env = dict(os.environ if environ is None else environ)
    record = read_verification(cwd, session_id, env)
    if record is None:
        return False, "no successful verification is recorded for this worktree", None
    if record.get("session_id") != session_id or Path(str(record.get("worktree"))).resolve() != cwd.resolve():
        return False, "verification belongs to another session or worktree", record
    snapshot = repository_snapshot(cwd)
    if record.get("head") != snapshot["head"] or record.get("snapshot") != snapshot["digest"]:
        return False, "the code snapshot changed after verification", record
    argv = record.get("argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(item, str) for item in argv):
        return False, "verification record has no valid command", record
    return True, "verification matches the current code snapshot", record


def permission_output(payload: dict[str, Any]) -> dict[str, Any]:
    decision: dict[str, Any] = {"behavior": "allow"}
    suggestions = payload.get("permission_suggestions")
    if isinstance(suggestions, list) and len(suggestions) == 1:
        decision["updatedPermissions"] = suggestions
    return {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": decision,
        }
    }


def post_plan_output(
    payload: dict[str, Any], environ: dict[str, str] | None = None
) -> dict[str, Any]:
    record_plan_approved(payload, environ)
    return {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": (
                "The plan is approved. Continue implementation now. First call "
                "EnterWorktree to create or resume this session's dedicated owned "
                "worktree; do not modify the primary checkout. Implement the approved "
                "plan, then run `.claude/hooks/development-workflow.py verify -- "
                "<verification command and arguments>`. Do not commit or push manually; "
                "the Stop hook will validate the exact verified snapshot and finalize "
                "the pull request."
            ),
        }
    }


def process_hook(
    payload: dict[str, Any], environ: dict[str, str] | None = None
) -> dict[str, Any]:
    event = payload.get("hook_event_name")
    if event == "PermissionRequest" and payload.get("tool_name") == "ExitPlanMode":
        return permission_output(payload)
    if event == "PostToolUse" and payload.get("tool_name") == "ExitPlanMode":
        return post_plan_output(payload, environ)
    return {}


def _verify_command(argv: list[str], environ: dict[str, str]) -> int:
    if not argv:
        print("verification command is required after --", file=sys.stderr)
        return 2
    session_id = environ.get("CLAUDE_CODE_SESSION_ID", "").strip()
    if not session_id:
        print("CLAUDE_CODE_SESSION_ID is required for recorded verification", file=sys.stderr)
        return 2
    cwd = Path.cwd().resolve()
    try:
        verify_worktree_owner(cwd, session_id)
    except WorkflowError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    try:
        completed = subprocess.run(argv, cwd=cwd, check=False)
    except OSError as exc:
        print(f"verification command failed to start: {exc}", file=sys.stderr)
        return 2
    if completed.returncode != 0:
        print(
            f"verification failed with exit code {completed.returncode}; no snapshot was recorded",
            file=sys.stderr,
        )
        return completed.returncode or 1
    try:
        record = write_verification(cwd, session_id, argv, environ)
    except WorkflowError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(
        f"Recorded successful verification for {record['worktree']} "
        f"at {record['head']} ({record['snapshot']})."
    )
    return 0


def main() -> int:
    if len(sys.argv) >= 2 and sys.argv[1] == "verify":
        arguments = sys.argv[2:]
        if arguments[:1] == ["--"]:
            arguments = arguments[1:]
        return _verify_command(arguments, dict(os.environ))
    try:
        raw = sys.stdin.read().lstrip("﻿")
        payload = json.loads(raw) if raw.strip() else {}
        output = process_hook(payload if isinstance(payload, dict) else {})
    except (json.JSONDecodeError, WorkflowError, OSError) as exc:
        output = {"systemMessage": f"Claude development workflow hook skipped: {exc}"}
    print(json.dumps(output, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
