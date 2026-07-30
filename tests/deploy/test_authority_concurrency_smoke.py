from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).parents[2]
SMOKE = ROOT / "deploy" / "smoke-authority-concurrency.py"
PYTHON = ROOT / ".venv" / "bin" / "python"


def _run_smoke(tmp_path: Path, *arguments: str) -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
    isolated = tmp_path / "isolated"
    isolated.mkdir(mode=0o700)
    environment = {
        "HOME": str(isolated),
        "TMPDIR": str(isolated),
        "HERMES_HOME": str(isolated),
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(ROOT),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    result = subprocess.run(
        [str(PYTHON), str(SMOKE), "--root", str(isolated / "work"), *arguments],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    payload = json.loads(result.stdout)
    return result, payload


def _passed_checks(payload: dict[str, object]) -> set[str]:
    return {
        str(check["name"])
        for check in payload["checks"]  # type: ignore[union-attr]
        if check["status"] == "passed"
    }


def test_authority_concurrency_smoke_passes_with_complete_contract(tmp_path):
    result, payload = _run_smoke(tmp_path)

    assert result.returncode == 0, result.stderr
    assert payload["schemaVersion"] == 1
    assert payload["kind"] == "hermes.authority-concurrency-smoke"
    assert payload["status"] == "passed"
    assert _passed_checks(payload) >= {
        "environment_isolation",
        "concurrent_initialization",
        "scope_visibility",
        "browser_exact_once",
        "worker_bootstrap_exact_once",
        "worker_lifecycle",
        "authority_checkpoint",
        "authority_integrity",
        "authority_schema",
        "authority_recovery_state",
        "recovery_artifacts",
        "artifact_cleanup",
    }
    observations = payload["observations"]
    assert observations["initializationWorkers"] == 8  # type: ignore[index]
    assert observations["browserAccepted"] == 1  # type: ignore[index]
    assert observations["browserReplayed"] == 1  # type: ignore[index]
    assert observations["bootstrapAccepted"] == 1  # type: ignore[index]
    assert observations["bootstrapReplayed"] == 1  # type: ignore[index]
    assert observations["workerTransitions"] == 3  # type: ignore[index]
    assert observations["checkpoint"]["busy"] == 0  # type: ignore[index]
    assert observations["integrity"] == "ok"  # type: ignore[index]
    assert observations["schemaVersion"] == 6  # type: ignore[index]
    assert observations["recoveryRequired"] == 0  # type: ignore[index]
    assert observations["recoveryArtifacts"] == 0  # type: ignore[index]
    assert payload["cleanup"] == {
        "executorStopped": True,
        "temporaryRootRemoved": True,
    }
    assert not (tmp_path / "isolated" / "work").exists()


def test_authority_concurrency_smoke_failure_is_bounded_and_cleans_up(tmp_path):
    result, payload = _run_smoke(tmp_path, "--test-fault", "after_concurrency")

    assert result.returncode == 1
    assert payload["status"] == "failed"
    assert payload["failure"] == {
        "code": "injected_failure",
        "check": "fault_injection",
        "message": "Injected authority smoke failure",
    }
    assert payload["cleanup"] == {
        "executorStopped": True,
        "temporaryRootRemoved": True,
    }
    assert not (tmp_path / "isolated" / "work").exists()
