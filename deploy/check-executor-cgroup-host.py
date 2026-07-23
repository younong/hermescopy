#!/usr/bin/env python3
"""Read-only preflight for authenticated executor cgroup v2 enforcement."""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any, Sequence


_REQUIRED_CONTROLLERS = {"cpu", "memory", "pids"}


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="ascii").strip()
    except OSError:
        return ""


def _mountpoint(path: Path = Path("/proc/self/mountinfo")) -> Path | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    matches: list[Path] = []
    for line in lines:
        fields = line.split()
        try:
            separator = fields.index("-")
        except ValueError:
            continue
        if separator + 1 < len(fields) and fields[separator + 1] == "cgroup2" and len(fields) >= 5:
            matches.append(Path(fields[4].replace("\\040", " ")))
    return matches[0] if len(matches) == 1 else None


def inspect(managed_root: Path, *, service: str) -> dict[str, Any]:
    mount = _mountpoint()
    root = managed_root.resolve(strict=False)
    service_root = root.parent
    controllers = set(_read(service_root / "cgroup.controllers").split())
    delegated = _REQUIRED_CONTROLLERS.issubset(controllers)
    controls = {
        "memorySwapMax": (service_root / "memory.swap.max").exists(),
        "cgroupFreeze": (service_root / "cgroup.freeze").exists(),
        "cgroupKill": (service_root / "cgroup.kill").exists(),
    }
    systemd: dict[str, str] = {}
    try:
        completed = subprocess.run(
            [
                "systemctl", "show", service,
                "--property=Delegate", "--property=CPUAccounting",
                "--property=MemoryAccounting", "--property=TasksAccounting",
                "--property=KillMode",
            ],
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
            env={"PATH": "/usr/bin:/bin"},
        )
        if completed.returncode == 0:
            systemd = dict(
                line.split("=", 1)
                for line in completed.stdout.splitlines()
                if "=" in line
            )
    except (OSError, subprocess.SubprocessError):
        systemd = {}
    delegated_value = systemd.get("Delegate", "")
    delegated_unit = delegated_value == "yes" or set(delegated_value.split()) >= _REQUIRED_CONTROLLERS
    unit_ok = (
        delegated_unit
        and systemd.get("CPUAccounting") == "yes"
        and systemd.get("MemoryAccounting") == "yes"
        and systemd.get("TasksAccounting") == "yes"
        and systemd.get("KillMode") == "control-group"
    )
    unified = mount is not None and (mount == service_root or mount in service_root.parents)
    service_processes = len(_read(service_root / "cgroup.procs").split())
    managed_processes = len(_read(root / "cgroup.procs").split()) if root.exists() else 0
    topology_ok = root.is_dir() and service_processes == 0 and managed_processes == 0
    ready = (
        unified
        and delegated
        and controls["memorySwapMax"]
        and controls["cgroupFreeze"]
        and unit_ok
        and topology_ok
    )
    return {
        "schemaVersion": 1,
        "ready": ready,
        "capabilities": {
            "unifiedCgroupV2": unified,
            "controllers": {name: name in controllers for name in sorted(_REQUIRED_CONTROLLERS)},
            **controls,
            "systemdDelegation": unit_ok,
        },
        "counts": {
            "serviceProcesses": service_processes,
            "managedProcesses": managed_processes,
        },
        "service": service,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--managed-root", required=True, type=Path)
    parser.add_argument("--service", default="hermes-dashboard.service")
    parser.add_argument("--require-ready", action="store_true")
    args = parser.parse_args(argv)
    result = inspect(args.managed_root, service=args.service)
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 1 if args.require_ready and not result["ready"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
