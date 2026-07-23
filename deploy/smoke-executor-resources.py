#!/usr/bin/env python3
"""Real Linux smoke for one delegated authenticated executor cgroup subtree.

This diagnostic is intentionally independent from owner identifiers. It verifies
kernel limits/events and deterministic descendant cleanup in a temporary leaf
below the configured delegated root. Run the PowerPoint smoke separately through
``ToolExecutorSupervisor`` to cover the complete product launcher path.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any, Sequence


def _write(path: Path, value: str) -> None:
    path.write_text(value, encoding="ascii")


def _events(path: Path) -> dict[str, int]:
    return {
        key: int(value)
        for key, value in (
            line.split() for line in path.read_text(encoding="ascii").splitlines()
        )
    }


def _wait_for_event(
    path: Path,
    key: str,
    before: dict[str, int],
    *,
    timeout: int,
) -> dict[str, int]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = _events(path)
        if current.get(key, 0) > before.get(key, 0):
            return current
        time.sleep(0.05)
    raise RuntimeError(f"{path.name}_{key}_event")


def _spawn(code: str) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        ["/usr/bin/python3", "-c", code],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _reap(process: subprocess.Popen[bytes] | None) -> None:
    if process is None:
        return
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass


def run(root: Path, *, timeout: int) -> dict[str, Any]:
    started = time.monotonic()
    checks: dict[str, str] = {}
    failure: dict[str, str] | None = None
    cleanup = "passed"
    leaf = root / f"resource-smoke-{os.getpid()}"
    process: subprocess.Popen[bytes] | None = None
    before_cpu: dict[str, int] = {}
    before_memory: dict[str, int] = {}
    before_pids: dict[str, int] = {}
    after_cpu: dict[str, int] = {}
    after_memory: dict[str, int] = {}
    after_pids: dict[str, int] = {}
    try:
        leaf.mkdir(mode=0o755)
        _write(leaf / "cpu.max", "25000 100000")
        _write(leaf / "memory.max", str(64 << 20))
        _write(leaf / "memory.swap.max", "0")
        _write(leaf / "pids.max", "16")
        _write(leaf / "memory.oom.group", "1")
        checks["limit_readback"] = "passed" if (
            (leaf / "cpu.max").read_text().strip() == "25000 100000"
            and (leaf / "memory.max").read_text().strip() == str(64 << 20)
            and (leaf / "memory.swap.max").read_text().strip() == "0"
            and (leaf / "pids.max").read_text().strip() == "16"
        ) else "failed"
        if checks["limit_readback"] != "passed":
            raise RuntimeError("limit_readback")

        before_cpu = _events(leaf / "cpu.stat")
        process = _spawn("while True: pass")
        _write(leaf / "cgroup.procs", str(process.pid))
        after_cpu = _wait_for_event(
            leaf / "cpu.stat", "nr_throttled", before_cpu, timeout=timeout,
        )
        checks["cpu_throttle_event"] = "passed"
        _reap(process)
        process = None

        before_memory = _events(leaf / "memory.events")
        process = _spawn("chunks=[]\nwhile True: chunks.append(bytearray(4 << 20))")
        _write(leaf / "cgroup.procs", str(process.pid))
        after_memory = _wait_for_event(
            leaf / "memory.events", "oom_kill", before_memory, timeout=timeout,
        )
        checks["memory_oom_event"] = "passed"
        _reap(process)
        process = None

        before_pids = _events(leaf / "pids.events")
        process = _spawn("import os,time\nwhile True:\n os.fork()\n time.sleep(.01)")
        _write(leaf / "cgroup.procs", str(process.pid))
        after_pids = _wait_for_event(
            leaf / "pids.events", "max", before_pids, timeout=timeout,
        )
        checks["pids_limit_event"] = "passed"
        _reap(process)
        process = None
    except Exception as exc:
        failure = {
            "check": str(exc) if isinstance(exc, RuntimeError) else "unexpected",
            "code": type(exc).__name__,
        }
    finally:
        try:
            if leaf.exists():
                try:
                    after_cpu = _events(leaf / "cpu.stat")
                    after_memory = _events(leaf / "memory.events")
                    after_pids = _events(leaf / "pids.events")
                except OSError:
                    pass
                if (leaf / "cgroup.kill").exists():
                    _write(leaf / "cgroup.kill", "1")
                elif (leaf / "cgroup.freeze").exists():
                    _write(leaf / "cgroup.freeze", "1")
                    for value in (leaf / "cgroup.procs").read_text().split():
                        try:
                            os.kill(int(value), signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    if _events(leaf / "cgroup.events").get("populated") == 0:
                        break
                    time.sleep(0.05)
                else:
                    raise RuntimeError("cleanup_populated")
                leaf.rmdir()
        except Exception:
            cleanup = "failed"
        _reap(process)
        if leaf.exists():
            cleanup = "failed"
        if cleanup == "passed":
            checks["smoke_scope_removed"] = "passed"
    after_cpu = after_cpu or before_cpu
    after_memory = after_memory or before_memory
    after_pids = after_pids or before_pids
    return {
        "schemaVersion": 1,
        "status": "passed" if failure is None and cleanup == "passed" else "failed",
        "checks": checks,
        "events": {
            "cpu": {
                key: after_cpu.get(key, 0) - before_cpu.get(key, 0)
                for key in ("nr_throttled", "throttled_usec")
            },
            "memory": {
                key: after_memory.get(key, 0) - before_memory.get(key, 0)
                for key in before_memory
            },
            "pids": {
                key: after_pids.get(key, 0) - before_pids.get(key, 0)
                for key in before_pids
            },
        },
        "cleanup": cleanup,
        "durationMs": round((time.monotonic() - started) * 1000),
        "failure": failure,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--managed-root", required=True, type=Path)
    parser.add_argument("--timeout", type=int, default=10)
    args = parser.parse_args(argv)
    result = run(args.managed_root.resolve(), timeout=args.timeout)
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
