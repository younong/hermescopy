from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess


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


def _install_deploy_script(repo: Path) -> Path:
    script = repo / "deploy" / "deploy.mjs"
    script.parent.mkdir()
    shutil.copy2(DEPLOY_SCRIPT, script)
    _git(repo, "add", script.relative_to(repo).as_posix())
    _git(repo, "commit", "-m", "add deploy script")
    _git(repo, "push", "origin", "main")
    return script


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
    wrapper = wrapper_dir / "git"
    counter = wrapper_dir / "push-count"
    real_git = shutil.which("git")
    assert real_git is not None
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


def test_stale_clean_main_is_updated_before_the_release_tag(tmp_path):
    origin, seed, work = _repositories(tmp_path)
    latest = _commit_file(seed, "remote.txt", "remote\n", "advance main")
    _git(seed, "push", "origin", "main")
    _git(work, "tag", "unrelated-local-tag")

    result = _prepare(work, "v-test-main")

    assert result.returncode == 0, result.stderr
    assert _ref(work, "main") == latest
    assert _ref(work, "v-test-main^{commit}") == latest
    assert _ref(origin, "refs/heads/main") == latest
    assert _ref(origin, "refs/tags/v-test-main^{commit}") == latest
    assert _git(
        origin, "rev-parse", "--verify", "refs/tags/unrelated-local-tag", check=False
    ).returncode != 0


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


def test_rebase_conflict_aborts_without_pushing_or_tagging(tmp_path):
    origin, seed, work = _repositories(tmp_path)
    local_head = _commit_file(work, "base.txt", "local\n", "local conflict")
    _commit_file(seed, "base.txt", "remote\n", "remote conflict")
    _git(seed, "push", "origin", "main")
    remote_head = _ref(origin, "refs/heads/main")

    result = _prepare(work, "v-test-conflict")

    assert result.returncode != 0
    assert "Rebase onto origin/main failed" in result.stderr
    assert _ref(work, "HEAD") == local_head
    assert _ref(origin, "refs/heads/main") == remote_head
    assert not (work / ".git" / "rebase-merge").exists()
    assert not (work / ".git" / "rebase-apply").exists()
    assert _git(work, "rev-parse", "--verify", "refs/tags/v-test-conflict", check=False).returncode != 0
    assert _git(origin, "rev-parse", "--verify", "refs/tags/v-test-conflict", check=False).returncode != 0


def test_dry_run_reports_sync_without_changing_refs(tmp_path):
    origin, seed, work = _repositories(tmp_path)
    _commit_file(seed, "remote.txt", "remote\n", "advance main")
    _git(seed, "push", "origin", "main")
    before_local = _git(work, "show-ref").stdout
    before_remote = _git(origin, "show-ref").stdout

    result = _prepare(work, "v-test-dry-run", dry_run=True)

    assert result.returncode == 0, result.stderr
    assert _git(work, "show-ref").stdout == before_local
    assert _git(origin, "show-ref").stdout == before_remote
    assert "git fetch --no-tags origin" in result.stdout
    assert "git rebase --no-autostash refs/remotes/origin/main" in result.stdout
    remote_main = _ref(origin, "refs/heads/main")
    assert (
        f"git push --force-with-lease=refs/heads/main:{remote_main} origin "
        "<post-rebase-commit>:refs/heads/main"
    ) in result.stdout
    assert "git tag -a v-test-dry-run" in result.stdout
    assert (
        "git push --atomic "
        "--force-with-lease=refs/heads/main:<post-rebase-commit> origin"
    ) in result.stdout
    push_lines = [line for line in result.stdout.splitlines() if "git push" in line]
    assert all(" --force " not in line and " +" not in line for line in push_lines)
    assert all("--force-with-lease=refs/heads/main:" in line for line in push_lines)
    assert "--tags" not in result.stdout


def test_create_tag_cli_dry_run_remains_tag_sourced(tmp_path):
    _origin, _seed, work = _repositories(tmp_path)
    script = _install_deploy_script(work)

    result = _run(
        ["node", str(script), "--create-tag", "v-test-cli", "--dry-run"],
        work,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Tag: v-test-cli" in result.stdout
    assert "/releases/v-test-cli" in result.stdout
    assert "Commit SHA:" not in result.stdout
    assert "/releases/commit-" not in result.stdout


def test_create_tag_cli_dry_run_reports_powerpoint_provisioning(tmp_path):
    _origin, _seed, work = _repositories(tmp_path)
    script = _install_deploy_script(work)

    result = _run(
        [
            "node",
            str(script),
            "--create-tag",
            "v-test-powerpoint",
            "--provision-powerpoint-deps",
            "--dry-run",
        ],
        work,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "PowerPoint runtime smoke: planned" in result.stdout
    assert "PowerPoint host provisioning: enabled" in result.stdout
    assert 'npm ci --omit=dev --ignore-scripts --no-audit' in result.stdout
    assert "v-test-powerpoint" in result.stdout


def test_create_tag_cli_rejects_commit_ref_override(tmp_path):
    _origin, _seed, work = _repositories(tmp_path)
    script = _install_deploy_script(work)
    commit = _ref(work, "HEAD")

    result = _run(
        [
            "node",
            str(script),
            "--create-tag",
            "v-test-cli",
            "--ref",
            commit,
            "--dry-run",
        ],
        work,
        check=False,
    )

    assert result.returncode != 0
    assert "Pass exactly one of --tag, --create-tag, or --ref." in result.stderr


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
