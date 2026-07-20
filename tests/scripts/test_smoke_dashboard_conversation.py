from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "smoke_dashboard_conversation.py"


@pytest.fixture
def smoke_module():
    scripts = str(SCRIPT.parent)
    sys.path.insert(0, scripts)
    spec = importlib.util.spec_from_file_location("_smoke_dashboard_conversation", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
        sys.path.remove(scripts)
    return module


def test_generated_smoke_uses_public_ticket_and_full_session_lifecycle(smoke_module):
    javascript = smoke_module._smoke_javascript(
        base="https://example.com/hermes/",
        path_prefix="/hermes/",
        marker="release-marker",
        timeout_ms=15_000,
    )

    assert "api/auth/ws-ticket" in javascript
    assert "audience: 'browser-ws:/api/ws'" in javascript
    assert "config.pathPrefix.replace" in javascript
    assert "/api/ws" in javascript
    assert "encodeURIComponent(ticketResponse)" in javascript
    assert "close_on_disconnect: false" in javascript
    assert "source: 'dashboard-gui'" in javascript
    for method in (
        "session.create",
        "prompt.submit",
        "session.close",
        "session.resume",
        "session.delete",
    ):
        assert method in javascript
    assert "message.delta" in javascript
    assert "message.complete" in javascript
    assert "cold resume did not restore the smoke transcript" in javascript
    assert "release-marker" in javascript


def test_public_smoke_returns_redacted_success_and_always_closes_browser(
    smoke_module, monkeypatch, tmp_path
):
    credentials = smoke_module.Credentials("member@example.com", "secret value")
    calls: list[list[str]] = []
    observed: dict[str, str] = {}
    monkeypatch.setattr(smoke_module.shutil, "which", lambda _name: "playwright-cli")
    monkeypatch.setattr(smoke_module, "load_credentials", lambda _root: credentials)
    monkeypatch.setattr(smoke_module, "login_dashboard", lambda **_kwargs: {"ok": True})

    browser_result = {
        "ok": True,
        "checks": [
            {"name": "public_ws_ticket", "status": "passed"},
            {"name": "public_session_create", "status": "passed"},
            {"name": "public_model_response", "status": "passed", "deltaCount": 2},
            {"name": "public_cold_resume", "status": "passed"},
            {"name": "public_cleanup", "status": "passed"},
        ],
        "cleanup": {"sessionClosed": True, "sessionDeleted": True, "socketClosed": True},
    }

    def fake_secure(**kwargs):
        observed["javascript"] = kwargs["javascript"]
        assert kwargs["credentials"] is credentials
        return json.dumps(browser_result)

    def fake_run(args, *, capture_output, text, check):
        calls.append([str(value) for value in args])
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(smoke_module, "run_secure_playwright_code", fake_secure)
    monkeypatch.setattr(smoke_module.subprocess, "run", fake_run)

    result, status = smoke_module.run_public_smoke(
        repo_root=tmp_path,
        raw_url="https://example.com/hermes/",
        session="release-smoke",
        playwright_cli=None,
        timeout=30,
    )

    assert status == 0
    assert result["status"] == "passed"
    assert {item["name"] for item in result["checks"]} == {
        "public_login",
        "public_ws_ticket",
        "public_session_create",
        "public_model_response",
        "public_cold_resume",
        "public_cleanup",
    }
    assert result["cleanup"] == {
        "sessionClosed": True,
        "sessionDeleted": True,
        "socketClosed": True,
        "browserClosed": True,
    }
    flattened = "\n".join(" ".join(call) for call in calls)
    assert credentials.username not in flattened
    assert credentials.password not in flattened
    assert calls[-1] == ["playwright-cli", "-s=release-smoke", "close"]
    assert credentials.username not in observed["javascript"]
    assert credentials.password not in observed["javascript"]


def test_public_smoke_classifies_browser_failure_without_leaking_secrets(
    smoke_module, monkeypatch, tmp_path
):
    credentials = smoke_module.Credentials("member@example.com", "secret value")
    monkeypatch.setattr(smoke_module, "load_credentials", lambda _root: credentials)
    monkeypatch.setattr(smoke_module, "login_dashboard", lambda **_kwargs: {"ok": True})
    monkeypatch.setattr(
        smoke_module,
        "run_secure_playwright_code",
        lambda **_kwargs: json.dumps(
            {
                "ok": False,
                "checks": [{"name": "public_ws_ticket", "status": "passed"}],
                "cleanup": {"socketClosed": True},
                "failure": {
                    "code": "timeout",
                    "check": "public_model_response",
                    "message": f"timed out for {credentials.username} {credentials.password}",
                },
            }
        ),
    )
    monkeypatch.setattr(
        smoke_module.subprocess,
        "run",
        lambda args, **_kwargs: subprocess.CompletedProcess(args, 0, "", ""),
    )

    result, status = smoke_module.run_public_smoke(
        repo_root=tmp_path,
        raw_url="https://example.com/hermes/",
        session="release-smoke",
        playwright_cli="playwright-cli",
        timeout=10,
    )

    assert status == 1
    assert result["status"] == "failed"
    assert result["failure"]["code"] == "timeout"
    assert result["failure"]["check"] == "public_model_response"
    serialized = json.dumps(result)
    assert credentials.username not in serialized
    assert credentials.password not in serialized
    assert result["cleanup"]["browserClosed"] is True
