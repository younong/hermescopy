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

    assert "page.evaluate(async (config)" in javascript
    assert "page.evaluate(async (url)" not in javascript
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
    assert "api/sessions?limit=30&offset=0&order=recent&compact=true" in javascript
    assert "api/sessions/${encodeURIComponent(storedSessionId)}/messages?limit=100" in javascript
    assert "public_session_reader_list" in javascript
    assert "public_session_reader_messages" in javascript
    assert "response.status !== 503" in javascript
    assert "Math.min(config.timeoutMs, 10_000)" in javascript
    assert "response.headers.get('Retry-After')" in javascript
    assert "response.text()" not in javascript
    assert "release-marker" in javascript


def test_generated_smoke_can_select_an_exact_managed_route(smoke_module):
    javascript = smoke_module._smoke_javascript(
        base="https://example.com/hermes/",
        path_prefix="/hermes/",
        marker="release-marker",
        timeout_ms=15_000,
        provider="custom:kimi-code",
        model="k3-256k",
    )

    assert "model.options" in javascript
    assert "config.set" in javascript
    assert "custom:kimi-code" in javascript
    assert "k3-256k" in javascript
    assert "public_model_picker_route" in javascript


def test_generated_continuity_smoke_holds_then_reconnects_same_session(smoke_module):
    prepare = smoke_module._continuity_javascript(
        base="https://example.com/hermes/",
        path_prefix="/hermes/",
        marker="continuity-marker",
        timeout_ms=30_000,
        phase="prepare",
    )
    verify = smoke_module._continuity_javascript(
        base="https://example.com/hermes/",
        path_prefix="/hermes/",
        marker="unused-verify-marker",
        timeout_ms=30_000,
        phase="verify",
    )

    assert smoke_module.CONTINUITY_STATE_KEY in prepare
    assert "state.socket = ws" in prepare
    assert "state.closeCode = event.code" in prepare
    assert "oldSocket.addEventListener('close'" in verify
    assert "state.closeCode !== 1012" in verify
    assert "while (now() < deadline)" in verify
    assert "api/auth/ws-ticket" in verify
    assert "session_id: state.storedSessionId" in verify
    assert "continuity resume did not restore the prepared transcript" in verify
    assert "continuity cold resume did not restore the prepared transcript" in verify
    assert "session.delete" in verify
    assert "response.text()" not in prepare + verify


def test_continuity_phases_keep_browser_open_until_verify(smoke_module, monkeypatch, tmp_path):
    credentials = smoke_module.Credentials("member@example.com", "secret value")
    close_calls: list[list[str]] = []
    phases: list[str] = []
    monkeypatch.setattr(smoke_module, "load_credentials", lambda _root, _path: credentials)
    monkeypatch.setattr(smoke_module, "login_dashboard", lambda **_kwargs: {"ok": True})

    def fake_secure(**kwargs):
        javascript = kwargs["javascript"]
        phase = "prepare" if '"phase": "prepare"' in javascript else "verify"
        phases.append(phase)
        return json.dumps(
            {
                "ok": True,
                "checks": [{"name": f"continuity_{phase}", "status": "passed"}],
                "cleanup": {
                    "sessionClosed": phase == "verify",
                    "sessionDeleted": phase == "verify",
                    "socketClosed": phase == "verify",
                },
            }
        )

    monkeypatch.setattr(smoke_module, "run_secure_playwright_code", fake_secure)
    monkeypatch.setattr(
        smoke_module.subprocess,
        "run",
        lambda args, **_kwargs: (
            close_calls.append([str(value) for value in args])
            or subprocess.CompletedProcess(args, 0, "", "")
        ),
    )

    prepared, prepare_status = smoke_module.run_continuity_smoke(
        repo_root=tmp_path,
        raw_url="https://example.com/hermes/",
        session="release-continuity",
        playwright_cli="playwright-cli",
        timeout=30,
        phase="prepare",
    )
    verified, verify_status = smoke_module.run_continuity_smoke(
        repo_root=tmp_path,
        raw_url="https://example.com/hermes/",
        session="release-continuity",
        playwright_cli="playwright-cli",
        timeout=30,
        phase="verify",
    )

    assert prepare_status == verify_status == 0
    assert prepared["kind"] == smoke_module.CONTINUITY_KIND
    assert prepared["phase"] == "prepare"
    assert prepared["cleanup"]["browserClosed"] is False
    assert verified["phase"] == "verify"
    assert verified["cleanup"]["browserClosed"] is True
    assert phases == ["prepare", "verify"]
    assert close_calls == [["playwright-cli", "-s=release-continuity", "close"]]
    assert credentials.username not in json.dumps([prepared, verified])
    assert credentials.password not in json.dumps([prepared, verified])


def test_public_smoke_returns_redacted_success_and_always_closes_browser(
    smoke_module, monkeypatch, tmp_path
):
    credentials = smoke_module.Credentials("member@example.com", "secret value")
    calls: list[list[str]] = []
    observed: dict[str, str] = {}
    monkeypatch.setattr(smoke_module.shutil, "which", lambda _name: "playwright-cli")
    monkeypatch.setattr(smoke_module, "load_credentials", lambda _root, _path: credentials)
    monkeypatch.setattr(smoke_module, "login_dashboard", lambda **_kwargs: {"ok": True})

    browser_result = {
        "ok": True,
        "checks": [
            {"name": "public_ws_ticket_mint", "status": "passed"},
            {"name": "public_ws_admission", "status": "passed"},
            {"name": "public_owner_worker_conversation", "status": "passed", "deltaCount": 2},
            {"name": "public_cold_session_reader_resume", "status": "passed"},
            {"name": "public_session_reader_list", "status": "passed", "durationMs": 109},
            {"name": "public_session_reader_messages", "status": "passed", "durationMs": 147},
            {"name": "public_cleanup_verified", "status": "passed"},
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
        "public_ws_ticket_mint",
        "public_ws_admission",
        "public_owner_worker_conversation",
        "public_cold_session_reader_resume",
        "public_session_reader_list",
        "public_session_reader_messages",
        "public_cleanup_verified",
    }
    checks = {item["name"]: item for item in result["checks"]}
    assert checks["public_session_reader_list"]["durationMs"] == 109
    assert checks["public_session_reader_messages"]["durationMs"] == 147
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
    monkeypatch.setattr(smoke_module, "load_credentials", lambda _root, _path: credentials)
    monkeypatch.setattr(smoke_module, "login_dashboard", lambda **_kwargs: {"ok": True})
    monkeypatch.setattr(
        smoke_module,
        "run_secure_playwright_code",
        lambda **_kwargs: json.dumps(
            {
                "ok": False,
                "checks": [{"name": "public_ws_ticket_mint", "status": "passed"}],
                "cleanup": {"socketClosed": True},
                "failure": {
                    "code": "timeout",
                    "check": "public_owner_worker_conversation",
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
    assert result["failure"]["check"] == "public_owner_worker_conversation"
    serialized = json.dumps(result)
    assert credentials.username not in serialized
    assert credentials.password not in serialized
    assert result["cleanup"]["browserClosed"] is True
