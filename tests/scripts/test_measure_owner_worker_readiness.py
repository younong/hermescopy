from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
MEASUREMENT = ROOT / "scripts" / "measure_owner_worker_readiness.py"


@pytest.fixture
def measurement_module():
    spec = importlib.util.spec_from_file_location(
        "_owner_worker_readiness_measurement", MEASUREMENT
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(spec.name, None)


def test_non_linux_measurement_reports_unsupported(measurement_module, monkeypatch):
    monkeypatch.setattr(measurement_module.sys, "platform", "darwin")

    result, status = measurement_module.run_measurement()

    assert status == 0
    assert result == {
        "schemaVersion": 3,
        "kind": "hermes.owner-worker-readiness-measurement",
        "status": "unsupported",
        "reason": "controlled roots require Linux",
        "standards": {
            "schema_version": 3,
            "ready_max_ms": 1000.0,
        },
        "samples": [],
    }


def test_ready_handler_keeps_only_bounded_timing_fields(measurement_module):
    import logging

    handler = measurement_module._ReadyHandler()
    record = logging.LogRecord(
        "measurement",
        logging.INFO,
        "",
        0,
        (
            "latency trace_id=opaque surface=owner-ws-bridge "
            "stage=owner_worker.ready elapsed_ms=23.4 outcome=ok "
            "path=hot_health_probe"
        ),
        (),
        None,
    )

    handler.emit(record)

    assert handler.samples == [
        {"elapsedMs": 23.4, "outcome": "ok", "path": "hot_health_probe"}
    ]
