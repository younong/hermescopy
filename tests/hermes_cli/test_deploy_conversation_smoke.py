from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SMOKE = ROOT / "deploy" / "smoke-conversation.py"
EXPECTED_CHECKS = {
    "gateway_ready",
    "session_create",
    "config_propagation",
    "file_attachment",
    "safe_tool",
    "prompt_stream",
    "approval_deny",
    "cold_resume",
    "resume_continuation",
    "artifact_cleanup",
}


def _load_smoke_module():
    spec = importlib.util.spec_from_file_location("_deploy_conversation_smoke", SMOKE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


def test_deterministic_conversation_smoke_exercises_core_gateway_flow():
    completed = subprocess.run(
        [sys.executable, str(SMOKE), "--timeout", "90"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
    result = json.loads(completed.stdout)
    assert result["schemaVersion"] == 1
    assert result["kind"] == "hermes.conversation-smoke"
    assert result["status"] == "passed"
    assert result["artifactsCleaned"] is True
    checks = {item["name"]: item for item in result["checks"]}
    assert set(checks) == EXPECTED_CHECKS
    assert all(item["status"] == "passed" for item in checks.values())
    assert checks["prompt_stream"]["deltaCount"] >= 2
    assert checks["config_propagation"]["provider"] == "custom:hermes-smoke"


def test_deterministic_smoke_failure_is_bounded_and_cleans_artifacts(monkeypatch, tmp_path):
    module = _load_smoke_module()
    temporary = tmp_path / "owned-smoke-root"
    monkeypatch.setattr(module.tempfile, "mkdtemp", lambda **_kwargs: str(temporary))

    class FailingModel:
        def __init__(self, _workspace):
            pass

        def start(self):
            raise RuntimeError("failure\n" + "x" * 900)

        def close(self):
            pass

    monkeypatch.setattr(module, "ModelStub", FailingModel)

    result, status = module.run_smoke(ROOT, 10)

    assert status == 1
    assert result["status"] == "failed"
    assert result["artifactsCleaned"] is True
    assert result["failure"]["code"] == "unexpected_error"
    assert result["failure"]["check"] == "runner"
    assert "\n" not in result["failure"]["message"]
    assert len(result["failure"]["message"]) <= 503
    assert not temporary.exists()
