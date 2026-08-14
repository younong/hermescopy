#!/usr/bin/env python3
"""Remove managed Hermes Python runtimes that no running process references."""

from __future__ import annotations

import argparse
import os
import re
import shutil
from pathlib import Path


RUNTIME_NAME_RE = re.compile(r"py311-[A-Za-z0-9_]+-[0-9a-f]{64}-sandbox[0-9]+")


def _resolved_link(path: Path) -> str | None:
    try:
        target = os.readlink(path)
    except FileNotFoundError:
        return None
    if not os.path.isabs(target):
        target = os.path.join(path.parent, target)
    return os.path.realpath(target.removesuffix(" (deleted)"))


def _references_path(value: str, runtime: str) -> bool:
    return value == runtime or value.startswith(f"{runtime}{os.sep}")


def _process_references_runtime(process: Path, runtime: str) -> bool:
    try:
        for name in ("exe", "cwd", "root"):
            target = _resolved_link(process / name)
            if target is not None and _references_path(target, runtime):
                return True

        fd_dir = process / "fd"
        try:
            descriptors = list(fd_dir.iterdir())
        except FileNotFoundError:
            descriptors = []
        for descriptor in descriptors:
            target = _resolved_link(descriptor)
            if target is not None and _references_path(target, runtime):
                return True

        for name in ("maps", "mountinfo"):
            try:
                content = (process / name).read_text(encoding="utf-8", errors="replace")
            except FileNotFoundError:
                continue
            if runtime in content:
                return True
    except PermissionError:
        raise
    except OSError:
        if process.exists():
            raise
    return False


def runtime_is_referenced(runtime: Path, proc_root: Path) -> bool:
    runtime_path = str(runtime.resolve())
    processes = [path for path in proc_root.iterdir() if path.name.isdigit() and path.is_dir()]
    if not processes:
        raise RuntimeError(f"no process entries found under {proc_root}")
    for process in processes:
        try:
            if _process_references_runtime(process, runtime_path):
                return True
        except PermissionError:
            print(f"Keeping runtime because process metadata is unreadable: {runtime}")
            return True
        except OSError as error:
            print(f"Keeping runtime because process inspection failed: {runtime}: {error}")
            return True
    return False


def prune_unused_runtimes(runtimes_dir: Path, keep_runtime: Path, proc_root: Path) -> tuple[int, int]:
    runtimes_dir = runtimes_dir.resolve(strict=True)
    keep_runtime = keep_runtime.resolve(strict=True)
    proc_root = proc_root.resolve(strict=True)
    if keep_runtime.parent != runtimes_dir:
        raise ValueError(f"kept runtime is outside the managed runtime directory: {keep_runtime}")
    if not RUNTIME_NAME_RE.fullmatch(keep_runtime.name):
        raise ValueError(f"kept runtime has an unexpected name: {keep_runtime.name}")

    removed = 0
    kept_referenced = 0
    for candidate in sorted(runtimes_dir.iterdir()):
        if candidate.is_symlink() or not candidate.is_dir():
            continue
        if not RUNTIME_NAME_RE.fullmatch(candidate.name):
            print(f"Skipping unmanaged runtime directory: {candidate}")
            continue
        if candidate.resolve() == keep_runtime:
            print(f"Keeping active runtime: {candidate}")
            continue
        if runtime_is_referenced(candidate, proc_root):
            print(f"Keeping process-referenced runtime: {candidate}")
            kept_referenced += 1
            continue
        print(f"Pruning unused runtime: {candidate}")
        shutil.rmtree(candidate)
        removed += 1

    print(f"Runtime pruning complete: removed={removed} kept_referenced={kept_referenced}")
    return removed, kept_referenced


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtimes-dir", type=Path, required=True)
    parser.add_argument("--keep-runtime", type=Path, required=True)
    parser.add_argument("--proc-root", type=Path, default=Path("/proc"))
    args = parser.parse_args()
    prune_unused_runtimes(args.runtimes_dir, args.keep_runtime, args.proc_root)


if __name__ == "__main__":
    main()
