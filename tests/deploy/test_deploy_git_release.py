from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


DEPLOY_SCRIPT = Path(__file__).parents[2] / "deploy" / "deploy.mjs"


def _run(
    args: list[str],
    cwd: Path,
    *,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        args,
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"command failed ({result.returncode}): {' '.join(args)}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return _run(["git", *args], cwd, check=check)


def _configure_identity(repo: Path) -> None:
    _git(repo, "config", "user.name", "Hermes Release Test")
    _git(repo, "config", "user.email", "release-test@example.com")


def _commit_file(repo: Path, name: str, content: str, message: str) -> str:
    (repo / name).write_text(content)
    _git(repo, "add", name)
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _repositories(tmp_path: Path) -> tuple[Path, Path, Path]:
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    work = tmp_path / "work"
    origin.mkdir()
    seed.mkdir()
    _git(origin, "init", "--bare")
    _git(seed, "init", "-b", "main")
    _configure_identity(seed)
    _commit_file(seed, "base.txt", "base\n", "base")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-u", "origin", "main")
    _git(origin, "symbolic-ref", "HEAD", "refs/heads/main")
    _git(tmp_path, "clone", str(origin), str(work))
    _configure_identity(work)
    return origin, seed, work


def _prepare(
    work: Path,
    tag: str,
    *,
    allow_non_main: bool = False,
    dry_run: bool = False,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    script_url = DEPLOY_SCRIPT.as_uri()
    source = f"""
import {{ prepareCreateTag }} from {json.dumps(script_url)};
try {{
  const result = prepareCreateTag(process.argv[1], {{
    cwd: process.argv[2],
    allowNonMain: process.argv[3] === "true",
    dryRun: process.argv[4] === "true",
  }});
  console.log("RESULT=" + JSON.stringify(result));
}} catch (error) {{
  console.error(error.message);
  process.exit(1);
}}
"""
    return _run(
        [
            "node",
            "--input-type=module",
            "--eval",
            source,
            tag,
            str(work),
            str(allow_non_main).lower(),
            str(dry_run).lower(),
        ],
        work,
        check=False,
        env=env,
    )


def _ref(repo: Path, ref: str) -> str:
    return _git(repo, "rev-parse", ref).stdout.strip()


def _run_deploy_cli(work: Path, *args: str) -> subprocess.CompletedProcess[str]:
    source = f"""
import {{ main }} from {json.dumps(DEPLOY_SCRIPT.as_uri())};
try {{
  await main({{ argv: process.argv.slice(1), cwd: process.cwd() }});
}} catch (error) {{
  console.error(`deploy failed: ${{error.message}}`);
  process.exitCode = 1;
}}
"""
    return _run(
        ["node", "--input-type=module", "--eval", source, "--", *args],
        work,
        check=False,
    )


def _move_remote_branch_before_push(
    tmp_path: Path,
    origin: Path,
    branch: str,
    commit: str,
    *,
    push_number: int,
) -> dict[str, str]:
    wrapper_dir = tmp_path / "git-wrapper"
    wrapper_dir.mkdir()
    counter = wrapper_dir / "push-count"
    real_git = shutil.which("git")
    assert real_git is not None
    if os.name == "nt":
        wrapper = wrapper_dir / "git.cmd"
        wrapper.write_text(
            "@echo off\n"
            "setlocal EnableDelayedExpansion\n"
            'if "%~1"=="push" (\n'
            "  set count=0\n"
            f'  if exist "{counter}" set /p count=<"{counter}"\n'
            "  set /a count+=1\n"
            f'  >"{counter}" echo !count!\n'
            f"  if !count!=={push_number} (\n"
            f'    "{real_git}" --git-dir="{origin}" update-ref refs/heads/{branch} {commit}\n'
            "  )\n"
            ")\n"
            f'"{real_git}" %*\n'
            "exit /b %errorlevel%\n"
        )
    else:
        wrapper = wrapper_dir / "git"
        wrapper.write_text(
            "#!/bin/sh\n"
            f'counter="{counter}"\n'
            'if [ "$1" = push ]; then\n'
            '  count=0\n'
            '  [ ! -f "$counter" ] || count="$(cat "$counter")"\n'
            '  count=$((count + 1))\n'
            '  printf "%s\n" "$count" > "$counter"\n'
            f'  if [ "$count" -eq {push_number} ]; then\n'
            f'    "{real_git}" --git-dir="{origin}" update-ref refs/heads/{branch} {commit}\n'
            "  fi\n"
            "fi\n"
            f'exec "{real_git}" "$@"\n'
        )
        wrapper.chmod(0o755)
    return {**os.environ, "PATH": f"{wrapper_dir}{os.pathsep}{os.environ['PATH']}"}


def test_synchronized_main_publishes_only_the_release_tag(tmp_path):
    origin, _seed, work = _repositories(tmp_path)
    main = _ref(work, "main")
    _git(work, "tag", "unrelated-local-tag")

    result = _prepare(work, "v-test-main")

    assert result.returncode == 0, result.stderr
    assert _ref(work, "main") == main
    assert _ref(origin, "refs/heads/main") == main
    assert _ref(work, "v-test-main^{commit}") == main
    assert _ref(origin, "refs/tags/v-test-main^{commit}") == main
    assert "git rebase" not in result.stdout
    push_lines = "\n".join(
        line for line in result.stdout.splitlines() if "$ git push" in line
    )
    assert "refs/heads/main" not in push_lines
    assert "--force-with-lease" not in push_lines
    assert "--tags" not in result.stdout
    assert _git(
        origin, "rev-parse", "--verify", "refs/tags/unrelated-local-tag", check=False
    ).returncode != 0


def test_stale_main_is_rejected_without_moving_refs(tmp_path):
    origin, seed, work = _repositories(tmp_path)
    local_main = _ref(work, "main")
    latest = _commit_file(seed, "remote.txt", "remote\n", "advance main")
    _git(seed, "push", "origin", "main")

    result = _prepare(work, "v-test-stale")

    assert result.returncode != 0
    assert "behind origin/main" in result.stderr
    assert _ref(work, "main") == local_main
    assert _ref(origin, "refs/heads/main") == latest
    assert _git(work, "rev-parse", "--verify", "refs/tags/v-test-stale", check=False).returncode != 0
    assert _git(origin, "rev-parse", "--verify", "refs/tags/v-test-stale", check=False).returncode != 0


def test_main_with_unmerged_commits_is_rejected(tmp_path):
    origin, _seed, work = _repositories(tmp_path)
    remote_main = _ref(origin, "refs/heads/main")
    local_main = _commit_file(work, "local.txt", "local\n", "unmerged local change")

    result = _prepare(work, "v-test-ahead")

    assert result.returncode != 0
    assert "not merged into origin/main" in result.stderr
    assert _ref(work, "main") == local_main
    assert _ref(origin, "refs/heads/main") == remote_main


def test_non_main_release_without_emergency_flag_is_rejected(tmp_path):
    origin, _seed, work = _repositories(tmp_path)
    _git(work, "checkout", "-b", "release/candidate")
    local_head = _commit_file(work, "feature.txt", "feature\n", "feature")
    before_remote = _git(origin, "show-ref").stdout

    result = _prepare(work, "v-test-no-emergency")

    assert result.returncode != 0
    assert "Merge the change through a PR" in result.stderr
    assert "--allow-non-main" not in result.stderr
    assert _ref(work, "HEAD") == local_head
    assert _git(origin, "show-ref").stdout == before_remote
    assert _git(work, "rev-parse", "--verify", "refs/tags/v-test-no-emergency", check=False).returncode != 0


def test_non_main_release_rebases_onto_origin_main_and_pushes_same_branch(tmp_path):
    origin, seed, work = _repositories(tmp_path)
    _git(work, "checkout", "-b", "release/candidate")
    old_local = _commit_file(work, "feature.txt", "feature\n", "feature")
    latest_main = _commit_file(seed, "remote.txt", "remote\n", "advance main")
    _git(seed, "push", "origin", "main")

    result = _prepare(work, "v-test-branch", allow_non_main=True)

    assert result.returncode == 0, result.stderr
    prepared = _ref(work, "HEAD")
    assert prepared != old_local
    assert _ref(origin, "refs/heads/release/candidate") == prepared
    assert _ref(origin, "refs/tags/v-test-branch^{commit}") == prepared
    assert _git(work, "merge-base", "--is-ancestor", latest_main, prepared).returncode == 0


def test_create_tag_rejects_dirty_worktree_even_in_dry_run(tmp_path):
    origin, _seed, work = _repositories(tmp_path)
    before_local = _git(work, "show-ref").stdout
    before_remote = _git(origin, "show-ref").stdout
    (work / "untracked.txt").write_text("not committed\n")

    result = _prepare(work, "v-test-dirty", dry_run=True)

    assert result.returncode != 0
    assert "Working tree is not clean" in result.stderr
    assert _git(work, "show-ref").stdout == before_local
    assert _git(origin, "show-ref").stdout == before_remote


def test_emergency_rebase_conflict_aborts_without_pushing_or_tagging(tmp_path):
    origin, seed, work = _repositories(tmp_path)
    _git(work, "checkout", "-b", "release/candidate")
    local_head = _commit_file(work, "base.txt", "local\n", "local conflict")
    _commit_file(seed, "base.txt", "remote\n", "remote conflict")
    _git(seed, "push", "origin", "main")
    remote_head = _ref(origin, "refs/heads/main")

    result = _prepare(work, "v-test-conflict", allow_non_main=True)

    assert result.returncode != 0
    assert "Rebase onto origin/main failed" in result.stderr
    assert _ref(work, "HEAD") == local_head
    assert _ref(origin, "refs/heads/main") == remote_head
    assert not (work / ".git" / "rebase-merge").exists()
    assert not (work / ".git" / "rebase-apply").exists()
    assert _git(work, "rev-parse", "--verify", "refs/tags/v-test-conflict", check=False).returncode != 0
    assert _git(origin, "rev-parse", "--verify", "refs/tags/v-test-conflict", check=False).returncode != 0


def test_main_dry_run_reports_tag_only_publication_without_changing_refs(tmp_path):
    origin, _seed, work = _repositories(tmp_path)
    before_local = _git(work, "show-ref").stdout
    before_remote = _git(origin, "show-ref").stdout

    result = _prepare(work, "v-test-dry-run", dry_run=True)

    assert result.returncode == 0, result.stderr
    assert _git(work, "show-ref").stdout == before_local
    assert _git(origin, "show-ref").stdout == before_remote
    assert "git fetch --no-tags origin" in result.stdout
    assert "git tag -a v-test-dry-run" in result.stdout
    push_lines = "\n".join(
        line for line in result.stdout.splitlines() if "git push" in line
    )
    assert "git push origin refs/tags/v-test-dry-run:refs/tags/v-test-dry-run" in push_lines
    assert "refs/heads/main" not in push_lines
    assert "--force-with-lease" not in push_lines
    assert "git rebase" not in result.stdout
    assert "<post-rebase-commit>" not in result.stdout
    assert "--tags" not in result.stdout


def test_create_tag_cli_dry_run_remains_tag_sourced(tmp_path):
    _origin, _seed, work = _repositories(tmp_path)
    result = _run_deploy_cli(
        work,
        "--create-tag",
        "v-test-cli",
        "--dry-run",
    )

    assert result.returncode == 0, result.stderr
    assert "Tag: v-test-cli" in result.stdout
    assert "/releases/v-test-cli" in result.stdout
    assert "Commit SHA:" not in result.stdout
    assert "/releases/commit-" not in result.stdout


def test_create_tag_cli_dry_run_reports_powerpoint_provisioning(tmp_path):
    _origin, _seed, work = _repositories(tmp_path)
    result = _run_deploy_cli(
        work,
        "--create-tag",
        "v-test-powerpoint",
        "--provision-powerpoint-deps",
        "--dry-run",
    )

    assert result.returncode == 0, result.stderr
    assert "PowerPoint runtime smoke: planned" in result.stdout
    assert "Authority concurrency smoke: planned" in result.stdout
    assert "PowerPoint host provisioning: enabled" in result.stdout
    assert 'npm ci --omit=dev --ignore-scripts --no-audit' in result.stdout
    assert "v-test-powerpoint" in result.stdout


def test_cli_rejects_allow_non_main_with_existing_tag(tmp_path):
    _origin, _seed, work = _repositories(tmp_path)

    result = _run_deploy_cli(
        work,
        "--tag",
        "v-test-existing",
        "--allow-non-main",
        "--dry-run",
    )

    assert result.returncode != 0
    assert "--allow-non-main is only valid with --create-tag" in result.stderr


def test_cli_rejects_removed_commit_ref_source(tmp_path):
    _origin, _seed, work = _repositories(tmp_path)
    commit = _ref(work, "HEAD")

    result = _run_deploy_cli(work, "--ref", commit, "--dry-run")

    assert result.returncode != 0
    assert "Unknown argument: --ref" in result.stderr


def test_existing_tag_cli_rejects_local_only_tag(tmp_path):
    _origin, _seed, work = _repositories(tmp_path)
    _git(work, "tag", "-a", "v-test-local-only", "-m", "local only")

    result = _run_deploy_cli(work, "--tag", "v-test-local-only", "--dry-run")

    assert result.returncode != 0
    assert "Tag does not exist on origin" in result.stderr


def test_existing_tag_cli_rejects_local_remote_commit_mismatch(tmp_path):
    _origin, seed, work = _repositories(tmp_path)
    local_commit = _ref(work, "HEAD")
    remote_commit = _commit_file(seed, "tagged.txt", "remote tag\n", "remote tag commit")
    _git(seed, "tag", "-a", "v-test-mismatch", "-m", "remote", remote_commit)
    _git(seed, "push", "origin", "refs/tags/v-test-mismatch")
    _git(work, "tag", "-a", "v-test-mismatch", "-m", "local", local_commit)

    result = _run_deploy_cli(work, "--tag", "v-test-mismatch", "--dry-run")

    assert result.returncode != 0
    assert "do not resolve to the same commit" in result.stderr


def test_existing_published_tag_cli_dry_run_succeeds(tmp_path):
    _origin, _seed, work = _repositories(tmp_path)
    commit = _ref(work, "HEAD")
    _git(work, "tag", "-a", "v-test-published", "-m", "published", commit)
    _git(work, "push", "origin", "refs/tags/v-test-published")

    result = _run_deploy_cli(work, "--tag", "v-test-published", "--dry-run")

    assert result.returncode == 0, result.stderr
    assert "Tag: v-test-published" in result.stdout
    assert "/releases/v-test-published" in result.stdout


def test_existing_remote_tag_is_not_overwritten(tmp_path):
    origin, seed, work = _repositories(tmp_path)
    _git(seed, "tag", "-a", "v-test-existing", "-m", "existing")
    _git(seed, "push", "origin", "refs/tags/v-test-existing")
    existing = _ref(origin, "refs/tags/v-test-existing^{commit}")

    result = _prepare(work, "v-test-existing")

    assert result.returncode != 0
    assert "Tag already exists on origin" in result.stderr
    assert _ref(origin, "refs/tags/v-test-existing^{commit}") == existing


def test_detached_head_is_rejected_even_with_non_main_override(tmp_path):
    origin, _seed, work = _repositories(tmp_path)
    _git(work, "checkout", "--detach", "HEAD")
    before_remote = _git(origin, "show-ref").stdout

    result = _prepare(work, "v-test-detached", allow_non_main=True)

    assert result.returncode != 0
    assert "detached HEAD is not supported" in result.stderr
    assert _git(origin, "show-ref").stdout == before_remote


def test_exact_lease_rewrites_existing_remote_branch_after_rebase(tmp_path):
    origin, seed, work = _repositories(tmp_path)
    _git(seed, "checkout", "-b", "release/candidate")
    old_remote = _commit_file(seed, "remote-branch.txt", "remote branch\n", "remote branch")
    _git(seed, "push", "origin", "release/candidate")
    _git(work, "checkout", "-b", "release/candidate")
    old_local = _commit_file(work, "local-branch.txt", "local branch\n", "local branch")

    result = _prepare(work, "v-test-non-ff", allow_non_main=True)

    assert result.returncode == 0, result.stderr
    prepared = _ref(work, "HEAD")
    assert prepared == old_local
    assert prepared != old_remote
    assert _ref(origin, "refs/heads/release/candidate") == prepared
    assert _ref(origin, "refs/tags/v-test-non-ff^{commit}") == prepared


@pytest.mark.skipif(os.name == "nt", reason="the Git push race wrapper requires POSIX executable lookup")
def test_exact_lease_preserves_branch_that_moves_before_initial_push(tmp_path):
    origin, seed, work = _repositories(tmp_path)
    _git(seed, "checkout", "-b", "release/candidate")
    _commit_file(seed, "remote-branch.txt", "remote branch\n", "remote branch")
    _git(seed, "push", "origin", "release/candidate")
    concurrent = _commit_file(seed, "concurrent.txt", "concurrent\n", "concurrent update")
    _git(seed, "push", "origin", "HEAD:refs/heads/race-source")
    _git(work, "checkout", "-b", "release/candidate")
    _commit_file(work, "local-branch.txt", "local branch\n", "local branch")
    env = _move_remote_branch_before_push(
        tmp_path, origin, "release/candidate", concurrent, push_number=1
    )

    result = _prepare(work, "v-test-initial-race", allow_non_main=True, env=env)

    assert result.returncode != 0
    assert "stale info" in result.stderr or "rejected" in result.stderr
    assert _ref(origin, "refs/heads/release/candidate") == concurrent
    assert _git(work, "rev-parse", "--verify", "refs/tags/v-test-initial-race", check=False).returncode != 0
    assert _git(origin, "rev-parse", "--verify", "refs/tags/v-test-initial-race", check=False).returncode != 0


@pytest.mark.skipif(os.name == "nt", reason="the Git push race wrapper requires POSIX executable lookup")
def test_exact_lease_rejects_branch_move_before_atomic_tag_push(tmp_path):
    origin, seed, work = _repositories(tmp_path)
    _git(seed, "checkout", "-b", "release/candidate")
    _commit_file(seed, "remote-branch.txt", "remote branch\n", "remote branch")
    _git(seed, "push", "origin", "release/candidate")
    concurrent = _commit_file(seed, "concurrent.txt", "concurrent\n", "concurrent update")
    _git(seed, "push", "origin", "HEAD:refs/heads/race-source")
    _git(work, "checkout", "-b", "release/candidate")
    _commit_file(work, "local-branch.txt", "local branch\n", "local branch")
    _git(work, "tag", "unrelated-local-tag")
    env = _move_remote_branch_before_push(
        tmp_path, origin, "release/candidate", concurrent, push_number=2
    )

    result = _prepare(work, "v-test-atomic-race", allow_non_main=True, env=env)

    assert result.returncode != 0
    assert "stale info" in result.stderr or "rejected" in result.stderr
    assert _ref(origin, "refs/heads/release/candidate") == concurrent
    assert _git(work, "rev-parse", "--verify", "refs/tags/v-test-atomic-race", check=False).returncode != 0
    assert _git(origin, "rev-parse", "--verify", "refs/tags/v-test-atomic-race", check=False).returncode != 0
    assert _git(work, "rev-parse", "--verify", "refs/tags/unrelated-local-tag").returncode == 0


def test_atomic_tag_rejection_cleans_only_the_new_local_tag(tmp_path):
    origin, _seed, work = _repositories(tmp_path)
    hook = origin / "hooks" / "pre-receive"
    hook.write_text(
        "#!/bin/sh\n"
        "while read old new ref; do\n"
        "  case \"$ref\" in refs/tags/v-test-rejected) exit 1 ;; esac\n"
        "done\n"
        "exit 0\n"
    )
    hook.chmod(0o755)
    _git(work, "tag", "unrelated-local-tag")

    result = _prepare(work, "v-test-rejected")

    assert result.returncode != 0
    assert _git(work, "rev-parse", "--verify", "refs/tags/v-test-rejected", check=False).returncode != 0
    assert _git(origin, "rev-parse", "--verify", "refs/tags/v-test-rejected", check=False).returncode != 0
    assert _git(work, "rev-parse", "--verify", "refs/tags/unrelated-local-tag").returncode == 0
