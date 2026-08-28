from __future__ import annotations

import array
import errno
import os
import signal
import socket
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli.owner_worker.preloaded_launcher import (
    LauncherProcessHandle,
    OwnerWorkerLauncher,
    OwnerWorkerLauncherError,
    _MAX_FDS,
    _PROTOCOL_VERSION,
    _pidfd_open,
    _pidfd_send_signal,
    _recv_packet,
    _require_pidfd_support,
    _run_child,
    _send_packet,
    _validate_launch,
)


def _fd_is_open(fd: int) -> bool:
    try:
        os.fstat(fd)
    except OSError:
        return False
    return True


class _FakeSyscall:
    def __init__(self, results):
        self.results = iter(results)
        self.calls = []
        self.restype = None

    def __call__(self, *args):
        self.calls.append(args)
        return next(self.results)


class _FakeLibc:
    def __init__(self, results):
        self.syscall = _FakeSyscall(results)


def _launch_payload(**overrides):
    payload = {
        "version": _PROTOCOL_VERSION,
        "op": "launch",
        "nonce": "nonce-1",
        "argv": ["--owner-key", "ok1_test"],
        "env": {"HERMES_HOME": "/owner"},
        "fdNames": ["cwd", "stdout", "stderr", "start"],
    }
    payload.update(overrides)
    return payload


def test_pidfd_helpers_prefer_native_wrappers(monkeypatch):
    opened = []
    signaled = []
    monkeypatch.setattr(
        os,
        "pidfd_open",
        lambda pid, flags=0: opened.append((pid, flags)) or 17,
        raising=False,
    )
    monkeypatch.setattr(
        signal,
        "pidfd_send_signal",
        lambda pidfd, sig, siginfo=None, flags=0: signaled.append(
            (pidfd, sig, siginfo, flags)
        ),
        raising=False,
    )

    assert _pidfd_open(42) == 17
    _pidfd_send_signal(17, signal.SIGTERM)

    assert opened == [(42, 0)]
    assert signaled == [(17, signal.SIGTERM, None, 0)]


def test_pidfd_helpers_use_exact_x86_64_syscalls_without_native_wrappers(monkeypatch):
    import hermes_cli.owner_worker.preloaded_launcher as launcher_module

    libc = _FakeLibc([23, 0])
    monkeypatch.delattr(os, "pidfd_open", raising=False)
    monkeypatch.delattr(signal, "pidfd_send_signal", raising=False)
    monkeypatch.setattr(launcher_module.sys, "platform", "linux")
    monkeypatch.setattr(launcher_module.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(launcher_module.ctypes, "CDLL", lambda *_args, **_kwargs: libc)

    assert _pidfd_open(1234) == 23
    _pidfd_send_signal(23, signal.SIGKILL)

    open_call, signal_call = libc.syscall.calls
    assert open_call[0].value == 434
    assert open_call[1].value == 1234
    assert open_call[2].value == 0
    assert signal_call[0].value == 424
    assert signal_call[1].value == 23
    assert signal_call[2].value == signal.SIGKILL
    assert signal_call[3].value is None
    assert signal_call[4].value == 0


@pytest.mark.parametrize("error", [errno.EINVAL, errno.EPERM, errno.ESRCH])
def test_pidfd_syscall_preserves_errno(monkeypatch, error):
    import hermes_cli.owner_worker.preloaded_launcher as launcher_module

    libc = _FakeLibc([-1])
    monkeypatch.delattr(os, "pidfd_open", raising=False)
    monkeypatch.setattr(launcher_module.sys, "platform", "linux")
    monkeypatch.setattr(launcher_module.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(launcher_module.ctypes, "CDLL", lambda *_args, **_kwargs: libc)
    monkeypatch.setattr(launcher_module.ctypes, "get_errno", lambda: error)

    with pytest.raises(OSError) as captured:
        _pidfd_open(1234)
    assert captured.value.errno == error


def test_pidfd_support_probe_closes_descriptor(monkeypatch):
    import hermes_cli.owner_worker.preloaded_launcher as launcher_module

    signaled = []
    closed = []
    monkeypatch.setattr(launcher_module, "_pidfd_open", lambda _pid: 31)
    monkeypatch.setattr(
        launcher_module,
        "_pidfd_send_signal",
        lambda pidfd, sig: signaled.append((pidfd, sig)),
    )
    monkeypatch.setattr(launcher_module.os, "close", closed.append)

    _require_pidfd_support()

    assert signaled == [(31, 0)]
    assert closed == [31]


def test_pidfd_support_probe_closes_descriptor_and_fails_closed(monkeypatch):
    import hermes_cli.owner_worker.preloaded_launcher as launcher_module

    closed = []
    monkeypatch.setattr(launcher_module, "_pidfd_open", lambda _pid: 31)
    monkeypatch.setattr(
        launcher_module,
        "_pidfd_send_signal",
        lambda _pidfd, _sig: (_ for _ in ()).throw(OSError(errno.ENOSYS, "unsupported")),
    )
    monkeypatch.setattr(launcher_module.os, "close", closed.append)

    with pytest.raises(OwnerWorkerLauncherError, match="requires Linux pidfds"):
        _require_pidfd_support()
    assert closed == [31]


@pytest.mark.parametrize(
    "load_libc",
    [
        lambda: (_ for _ in ()).throw(OSError("unavailable")),
        lambda: SimpleNamespace(),
    ],
)
def test_pidfd_support_rejects_unavailable_libc_before_process_start(
    monkeypatch, load_libc
):
    import hermes_cli.owner_worker.preloaded_launcher as launcher_module

    started = []
    monkeypatch.delattr(os, "pidfd_open", raising=False)
    monkeypatch.setattr(launcher_module.sys, "platform", "linux")
    monkeypatch.setattr(launcher_module.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(
        launcher_module.ctypes,
        "CDLL",
        lambda *_args, **_kwargs: load_libc(),
    )

    with pytest.raises(OwnerWorkerLauncherError, match="cannot access Linux pidfds"):
        OwnerWorkerLauncher(process_factory=lambda *_args, **_kwargs: started.append(True))
    assert started == []


def test_pidfd_support_rejects_unknown_arch_before_process_start(monkeypatch):
    import hermes_cli.owner_worker.preloaded_launcher as launcher_module

    started = []
    monkeypatch.delattr(os, "pidfd_open", raising=False)
    monkeypatch.setattr(launcher_module.sys, "platform", "linux")
    monkeypatch.setattr(launcher_module.platform, "machine", lambda: "unknown")

    with pytest.raises(OwnerWorkerLauncherError, match="supported Linux pidfds"):
        OwnerWorkerLauncher(process_factory=lambda *_args, **_kwargs: started.append(True))
    assert started == []


def test_launcher_process_handle_signals_only_by_pidfd(monkeypatch):
    import hermes_cli.owner_worker.preloaded_launcher as launcher_module

    signaled = []
    closed = []
    monkeypatch.setattr(launcher_module, "_pidfd_open", lambda _pid: 41)
    monkeypatch.setattr(
        launcher_module,
        "_pidfd_send_signal",
        lambda pidfd, sig: signaled.append((pidfd, sig)),
    )
    monkeypatch.setattr(
        launcher_module.os,
        "kill",
        lambda *_args: (_ for _ in ()).throw(AssertionError("pid signaling used")),
    )
    monkeypatch.setattr(launcher_module.os, "close", closed.append)
    handle = LauncherProcessHandle(1234, SimpleNamespace())
    monkeypatch.setattr(handle, "poll", lambda: None)

    handle.terminate()
    handle.kill()
    handle.close()
    handle.close()

    assert signaled == [(41, signal.SIGTERM), (41, signal.SIGKILL)]
    assert closed == [41]


def test_launcher_process_handle_close_is_atomic_under_concurrency(monkeypatch):
    import hermes_cli.owner_worker.preloaded_launcher as launcher_module

    closed = []
    close_entered = threading.Event()
    release_close = threading.Event()
    monkeypatch.setattr(launcher_module, "_pidfd_open", lambda _pid: 41)

    def close(fd):
        closed.append(fd)
        close_entered.set()
        assert release_close.wait(timeout=2)

    monkeypatch.setattr(launcher_module.os, "close", close)
    handle = LauncherProcessHandle(1234, SimpleNamespace())
    threads = [threading.Thread(target=handle.close) for _ in range(2)]

    for thread in threads:
        thread.start()
    assert close_entered.wait(timeout=2)
    release_close.set()
    for thread in threads:
        thread.join(timeout=2)
        assert not thread.is_alive()

    assert closed == [41]
    assert handle._pidfd == -1


def test_launcher_spawn_pidfd_open_baseexception_closes_and_revokes_ownership(
    monkeypatch,
):
    import hermes_cli.owner_worker.preloaded_launcher as launcher_module

    class _PidfdOpenFailure(BaseException):
        pass

    class _Channel:
        def __init__(self):
            self.close_calls = 0

        def close(self):
            self.close_calls += 1

    class _Process:
        def __init__(self):
            self.returncode = None
            self.wait_calls = []

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            self.wait_calls.append(timeout)
            self.returncode = 0
            return self.returncode

        def terminate(self):
            raise AssertionError("graceful launcher shutdown should not terminate")

        def kill(self):
            raise AssertionError("graceful launcher shutdown should not kill")

    channel = _Channel()
    process = _Process()
    launcher = object.__new__(OwnerWorkerLauncher)
    launcher._channel = channel
    launcher._process = process
    launcher._lock = threading.Lock()
    launcher._closed = False
    monkeypatch.setattr(
        launcher_module.uuid,
        "uuid4",
        lambda: SimpleNamespace(hex="fixed-nonce"),
    )
    sent_operations = []

    def send_packet(_channel, payload, *_args):
        sent_operations.append(payload["op"])

    responses = iter(
        (
            {
                "version": _PROTOCOL_VERSION,
                "op": "launched",
                "nonce": "fixed-nonce",
                "pid": 1234,
            },
            {
                "version": _PROTOCOL_VERSION,
                "op": "shutdown",
                "nonce": "fixed-nonce",
            },
        )
    )
    monkeypatch.setattr(launcher_module, "_send_packet", send_packet)
    monkeypatch.setattr(
        launcher_module,
        "_recv_packet",
        lambda _channel: (next(responses), []),
    )
    failure = _PidfdOpenFailure("pidfd open failed")
    monkeypatch.setattr(
        launcher_module,
        "_pidfd_open",
        lambda _pid: (_ for _ in ()).throw(failure),
    )
    close_events = []
    original_close = launcher.close

    def close():
        close_events.append("entered")
        original_close()
        close_events.append("returned")

    monkeypatch.setattr(launcher, "close", close)

    with pytest.raises(_PidfdOpenFailure) as captured:
        launcher.spawn(
            ["--owner-key", "ok1_test"],
            env={},
            cwd_fd=1,
            stdout_fd=1,
            stderr_fd=2,
            start_fd=1,
            relay_fds={},
        )

    assert captured.value is failure
    assert close_events == ["entered", "returned"]
    assert sent_operations == ["launch", "shutdown"]
    assert process.wait_calls == [launcher_module._LAUNCHER_SHUTDOWN_TIMEOUT]
    assert channel.close_calls == 1
    assert launcher._closed is True
    assert launcher._channel is None
    assert launcher._process is None
    with pytest.raises(OwnerWorkerLauncherError, match="unavailable"):
        launcher.spawn(
            ["--owner-key", "ok1_test"],
            env={},
            cwd_fd=1,
            stdout_fd=1,
            stderr_fd=2,
            start_fd=1,
            relay_fds={},
        )


@pytest.mark.parametrize(
    "status_error",
    [
        socket.timeout("status timed out"),
        EOFError("status channel closed"),
        OSError(errno.EIO, "status failed"),
        OwnerWorkerLauncherError("status failed"),
    ],
    ids=["socket-timeout", "eof", "os-error", "launcher-error"],
)
def test_launcher_process_handle_readable_pidfd_falls_back_on_status_error(
    monkeypatch,
    status_error,
):
    import hermes_cli.owner_worker.preloaded_launcher as launcher_module

    class _Selector:
        def __init__(self):
            self.registered = []
            self.select_calls = []
            self.closed = False

        def register(self, fd, event):
            self.registered.append((fd, event))

        def select(self, timeout=None):
            self.select_calls.append(timeout)
            return [(41, launcher_module.selectors.EVENT_READ)]

        def close(self):
            self.closed = True

    selector = _Selector()
    status_calls = []

    def child_returncode(pid):
        status_calls.append(pid)
        raise status_error

    launcher = SimpleNamespace(_child_returncode=child_returncode)
    monkeypatch.setattr(launcher_module, "_pidfd_open", lambda _pid: 41)
    monkeypatch.setattr(
        launcher_module.selectors,
        "DefaultSelector",
        lambda: selector,
    )
    handle = LauncherProcessHandle(1234, launcher)

    assert handle.poll() == 1
    assert handle.returncode == 1
    assert status_calls == [1234]
    assert selector.registered == [(41, launcher_module.selectors.EVENT_READ)]
    assert selector.select_calls == [0]
    assert selector.closed is True


def test_launcher_spawn_ambiguous_rpc_failure_poisons_channel(monkeypatch):
    import hermes_cli.owner_worker.preloaded_launcher as launcher_module

    class _Channel:
        closed = False

        def close(self):
            self.closed = True

    class _Process:
        def __init__(self):
            self.returncode = None
            self.wait_calls = []

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            self.wait_calls.append(timeout)
            self.returncode = 0
            return self.returncode

        def terminate(self):
            raise AssertionError("EOF cleanup should reap the launcher")

        def kill(self):
            raise AssertionError("EOF cleanup should reap the launcher")

    process = _Process()
    launcher = object.__new__(OwnerWorkerLauncher)
    launcher._lock = threading.Lock()
    launcher._closed = False
    launcher._process = process
    launcher._channel = _Channel()
    monkeypatch.setattr(launcher_module, "_send_packet", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        launcher_module,
        "_recv_packet",
        lambda *_args: (_ for _ in ()).throw(socket.timeout("ambiguous response")),
    )

    with pytest.raises(socket.timeout, match="ambiguous response"):
        launcher.spawn(
            ["--owner-key", "ok1_test"],
            env={},
            cwd_fd=1,
            stdout_fd=1,
            stderr_fd=2,
            start_fd=1,
            relay_fds={},
        )

    assert launcher._closed is True
    assert launcher._channel is None
    assert launcher._process is None
    assert process.wait_calls == [launcher_module._LAUNCHER_SHUTDOWN_TIMEOUT]
    with pytest.raises(OwnerWorkerLauncherError, match="unavailable"):
        launcher.spawn(
            ["--owner-key", "ok1_test"],
            env={},
            cwd_fd=1,
            stdout_fd=1,
            stderr_fd=2,
            start_fd=1,
            relay_fds={},
        )


def test_launcher_status_invalid_response_poisons_channel(monkeypatch):
    import hermes_cli.owner_worker.preloaded_launcher as launcher_module

    class _Channel:
        closed = False

        def close(self):
            self.closed = True

    class _Process:
        def __init__(self):
            self.returncode = None
            self.wait_calls = []

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            self.wait_calls.append(timeout)
            self.returncode = 0
            return self.returncode

        def terminate(self):
            raise AssertionError("EOF cleanup should reap the launcher")

        def kill(self):
            raise AssertionError("EOF cleanup should reap the launcher")

    process = _Process()
    launcher = object.__new__(OwnerWorkerLauncher)
    launcher._lock = threading.Lock()
    launcher._closed = False
    launcher._process = process
    launcher._channel = _Channel()
    monkeypatch.setattr(launcher_module, "_send_packet", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        launcher_module,
        "_recv_packet",
        lambda *_args: (
            {
                "version": _PROTOCOL_VERSION,
                "op": "status",
                "nonce": "stale-response",
                "pid": 1234,
                "known": True,
                "returncode": 0,
            },
            [],
        ),
    )

    with pytest.raises(OwnerWorkerLauncherError, match="status is invalid"):
        launcher._child_returncode(1234)

    assert launcher._closed is True
    assert launcher._channel is None
    assert launcher._process is None
    assert process.wait_calls == [launcher_module._LAUNCHER_SHUTDOWN_TIMEOUT]


def test_launcher_close_waits_for_graceful_child_cleanup_before_escalating(monkeypatch):
    import hermes_cli.owner_worker.preloaded_launcher as launcher_module

    class _Channel:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    class _Process:
        def __init__(self):
            self.returncode = None
            self.wait_calls = []
            self.terminate_calls = 0
            self.kill_calls = 0

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            self.wait_calls.append(timeout)
            self.returncode = 0
            return 0

        def terminate(self):
            self.terminate_calls += 1

        def kill(self):
            self.kill_calls += 1

    channel = _Channel()
    process = _Process()
    launcher = object.__new__(OwnerWorkerLauncher)
    launcher._channel = channel
    launcher._process = process
    launcher._lock = threading.Lock()
    launcher._closed = False
    monkeypatch.setattr(launcher_module.uuid, "uuid4", lambda: SimpleNamespace(hex="shutdown-nonce"))
    monkeypatch.setattr(launcher_module, "_send_packet", lambda *_args: None)
    monkeypatch.setattr(
        launcher_module,
        "_recv_packet",
        lambda _channel: (
            {
                "version": _PROTOCOL_VERSION,
                "op": "shutdown",
                "nonce": "shutdown-nonce",
            },
            [],
        ),
    )

    launcher.close()

    assert channel.closed is True
    assert process.wait_calls == [launcher_module._LAUNCHER_SHUTDOWN_TIMEOUT]
    assert process.terminate_calls == 0
    assert process.kill_calls == 0
    assert launcher._process is None


def test_launcher_close_escalates_after_graceful_cleanup_timeout(monkeypatch):
    import hermes_cli.owner_worker.preloaded_launcher as launcher_module

    class _Channel:
        def close(self):
            pass

    class _Process:
        def __init__(self):
            self.returncode = None
            self.wait_calls = []
            self.terminate_calls = 0
            self.kill_calls = 0

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            self.wait_calls.append(timeout)
            if self.terminate_calls == 0:
                raise subprocess.TimeoutExpired("launcher", timeout)
            self.returncode = -signal.SIGTERM
            return self.returncode

        def terminate(self):
            self.terminate_calls += 1

        def kill(self):
            self.kill_calls += 1

    process = _Process()
    launcher = object.__new__(OwnerWorkerLauncher)
    launcher._channel = _Channel()
    launcher._process = process
    launcher._lock = threading.Lock()
    launcher._closed = False
    monkeypatch.setattr(launcher_module.uuid, "uuid4", lambda: SimpleNamespace(hex="shutdown-nonce"))
    monkeypatch.setattr(launcher_module, "_send_packet", lambda *_args: None)
    monkeypatch.setattr(
        launcher_module,
        "_recv_packet",
        lambda _channel: (
            {
                "version": _PROTOCOL_VERSION,
                "op": "shutdown",
                "nonce": "shutdown-nonce",
            },
            [],
        ),
    )

    launcher.close()

    assert process.wait_calls == [launcher_module._LAUNCHER_SHUTDOWN_TIMEOUT, 2]
    assert process.terminate_calls == 1
    assert process.kill_calls == 0
    assert launcher._process is None


def test_launcher_close_retries_reap_after_sigkill_wait_timeout(monkeypatch):
    import hermes_cli.owner_worker.preloaded_launcher as launcher_module

    class _Channel:
        def close(self):
            pass

    class _Process:
        def __init__(self):
            self.returncode = None
            self.wait_calls = []
            self.terminate_calls = 0
            self.kill_calls = 0

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            self.wait_calls.append(timeout)
            if len(self.wait_calls) < 4:
                raise subprocess.TimeoutExpired("launcher", timeout)
            self.returncode = -9
            return self.returncode

        def terminate(self):
            self.terminate_calls += 1

        def kill(self):
            self.kill_calls += 1

    process = _Process()
    launcher = object.__new__(OwnerWorkerLauncher)
    launcher._channel = _Channel()
    launcher._process = process
    launcher._lock = threading.Lock()
    launcher._closed = False
    monkeypatch.setattr(
        launcher_module.uuid,
        "uuid4",
        lambda: SimpleNamespace(hex="shutdown-nonce"),
    )
    monkeypatch.setattr(launcher_module, "_send_packet", lambda *_args: None)
    monkeypatch.setattr(
        launcher_module,
        "_recv_packet",
        lambda _channel: (
            {
                "version": _PROTOCOL_VERSION,
                "op": "shutdown",
                "nonce": "shutdown-nonce",
            },
            [],
        ),
    )

    with pytest.raises(subprocess.TimeoutExpired):
        launcher.close()

    assert launcher._closed is True
    assert launcher._process is process

    launcher.close()

    assert process.wait_calls == [launcher_module._LAUNCHER_SHUTDOWN_TIMEOUT, 2, 2, 2]
    assert process.terminate_calls == 2
    assert process.kill_calls == 1
    assert launcher._process is None


def test_launcher_close_is_idempotent(monkeypatch):
    import hermes_cli.owner_worker.preloaded_launcher as launcher_module

    class _Channel:
        close_calls = 0

        def close(self):
            self.close_calls += 1

    process = SimpleNamespace(
        poll=lambda: 0,
    )
    channel = _Channel()
    launcher = object.__new__(OwnerWorkerLauncher)
    launcher._channel = channel
    launcher._process = process
    launcher._lock = threading.Lock()
    launcher._closed = False
    monkeypatch.setattr(launcher_module, "_send_packet", lambda *_args: None)
    monkeypatch.setattr(
        launcher_module,
        "_recv_packet",
        lambda _channel: ({"version": _PROTOCOL_VERSION, "op": "shutdown", "nonce": "ignored"}, []),
    )

    launcher.close()
    launcher.close()

    assert channel.close_calls == 1
    assert launcher._closed is True


def test_launcher_spawn_rejects_closed_launcher():
    launcher = object.__new__(OwnerWorkerLauncher)
    launcher._lock = threading.Lock()
    launcher._closed = True
    launcher._process = None
    launcher._channel = None

    with pytest.raises(OwnerWorkerLauncherError, match="unavailable"):
        launcher.spawn(
            ["--owner-key", "ok1_test"],
            env={},
            cwd_fd=1,
            stdout_fd=1,
            stderr_fd=2,
            start_fd=1,
            relay_fds={},
        )


def test_launcher_process_handle_confirms_exit_after_launcher_close(monkeypatch):
    import hermes_cli.owner_worker.preloaded_launcher as launcher_module

    class _Selector:
        def __init__(self):
            self.select_results = iter(([], [(41, 1)]))
            self.registered = []
            self.closed = False

        def register(self, fd, event):
            self.registered.append((fd, event))

        def select(self, timeout=None):
            return next(self.select_results)

        def close(self):
            self.closed = True

    selector = _Selector()
    signaled = []
    monkeypatch.setattr(launcher_module.signal, "SIGKILL", 9, raising=False)
    monkeypatch.setattr(launcher_module, "_pidfd_open", lambda _pid: 41)
    monkeypatch.setattr(
        launcher_module,
        "_pidfd_send_signal",
        lambda pidfd, sig: signaled.append((pidfd, sig)),
    )
    monkeypatch.setattr(
        launcher_module.selectors,
        "DefaultSelector",
        lambda: selector,
    )
    handle = LauncherProcessHandle(1234, SimpleNamespace())

    assert handle.confirm_exit_after_launcher_close(timeout=0.25) is True
    assert selector.registered == [(41, launcher_module.selectors.EVENT_READ)]
    assert signaled == [(41, signal.SIGKILL)]
    assert selector.closed is True


def test_validate_launch_requires_exact_version_and_core_descriptor_order():
    argv, env, names = _validate_launch(_launch_payload(), 4)
    assert argv == ["--owner-key", "ok1_test"]
    assert env == {"HERMES_HOME": "/owner"}
    assert names == ["cwd", "stdout", "stderr", "start"]

    with pytest.raises(OwnerWorkerLauncherError, match="version"):
        _validate_launch(_launch_payload(version=_PROTOCOL_VERSION + 1), 4)
    with pytest.raises(OwnerWorkerLauncherError, match="request is invalid"):
        _validate_launch(
            _launch_payload(fdNames=["stdout", "cwd", "stderr", "start"]),
            4,
        )
    with pytest.raises(OwnerWorkerLauncherError, match="request is invalid"):
        _validate_launch(_launch_payload(), 3)


def test_validate_launch_accepts_supervisor_relay_descriptor_set():
    # The supervisor passes one fd per relay channel
    # (inference/media/resource); the allowlist must accept them all.
    names = ["cwd", "stdout", "stderr", "start", "inference", "media", "resource"]
    _argv, _env, validated = _validate_launch(
        _launch_payload(fdNames=names),
        len(names),
    )
    assert validated == names


def test_recv_packet_rejects_invalid_json_and_excess_descriptors(tmp_path):
    sender, receiver = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        sender.send(b"not-json")
        with pytest.raises(OwnerWorkerLauncherError, match="packet is invalid"):
            _recv_packet(receiver)

        descriptors = tuple(os.open(tmp_path, os.O_RDONLY) for _ in range(_MAX_FDS + 1))
        try:
            sender.sendmsg(
                [b"{}"],
                [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", descriptors))],
            )
            with pytest.raises(OwnerWorkerLauncherError, match="packet is invalid"):
                _recv_packet(receiver)
        finally:
            for fd in descriptors:
                os.close(fd)
    finally:
        sender.close()
        receiver.close()


def test_run_child_binds_cwd_logs_environment_and_start_gate(tmp_path, monkeypatch):
    control, child_channel = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
    cwd_fd = os.open(tmp_path, os.O_RDONLY)
    stdout_path = tmp_path / "stdout.log"
    stderr_path = tmp_path / "stderr.log"
    stdout_fd = os.open(stdout_path, os.O_CREAT | os.O_WRONLY, 0o600)
    stderr_fd = os.open(stderr_path, os.O_CREAT | os.O_WRONLY, 0o600)
    start_read, start_write = os.pipe()
    os.write(start_write, b"1")
    os.close(start_write)
    result_read, result_write = os.pipe()
    monkeypatch.setenv("AMBIENT_SECRET", "must-not-leak")
    keep_result_fd = os.dup(result_write)
    os.set_inheritable(keep_result_fd, True)

    pid = os.fork()
    if pid == 0:
        os.close(result_read)
        import hermes_cli.owner_worker.entrypoint as entrypoint
        import hermes_cli.owner_worker.preloaded_launcher as launcher_module

        real_listdir = os.listdir
        launcher_module.os.listdir = lambda path: (
            [str(fd) for fd in range(3, 256) if _fd_is_open(fd)]
            if path == "/proc/self/fd"
            else real_listdir(path)
        )

        def fake_run_worker(argv):
            payload = "|".join(
                (
                    str(Path.cwd()),
                    os.environ.get("ALLOWED", ""),
                    os.environ.get("AMBIENT_SECRET", "missing"),
                    repr(argv),
                )
            ).encode()
            os.write(keep_result_fd, payload)
            os.write(1, b"stdout-bound\n")
            os.write(2, b"stderr-bound\n")

        entrypoint.run_worker = fake_run_worker
        _run_child(
            child_channel.fileno(),
            ["--owner-key", "ok1_test"],
            {"ALLOWED": "yes"},
            ["cwd", "stdout", "stderr", "start", "inference"],
            [cwd_fd, stdout_fd, stderr_fd, start_read, keep_result_fd],
        )
        raise AssertionError("child did not exit")

    child_channel.close()
    os.close(cwd_fd)
    os.close(stdout_fd)
    os.close(stderr_fd)
    os.close(start_read)
    os.close(result_write)
    os.close(keep_result_fd)
    try:
        result = os.read(result_read, 4096).decode()
        waited, status = os.waitpid(pid, 0)
    finally:
        os.close(result_read)
        control.close()

    assert waited == pid
    assert os.waitstatus_to_exitcode(status) == 0, stderr_path.read_text()
    assert result == f"{tmp_path}|yes|missing|['--owner-key', 'ok1_test']"
    assert stdout_path.read_text() == "stdout-bound\n"
    assert stderr_path.read_text() == "stderr-bound\n"


def test_packet_round_trip_preserves_payload_and_descriptors(tmp_path):
    sender, receiver = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
    fd = os.open(tmp_path, os.O_RDONLY)
    try:
        payload = {"version": _PROTOCOL_VERSION, "op": "ready"}
        _send_packet(sender, payload, (fd,))
        received, descriptors = _recv_packet(receiver)
        assert received == payload
        assert len(descriptors) == 1
        assert os.fstat(descriptors[0]).st_ino == os.fstat(fd).st_ino
        os.close(descriptors[0])
    finally:
        os.close(fd)
        sender.close()
        receiver.close()
