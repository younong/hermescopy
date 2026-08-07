#!/usr/bin/env python3
"""Securely configure the Volcengine Agent Plan key on a Hermes server."""

from __future__ import annotations

import argparse
import getpass
import http.server
import json
import os
import secrets
import shlex
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path
from typing import Callable, Sequence


DEFAULT_HOST = "106.15.186.104"
DEFAULT_USER = "root"
DEFAULT_PORT = 22
DEFAULT_IDENTITY = "~/.ssh/hermes_apiyi_ed25519"
SECRET_NAME = "VOLCENGINE_AGENT_PLAN_API_KEY"
REMOTE_ENV_FILE = "/opt/hermes/shared/.env"

_REMOTE_CONFIGURATOR = r'''
import json
import os
import re
import socket
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

name = "VOLCENGINE_AGENT_PLAN_API_KEY"
target = Path("/opt/hermes/shared/.env")
raw = sys.stdin.buffer.read(4097)
if not raw or len(raw) > 4096 or b"\x00" in raw or b"\n" in raw or b"\r" in raw:
    raise SystemExit("invalid secret input")
try:
    secret = raw.decode("utf-8")
except UnicodeDecodeError:
    raise SystemExit("invalid secret encoding")
if secret != secret.strip() or not secret:
    raise SystemExit("invalid secret input")

os.umask(0o077)
target.parent.mkdir(parents=True, exist_ok=True)
old_exists = target.exists()
if target.is_symlink():
    raise SystemExit("refusing symlink environment file")
if old_exists:
    metadata = target.stat()
    if not stat.S_ISREG(metadata.st_mode):
        raise SystemExit("environment path is not a regular file")
    old_bytes = target.read_bytes()
    owner = (metadata.st_uid, metadata.st_gid)
else:
    old_bytes = b""
    owner = (0, 0)
try:
    old_text = old_bytes.decode("utf-8")
except UnicodeDecodeError:
    raise SystemExit("environment file is not UTF-8")

pattern = re.compile(r"^\s*(?:export\s+)?VOLCENGINE_AGENT_PLAN_API_KEY\s*=", re.ASCII)
lines = [line for line in old_text.splitlines() if not pattern.match(line)]
lines.append(f"{name}={secret}")
next_bytes = ("\n".join(lines).rstrip("\n") + "\n").encode("utf-8")

def replace_file(payload):
    descriptor, temporary = tempfile.mkstemp(prefix=".env.", dir=str(target.parent))
    try:
        os.fchmod(descriptor, 0o640)
        os.fchown(descriptor, *owner)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise

replace_file(next_bytes)
del raw, next_bytes

def dashboard_ready():
    if subprocess.run(
        ["systemctl", "is-active", "--quiet", "hermes-dashboard.service"],
        check=False,
    ).returncode != 0:
        return False
    try:
        with socket.create_connection(("127.0.0.1", 9119), timeout=1):
            return True
    except OSError:
        return False

try:
    restarted = subprocess.run(
        ["systemctl", "restart", "hermes-dashboard.service"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if restarted.returncode != 0:
        raise RuntimeError("dashboard restart failed")
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline and not dashboard_ready():
        time.sleep(1)
    if not dashboard_ready():
        raise RuntimeError("dashboard did not become ready")

    request = urllib.request.Request(
        "https://ark.cn-beijing.volces.com/api/plan/v3/responses",
        data=json.dumps({
            "model": "doubao-seed-2.0-mini",
            "input": "只回复OK",
            "max_output_tokens": 8,
        }).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {secret}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            response_bytes = response.read(1024 * 1024)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"live model request failed with HTTP {exc.code}") from None
    except urllib.error.URLError as exc:
        raise RuntimeError("live model request failed to connect") from None
    try:
        response_payload = json.loads(response_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeError("live model request returned invalid JSON") from None
    if not response_payload.get("id") and not response_payload.get("output"):
        raise RuntimeError("live model request returned no output")
except Exception as exc:
    if old_exists:
        replace_file(old_bytes)
    else:
        target.unlink(missing_ok=True)
    subprocess.run(
        ["systemctl", "restart", "hermes-dashboard.service"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if str(exc).startswith("live model request"):
        raise SystemExit(str(exc))
    raise SystemExit("configuration rolled back because dashboard restart failed")
finally:
    del secret

print(json.dumps({
    "status": "configured",
    "service": "active",
    "live_request": "passed",
}, sort_keys=True))
'''


def _validate_secret(secret: str) -> None:
    if not secret or secret != secret.strip():
        raise ValueError("Key 不能为空，且首尾不能包含空格。")
    if any(character in secret for character in ("\x00", "\n", "\r")):
        raise ValueError("Key 不能包含换行或 NUL 字符。")
    if len(secret.encode("utf-8")) > 4096:
        raise ValueError("Key 长度超过限制。")


def verify_live_request(secret: str, *, timeout: float = 45.0) -> None:
    """Verify the key locally without exposing the response or secret."""
    _validate_secret(secret)
    request = urllib.request.Request(
        "https://ark.cn-beijing.volces.com/api/plan/v3/responses",
        data=json.dumps(
            {
                "model": "doubao-seed-2.0-mini",
                "input": "只回复OK",
                "max_output_tokens": 8,
            }
        ).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {secret}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response_bytes = response.read(1024 * 1024)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"真实模型请求失败：HTTP {exc.code}") from None
    except urllib.error.URLError:
        raise RuntimeError("真实模型请求连接失败。") from None
    try:
        response_payload = json.loads(response_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeError("真实模型请求返回了无效 JSON。") from None
    if not response_payload.get("id") and not response_payload.get("output"):
        raise RuntimeError("真实模型请求没有返回输出。")


def configure_remote(
    secret: str,
    *,
    host: str = DEFAULT_HOST,
    user: str = DEFAULT_USER,
    port: int = DEFAULT_PORT,
    identity_file: str = DEFAULT_IDENTITY,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, str]:
    """Send *secret* over SSH stdin; it never appears in argv or output."""
    _validate_secret(secret)
    command = [
        "ssh",
        "-p",
        str(port),
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=15",
        "-i",
        str(Path(identity_file).expanduser()),
        f"{user}@{host}",
        f"python3 -c {shlex.quote(_REMOTE_CONFIGURATOR)}",
    ]
    completed = runner(
        command,
        input=secret,
        text=True,
        capture_output=True,
        timeout=150,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "remote configuration failed").strip()
        raise RuntimeError(detail[-500:])
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("服务器返回了无效的配置结果。") from exc
    if result != {
        "live_request": "passed",
        "service": "active",
        "status": "configured",
    }:
        raise RuntimeError("服务器未确认配置和真实模型请求成功。")
    return result


def _write_status(path: Path, result: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(result, stream, sort_keys=True)
            stream.write("\n")
        os.replace(temporary, path)
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _browser_form(path: str, message: str = "") -> bytes:
    notice = f'<p role="alert">{message}</p>' if message else ""
    return f"""<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Cache-Control" content="no-store">
<title>配置 Volcengine Agent Plan</title>
<h1>配置 Volcengine Agent Plan</h1>
<p>请输入已轮换的新 Key。Key 只通过本机回环连接和 SSH 标准输入传输。</p>
{notice}
<form action="{path}" method="post" autocomplete="off">
<label>Agent Plan Key
<input type="password" name="key" required autofocus autocomplete="new-password"
       autocapitalize="none" spellcheck="false" size="48" maxlength="4096">
</label>
<button type="submit">配置并验证</button>
</form>
""".encode("utf-8")


def _prompt_secret_in_browser(*, timeout: float = 600) -> str:
    token = secrets.token_urlsafe(32)
    path = f"/{token}"
    result: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def _respond(self, status: int, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Content-Security-Policy", "default-src 'none'; form-action 'self'")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if urllib.parse.urlsplit(self.path).path != path:
                self._respond(404, b"Not found")
                return
            self._respond(200, _browser_form(path))

        def do_POST(self) -> None:
            if urllib.parse.urlsplit(self.path).path != path:
                self._respond(404, b"Not found")
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if length <= 0 or length > 8192:
                self._respond(400, _browser_form(path, "Key 输入无效，请重试。"))
                return
            payload = self.rfile.read(length).decode("utf-8", errors="strict")
            secret = urllib.parse.parse_qs(
                payload,
                keep_blank_values=True,
                strict_parsing=True,
            ).get("key", [""])[0]
            try:
                _validate_secret(secret)
            except ValueError:
                self._respond(400, _browser_form(path, "Key 输入无效，请重试。"))
                return
            result["secret"] = secret
            self._respond(
                200,
                """<!doctype html><meta charset="utf-8"><title>已接收</title>
<h1>Key 已安全接收</h1><p>正在配置并执行真实模型请求，可以关闭此页面。</p>""".encode("utf-8"),
            )

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    server.timeout = 1
    url = f"http://127.0.0.1:{server.server_port}{path}"
    try:
        if sys.platform == "darwin":
            opened = subprocess.run(
                ["open", url],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
                check=False,
            ).returncode == 0
        else:
            opened = webbrowser.open(url, new=1)
        if not opened:
            raise RuntimeError(f"无法自动打开浏览器，请在本机打开：{url}")
        deadline = time.monotonic() + timeout
        while "secret" not in result and time.monotonic() < deadline:
            server.handle_request()
        if "secret" not in result:
            raise TimeoutError("等待 Key 输入超时，未修改服务器配置。")
        return result.pop("secret")
    finally:
        server.server_close()


def _prompt_secret() -> str:
    prompt = "请输入轮换后的 Volcengine Agent Plan Key（输入不可见）: "
    if sys.stdin.isatty() and sys.stdout.isatty():
        return getpass.getpass(prompt)
    return _prompt_secret_in_browser()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--user", default=DEFAULT_USER)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--identity-file", default=DEFAULT_IDENTITY)
    parser.add_argument("--status-file", type=Path, help=argparse.SUPPRESS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    status: dict[str, str]
    try:
        secret = _prompt_secret()
        verify_live_request(secret)
        configure_remote(
            secret,
            host=args.host,
            user=args.user,
            port=args.port,
            identity_file=args.identity_file,
        )
        del secret
        status = {"status": "configured"}
        print("配置成功，Hermes Dashboard 已重启，真实模型请求已通过。")
        exit_code = 0
    except (EOFError, KeyboardInterrupt):
        status = {"status": "cancelled"}
        print("\n已取消，未修改服务器配置。", file=sys.stderr)
        exit_code = 130
    except Exception as exc:
        status = {"status": "failed", "error_type": type(exc).__name__}
        print(f"配置失败：{exc}", file=sys.stderr)
        exit_code = 1
    if args.status_file:
        _write_status(args.status_file.expanduser(), status)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
