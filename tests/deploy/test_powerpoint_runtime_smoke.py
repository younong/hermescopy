from __future__ import annotations

import importlib.util
import json
import os
import threading
from http.server import HTTPServer
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
    monkeypatch.setattr(
        smoke.resource,
        "getrlimit",
        lambda _kind: (64, 64),
    )
    monkeypatch.setattr(smoke.tempfile, "mkdtemp", lambda **_kwargs: str(tmp_path / "work"))
    (tmp_path / "work").mkdir()

    result = smoke._run_checks(
        wrapper="/skill/soffice.py", timeout=9, expected_nofile=64
    )

    assert result["status"] == "passed"
    assert result["cleanup"] == "passed"
    assert result["checks"] == {
        "executor_nofile_limit": "passed:64",
        "pptxgenjs_generation": "passed",
        "markitdown_extract": "passed",
        "markitdown_order": "passed",
        "libreoffice_conversion": "passed",
    }
    assert calls[0][0] == "node"
    assert calls[1][1:3] == ["-m", "markitdown"]
    assert calls[2][:2] == ["python", "/skill/soffice.py"]
    assert not (tmp_path / "work").exists()


def test_network_smoke_dispatcher_performs_loopback_request():
    smoke = _module()
    server = HTTPServer(("127.0.0.1", 0), smoke._NetworkSmokeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = json.loads(smoke._dispatch_network_smoke(
            f"http://127.0.0.1:{server.server_port}",
            "web_search",
            {"query": smoke._NETWORK_SMOKE_QUERY, "limit": 1},
            None,
            None,
        ))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert result == {
        "success": True,
        "marker": smoke._NETWORK_SMOKE_MARKER,
    }


def test_network_smoke_dispatcher_rejects_any_other_invocation():
    smoke = _module()

    try:
        smoke._dispatch_network_smoke(
            "http://127.0.0.1:1",
            "web_search",
            {"query": "other", "limit": 1},
            None,
            None,
        )
    except RuntimeError as exc:
        assert str(exc) == "owner_relay_network_invocation"
    else:
        raise AssertionError("unexpected network invocation was accepted")


def test_fd_pressure_opens_above_target_and_can_be_cleaned_up():
    smoke = _module()
    descriptors = smoke._open_fd_pressure(24)
    try:
        assert descriptors[-1] > 24
    finally:
        for descriptor in descriptors:
            os.close(descriptor)
    for descriptor in descriptors:
        try:
            os.fstat(descriptor)
        except OSError:
            continue
        raise AssertionError("pressure descriptor remained open")


def test_authenticated_smoke_dispatches_terminal_through_supervisor_source():
    source = SMOKE.read_text(encoding="utf-8")

    assert "host_sandbox_deployment_policy(policy_path)" in source
    assert "os.chdir(runtime_paths.default_workspace)" in source
    assert "os.chdir(original_cwd)" in source
    assert 'function_name="terminal"' in source
    assert '"/opt/hermes/release/deploy/smoke-powerpoint-runtime.py"' in source
    assert '"--inside"' in source
    assert '"--expected-nofile"' in source
    assert '"/opt/hermes/release/skills/productivity/powerpoint/scripts/office/soffice.py"' in source
    assert "_open_fd_pressure(nofile_limit + 8)" in source
    assert 'function_name="web_search"' in source
    assert "owner_tool_relay=relay" in source
    assert "supervisor.stop_generation()" in source
    assert "network_server.shutdown()" in source
    assert "relay.close()" in source


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

    assert smoke.main([
        "--inside", "--wrapper", "/skill/soffice.py", "--expected-nofile", "64",
    ]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["failure"] == {
        "check": "pptxgenjs_generation",
        "code": "RuntimeError",
    }
