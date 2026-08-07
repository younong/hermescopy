from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "configure_remote_volcengine_agent_plan.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("_configure_remote_volcengine", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


def test_local_verification_sends_minimal_request_without_printing_secret(monkeypatch):
    module = _load_module()
    observed = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def read(self, _limit):
            return b'{"id":"response-id"}'

    def urlopen(request, *, timeout):
        observed["request"] = request
        observed["timeout"] = timeout
        return Response()

    monkeypatch.setattr(module.urllib.request, "urlopen", urlopen)

    module.verify_live_request("replacement-secret", timeout=5)

    request = observed["request"]
    payload = json.loads(request.data)
    assert observed["timeout"] == 5
    assert request.full_url.endswith("/api/plan/v3/responses")
    assert request.get_header("Authorization") == "Bearer replacement-secret"
    assert payload == {
        "model": "doubao-seed-2.0-mini",
        "input": "只回复OK",
        "max_output_tokens": 8,
    }


def test_configurator_sends_secret_only_over_stdin():
    module = _load_module()
    observed = {}

    def runner(command, **kwargs):
        observed["command"] = command
        observed.update(kwargs)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=(
                '{"live_request":"passed","service":"active",'
                '"status":"configured"}\n'
            ),
            stderr="",
        )

    result = module.configure_remote("replacement-secret", runner=runner)

    assert result == {
        "live_request": "passed",
        "service": "active",
        "status": "configured",
    }
    assert observed["input"] == "replacement-secret"
    assert all("replacement-secret" not in argument for argument in observed["command"])
    assert observed["capture_output"] is True
    assert observed["text"] is True
    assert observed["check"] is False
    assert observed["command"][-1].startswith("python3 -c ")
    assert "/opt/hermes/shared/.env" in observed["command"][-1]
    assert "systemctl" in observed["command"][-1]
    assert "/api/plan/v3/responses" in observed["command"][-1]
    assert "doubao-seed-2.0-mini" in observed["command"][-1]


@pytest.mark.parametrize("secret", ["", " key", "key ", "a\nb", "a\rb", "a\x00b"])
def test_configurator_rejects_unsafe_secret_without_running(secret):
    module = _load_module()

    with pytest.raises(ValueError):
        module.configure_remote(
            secret,
            runner=lambda *_args, **_kwargs: pytest.fail("runner must not execute"),
        )


def test_non_tty_prompt_uses_loopback_browser(monkeypatch):
    module = _load_module()

    class NonTty:
        def isatty(self):
            return False

    monkeypatch.setattr(module.sys, "stdin", NonTty())
    monkeypatch.setattr(module.sys, "stdout", NonTty())
    monkeypatch.setattr(
        module,
        "_prompt_secret_in_browser",
        lambda: "replacement-secret",
    )

    assert module._prompt_secret() == "replacement-secret"


def test_browser_form_uses_password_input_and_no_secret():
    module = _load_module()

    form = module._browser_form("/one-time-token").decode()

    assert 'type="password"' in form
    assert 'autocomplete="new-password"' in form
    assert 'action="/one-time-token"' in form
    assert "replacement-secret" not in form


def test_loopback_browser_prompt_accepts_secret_without_logging(monkeypatch):
    module = _load_module()
    requests = []

    class Headers:
        @staticmethod
        def get(name, default=None):
            assert name == "Content-Length"
            return str(len(b"key=replacement-secret"))

    class Handler:
        headers = Headers()
        path = ""
        rfile = io.BytesIO(b"key=replacement-secret")
        wfile = io.BytesIO()
        response_headers = {}

        def send_response(self, status):
            self.status = status

        def send_header(self, name, value):
            self.response_headers[name] = value

        def end_headers(self):
            pass

    class Server:
        server_port = 47321
        timeout = None

        def __init__(self, address, handler_type):
            assert address == ("127.0.0.1", 0)
            self.handler_type = handler_type

        def handle_request(self):
            handler = Handler()
            handler.path = requests[0]
            handler._respond = self.handler_type._respond.__get__(handler)
            self.handler_type.do_POST(handler)

        def server_close(self):
            pass

    def open_url(command, **kwargs):
        assert command[0] == "open"
        url = command[1]
        requests.append(module.urllib.parse.urlsplit(url).path)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(module.http.server, "HTTPServer", Server)
    monkeypatch.setattr(module.subprocess, "run", open_url)
    monkeypatch.setattr(module.sys, "platform", "darwin")

    assert module._prompt_secret_in_browser(timeout=1) == "replacement-secret"


def test_main_writes_only_non_secret_status(tmp_path, monkeypatch, capsys):
    module = _load_module()
    status_file = tmp_path / "status.json"
    captured = {}
    monkeypatch.setattr(module, "_prompt_secret", lambda: "replacement-secret")
    monkeypatch.setattr(module, "verify_live_request", lambda _secret: None)
    monkeypatch.setattr(
        module,
        "configure_remote",
        lambda secret, **_kwargs: captured.setdefault("secret", secret),
    )

    assert module.main(["--status-file", str(status_file)]) == 0

    assert captured == {"secret": "replacement-secret"}
    assert "replacement-secret" not in capsys.readouterr().out
    assert status_file.read_text() == '{"status": "configured"}\n'
    assert status_file.stat().st_mode & 0o777 == 0o600
