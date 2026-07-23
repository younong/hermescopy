from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace


SMOKE = Path(__file__).parents[2] / "deploy" / "smoke-powerpoint-runtime.py"


def _module():
    spec = importlib.util.spec_from_file_location("powerpoint_runtime_smoke", SMOKE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_inside_smoke_checks_generation_order_and_conversion(tmp_path, monkeypatch):
    smoke = _module()
    calls: list[list[str]] = []

    def fake_run(command, *, cwd, timeout):
        calls.append(command)
        if command[0] == "node":
            Path(command[2]).write_bytes(b"pptx")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if command[1:3] == ["-m", "markitdown"]:
            return SimpleNamespace(
                returncode=0,
                stdout="HERMES_PPTX_SMOKE_ALPHA\nHERMES_PPTX_SMOKE_OMEGA",
                stderr="",
            )
        Path(command[-1]).with_suffix(".pdf").write_bytes(b"pdf")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(smoke, "_run", fake_run)
    monkeypatch.setattr(smoke.tempfile, "mkdtemp", lambda **_kwargs: str(tmp_path / "work"))
    (tmp_path / "work").mkdir()

    result = smoke._run_checks(wrapper="/skill/soffice.py", timeout=9)

    assert result["status"] == "passed"
    assert result["cleanup"] == "passed"
    assert result["checks"] == {
        "pptxgenjs_generation": "passed",
        "markitdown_extract": "passed",
        "markitdown_order": "passed",
        "libreoffice_conversion": "passed",
    }
    assert calls[0][0] == "node"
    assert calls[1][1:3] == ["-m", "markitdown"]
    assert calls[2][:2] == ["python", "/skill/soffice.py"]
    assert not (tmp_path / "work").exists()


def test_authenticated_smoke_dispatches_terminal_through_supervisor_source():
    source = SMOKE.read_text(encoding="utf-8")

    assert "host_sandbox_deployment_policy(policy_path)" in source
    assert 'function_name="terminal"' in source
    assert '"/opt/hermes/release/deploy/smoke-powerpoint-runtime.py"' in source
    assert '"--inside"' in source
    assert '"/opt/hermes/release/skills/productivity/powerpoint/scripts/office/soffice.py"' in source
    assert "supervisor.stop_generation()" in source


def test_main_emits_bounded_json_for_inside_failure(monkeypatch, capsys):
    smoke = _module()
    monkeypatch.setattr(
        smoke,
        "_run_checks",
        lambda **_kwargs: {
            "schemaVersion": 1,
            "status": "failed",
            "checks": {},
            "durationMs": 1,
            "cleanup": "passed",
            "failure": {"check": "pptxgenjs_generation", "code": "RuntimeError"},
        },
    )

    assert smoke.main(["--inside", "--wrapper", "/skill/soffice.py"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["failure"] == {
        "check": "pptxgenjs_generation",
        "code": "RuntimeError",
    }
