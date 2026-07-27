from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SMOKE = ROOT / "deploy" / "smoke-session-reader.py"


@pytest.fixture
def smoke_module():
    spec = importlib.util.spec_from_file_location("_session_reader_performance_smoke", SMOKE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(spec.name, None)


def test_latency_contract_is_strictly_below_300ms(smoke_module):
    smoke_module._require_latency("check", "too_slow", 299.999, 300.0)

    with pytest.raises(smoke_module.SmokeFailure) as raised:
        smoke_module._require_latency("check", "too_slow", 300.0, 300.0)

    assert raised.value.code == "too_slow"
    assert raised.value.check == "check"


def test_resource_contract_matches_actual_reader_settings(smoke_module):
    observed = smoke_module._require_resource_contract()

    assert observed == {
        "dbPoolSize": 4,
        "serverWorkers": 8,
        "serverInFlight": 16,
        "clientConnections": 8,
        "clientKeepaliveConnections": 4,
    }


def test_smoke_result_passes_all_mandatory_contracts(smoke_module):
    result, status = smoke_module.run_smoke()

    assert status == 0
    assert result["schemaVersion"] == 1
    assert result["kind"] == "hermes.session-reader-performance-smoke"
    assert result["status"] == "passed"
    assert {check["name"] for check in result["checks"]} >= {
        "reader_resource_contract",
        "reader_fixture",
        "reader_query_plan",
        "list_sql_budget",
        "stats_sql_budget",
        "search_sql_budget",
        "local_compact_listing",
        "reader_startup",
        "reader_uds_cold",
        "reader_uds_warm",
    }
    assert result["observations"]["sql"] == {"list": 6, "stats": 3, "search": 6}
    assert all(result["cleanup"].values())


def test_smoke_failure_is_bounded_and_cleans_artifacts(smoke_module, monkeypatch):
    monkeypatch.setattr(
        smoke_module,
        "_require_resource_contract",
        lambda: (_ for _ in ()).throw(
            smoke_module.SmokeFailure(
                "resource_contract_mismatch",
                "reader_resource_contract",
                "x" * 1000,
            )
        ),
    )

    result, status = smoke_module.run_smoke()

    assert status == 1
    assert result["status"] == "failed"
    assert result["failure"]["code"] == "resource_contract_mismatch"
    assert result["failure"]["check"] == "reader_resource_contract"
    assert len(result["failure"]["message"]) <= 503
    assert result["cleanup"]["databasesClosed"] is True
    assert result["cleanup"]["socketRemoved"] is True
    assert result["cleanup"]["temporaryRootRemoved"] is True
