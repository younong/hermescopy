from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[2] / "scripts" / "ci" / "check_primary_checkout_clean.py"
spec = importlib.util.spec_from_file_location("check_primary_checkout_clean", SCRIPT)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def git(repo: Path, *args: str) -> None:
    import subprocess

    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-b", "main")
    (root / "tracked.txt").write_text("initial\n")
    git(root, "add", "tracked.txt")
    git(root, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "init")
    return root


def test_primary_checkout_clean(monkeypatch, tmp_path):
    root = repo(tmp_path)
    monkeypatch.chdir(root)
    assert module.main() == 0


def test_primary_checkout_dirty_tracked_file(monkeypatch, tmp_path):
    root = repo(tmp_path)
    (root / "tracked.txt").write_text("changed\n")
    monkeypatch.chdir(root)
    assert module.main() == 1


def test_primary_checkout_dirty_untracked_file(monkeypatch, tmp_path):
    root = repo(tmp_path)
    (root / "new.txt").write_text("new\n")
    monkeypatch.chdir(root)
    assert module.main() == 1


def test_primary_checkout_checked_from_linked_worktree(monkeypatch, tmp_path):
    root = repo(tmp_path)
    worktree = tmp_path / "worktree"
    git(root, "worktree", "add", "-b", "feature", str(worktree))
    monkeypatch.chdir(worktree)
    assert module.main() == 0


def test_non_git_directory_fails(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert module.main() == 1
