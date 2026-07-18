from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture
def login_module():
    path = Path(__file__).resolve().parents[2] / "scripts" / "playwright_dashboard_login.py"
    spec = importlib.util.spec_from_file_location("_playwright_dashboard_login", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


def _credentials(module, root: Path, *, mode: int = 0o600) -> Path:
    path = root / module.CREDENTIALS_FILENAME
    path.write_text(
        f"{module.USERNAME_KEY}='member@example.com'\n"
        f"{module.PASSWORD_KEY}='secret value'\n",
        encoding="utf-8",
    )
    path.chmod(mode)
    return path


def _git_result(returncode: int) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["git"], returncode, "", "")


def _allow_ignored_untracked(monkeypatch, module):
    def fake_git(_root, command, *_args):
        if command == "ls-files":
            return _git_result(1)
        if command == "check-ignore":
            return _git_result(0)
        raise AssertionError(command)

    monkeypatch.setattr(module, "_run_git", fake_git)


def test_load_credentials_reads_safe_ignored_file(login_module, monkeypatch, tmp_path):
    _credentials(login_module, tmp_path)
    _allow_ignored_untracked(monkeypatch, login_module)

    credentials = login_module.load_credentials(tmp_path)

    assert credentials.username == "member@example.com"
    assert credentials.password == "secret value"


def test_load_credentials_requires_file(login_module, monkeypatch, tmp_path):
    _allow_ignored_untracked(monkeypatch, login_module)

    with pytest.raises(login_module.LoginError, match="Missing .env.local"):
        login_module.load_credentials(tmp_path)


def test_load_credentials_rejects_missing_key(login_module, monkeypatch, tmp_path):
    path = tmp_path / login_module.CREDENTIALS_FILENAME
    path.write_text(f"{login_module.USERNAME_KEY}=member\n", encoding="utf-8")
    path.chmod(0o600)
    _allow_ignored_untracked(monkeypatch, login_module)

    with pytest.raises(login_module.LoginError, match=login_module.PASSWORD_KEY):
        login_module.load_credentials(tmp_path)


def test_load_credentials_rejects_tracked_file(login_module, monkeypatch, tmp_path):
    _credentials(login_module, tmp_path)
    monkeypatch.setattr(login_module, "_run_git", lambda *_args: _git_result(0))

    with pytest.raises(login_module.LoginError, match="tracked credential"):
        login_module.load_credentials(tmp_path)


def test_load_credentials_rejects_unignored_file(login_module, monkeypatch, tmp_path):
    _credentials(login_module, tmp_path)

    def fake_git(_root, command, *_args):
        return _git_result(1)

    monkeypatch.setattr(login_module, "_run_git", fake_git)

    with pytest.raises(login_module.LoginError, match="not covered by .gitignore"):
        login_module.load_credentials(tmp_path)


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission invariant")
def test_load_credentials_rejects_permissive_mode(login_module, monkeypatch, tmp_path):
    _credentials(login_module, tmp_path, mode=0o640)
    _allow_ignored_untracked(monkeypatch, login_module)

    with pytest.raises(login_module.LoginError, match="permissions 0600"):
        login_module.load_credentials(tmp_path)


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks unavailable")
def test_load_credentials_rejects_symlink(login_module, monkeypatch, tmp_path):
    target = tmp_path / "secret-target"
    target.write_text("unused", encoding="utf-8")
    target.chmod(0o600)
    (tmp_path / login_module.CREDENTIALS_FILENAME).symlink_to(target)
    _allow_ignored_untracked(monkeypatch, login_module)

    with pytest.raises(login_module.LoginError, match="Cannot safely open"):
        login_module.load_credentials(tmp_path)


def test_load_credentials_rejects_wrong_owner(login_module, monkeypatch, tmp_path):
    path = _credentials(login_module, tmp_path)
    _allow_ignored_untracked(monkeypatch, login_module)
    real_fstat = login_module.os.fstat

    class Metadata:
        def __init__(self, source):
            self.st_mode = source.st_mode
            self.st_uid = source.st_uid + 1

    monkeypatch.setattr(login_module.os, "fstat", lambda fd: Metadata(real_fstat(fd)))

    with pytest.raises(login_module.LoginError, match="owned by the current user"):
        login_module.load_credentials(tmp_path)
    assert path.exists()


def test_normalize_dashboard_url_preserves_prefix(login_module):
    urls = login_module.normalize_dashboard_url("https://abinllm.xyz/hermes")

    assert urls.base == "https://abinllm.xyz/hermes/"
    assert urls.login == "https://abinllm.xyz/hermes/login"
    assert urls.auth_me == "https://abinllm.xyz/hermes/api/auth/me"
    assert urls.path_prefix == "/hermes/"


def test_normalize_dashboard_url_allows_loopback_http(login_module):
    urls = login_module.normalize_dashboard_url("http://127.0.0.1:9119/dev/")

    assert urls.login == "http://127.0.0.1:9119/dev/login"
    assert urls.origin == "http://127.0.0.1:9119"


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/hermes/",
        "https://user:pass@example.com/hermes/",
        "https://example.com/hermes/?next=x",
        "https://example.com/hermes/#fragment",
        "https://example.com/hermes/%2e%2e/admin/",
        "file:///tmp/hermes",
    ],
)
def test_normalize_dashboard_url_rejects_unsafe_values(login_module, url):
    with pytest.raises(login_module.LoginError):
        login_module.normalize_dashboard_url(url)


def test_login_dashboard_keeps_secrets_out_of_argv_and_removes_script(
    login_module, monkeypatch, tmp_path
):
    credentials = login_module.Credentials("member@example.com", "secret value")
    monkeypatch.setattr(login_module, "load_credentials", lambda _root: credentials)
    calls: list[list[str]] = []
    observed_script: dict[str, object] = {}

    def fake_run(args, *, capture_output, text, check):
        args = [str(value) for value in args]
        calls.append(args)
        if "run-code" in args:
            filename_arg = next(value for value in args if value.startswith("--filename="))
            script = Path(filename_arg.split("=", 1)[1])
            observed_script["path"] = script
            observed_script["mode"] = stat.S_IMODE(script.stat().st_mode)
            body = script.read_text(encoding="utf-8")
            assert credentials.username in body
            assert credentials.password in body
            return subprocess.CompletedProcess(args, 0, json.dumps({"ok": True, "status": 200, "provider": "basic"}), "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(login_module.subprocess, "run", fake_run)

    result = login_module.login_dashboard(
        repo_root=tmp_path,
        raw_url="https://example.com/hermes/",
        session="hermes-validation",
        playwright_cli="playwright-cli",
    )

    assert result["ok"] is True
    assert observed_script["mode"] == 0o600
    assert not observed_script["path"].exists()
    flattened = "\n".join(" ".join(call) for call in calls)
    assert credentials.username not in flattened
    assert credentials.password not in flattened
    assert sum("close" in call for call in calls) == 1


def test_login_dashboard_redacts_failure_and_closes_session(login_module, monkeypatch, tmp_path):
    credentials = login_module.Credentials("member@example.com", "secret value")
    monkeypatch.setattr(login_module, "load_credentials", lambda _root: credentials)
    calls: list[list[str]] = []

    def fake_run(args, *, capture_output, text, check):
        args = [str(value) for value in args]
        calls.append(args)
        if "run-code" in args:
            return subprocess.CompletedProcess(
                args,
                1,
                "",
                f"bad login for {credentials.username}: {credentials.password}",
            )
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(login_module.subprocess, "run", fake_run)

    with pytest.raises(login_module.LoginError) as raised:
        login_module.login_dashboard(
            repo_root=tmp_path,
            raw_url="https://example.com/hermes/",
            session="hermes-validation",
            playwright_cli="playwright-cli",
        )

    message = str(raised.value)
    assert credentials.username not in message
    assert credentials.password not in message
    assert message.count("[REDACTED]") == 2
    assert sum("close" in call for call in calls) == 2


def test_login_javascript_checks_form_and_authenticated_identity(login_module):
    urls = login_module.normalize_dashboard_url("https://example.com/hermes/")
    credentials = login_module.Credentials("member", "secret")

    script = login_module._login_javascript(urls, credentials)

    assert 'form.provider-form[data-provider="basic"]' in script
    assert 'input[name="username"]' in script
    assert 'input[name="password"]' in script
    assert "page.request.get(config.authMeUrl)" in script
    assert "identity.provider !== 'basic'" in script
