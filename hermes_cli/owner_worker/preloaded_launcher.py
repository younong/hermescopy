"""Resident single-threaded launcher for fast, isolated Owner Worker forks."""
from __future__ import annotations

import array
import json
import os
import selectors
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

_PROTOCOL_VERSION = 1
_MAX_PACKET_BYTES = 128 * 1024
_MAX_FDS = 8
_RESPONSE_TIMEOUT = 10.0
_FD_NAMES = ("cwd", "stdout", "stderr", "start", "inference", "image", "resource")


class OwnerWorkerLauncherError(RuntimeError):
    """Raised when the resident launcher cannot safely start a worker."""


def _packet(payload: dict[str, Any]) -> bytes:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) > _MAX_PACKET_BYTES:
        raise OwnerWorkerLauncherError("owner worker launch request is too large")
    return encoded


def _recv_packet(channel: socket.socket) -> tuple[dict[str, Any], list[int]]:
    data, ancdata, flags, _address = channel.recvmsg(
        _MAX_PACKET_BYTES + 1,
        socket.CMSG_SPACE(_MAX_FDS * array.array("i").itemsize),
    )
    received: list[int] = []
    for level, kind, value in ancdata:
        if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
            descriptors = array.array("i")
            descriptors.frombytes(value[: len(value) - len(value) % descriptors.itemsize])
            received.extend(descriptors)
    if not data:
        _close_fds(received)
        raise EOFError
    if (
        len(data) > _MAX_PACKET_BYTES
        or flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC)
        or len(received) > _MAX_FDS
    ):
        _close_fds(received)
        raise OwnerWorkerLauncherError("owner worker launcher packet is invalid")
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _close_fds(received)
        raise OwnerWorkerLauncherError("owner worker launcher packet is invalid") from exc
    if not isinstance(payload, dict):
        _close_fds(received)
        raise OwnerWorkerLauncherError("owner worker launcher packet is invalid")
    return payload, received


def _send_packet(channel: socket.socket, payload: dict[str, Any], fds: tuple[int, ...] = ()) -> None:
    ancillary = []
    if fds:
        ancillary.append((socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", fds)))
    channel.sendmsg([_packet(payload)], ancillary)


def _close_fds(fds: list[int] | tuple[int, ...]) -> None:
    for fd in fds:
        try:
            os.close(fd)
        except OSError:
            pass


def _require_text(payload: dict[str, Any], key: str, *, maximum: int = 8192) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value or "\x00" in value or len(value) > maximum:
        raise OwnerWorkerLauncherError(f"owner worker launcher {key} is invalid")
    return value


def _validate_launch(payload: dict[str, Any], descriptor_count: int) -> tuple[list[str], dict[str, str], list[str]]:
    if payload.get("version") != _PROTOCOL_VERSION or payload.get("op") != "launch":
        raise OwnerWorkerLauncherError("owner worker launcher request version is invalid")
    _require_text(payload, "nonce", maximum=128)
    argv = payload.get("argv")
    env = payload.get("env")
    fd_names = payload.get("fdNames")
    if (
        not isinstance(argv, list)
        or not argv
        or len(argv) > 64
        or any(not isinstance(item, str) or not item or "\x00" in item or len(item) > 8192 for item in argv)
        or not isinstance(env, dict)
        or len(env) > 256
        or any(
            not isinstance(key, str)
            or not key
            or "\x00" in key
            or not isinstance(value, str)
            or "\x00" in value
            or len(key) > 256
            or len(value) > 65536
            for key, value in env.items()
        )
        or not isinstance(fd_names, list)
        or any(name not in _FD_NAMES for name in fd_names)
        or len(fd_names) != len(set(fd_names))
        or len(fd_names) != descriptor_count
        or fd_names[:4] != ["cwd", "stdout", "stderr", "start"]
    ):
        raise OwnerWorkerLauncherError("owner worker launcher request is invalid")
    return argv, env, fd_names


def _reset_child_runtime(channel_fd: int, keep_fds: set[int]) -> None:
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    signal.signal(signal.SIGHUP, signal.SIG_DFL)
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    os.close(channel_fd)
    try:
        open_fds = tuple(int(name) for name in os.listdir("/proc/self/fd"))
    except OSError as exc:
        raise OwnerWorkerLauncherError("owner worker launcher cannot enumerate descriptors") from exc
    for fd in open_fds:
        if fd > 2 and fd not in keep_fds:
            try:
                os.close(fd)
            except OSError:
                pass


def _run_child(
    channel_fd: int,
    argv: list[str],
    env: dict[str, str],
    fd_names: list[str],
    descriptors: list[int],
) -> None:
    by_name = dict(zip(fd_names, descriptors, strict=True))
    keep = set(descriptors)
    try:
        _reset_child_runtime(channel_fd, keep)
        os.fchdir(by_name["cwd"])
        os.dup2(by_name["stdout"], 1)
        os.dup2(by_name["stderr"], 2)
        for fd in (by_name["cwd"], by_name["stdout"], by_name["stderr"]):
            if fd > 2:
                os.close(fd)
        os.environ.clear()
        os.environ.update(env)
        relay_names = {
            "inference": "HERMES_DEPLOYMENT_INFERENCE_RELAY_FD",
            "image": "HERMES_DEPLOYMENT_IMAGE_RELAY_FD",
            "resource": "HERMES_DEPLOYMENT_RESOURCE_BROKER_FD",
        }
        for name, env_key in relay_names.items():
            fd = by_name.get(name)
            if fd is not None:
                os.environ[env_key] = str(fd)
        if os.read(by_name["start"], 1) != b"1":
            raise SystemExit("owner worker launch was not admitted")
        os.close(by_name["start"])
        from hermes_cli.owner_worker.entrypoint import run_worker

        run_worker(argv)
    except BaseException:
        import traceback

        traceback.print_exc(file=sys.stderr)
        os._exit(1)
    os._exit(0)


def _preload_owner_worker() -> None:
    if threading.active_count() != 1:
        raise OwnerWorkerLauncherError("owner worker launcher preload is not single-threaded")
    # Import the complete owner-worker graph without constructing owner state.
    import fastapi  # noqa: F401
    import uvicorn  # noqa: F401
    import hermes_state  # noqa: F401
    from hermes_cli import session_api  # noqa: F401
    # The first OWP1 operation constructs the agent runtime. Import its
    # owner-neutral definitions here so readiness does not defer that work.
    import model_tools  # noqa: F401
    import run_agent  # noqa: F401
    from hermes_cli.owner_worker import entrypoint  # noqa: F401
    from hermes_cli.owner_worker import tool_executor_supervisor  # noqa: F401
    from hermes_cli.owner_worker import ws_routes  # noqa: F401
    from tools import checkpoint_manager, process_registry  # noqa: F401
    from gateway import channel_directory, mirror  # noqa: F401
    from tui_gateway import server as gateway_server
    # Import request-only modules now so forked children only bind owner state
    # and routes. These imports are owner-neutral and must remain thread-free.
    from hermes_cli import config, dashboard_owner_payloads, session_analytics  # noqa: F401
    from hermes_cli.owner_worker import credential_broker, tool_executor_sandbox  # noqa: F401
    from tools import skill_manager_tool, skills_tool  # noqa: F401

    if gateway_server._gateway_runtime_initialized:
        raise OwnerWorkerLauncherError("owner worker launcher initialized owner runtime state")
    if gateway_server._pool is not None:
        raise OwnerWorkerLauncherError("owner worker launcher initialized gateway threads")
    if threading.active_count() != 1:
        raise OwnerWorkerLauncherError("owner worker launcher preload started threads")


def _reap_children(children: dict[int, int | None]) -> None:
    for pid, returncode in tuple(children.items()):
        if returncode is not None:
            continue
        try:
            waited, status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            children[pid] = 1
            continue
        if waited == pid:
            children[pid] = os.waitstatus_to_exitcode(status)


def _launcher_loop(channel_fd: int) -> None:
    channel = socket.socket(fileno=channel_fd)
    channel.settimeout(0.1)
    children: dict[int, int | None] = {}
    try:
        # The launcher may inherit Dashboard identity in its own environment,
        # but it must never preload any owner's process-scoped state.
        for key in tuple(os.environ):
            if key == "HERMES_HOME" or key.startswith("HERMES_OWNER_") or key.startswith("HERMES_WORKER_"):
                os.environ.pop(key, None)
        os.environ.pop("HERMES_CONTROL_HOME", None)
        _preload_owner_worker()
        _send_packet(channel, {"version": _PROTOCOL_VERSION, "op": "ready"})
        while True:
            try:
                payload, descriptors = _recv_packet(channel)
            except socket.timeout:
                _reap_children(children)
                continue
            except EOFError:
                break
            nonce = str(payload.get("nonce") or "")[:128]
            try:
                if payload.get("op") == "shutdown":
                    _close_fds(descriptors)
                    _send_packet(
                        channel,
                        {"version": _PROTOCOL_VERSION, "op": "shutdown", "nonce": nonce},
                    )
                    break
                if payload.get("op") == "status":
                    _close_fds(descriptors)
                    _reap_children(children)
                    pid = payload.get("pid")
                    if payload.get("version") != _PROTOCOL_VERSION or not isinstance(pid, int):
                        raise OwnerWorkerLauncherError("owner worker launcher status request is invalid")
                    returncode = children.get(pid)
                    _send_packet(
                        channel,
                        {
                            "version": _PROTOCOL_VERSION,
                            "op": "status",
                            "nonce": nonce,
                            "pid": pid,
                            "returncode": returncode,
                            "known": pid in children,
                        },
                    )
                    if returncode is not None:
                        children.pop(pid, None)
                    continue
                argv, env, fd_names = _validate_launch(payload, len(descriptors))
                pid = os.fork()
                if pid == 0:
                    _run_child(channel.fileno(), argv, env, fd_names, descriptors)
                children[pid] = None
                _close_fds(descriptors)
                _send_packet(
                    channel,
                    {
                        "version": _PROTOCOL_VERSION,
                        "op": "launched",
                        "nonce": nonce,
                        "pid": pid,
                    },
                )
            except BaseException as exc:
                _close_fds(descriptors)
                _send_packet(
                    channel,
                    {
                        "version": _PROTOCOL_VERSION,
                        "op": "error",
                        "nonce": nonce,
                        "errorType": type(exc).__name__,
                    },
                )
            _reap_children(children)
    finally:
        channel.close()
        running = {pid for pid, returncode in children.items() if returncode is None}
        for pid in running:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + 2
        while running and time.monotonic() < deadline:
            _reap_children(children)
            running = {pid for pid in running if children.get(pid) is None}
            if running:
                time.sleep(0.01)
        for pid in running:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                os.waitpid(pid, 0)
            except ChildProcessError:
                pass


class LauncherProcessHandle:
    """pidfd-backed process facade used by OwnerWorkerSupervisor."""

    def __init__(self, pid: int, launcher: "OwnerWorkerLauncher") -> None:
        if not sys.platform.startswith("linux") or not hasattr(os, "pidfd_open"):
            raise OwnerWorkerLauncherError("owner worker launcher requires Linux pidfds")
        self.pid = pid
        self._launcher = launcher
        self._pidfd = os.pidfd_open(pid)
        self.returncode: int | None = None

    def poll(self) -> int | None:
        if self.returncode is not None:
            return self.returncode
        selector = selectors.DefaultSelector()
        try:
            selector.register(self._pidfd, selectors.EVENT_READ)
            if selector.select(0):
                self.returncode = self._launcher._child_returncode(self.pid)
        finally:
            selector.close()
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is not None:
            return self.returncode
        selector = selectors.DefaultSelector()
        try:
            selector.register(self._pidfd, selectors.EVENT_READ)
            if not selector.select(timeout):
                raise subprocess.TimeoutExpired("owner-worker", timeout)
            self.returncode = self._launcher._child_returncode(self.pid)
            return self.returncode
        finally:
            selector.close()

    def terminate(self) -> None:
        self._signal(signal.SIGTERM)

    def kill(self) -> None:
        self._signal(signal.SIGKILL)

    def _signal(self, sig: signal.Signals) -> None:
        if self.poll() is not None:
            return
        if hasattr(signal, "pidfd_send_signal"):
            signal.pidfd_send_signal(self._pidfd, sig)
            return
        os.kill(self.pid, sig)

    def close(self) -> None:
        fd = getattr(self, "_pidfd", -1)
        if fd >= 0:
            os.close(fd)
            self._pidfd = -1


class OwnerWorkerLauncher:
    """Own the preload process and synchronously request exact child forks."""

    def __init__(self, *, process_factory: Any = subprocess.Popen) -> None:
        if not sys.platform.startswith("linux") or not hasattr(os, "pidfd_open"):
            raise OwnerWorkerLauncherError("owner worker launcher requires Linux pidfds")
        parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        child.set_inheritable(True)
        try:
            self._process = process_factory(
                [sys.executable, "-m", "hermes_cli.owner_worker.preloaded_launcher", "--fd", str(child.fileno())],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                pass_fds=(child.fileno(),),
            )
        finally:
            child.close()
        self._channel = parent
        self._channel.settimeout(_RESPONSE_TIMEOUT)
        self._lock = threading.Lock()
        try:
            ready, fds = _recv_packet(self._channel)
            _close_fds(fds)
            if ready != {"version": _PROTOCOL_VERSION, "op": "ready"}:
                raise OwnerWorkerLauncherError("owner worker launcher preload failed")
        except Exception:
            self.close()
            raise

    def spawn(
        self,
        argv: list[str],
        *,
        env: dict[str, str],
        cwd_fd: int,
        stdout_fd: int,
        stderr_fd: int,
        start_fd: int,
        relay_fds: dict[str, int],
    ) -> LauncherProcessHandle:
        nonce = uuid.uuid4().hex
        names = ["cwd", "stdout", "stderr", "start", *relay_fds]
        descriptors = (cwd_fd, stdout_fd, stderr_fd, start_fd, *relay_fds.values())
        request = {
            "version": _PROTOCOL_VERSION,
            "op": "launch",
            "nonce": nonce,
            "argv": argv,
            "env": env,
            "fdNames": names,
        }
        with self._lock:
            if self._process.poll() is not None:
                raise OwnerWorkerLauncherError("owner worker launcher is unavailable")
            _send_packet(self._channel, request, descriptors)
            response, received = _recv_packet(self._channel)
        _close_fds(received)
        if (
            response.get("version") != _PROTOCOL_VERSION
            or response.get("nonce") != nonce
            or response.get("op") != "launched"
            or not isinstance(response.get("pid"), int)
            or response["pid"] <= 0
        ):
            raise OwnerWorkerLauncherError("owner worker launcher rejected the launch")
        return LauncherProcessHandle(response["pid"], self)

    def _child_returncode(self, pid: int) -> int:
        nonce = uuid.uuid4().hex
        with self._lock:
            if self._process.poll() is not None:
                raise OwnerWorkerLauncherError("owner worker launcher is unavailable")
            _send_packet(
                self._channel,
                {
                    "version": _PROTOCOL_VERSION,
                    "op": "status",
                    "nonce": nonce,
                    "pid": pid,
                },
            )
            response, received = _recv_packet(self._channel)
        _close_fds(received)
        if (
            response.get("version") != _PROTOCOL_VERSION
            or response.get("op") != "status"
            or response.get("nonce") != nonce
            or response.get("pid") != pid
            or response.get("known") is not True
            or not isinstance(response.get("returncode"), int)
        ):
            raise OwnerWorkerLauncherError("owner worker launcher child status is invalid")
        return response["returncode"]

    def close(self) -> None:
        channel = getattr(self, "_channel", None)
        process = getattr(self, "_process", None)
        if channel is not None:
            try:
                nonce = uuid.uuid4().hex
                _send_packet(
                    channel,
                    {"version": _PROTOCOL_VERSION, "op": "shutdown", "nonce": nonce},
                )
                response, fds = _recv_packet(channel)
                _close_fds(fds)
                if response.get("nonce") != nonce:
                    raise OwnerWorkerLauncherError("owner worker launcher shutdown failed")
            except (OSError, EOFError, OwnerWorkerLauncherError, socket.timeout):
                pass
            channel.close()
            self._channel = None
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)


def main() -> None:
    if len(sys.argv) != 3 or sys.argv[1] != "--fd":
        raise SystemExit("owner worker launcher fd is required")
    try:
        fd = int(sys.argv[2])
    except ValueError as exc:
        raise SystemExit("owner worker launcher fd is invalid") from exc
    _launcher_loop(fd)


if __name__ == "__main__":  # pragma: no cover
    main()
