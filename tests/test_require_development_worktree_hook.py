"""Tests for the Claude Code primary-checkout edit guard."""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


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
    session_id: str = "task-session",
) -> subprocess.CompletedProcess[str]:
    payload = raw_payload
    if payload is None:
        payload = json.dumps(
            {
                "hook_event_name": "PreToolUse",
                "session_id": session_id,
                "cwd": str(project_dir),
                "tool_name": "Write",
                "tool_input": {"file_path": str(target)},
            }
        )
    env = os.environ.copy()
    env["CLAUDE_PROJECT_DIR"] = str(project_dir)
    if path_prefix is not None:
        env["PATH"] = f"{path_prefix}{os.pathsep}{env.get('PATH', '')}"
    return subprocess.run(
        [sys.executable, str(HOOK_PATH)],
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


def _payload(
    event: str,
    project_dir: Path,
    *,
    session_id: str,
    tool_name: str | None = None,
    tool_input: dict | None = None,
    tool_response: object | None = None,
    cwd: Path | None = None,
    transcript_path: Path | None = None,
) -> str:
    payload: dict[str, object] = {
        "hook_event_name": event,
        "session_id": session_id,
        "transcript_path": str(
            transcript_path or Path("/transcripts") / f"{session_id}.jsonl"
        ),
        "cwd": str(cwd or project_dir),
    }
    if tool_name is not None:
        payload["tool_name"] = tool_name
    if tool_input is not None:
        payload["tool_input"] = tool_input
    if tool_response is not None:
        payload["tool_response"] = tool_response
    return json.dumps(payload)


def _owner_path(worktree: Path) -> Path:
    git_dir = Path(_git(worktree, "rev-parse", "--git-dir").stdout.strip())
    if not git_dir.is_absolute():
        git_dir = worktree / git_dir
    return git_dir.resolve() / "claude-task-owner.json"


def _write_owner(worktree: Path, task_id: str) -> None:
    _owner_path(worktree).write_text(
        json.dumps({"version": 1, "task_id": task_id}) + "\n"
    )


def _write_worktree_state(
    transcript: Path,
    worktree: Path,
    session_id: str,
    *,
    outer_session_id: str | None = None,
    append: bool = False,
) -> None:
    with transcript.open("a" if append else "w") as stream:
        stream.write(
            json.dumps(
                {
                    "type": "worktree-state",
                    "worktreeSession": {
                        "worktreePath": str(worktree),
                        "sessionId": session_id,
                    },
                    "sessionId": outer_session_id or session_id,
                }
            )
            + "\n"
        )


def _run_worktree_write(
    repo: Path,
    worktree: Path,
    transcript: Path,
    *,
    session_id: str = "task-session",
) -> subprocess.CompletedProcess[str]:
    return _run_hook(
        repo,
        worktree / "new-file.txt",
        raw_payload=_payload(
            "PreToolUse",
            repo,
            session_id=session_id,
            tool_name="Write",
            tool_input={"file_path": str(worktree / "new-file.txt")},
            cwd=worktree,
            transcript_path=transcript,
        ),
    )


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

    assert json.dumps(str(worktree)) in reason
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

    assert json.dumps(str(first)) in reason
    assert json.dumps(str(second)) in reason
    assert "If candidates are ambiguous, do not create another worktree" in reason
    assert "ask the user if it is still unclear" in reason


def test_primary_checkout_without_candidates_remains_blocked(tmp_path):
    repo = _new_repo(tmp_path)

    reason = _denial_reason(_run_hook(repo, repo / "tracked.txt"))

    assert "No registered Claude Code worktree candidates were found" in reason
    assert "Only after confirming" in reason


def test_edit_targeting_owned_linked_worktree_is_allowed(tmp_path):
    repo = _new_repo(tmp_path)
    worktree = _add_worktree(repo, "active-task")
    _write_owner(worktree, "task-session")

    result = _run_hook(repo, worktree / "new-file.txt")

    assert result.stdout == ""


def test_edit_targeting_unowned_linked_worktree_is_blocked(tmp_path):
    repo = _new_repo(tmp_path)
    worktree = _add_worktree(repo, "unowned-task")

    reason = _denial_reason(_run_hook(repo, worktree / "new-file.txt"))

    assert "not registered to any task" in reason
    assert "TaskCreate does not register worktree ownership" in reason


def test_missing_owner_recovers_from_matching_transcript_state(tmp_path):
    repo = _new_repo(tmp_path)
    worktree = _add_worktree(repo, "recover-task")
    (worktree / "dirty.txt").write_text("task changes\n")
    transcript = tmp_path / "task-session.jsonl"
    _write_worktree_state(transcript, worktree, "task-session")

    result = _run_worktree_write(repo, worktree, transcript)

    assert result.stdout == ""
    assert json.loads(_owner_path(worktree).read_text()) == {
        "version": 1,
        "task_id": "task-session",
    }


def test_missing_owner_rejects_transcript_for_another_session(tmp_path):
    repo = _new_repo(tmp_path)
    worktree = _add_worktree(repo, "unowned-task")
    transcript = tmp_path / "task-session.jsonl"
    _write_worktree_state(
        transcript,
        worktree,
        "other-session",
        outer_session_id="other-session",
    )

    reason = _denial_reason(_run_worktree_write(repo, worktree, transcript))

    assert "does not prove ownership" in reason
    assert not _owner_path(worktree).exists()


def test_missing_owner_rejects_transcript_for_another_worktree(tmp_path):
    repo = _new_repo(tmp_path)
    worktree = _add_worktree(repo, "unowned-task")
    other = _add_worktree(repo, "other-task")
    transcript = tmp_path / "task-session.jsonl"
    _write_worktree_state(transcript, other, "task-session")

    reason = _denial_reason(_run_worktree_write(repo, worktree, transcript))

    assert "does not prove ownership" in reason
    assert not _owner_path(worktree).exists()


def test_missing_owner_uses_latest_matching_session_state(tmp_path):
    repo = _new_repo(tmp_path)
    worktree = _add_worktree(repo, "current-task")
    old = _add_worktree(repo, "old-task")
    transcript = tmp_path / "task-session.jsonl"
    _write_worktree_state(transcript, old, "task-session")
    with transcript.open("a") as stream:
        stream.write("not json\n")
    _write_worktree_state(transcript, worktree, "task-session", append=True)

    result = _run_worktree_write(repo, worktree, transcript)

    assert result.stdout == ""
    assert json.loads(_owner_path(worktree).read_text())["task_id"] == "task-session"


def test_missing_owner_rejects_missing_or_malformed_transcript(tmp_path):
    repo = _new_repo(tmp_path)
    worktree = _add_worktree(repo, "unowned-task")
    malformed = tmp_path / "malformed.jsonl"
    malformed.write_text("not json\n")

    for transcript in (tmp_path / "missing.jsonl", malformed):
        reason = _denial_reason(_run_worktree_write(repo, worktree, transcript))
        assert "does not prove ownership" in reason
        assert not _owner_path(worktree).exists()


def test_edit_targeting_another_tasks_worktree_is_blocked(tmp_path):
    repo = _new_repo(tmp_path)
    worktree = _add_worktree(repo, "other-task")
    _write_owner(worktree, "first-task")

    reason = _denial_reason(
        _run_hook(repo, worktree / "new-file.txt", session_id="second-task")
    )

    assert "belongs to task first-task" in reason
    assert "current task is second-task" in reason


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

    assert json.dumps(str(listed)) in reason
    assert json.dumps(str(unregistered)) not in reason
    assert json.dumps(str(external)) not in reason


def test_detached_and_locked_worktrees_are_described(tmp_path):
    repo = _new_repo(tmp_path)
    worktree = _add_worktree(repo, "detached task", detach=True)
    _git(repo, "worktree", "lock", "--reason", "test lock", str(worktree))

    reason = _denial_reason(_run_hook(repo, repo / "tracked.txt"))

    assert json.dumps(str(worktree)) in reason
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
    real_git = shutil.which("git")
    assert real_git is not None
    wrapper_script = wrapper_dir / "git-wrapper.py"
    wrapper_script.write_text(
        "import os, subprocess, sys\n"
        "if 'worktree list' in ' '.join(sys.argv[1:]):\n"
        "    raise SystemExit(1)\n"
        f"raise SystemExit(subprocess.call([{real_git!r}, *sys.argv[1:]], env=os.environ))\n"
    )
    if os.name == "nt":
        wrapper = wrapper_dir / "git.cmd"
        wrapper.write_text(f'@"{sys.executable}" "{wrapper_script}" %*\n')
    else:
        wrapper = wrapper_dir / "git"
        wrapper.write_text(f"#!/bin/sh\nexec {sys.executable!r} {wrapper_script!r} \"$@\"\n")
        wrapper.chmod(0o755)

    if os.name == "nt":
        pytest.skip("PATH-based Git wrapper resolution is not reliable on Windows")

    reason = _denial_reason(
        _run_hook(repo, repo / "tracked.txt", path_prefix=wrapper_dir)
    )

    assert "Registered worktree discovery failed" in reason


def test_unverifiable_repository_target_fails_closed(tmp_path):
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    reason = _denial_reason(_run_hook(project_dir, project_dir / "file.txt"))

    assert "could not verify" in reason


def test_enter_existing_owned_worktree_is_allowed(tmp_path):
    repo = _new_repo(tmp_path)
    worktree = _add_worktree(repo, "owned-task")
    _write_owner(worktree, "task-session")

    result = _run_hook(
        repo,
        worktree,
        raw_payload=_payload(
            "PreToolUse",
            repo,
            session_id="task-session",
            tool_name="EnterWorktree",
            tool_input={"path": str(worktree)},
        ),
    )

    assert result.stdout == ""


def test_enter_existing_worktree_owned_by_another_task_is_blocked(tmp_path):
    repo = _new_repo(tmp_path)
    worktree = _add_worktree(repo, "owned-task")
    _write_owner(worktree, "first-task")

    reason = _denial_reason(
        _run_hook(
            repo,
            worktree,
            raw_payload=_payload(
                "PreToolUse",
                repo,
                session_id="second-task",
                tool_name="EnterWorktree",
                tool_input={"path": str(worktree)},
            ),
        )
    )

    assert "cross-task reuse is blocked" in reason


def test_post_enter_registers_clean_new_worktree(tmp_path):
    repo = _new_repo(tmp_path)
    worktree = _add_worktree(repo, "new-task")

    result = _run_hook(
        repo,
        worktree,
        raw_payload=_payload(
            "PostToolUse",
            repo,
            session_id="new-task-session",
            tool_name="EnterWorktree",
            tool_input={"name": "new-task"},
            tool_response=(
                f"Created worktree at {worktree} on branch worktree-new-task."
            ),
            cwd=worktree,
        ),
    )

    owner = json.loads(_owner_path(worktree).read_text())
    assert owner == {"version": 1, "task_id": "new-task-session"}
    assert "authorized worktree" in result.stdout


def test_post_enter_does_not_claim_dirty_unowned_worktree(tmp_path):
    repo = _new_repo(tmp_path)
    worktree = _add_worktree(repo, "dirty-task")
    (worktree / "dirty.txt").write_text("uncommitted\n")

    result = _run_hook(
        repo,
        worktree,
        raw_payload=_payload(
            "PostToolUse",
            repo,
            session_id="new-task-session",
            tool_name="EnterWorktree",
            tool_input={"path": str(worktree)},
            tool_response=f"Entered worktree at {worktree} on branch worktree-dirty-task.",
            cwd=worktree,
        ),
    )

    output = json.loads(result.stdout)
    assert output["continue"] is False
    assert "has changes and cannot be claimed" in output["stopReason"]
    assert not _owner_path(worktree).exists()


def test_session_start_claims_clean_unowned_worktree(tmp_path):
    repo = _new_repo(tmp_path)
    worktree = _add_worktree(repo, "resumed-task")

    result = _run_hook(
        repo,
        worktree,
        raw_payload=_payload(
            "SessionStart",
            repo,
            session_id="resumed-session",
            cwd=worktree,
        ),
    )

    owner = json.loads(_owner_path(worktree).read_text())
    assert owner["task_id"] == "resumed-session"
    assert "Persistent task identity" in result.stdout


def test_post_compact_preserves_existing_task_identity(tmp_path):
    repo = _new_repo(tmp_path)
    worktree = _add_worktree(repo, "compact-task")
    _write_owner(worktree, "compact-session")

    result = _run_hook(
        repo,
        worktree,
        raw_payload=_payload(
            "PostCompact",
            repo,
            session_id="compact-session",
            cwd=worktree,
        ),
    )

    assert "Persistent task identity: compact-session" in result.stdout


def test_lifecycle_event_rejects_another_tasks_worktree(tmp_path):
    repo = _new_repo(tmp_path)
    worktree = _add_worktree(repo, "first-task")
    _write_owner(worktree, "first-session")

    result = _run_hook(
        repo,
        worktree,
        raw_payload=_payload(
            "CwdChanged",
            repo,
            session_id="second-session",
            cwd=worktree,
        ),
    )

    output = json.loads(result.stdout)
    assert output["continue"] is False
    assert "belongs to task first-session" in output["stopReason"]


@pytest.mark.parametrize(
    ("tool_name", "tool_input"),
    [
        ("Agent", {"prompt": "implement"}),
        ("Bash", {"command": "git status"}),
        ("Monitor", {"command": "git status"}),
        ("PowerShell", {"command": "git status"}),
        ("Workflow", {"script": "return null"}),
    ],
)
def test_development_tools_in_primary_checkout_are_blocked(
    tmp_path, tool_name, tool_input
):
    repo = _new_repo(tmp_path)

    reason = _denial_reason(
        _run_hook(
            repo,
            repo,
            raw_payload=_payload(
                "PreToolUse",
                repo,
                session_id="task-session",
                tool_name=tool_name,
                tool_input=tool_input,
                cwd=repo,
            ),
        )
    )

    assert "primary checkout" in reason
    assert "EnterWorktree" in reason


@pytest.mark.parametrize("tool_name", ["Agent", "Bash", "Monitor", "PowerShell", "Workflow"])
def test_development_tools_in_owned_worktree_are_allowed(tmp_path, tool_name):
    repo = _new_repo(tmp_path)
    worktree = _add_worktree(repo, "active-task")
    _write_owner(worktree, "task-session")

    result = _run_hook(
        repo,
        worktree,
        raw_payload=_payload(
            "PreToolUse",
            repo,
            session_id="task-session",
            tool_name=tool_name,
            tool_input={"command": "git status"},
            cwd=worktree,
        ),
    )

    assert result.stdout == ""


@pytest.mark.parametrize("tool_name", ["Agent", "Bash", "Monitor", "PowerShell", "Workflow"])
def test_development_tools_in_another_tasks_worktree_are_blocked(
    tmp_path, tool_name
):
    repo = _new_repo(tmp_path)
    worktree = _add_worktree(repo, "first-task")
    _write_owner(worktree, "first-session")

    reason = _denial_reason(
        _run_hook(
            repo,
            worktree,
            raw_payload=_payload(
                "PreToolUse",
                repo,
                session_id="second-session",
                tool_name=tool_name,
                tool_input={"command": "git status"},
                cwd=worktree,
            ),
        )
    )

    assert "belongs to task first-session" in reason


def test_concurrent_clean_worktree_claim_has_one_owner(tmp_path):
    repo = _new_repo(tmp_path)
    worktree = _add_worktree(repo, "contended-task")
    env = os.environ.copy()
    env["CLAUDE_PROJECT_DIR"] = str(repo)
    processes = []
    for session_id in ("first-session", "second-session"):
        processes.append(
            subprocess.Popen(
                [sys.executable, str(HOOK_PATH)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
        )
        assert processes[-1].stdin is not None
        processes[-1].stdin.write(
            _payload("SessionStart", repo, session_id=session_id, cwd=worktree)
        )
        processes[-1].stdin.close()

    outputs = []
    for process in processes:
        assert process.stdout is not None
        assert process.stderr is not None
        outputs.append(process.stdout.read())
        stderr = process.stderr.read()
        assert process.wait() == 0, stderr

    owner = json.loads(_owner_path(worktree).read_text())
    assert owner["task_id"] in {"first-session", "second-session"}
    assert sum("Persistent task identity" in output for output in outputs) == 1
    assert sum('"continue": false' in output for output in outputs) == 1


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


def test_settings_enable_ownership_guard_across_lifecycle_events():
    settings = json.loads((REPO_ROOT / ".claude" / "settings.json").read_text())

    assert settings["worktree"]["baseRef"] == "fresh"
    pretool_hooks = [
        entry
        for entry in settings["hooks"]["PreToolUse"]
        if entry.get("matcher") == "EnterWorktree|Write|Edit|NotebookEdit"
    ]
    assert len(pretool_hooks) == 1
    assert all(
        tool not in pretool_hooks[0]["matcher"]
        for tool in ("Bash", "PowerShell", "Monitor", "Agent", "Workflow")
    )
    require_hooks = [
        hook
        for entries in settings["hooks"].values()
        for entry in entries
        for hook in entry["hooks"]
        if "require-development-worktree.py" in hook.get("command", "")
        or any(
            "require-development-worktree.py" in argument
            for argument in hook.get("args", [])
        )
    ]
    assert len(require_hooks) == 5
    assert all(hook["type"] == "command" for hook in require_hooks)
    assert all(
        "require-development-worktree.py"
        in " ".join([hook.get("command", ""), *hook.get("args", [])])
        for hook in require_hooks
    )
    assert settings["hooks"]["PostToolUse"][0]["matcher"] == "EnterWorktree"
