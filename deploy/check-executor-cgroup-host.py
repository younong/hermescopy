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


def _parse_systemd_limit(value: str | None) -> int | None:
    if value is None or not value.isascii() or not value.isdecimal():
        return None
    return int(value)


def _positive_integer(value: str) -> int:
    if not value.isascii() or not value.isdecimal():
        raise argparse.ArgumentTypeError("must be a positive integer")
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def inspect(
    managed_root: Path,
    *,
    service: str,
    expected_soft_nofile: int,
    expected_hard_nofile: int,
) -> dict[str, Any]:
    if (
        isinstance(expected_soft_nofile, bool)
        or isinstance(expected_hard_nofile, bool)
        or not isinstance(expected_soft_nofile, int)
        or not isinstance(expected_hard_nofile, int)
        or expected_soft_nofile < 1
        or expected_hard_nofile < expected_soft_nofile
    ):
        raise ValueError("expected nofile limits must be positive and soft must not exceed hard")

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
                "--property=KillMode", "--property=LimitNOFILESoft",
                "--property=LimitNOFILE",
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
        and systemd.get("KillMode") == "mixed"
    )
    observed_soft_nofile = _parse_systemd_limit(systemd.get("LimitNOFILESoft"))
    observed_hard_nofile = _parse_systemd_limit(systemd.get("LimitNOFILE"))
    nofile_ok = (
        observed_soft_nofile is not None
        and observed_hard_nofile is not None
        and observed_soft_nofile >= expected_soft_nofile
        and observed_hard_nofile >= expected_hard_nofile
    )
    unified = mount is not None and (mount == service_root or mount in service_root.parents)
    service_processes = len(_read(service_root / "cgroup.procs").split())
    managed_processes = len(_read(root / "cgroup.procs").split()) if root.exists() else 0
    topology_ok = root.is_dir() and service_processes == 0 and managed_processes == 0
    resource_ready = (
        unified
        and delegated
        and controls["memorySwapMax"]
        and controls["cgroupFreeze"]
        and unit_ok
        and topology_ok
    )
    ready = resource_ready and nofile_ok
    return {
        "schemaVersion": 1,
        "ready": ready,
        "resourceReady": resource_ready,
        "mandatoryReady": nofile_ok,
        "capabilities": {
            "unifiedCgroupV2": unified,
            "controllers": {name: name in controllers for name in sorted(_REQUIRED_CONTROLLERS)},
            **controls,
            "systemdDelegation": unit_ok,
            "systemdNofileLimits": nofile_ok,
        },
        "counts": {
            "serviceProcesses": service_processes,
            "managedProcesses": managed_processes,
        },
        "limits": {
            "nofile": {
                "expectedSoft": expected_soft_nofile,
                "expectedHard": expected_hard_nofile,
                "observedSoft": observed_soft_nofile,
                "observedHard": observed_hard_nofile,
            },
        },
        "service": service,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--managed-root", required=True, type=Path)
    parser.add_argument("--service", default="hermes-dashboard.service")
    parser.add_argument("--expected-soft-nofile", required=True, type=_positive_integer)
    parser.add_argument("--expected-hard-nofile", required=True, type=_positive_integer)
    parser.add_argument("--require-ready", action="store_true")
    parser.add_argument("--require-mandatory", action="store_true")
    args = parser.parse_args(argv)
    if args.expected_soft_nofile > args.expected_hard_nofile:
        parser.error("--expected-soft-nofile must not exceed --expected-hard-nofile")
    result = inspect(
        args.managed_root,
        service=args.service,
        expected_soft_nofile=args.expected_soft_nofile,
        expected_hard_nofile=args.expected_hard_nofile,
    )
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    if args.require_mandatory and not result["mandatoryReady"]:
        return 1
    return 1 if args.require_ready and not result["ready"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
