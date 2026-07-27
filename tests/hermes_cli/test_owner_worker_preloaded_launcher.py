from __future__ import annotations

import array
import os
import socket
from pathlib import Path

import pytest

from hermes_cli.owner_worker.preloaded_launcher import (
    OwnerWorkerLauncherError,
    _MAX_FDS,
    _PROTOCOL_VERSION,
    _recv_packet,
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
