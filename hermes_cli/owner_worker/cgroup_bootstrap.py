"""Prepare a systemd-delegated cgroup v2 subtree before Dashboard exec.

The service process starts in the delegated unit cgroup.  It moves itself into a
control-plane leaf before enabling controllers on the now-empty unit cgroup,
leaving the sibling authenticated-owner root empty for ``CgroupV2Manager``.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path


_CONTROLLERS = ("cpu", "memory", "pids")
_COMPONENT_RE = re.compile(r"[a-z][a-z0-9-]*\Z")


class CgroupBootstrapUnavailable(RuntimeError):
    """The current service cannot establish its delegated cgroup topology."""


def _current_unified_path(proc_cgroup: Path = Path("/proc/self/cgroup")) -> str:
    try:
        lines = proc_cgroup.read_text(encoding="ascii").splitlines()
    except OSError as exc:
        raise CgroupBootstrapUnavailable("unified process cgroup is unavailable") from exc
    matches = [line.split(":", 2)[2] for line in lines if line.startswith("0::")]
    if len(matches) != 1 or not matches[0].startswith("/"):
        raise CgroupBootstrapUnavailable("unified process cgroup is unavailable")
    return matches[0]


def _mountpoint(mountinfo: Path = Path("/proc/self/mountinfo")) -> Path:
    try:
        lines = mountinfo.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise CgroupBootstrapUnavailable("cgroup v2 mount is unavailable") from exc
    matches: list[Path] = []
    for line in lines:
        fields = line.split()
        try:
            separator = fields.index("-")
        except ValueError:
            continue
        if separator + 1 < len(fields) and fields[separator + 1] == "cgroup2" and len(fields) >= 5:
            matches.append(Path(fields[4].replace("\\040", " ")))
    if len(matches) != 1:
        raise CgroupBootstrapUnavailable("exactly one cgroup v2 mount is required")
    return matches[0]


def _write(path: Path, value: str) -> None:
    try:
        with path.open("w", encoding="ascii") as handle:
            handle.write(value)
    except OSError as exc:
        raise CgroupBootstrapUnavailable("delegated cgroup control is unavailable") from exc


def prepare_delegated_subtree(
    managed_root: str | Path,
    *,
    proc_cgroup: Path = Path("/proc/self/cgroup"),
    mountinfo: Path = Path("/proc/self/mountinfo"),
) -> Path:
    """Move this process to ``control-plane`` and return the empty managed root."""
    root = Path(managed_root)
    if not root.is_absolute() or root.name != "authenticated-owners":
        raise CgroupBootstrapUnavailable("managed cgroup root is invalid")
    mount = _mountpoint(mountinfo)
    current = _current_unified_path(proc_cgroup)
    current_path = mount / current.lstrip("/")
    service_root = root.parent
    control_plane = service_root / "control-plane"
    if current_path not in {service_root, control_plane}:
        raise CgroupBootstrapUnavailable("process is outside the expected delegated service cgroup")
    if mount != root and mount not in root.parents:
        raise CgroupBootstrapUnavailable("managed root is outside cgroup v2")
    if not _COMPONENT_RE.fullmatch(root.name) or not _COMPONENT_RE.fullmatch(control_plane.name):
        raise CgroupBootstrapUnavailable("managed cgroup component is invalid")
    try:
        control_plane.mkdir(mode=0o755, exist_ok=True)
    except OSError as exc:
        raise CgroupBootstrapUnavailable("control-plane cgroup cannot be created") from exc
    if current_path == service_root:
        _write(control_plane / "cgroup.procs", str(os.getpid()))
    try:
        if str(os.getpid()) not in (control_plane / "cgroup.procs").read_text(encoding="ascii").split():
            raise CgroupBootstrapUnavailable("control-plane membership could not be verified")
        if (service_root / "cgroup.procs").read_text(encoding="ascii").strip():
            raise CgroupBootstrapUnavailable("delegated service cgroup is not empty")
        available = set((service_root / "cgroup.controllers").read_text(encoding="ascii").split())
    except OSError as exc:
        raise CgroupBootstrapUnavailable("delegated cgroup state cannot be verified") from exc
    if not set(_CONTROLLERS).issubset(available):
        raise CgroupBootstrapUnavailable("required cgroup controllers were not delegated")
    _write(
        service_root / "cgroup.subtree_control",
        " ".join(f"+{controller}" for controller in _CONTROLLERS),
    )
    try:
        enabled = set((service_root / "cgroup.subtree_control").read_text(encoding="ascii").split())
        if not set(_CONTROLLERS).issubset(enabled):
            raise CgroupBootstrapUnavailable("required cgroup controllers could not be enabled")
        root.mkdir(mode=0o755, exist_ok=True)
        if (root / "cgroup.procs").read_text(encoding="ascii").strip():
            raise CgroupBootstrapUnavailable("authenticated-owner cgroup root is populated")
    except OSError as exc:
        raise CgroupBootstrapUnavailable("authenticated-owner cgroup root cannot be prepared") from exc
    return root


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--managed-root", required=True)
    parser.add_argument("--require", action="store_true")
    parser.add_argument("argv", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    argv = list(args.argv)
    if argv[:1] == ["--"]:
        argv = argv[1:]
    if not argv:
        parser.error("a command is required")
    try:
        prepare_delegated_subtree(args.managed_root)
    except CgroupBootstrapUnavailable as exc:
        if args.require:
            raise SystemExit(str(exc)) from exc
        print(f"authenticated tool resource governance unavailable: {exc}", file=sys.stderr)
    os.execvpe(argv[0], argv, os.environ)


if __name__ == "__main__":
    main()
