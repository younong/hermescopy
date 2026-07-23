from __future__ import annotations

import os
from pathlib import Path

import pytest

from hermes_cli.owner_worker.cgroup_bootstrap import (
    CgroupBootstrapUnavailable,
    prepare_delegated_subtree,
)


def _hierarchy(tmp_path: Path):
    mount = tmp_path / "cgroup"
    service = mount / "system.slice" / "hermes-dashboard.service"
    managed = service / "authenticated-owners"
    service.mkdir(parents=True)
    (service / "cgroup.procs").write_text(f"{os.getpid()}\n", encoding="ascii")
    (service / "cgroup.controllers").write_text("cpu memory pids io\n", encoding="ascii")
    (service / "cgroup.subtree_control").write_text("", encoding="ascii")
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        f"31 23 0:28 / {mount} rw - cgroup2 cgroup rw\n",
        encoding="utf-8",
    )
    proc = tmp_path / "cgroup.txt"
    proc.write_text("0::/system.slice/hermes-dashboard.service\n", encoding="ascii")
    return managed, proc, mountinfo


def test_prepares_empty_sibling_root_after_moving_control_plane(tmp_path, monkeypatch):
    managed, proc, mountinfo = _hierarchy(tmp_path)
    original_write = Path.open

    class _ControlWriter:
        def __init__(self, path: Path):
            self.path = path

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def write(self, value: str):
            if self.path.name == "cgroup.procs" and self.path.parent.name == "control-plane":
                with original_write(self.path, "w", encoding="ascii") as handle:
                    handle.write(value)
                with original_write(managed.parent / "cgroup.procs", "w", encoding="ascii") as handle:
                    handle.write("")
            elif self.path.name == "cgroup.subtree_control":
                with original_write(self.path, "w", encoding="ascii") as handle:
                    handle.write("cpu memory pids")
                managed.mkdir(exist_ok=True)
                for name, value in {
                    "cgroup.procs": "",
                    "cgroup.controllers": "cpu memory pids",
                    "cgroup.subtree_control": "",
                }.items():
                    with original_write(managed / name, "w", encoding="ascii") as handle:
                        handle.write(value)
            return len(value)

    def fake_open(path, mode="r", *args, **kwargs):
        candidate = Path(path)
        if mode == "w" and candidate.name in {"cgroup.procs", "cgroup.subtree_control"}:
            return _ControlWriter(candidate)
        return original_write(candidate, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fake_open)
    assert prepare_delegated_subtree(managed, proc_cgroup=proc, mountinfo=mountinfo) == managed
    assert (managed.parent / "control-plane" / "cgroup.procs").read_text().strip() == str(os.getpid())
    assert managed.is_dir()


def test_rejects_missing_required_controller(tmp_path, monkeypatch):
    managed, proc, mountinfo = _hierarchy(tmp_path)
    (managed.parent / "cgroup.controllers").write_text("cpu memory\n", encoding="ascii")
    original_write = Path.open

    class _ControlWriter:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def write(self, value: str):
            control = managed.parent / "control-plane" / "cgroup.procs"
            with original_write(control, "w", encoding="ascii") as handle:
                handle.write(value)
            with original_write(managed.parent / "cgroup.procs", "w", encoding="ascii") as handle:
                handle.write("")
            return len(value)

    def fake_open(path, mode="r", *args, **kwargs):
        candidate = Path(path)
        if mode == "w" and candidate.name == "cgroup.procs":
            return _ControlWriter()
        return original_write(candidate, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fake_open)
    with pytest.raises(CgroupBootstrapUnavailable, match="controllers"):
        prepare_delegated_subtree(managed, proc_cgroup=proc, mountinfo=mountinfo)
