from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
PRUNER = ROOT / "deploy" / "prune-unused-runtimes.py"


def _load_pruner():
    spec = importlib.util.spec_from_file_location("deploy_runtime_pruner", PRUNER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _runtime_name(digest: str) -> str:
    return f"py311-x86_64-{digest * 64}-sandbox10"


def _make_process(proc_root: Path, pid: int, runtime: Path | None = None) -> Path:
    process = proc_root / str(pid)
    process.mkdir()
    (process / "fd").mkdir()
    (process / "maps").write_text("", encoding="utf-8")
    (process / "mountinfo").write_text("", encoding="utf-8")
    for name in ("exe", "cwd", "root"):
        os.symlink(runtime if name == "exe" and runtime is not None else "/", process / name)
    return process


def test_prune_removes_only_unreferenced_managed_runtimes(tmp_path):
    pruner = _load_pruner()
    runtimes = tmp_path / "runtimes"
    proc_root = tmp_path / "proc"
    runtimes.mkdir()
    proc_root.mkdir()
    active = runtimes / _runtime_name("a")
    referenced = runtimes / _runtime_name("b")
    unused = runtimes / _runtime_name("c")
    unmanaged = runtimes / "manual-backup"
    for runtime in (active, referenced, unused, unmanaged):
        runtime.mkdir()
        (runtime / "marker").write_text(runtime.name, encoding="utf-8")
    _make_process(proc_root, 100, referenced)

    removed, kept_referenced = pruner.prune_unused_runtimes(runtimes, active, proc_root)

    assert (removed, kept_referenced) == (1, 1)
    assert active.is_dir()
    assert referenced.is_dir()
    assert not unused.exists()
    assert unmanaged.is_dir()


def test_prune_detects_runtime_references_in_maps_and_file_descriptors(tmp_path):
    pruner = _load_pruner()
    runtimes = tmp_path / "runtimes"
    proc_root = tmp_path / "proc"
    runtimes.mkdir()
    proc_root.mkdir()
    active = runtimes / _runtime_name("a")
    mapped = runtimes / _runtime_name("b")
    opened = runtimes / _runtime_name("c")
    unused = runtimes / _runtime_name("d")
    for runtime in (active, mapped, opened, unused):
        runtime.mkdir()
    mapped_process = _make_process(proc_root, 100)
    (mapped_process / "maps").write_text(
        f"7f00-7f01 r-xp 00000000 00:00 0 {mapped}/lib/python.so\n",
        encoding="utf-8",
    )
    opened_process = _make_process(proc_root, 101)
    os.symlink(opened / "state.db", opened_process / "fd" / "7")

    removed, kept_referenced = pruner.prune_unused_runtimes(runtimes, active, proc_root)

    assert (removed, kept_referenced) == (1, 2)
    assert mapped.is_dir()
    assert opened.is_dir()
    assert not unused.exists()


def test_prune_fails_closed_without_process_entries(tmp_path):
    pruner = _load_pruner()
    runtimes = tmp_path / "runtimes"
    proc_root = tmp_path / "proc"
    runtimes.mkdir()
    proc_root.mkdir()
    active = runtimes / _runtime_name("a")
    unused = runtimes / _runtime_name("b")
    active.mkdir()
    unused.mkdir()

    with pytest.raises(RuntimeError, match="no process entries"):
        pruner.prune_unused_runtimes(runtimes, active, proc_root)

    assert active.is_dir()
    assert unused.is_dir()
