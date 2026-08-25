from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "validate_dashboard_collaboration.py"
GROUP_ID = "cg_5d70a86870274443a2478adc30145be1"


@pytest.fixture
def validation_module():
    scripts = str(SCRIPT.parent)
    sys.path.insert(0, scripts)
    spec = importlib.util.spec_from_file_location("_validate_dashboard_collaboration", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
        sys.path.remove(scripts)
    return module


def test_generated_validation_is_read_only_and_covers_pagination_and_reconnect(validation_module):
    javascript = validation_module._validation_javascript(
        base="https://example.com/hermes/",
        path_prefix="/hermes/",
        group_id=GROUP_ID,
        timeout_ms=30_000,
    )

    assert "api/auth/ws-ticket" in javascript
    assert "audience: 'browser-ws:/api/ws'" in javascript
    assert "session.owner_attach" in javascript
    assert "collaboration.group.get" in javascript
    assert "before_sequence" in javascript
    assert "after_sequence" in javascript
    assert "through_sequence" in javascript
    assert "collaboration_backward_history" in javascript
    assert "collaboration_reconnect" in javascript
    assert "new TextEncoder().encode(raw).byteLength" in javascript
    for mutation in (
        "collaboration.message.submit",
        "collaboration.members.update",
        "collaboration.approval.respond",
        "collaboration.target.interrupt",
        "collaboration.group.archive",
        "collaboration.image.attach",
        "collaboration.pdf.attach",
        "collaboration.file.attach",
    ):
        assert mutation not in javascript


def test_validation_returns_sanitized_metrics_and_closes_browser(validation_module, monkeypatch, tmp_path):
    credentials = validation_module.Credentials("member@example.com", "secret value")
    observed: dict[str, str] = {}
    close_calls: list[list[str]] = []
    monkeypatch.setattr(validation_module, "load_credentials", lambda _root, _path: credentials)
    monkeypatch.setattr(validation_module, "login_dashboard", lambda **_kwargs: {"ok": True})

    browser_result = {
        "ok": True,
        "checks": [
            {"name": "collaboration_ws_admission", "status": "passed", "durationMs": 5},
            {
                "name": "collaboration_latest_page",
                "status": "passed",
                "eventCount": 100,
                "rangeStartSequence": 72,
                "rangeEndSequence": 171,
                "snapshotSequence": 171,
            },
            {
                "name": "collaboration_backward_history",
                "status": "passed",
                "eventCount": 171,
                "loadEarlierCount": 1,
                "pageCount": 2,
                "snapshotSequence": 171,
            },
            {
                "name": "collaboration_reconnect",
                "status": "passed",
                "events": 0,
                "pages": 1,
                "throughSequence": 171,
            },
        ],
        "cleanup": {"socketClosed": True},
        "transport": {
            "closeCode": 1000,
            "maxFrameBytes": 640_000,
            "maxMessageBytes": validation_module.MAX_MESSAGE_BYTES,
        },
    }

    def fake_secure(**kwargs):
        observed["javascript"] = kwargs["javascript"]
        assert kwargs["credentials"] is credentials
        return json.dumps(browser_result)

    def fake_run(args, **_kwargs):
        close_calls.append([str(value) for value in args])
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(validation_module, "run_secure_playwright_code", fake_secure)
    monkeypatch.setattr(validation_module.subprocess, "run", fake_run)

    result, status = validation_module.run_validation(
        repo_root=tmp_path,
        raw_url="https://example.com/hermes/",
        group_id=GROUP_ID,
        session="collaboration-validation",
        playwright_cli="playwright-cli",
        timeout=30,
    )

    assert status == 0
    assert result["status"] == "passed"
    assert result["groupId"] == GROUP_ID
    assert result["transport"] == browser_result["transport"]
    assert result["cleanup"] == {"socketClosed": True, "browserClosed": True}
    assert close_calls == [["playwright-cli", "-s=collaboration-validation", "close"]]
    serialized = json.dumps(result)
    assert credentials.username not in serialized
    assert credentials.password not in serialized
    assert credentials.username not in observed["javascript"]
    assert credentials.password not in observed["javascript"]


def test_validation_redacts_browser_failure(validation_module, monkeypatch, tmp_path):
    credentials = validation_module.Credentials("member@example.com", "secret value")
    monkeypatch.setattr(validation_module, "load_credentials", lambda _root, _path: credentials)
    monkeypatch.setattr(validation_module, "login_dashboard", lambda **_kwargs: {"ok": True})
    monkeypatch.setattr(
        validation_module,
        "run_secure_playwright_code",
        lambda **_kwargs: json.dumps(
            {
                "ok": False,
                "checks": [{"name": "collaboration_ws_admission", "status": "passed"}],
                "cleanup": {"socketClosed": True},
                "transport": {"closeCode": 1009, "maxFrameBytes": 1_048_576},
                "failure": {
                    "code": "collaboration_validation_failed",
                    "check": "collaboration_latest_page",
                    "message": f"failed for {credentials.username} {credentials.password}",
                },
            }
        ),
    )
    monkeypatch.setattr(
        validation_module.subprocess,
        "run",
        lambda args, **_kwargs: subprocess.CompletedProcess(args, 0, "", ""),
    )

    result, status = validation_module.run_validation(
        repo_root=tmp_path,
        raw_url="https://example.com/hermes/",
        group_id=GROUP_ID,
        session="collaboration-validation",
        playwright_cli="playwright-cli",
        timeout=30,
    )

    assert status == 1
    assert result["failure"]["check"] == "collaboration_latest_page"
    assert result["transport"]["closeCode"] == 1009
    serialized = json.dumps(result)
    assert credentials.username not in serialized
    assert credentials.password not in serialized


@pytest.mark.parametrize("group_id", ["group-a", "cg_bad/slash", "cg_", "cg_!bad"])
def test_validation_rejects_invalid_group_ids(validation_module, group_id):
    with pytest.raises(validation_module.LoginError):
        validation_module._validate_group_id(group_id)
