#!/usr/bin/env python3
"""Deterministic end-to-end smoke test for the Hermes conversation gateway."""

from __future__ import annotations

import argparse
import base64
import itertools
import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from hermes_cli import __version__ as HERMES_VERSION

SCHEMA_VERSION = 1
KIND = "hermes.conversation-smoke"
MODEL = "hermes-smoke-model"
PROVIDER = "custom:hermes-smoke"
ATTACHMENT_MARKER = "attachment-smoke-marker-731"
SAFE_TOOL_MARKER = "safe-tool-ok-419"
RESUME_MARKER = "resume-context-marker-587"
DEFAULT_TIMEOUT = 90.0
STEP_TIMEOUT = 60.0


class SmokeFailure(RuntimeError):
    def __init__(self, code: str, check: str, message: str):
        super().__init__(message)
        self.code = code
        self.check = check


def _bounded(value: object, limit: int = 500) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit] + ("..." if len(text) > limit else "")


class ModelStub:
    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.requests: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                    with owner._lock:
                        owner.requests.append(payload)
                    response = owner._response(payload)
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-cache")
                    self.end_headers()
                    for chunk in response:
                        self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode("utf-8"))
                        self.wfile.flush()
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, _format: str, *_args: object) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.server.request_queue_size = 32
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}/v1"

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def saw_text(self, marker: str) -> bool:
        with self._lock:
            return any(marker in json.dumps(item, ensure_ascii=False) for item in self.requests)

    @staticmethod
    def _text_chunks(parts: list[str]) -> list[dict[str, Any]]:
        chunks: list[dict[str, Any]] = [
            {
                "id": "smoke",
                "choices": [
                    {"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}
                ],
            }
        ]
        chunks.extend(
            {
                "id": "smoke",
                "choices": [{"index": 0, "delta": {"content": part}, "finish_reason": None}],
            }
            for part in parts
        )
        chunks.append(
            {"id": "smoke", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
        )
        return chunks

    @staticmethod
    def _tool_chunk(name: str, arguments: dict[str, Any], call_id: str) -> list[dict[str, Any]]:
        return [
            {
                "id": "smoke",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": call_id,
                                    "type": "function",
                                    "function": {
                                        "name": name,
                                        "arguments": json.dumps(arguments),
                                    },
                                }
                            ],
                        },
                        "finish_reason": None,
                    }
                ],
            },
            {
                "id": "smoke",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
            },
        ]

    def _response(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        messages = payload.get("messages") or []
        serialized = json.dumps(messages, ensure_ascii=False)
        tool_messages = [item for item in messages if isinstance(item, dict) and item.get("role") == "tool"]
        latest_tool = json.dumps(tool_messages[-1], ensure_ascii=False) if tool_messages else ""
        latest_user = ""
        for item in reversed(messages):
            if isinstance(item, dict) and item.get("role") == "user":
                latest_user = json.dumps(item.get("content", ""), ensure_ascii=False)
                break

        if "approval-deny" in latest_user:
            if tool_messages and ("BLOCKED" in latest_tool or "denied" in latest_tool.lower()):
                return self._text_chunks(["approval ", "denied safely"])
            protected = self.workspace / "protected"
            return self._tool_chunk(
                "terminal", {"command": f"rm -rf {protected}", "timeout": 5}, "call-dangerous"
            )

        if "resume-continuation" in latest_user:
            status = "prior context present" if RESUME_MARKER in serialized else "prior context missing"
            return self._text_chunks(["resume ", status])

        if "attachment-safe-tool" in latest_user:
            if tool_messages and SAFE_TOOL_MARKER in latest_tool:
                return self._text_chunks(["stream-one ", f"stream-two {RESUME_MARKER}"])
            return self._tool_chunk(
                "terminal",
                {"command": f"printf '%s\\n' '{SAFE_TOOL_MARKER}'", "timeout": 5},
                "call-safe",
            )

        return self._text_chunks(["smoke ", "ok"])


class GatewayProcess:
    _sequence = itertools.count(1)

    def __init__(self, repo_root: Path, env: dict[str, str]):
        self.messages: queue.Queue[dict[str, Any]] = queue.Queue()
        self.stderr: list[str] = []
        self.pending: list[dict[str, Any]] = []
        self.next_id = 1
        launch_env = env.copy()
        launch_env["HERMES_SMOKE_RUN_ID"] = f"{os.getpid()}-{next(self._sequence)}"
        self.process = subprocess.Popen(
            [sys.executable, "-m", "tui_gateway.entry"],
            cwd=repo_root,
            env=launch_env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            start_new_session=(os.name == "posix"),
        )
        assert self.process.stdout is not None and self.process.stderr is not None
        self.stdout_thread = threading.Thread(target=self._read_stdout, daemon=True)
        self.stderr_thread = threading.Thread(target=self._read_stderr, daemon=True)
        self.stdout_thread.start()
        self.stderr_thread.start()

    def _read_stdout(self) -> None:
        assert self.process.stdout is not None
        for raw in self.process.stdout:
            try:
                value = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                self.messages.put(value)

    def _read_stderr(self) -> None:
        assert self.process.stderr is not None
        for raw in self.process.stderr:
            line = _bounded(raw, 300)
            self.stderr.append(line)
            if len(self.stderr) > 80:
                del self.stderr[0]

    def _next(self, deadline: float) -> dict[str, Any]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise queue.Empty
        return self.messages.get(timeout=remaining)

    def wait_event(
        self,
        event_type: str,
        *,
        session_id: str | None = None,
        timeout: float = STEP_TIMEOUT,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            for index, message in enumerate(self.pending):
                params = message.get("params") or {}
                if (
                    message.get("method") == "event"
                    and params.get("type") == event_type
                    and (session_id is None or params.get("session_id") == session_id)
                ):
                    return self.pending.pop(index)
            try:
                message = self._next(deadline)
            except queue.Empty as exc:
                detail = " | ".join(self.stderr[-5:]) if self.stderr else "no gateway error output"
                buffered: list[str] = []
                for item in self.pending[-12:]:
                    params = item.get("params") or {}
                    label = str(params.get("type") or item.get("id") or "unknown")
                    if label == "error":
                        label = f"error:{_bounded(_event_payload(item).get('message'), 240)}"
                    buffered.append(label)
                raise SmokeFailure(
                    "event_timeout",
                    event_type,
                    f"Timed out waiting for {event_type}: {detail}; buffered={buffered}",
                ) from exc
            self.pending.append(message)

    def request(self, method: str, params: dict[str, Any], timeout: float = STEP_TIMEOUT) -> dict[str, Any]:
        request_id = f"smoke-{self.next_id}"
        self.next_id += 1
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        if self.process.poll() is not None or self.process.stdin is None:
            raise SmokeFailure("gateway_exited", method, f"Gateway exited before {method}")
        self.process.stdin.write(json.dumps(payload) + "\n")
        self.process.stdin.flush()
        deadline = time.monotonic() + timeout
        while True:
            for index, message in enumerate(self.pending):
                if message.get("id") == request_id:
                    response = self.pending.pop(index)
                    break
            else:
                try:
                    self.pending.append(self._next(deadline))
                except queue.Empty as exc:
                    raise SmokeFailure("rpc_timeout", method, f"Timed out waiting for {method}") from exc
                continue
            if "error" in response:
                error = response.get("error") or {}
                raise SmokeFailure(
                    "rpc_error",
                    method,
                    f"{method} failed ({error.get('code')}): {_bounded(error.get('message'))}",
                )
            result = response.get("result")
            if not isinstance(result, dict):
                raise SmokeFailure("invalid_rpc_result", method, f"{method} returned an invalid result")
            return result

    def close(self) -> None:
        if self.process.poll() is None:
            if self.process.stdin is not None:
                try:
                    self.process.stdin.close()
                except OSError:
                    pass
            try:
                self.process.wait(timeout=4)
            except subprocess.TimeoutExpired:
                if os.name == "posix":
                    try:
                        os.killpg(self.process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                else:
                    self.process.terminate()
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    if os.name == "posix":
                        try:
                            os.killpg(self.process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    else:
                        self.process.kill()
                    self.process.wait(timeout=3)
        self.stdout_thread.join(timeout=2)
        self.stderr_thread.join(timeout=2)


def _event_payload(event: dict[str, Any]) -> dict[str, Any]:
    params = event.get("params") or {}
    payload = params.get("payload") or {}
    return payload if isinstance(payload, dict) else {}


def _record(checks: list[dict[str, Any]], name: str, started: float, **details: object) -> None:
    item: dict[str, Any] = {
        "name": name,
        "status": "passed",
        "durationMs": round((time.monotonic() - started) * 1000),
    }
    item.update(details)
    checks.append(item)


def _wait_complete(gateway: GatewayProcess, sid: str) -> tuple[list[str], dict[str, Any]]:
    deltas: list[str] = []
    deadline = time.monotonic() + STEP_TIMEOUT
    while True:
        for index, pending in enumerate(gateway.pending):
            params = pending.get("params") or {}
            if pending.get("method") != "event" or params.get("session_id") != sid:
                continue
            event_type = params.get("type")
            if event_type == "message.delta":
                event = gateway.pending.pop(index)
                payload = _event_payload(event)
                if isinstance(payload.get("text"), str):
                    deltas.append(payload["text"])
                break
            if event_type == "message.complete":
                complete = gateway.pending.pop(index)
                result = _event_payload(complete)
                if result.get("status") != "complete":
                    raise SmokeFailure("turn_failed", "prompt_stream", "Conversation turn did not complete")
                return deltas, result
        else:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SmokeFailure("turn_timeout", "prompt_stream", "Timed out waiting for message.complete")
            try:
                gateway.pending.append(gateway._next(deadline))
            except queue.Empty as exc:
                raise SmokeFailure("turn_timeout", "prompt_stream", "Timed out waiting for message.complete") from exc
            continue


def _write_config(home: Path, base_url: str) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        "model:\n"
        f"  default: {MODEL}\n"
        f"  provider: {PROVIDER}\n"
        "  api_mode: chat_completions\n"
        "custom_providers:\n"
        "  - name: hermes-smoke\n"
        f"    base_url: {base_url}\n"
        "    api_key: smoke-local-only\n"
        "    api_mode: chat_completions\n"
        "agent:\n"
        "  max_turns: 8\n"
        "display:\n"
        "  tool_progress: full\n"
        "approvals:\n"
        "  mode: ask\n",
        encoding="utf-8",
    )


def _seed_offline_caches(home: Path) -> None:
    # Gateway startup performs a best-effort update check in a daemon thread.
    # A fresh cache exercises that production path without allowing git/PyPI I/O.
    (home / ".update_check").write_text(
        json.dumps(
            {
                "ts": time.time(),
                "behind": 0,
                "rev": None,
                "ver": HERMES_VERSION,
            }
        ),
        encoding="utf-8",
    )



def _write_network_guard(root: Path) -> Path:
    guard = root / "network-guard"
    guard.mkdir()
    (guard / "sitecustomize.py").write_text(
        """import socket

_original_connect = socket.socket.connect
_original_connect_ex = socket.socket.connect_ex


def _is_loopback(address):
    if isinstance(address, str):
        return True  # Unix-domain socket path.
    if not isinstance(address, tuple) or not address:
        return False
    host = str(address[0]).strip().lower()
    return host in {"127.0.0.1", "::1", "localhost"}


def _guarded_connect(sock, address):
    if not _is_loopback(address):
        raise OSError("deterministic smoke blocks non-loopback network access")
    return _original_connect(sock, address)


def _guarded_connect_ex(sock, address):
    if not _is_loopback(address):
        return 101
    return _original_connect_ex(sock, address)


socket.socket.connect = _guarded_connect
socket.socket.connect_ex = _guarded_connect_ex
""",
        encoding="utf-8",
    )
    return guard



def _gateway_env(home: Path, workspace: Path, network_guard: Path) -> dict[str, str]:
    env = os.environ.copy()
    for key in list(env):
        if (
            key.endswith("_API_KEY")
            or key.upper() in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"}
            or key in {"ANTHROPIC_AUTH_TOKEN", "HERMES_DEPLOY_PASSWORD"}
        ):
            env.pop(key, None)
    inherited_pythonpath = env.get("PYTHONPATH", "")
    env.update(
        {
            "HERMES_HOME": str(home),
            "HERMES_CWD": str(workspace),
            "TERMINAL_CWD": str(workspace),
            "HERMES_TUI_TOOLSETS": "terminal",
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
            "TERMINAL_ENV": "local",
            "HERMES_IGNORE_RULES": "1",
            "HERMES_DISABLE_LAZY_INSTALLS": "1",
            "HERMES_TUI_CHECKPOINTS": "0",
            "PYTHONPATH": os.pathsep.join(
                item for item in (str(network_guard), inherited_pythonpath) if item
            ),
            "PYTHONUNBUFFERED": "1",
        }
    )
    return env


def run_smoke(repo_root: Path, timeout: float) -> tuple[dict[str, Any], int]:
    started_all = time.monotonic()
    checks: list[dict[str, Any]] = []
    temporary = Path(tempfile.mkdtemp(prefix="hermes-conversation-smoke-"))
    home = temporary / "home"
    workspace = temporary / "workspace"
    workspace.mkdir(parents=True)
    protected = workspace / "protected"
    protected.mkdir()
    sentinel = protected / "sentinel.txt"
    sentinel.write_text("must-survive", encoding="utf-8")
    model = ModelStub(workspace)
    gateway: GatewayProcess | None = None
    stored_id = ""
    failure: SmokeFailure | None = None

    try:
        model.start()
        _write_config(home, model.base_url)
        _seed_offline_caches(home)
        network_guard = _write_network_guard(temporary)
        env = _gateway_env(home, workspace, network_guard)
        deadline = time.monotonic() + timeout

        stage = time.monotonic()
        gateway = GatewayProcess(repo_root, env)
        gateway.wait_event("gateway.ready", timeout=min(STEP_TIMEOUT, deadline - time.monotonic()))
        _record(checks, "gateway_ready", stage)

        stage = time.monotonic()
        created = gateway.request("session.create", {"cols": 96, "cwd": str(workspace)})
        sid = str(created.get("session_id") or "")
        stored_id = str(created.get("stored_session_id") or "")
        if not sid or not stored_id:
            raise SmokeFailure("missing_session_id", "session_create", "session.create omitted an ID")
        _record(checks, "session_create", stage)

        stage = time.monotonic()
        info_event = gateway.wait_event("session.info", session_id=sid)
        info = _event_payload(info_event)
        if info.get("model") != MODEL or info.get("provider") != "custom":
            raise SmokeFailure("config_mismatch", "config_propagation", "Custom provider/model did not reach the live agent")
        _record(checks, "config_propagation", stage, model=MODEL, provider=PROVIDER)

        stage = time.monotonic()
        data = base64.b64encode(ATTACHMENT_MARKER.encode("utf-8")).decode("ascii")
        attached = gateway.request(
            "file.attach",
            {
                "session_id": sid,
                "path": "smoke-note.txt",
                "name": "smoke-note.txt",
                "data_url": f"data:text/plain;base64,{data}",
            },
        )
        ref_text = str(attached.get("ref_text") or "")
        if not attached.get("attached") or not ref_text.startswith("@file:"):
            raise SmokeFailure("attachment_failed", "file_attachment", "file.attach did not stage the file")
        _record(checks, "file_attachment", stage)

        stage = time.monotonic()
        gateway.request(
            "prompt.submit",
            {"session_id": sid, "text": f"attachment-safe-tool {ref_text}"},
        )
        gateway.wait_event("tool.start", session_id=sid)
        tool_done = gateway.wait_event("tool.complete", session_id=sid)
        if _event_payload(tool_done).get("name") != "terminal":
            raise SmokeFailure("wrong_tool", "safe_tool", "Expected the terminal tool")
        _record(checks, "safe_tool", stage)

        deltas, complete = _wait_complete(gateway, sid)
        if len([item for item in deltas if item]) < 2 or RESUME_MARKER not in str(complete.get("text") or ""):
            raise SmokeFailure("stream_contract_failed", "prompt_stream", "Expected multiple deltas and completion marker")
        if not model.saw_text(ATTACHMENT_MARKER) or not model.saw_text(SAFE_TOOL_MARKER):
            raise SmokeFailure("model_context_missing", "prompt_stream", "Attachment or tool output did not reach the model")
        _record(checks, "prompt_stream", stage, deltaCount=len(deltas))

        stage = time.monotonic()
        gateway.request("prompt.submit", {"session_id": sid, "text": "approval-deny"})
        approval = gateway.wait_event("approval.request", session_id=sid)
        if "rm -rf" not in str(_event_payload(approval).get("command") or ""):
            raise SmokeFailure("approval_contract_failed", "approval_deny", "Dangerous command did not request approval")
        resolved = gateway.request("approval.respond", {"session_id": sid, "choice": "deny"})
        if int(resolved.get("resolved") or 0) != 1:
            raise SmokeFailure("approval_not_resolved", "approval_deny", "Approval denial did not resolve the request")
        _, approval_complete = _wait_complete(gateway, sid)
        if "denied" not in str(approval_complete.get("text") or "").lower() or not sentinel.exists():
            raise SmokeFailure("dangerous_command_ran", "approval_deny", "Denied command changed protected smoke data")
        _record(checks, "approval_deny", stage)

        stage = time.monotonic()
        gateway.request("session.close", {"session_id": sid})
        gateway.close()
        gateway = GatewayProcess(repo_root, env)
        gateway.wait_event("gateway.ready")
        resumed = gateway.request("session.resume", {"session_id": stored_id, "cols": 96})
        resumed_sid = str(resumed.get("session_id") or "")
        messages = resumed.get("messages") or []
        if not resumed_sid or RESUME_MARKER not in json.dumps(messages, ensure_ascii=False):
            raise SmokeFailure("resume_history_missing", "cold_resume", "Cold resume did not restore conversation history")
        resumed_info = resumed.get("info") or {}
        if resumed_info.get("model") != MODEL:
            raise SmokeFailure("resume_config_missing", "cold_resume", "Cold resume did not restore the model")
        _record(checks, "cold_resume", stage)

        stage = time.monotonic()
        gateway.request("prompt.submit", {"session_id": resumed_sid, "text": "resume-continuation"})
        _, resumed_complete = _wait_complete(gateway, resumed_sid)
        if "prior context present" not in str(resumed_complete.get("text") or ""):
            raise SmokeFailure("resume_context_missing", "resume_continuation", "Continued turn lacked prior context")
        _record(checks, "resume_continuation", stage)

        gateway.request("session.close", {"session_id": resumed_sid})
        deleted = gateway.request("session.delete", {"session_id": stored_id})
        if deleted.get("deleted") != stored_id:
            raise SmokeFailure("session_cleanup_failed", "artifact_cleanup", "Smoke session was not deleted")
    except SmokeFailure as exc:
        failure = exc
    except Exception as exc:  # keep output stable and sanitized
        failure = SmokeFailure("unexpected_error", "runner", f"{type(exc).__name__}: {_bounded(exc)}")
    finally:
        if gateway is not None:
            gateway.close()
        model.close()
        shutil.rmtree(temporary, ignore_errors=True)

    artifacts_cleaned = not temporary.exists()
    cleanup_started = time.monotonic()
    if artifacts_cleaned and failure is None:
        _record(checks, "artifact_cleanup", cleanup_started)
    elif not artifacts_cleaned and failure is None:
        failure = SmokeFailure("artifact_cleanup_failed", "artifact_cleanup", "Temporary smoke artifacts remain")

    result: dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "kind": KIND,
        "status": "failed" if failure else "passed",
        "checks": checks,
        "artifactsCleaned": artifacts_cleaned,
        "durationMs": round((time.monotonic() - started_all) * 1000),
    }
    if failure:
        result["failure"] = {
            "code": failure.code,
            "check": failure.check,
            "message": _bounded(failure),
        }
    return result, 1 if failure else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    args = parser.parse_args(argv)
    result, status = run_smoke(REPO_ROOT, max(10.0, args.timeout))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
