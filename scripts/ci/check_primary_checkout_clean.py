#!/usr/bin/env python3
"""Fail when the repository's primary checkout contains changes."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def git_output(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout


def primary_checkout(cwd: Path) -> Path:
    output = git_output(cwd, "worktree", "list", "--porcelain")
    for block in output.split("\n\n"):
        for line in block.splitlines():
            if line.startswith("worktree "):
                return Path(line.removeprefix("worktree ")).resolve()
    raise RuntimeError("git worktree list did not report a primary checkout")


def main() -> int:
    cwd = Path.cwd()
    try:
        primary = primary_checkout(cwd)
        status = git_output(primary, "status", "--porcelain", "--untracked-files=all")
    except (OSError, subprocess.CalledProcessError, RuntimeError) as exc:
        print(f"primary checkout cleanliness check failed: {exc}", file=sys.stderr)
        return 1
    if status:
        print(f"primary checkout is dirty: {primary}", file=sys.stderr)
        print(status, file=sys.stderr, end="")
        return 1
    print(f"primary checkout is clean: {primary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
