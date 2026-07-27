#!/usr/bin/env python3
"""Measure real Owner Worker readiness paths without exposing owner content."""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sys
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hermes_cli.latency_trace import latency_trace_scope
from hermes_cli.owner_runtime import ensure_owner_runtime_dirs
from hermes_cli.owner_worker.performance_contract import (
    STANDARDS,
    require_ready_latency,
)
from hermes_cli.owner_worker.supervisor import OwnerWorkerSupervisor

KIND = "hermes.owner-worker-readiness-measurement"
_READY_RE = re.compile(
    r"stage=owner_worker\.ready elapsed_ms=(?P<elapsed>[0-9.]+) "
    r"outcome=(?P<outcome>[a-z]+) path=(?P<path>[a-z_]+)$"
)


@dataclass(frozen=True)
class _Owner:
    owner_key: str
    owner_home: Path
    tenant_id: str = "measurement"
    owner_user_id: str = "measurement"
    auth_provider: str = "measurement"


class _ReadyHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.samples: list[dict[str, Any]] = []

    def emit(self, record: logging.LogRecord) -> None:
        match = _READY_RE.search(record.getMessage())
        if match:
            self.samples.append(
                {
                    "elapsedMs": float(match.group("elapsed")),
                    "outcome": match.group("outcome"),
                    "path": match.group("path"),
                }
            )


def _traced_ready(
    supervisor: OwnerWorkerSupervisor,
    owner: _Owner,
    logger: logging.Logger,
    handler: _ReadyHandler,
    *,
    scenario: str,
):
    before = len(handler.samples)
    with latency_trace_scope(
        logger,
        trace_id=f"measurement-{scenario}-trace",
        surface="owner-ws-bridge",
    ):
        handle = supervisor.get_or_start(owner)
    samples = handler.samples[before:]
    if len(samples) != 1:
        raise RuntimeError(f"{scenario} emitted {len(samples)} aggregate samples")
    return handle, {"scenario": scenario, **samples[0]}


def _wait_for_worker_socket(owner_home: Path, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    workers = owner_home / "runtime" / "workers"
    while time.monotonic() < deadline:
        if any(workers.glob("*/worker.sock")):
            return
        time.sleep(0.01)
    raise TimeoutError("warmup did not expose its worker socket")


def run_measurement() -> tuple[dict[str, Any], int]:
    if not sys.platform.startswith("linux"):
        return (
            {
                "schemaVersion": STANDARDS.schema_version,
                "kind": KIND,
                "status": "unsupported",
                "reason": "controlled roots require Linux",
                "standards": STANDARDS.payload(),
                "samples": [],
            },
            0,
        )

    root = Path("/tmp") / f"howp-{os.getpid():x}"
    cleanup = {"temporaryRootRemoved": False, "workersStopped": False}
    supervisors: list[OwnerWorkerSupervisor] = []
    logger = logging.getLogger("hermes.owner-worker-readiness-measurement")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = _ReadyHandler()
    logger.addHandler(handler)
    samples: list[dict[str, Any]] = []
    status = 0
    failure: dict[str, str] | None = None

    try:
        root.mkdir(mode=0o700)
        cold_owner = _Owner(
            "ok1_readiness_measurement_cold",
            ensure_owner_runtime_dirs(root / "cold-owner"),
        )
        cold_supervisor = OwnerWorkerSupervisor(
            control_home=root / "cold-control",
            global_home=root / "global",
            startup_timeout=10,
            startup_cooldown=0,
        )
        supervisors.append(cold_supervisor)
        cold_handle, cold_sample = _traced_ready(
            cold_supervisor,
            cold_owner,
            logger,
            handler,
            scenario="cold_start",
        )
        samples.append(cold_sample)
        require_ready_latency(
            cold_sample["scenario"], cold_sample["path"], cold_sample["elapsedMs"]
        )
        cold_handle.process.terminate()
        cold_handle.process.wait(timeout=5)
        _replacement, replacement_sample = _traced_ready(
            cold_supervisor,
            cold_owner,
            logger,
            handler,
            scenario="replace_unhealthy",
        )
        samples.append(replacement_sample)
        require_ready_latency(
            replacement_sample["scenario"],
            replacement_sample["path"],
            replacement_sample["elapsedMs"],
        )

        warm_owner = _Owner(
            "ok1_readiness_measurement_warm",
            ensure_owner_runtime_dirs(root / "warm-owner"),
        )
        warm_supervisor = OwnerWorkerSupervisor(
            control_home=root / "warm-control",
            global_home=root / "global",
            startup_timeout=10,
            startup_cooldown=0,
        )
        supervisors.append(warm_supervisor)
        warmup_errors: list[BaseException] = []

        def warmup() -> None:
            try:
                warm_supervisor.get_or_start(warm_owner)
            except BaseException as exc:
                warmup_errors.append(exc)

        warmup_thread = threading.Thread(target=warmup, name="owner-worker-measurement-warmup")
        warmup_thread.start()
        _wait_for_worker_socket(warm_owner.owner_home, 10)
        warm_handle, follower_sample = _traced_ready(
            warm_supervisor,
            warm_owner,
            logger,
            handler,
            scenario="warmup_followed_ready",
        )
        warmup_thread.join(timeout=10)
        if warmup_thread.is_alive() or warmup_errors:
            raise RuntimeError("warmup did not finish cleanly")
        samples.append(follower_sample)
        require_ready_latency(
            follower_sample["scenario"],
            follower_sample["path"],
            follower_sample["elapsedMs"],
        )

        use = warm_supervisor.acquire_use(warm_handle)
        try:
            _active, active_sample = _traced_ready(
                warm_supervisor,
                warm_owner,
                logger,
                handler,
                scenario="hot_active",
            )
        finally:
            use.release()
        samples.append(active_sample)
        require_ready_latency(
            active_sample["scenario"], active_sample["path"], active_sample["elapsedMs"]
        )

        _probe, probe_sample = _traced_ready(
            warm_supervisor,
            warm_owner,
            logger,
            handler,
            scenario="hot_health_probe",
        )
        samples.append(probe_sample)
        require_ready_latency(
            probe_sample["scenario"], probe_sample["path"], probe_sample["elapsedMs"]
        )
    except Exception as exc:
        status = 1
        failure = {"errorType": type(exc).__name__, "message": str(exc)[:500]}
    finally:
        for supervisor in reversed(supervisors):
            try:
                supervisor.shutdown()
            except Exception:
                status = 1
        cleanup["workersStopped"] = all(
            not supervisor._handles and not supervisor._terminating_handles
            for supervisor in supervisors
        )
        logger.removeHandler(handler)
        shutil.rmtree(root, ignore_errors=True)
        cleanup["temporaryRootRemoved"] = not root.exists()
        if not all(cleanup.values()):
            status = 1

    result: dict[str, Any] = {
        "schemaVersion": STANDARDS.schema_version,
        "kind": KIND,
        "status": "passed" if status == 0 else "failed",
        "standards": STANDARDS.payload(),
        "samples": samples,
        "cleanup": cleanup,
    }
    if failure is not None:
        result["failure"] = failure
    return result, status


def main() -> int:
    result, status = run_measurement()
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
