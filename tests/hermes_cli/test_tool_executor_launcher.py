from __future__ import annotations

import os

import pytest

from hermes_cli.owner_worker import tool_executor_launcher


def test_launcher_waits_closes_gate_and_execs_without_nofile(monkeypatch):
    read_fd, write_fd = os.pipe()
    monkeypatch.setattr(
        os,
        "execvpe",
        lambda executable, argv, environment: (_ for _ in ()).throw(
            RuntimeError((executable, argv, environment.get("SENTINEL")))
        ),
    )
    monkeypatch.setenv("SENTINEL", "trusted")
    monkeypatch.setattr(
        "sys.argv",
        ["tool-executor-launcher", "--start-fd", str(read_fd), "--", "/bin/tool", "arg"],
    )
    os.write(write_fd, b"1")
    os.close(write_fd)

    with pytest.raises(RuntimeError) as exc_info:
        tool_executor_launcher.main()

    assert exc_info.value.args[0][:2] == ("/bin/tool", ["/bin/tool", "arg"])
    assert exc_info.value.args[0][2] == "trusted"
    with pytest.raises(OSError):
        os.fstat(read_fd)


def test_launcher_rejects_closed_start_gate_before_exec(monkeypatch):
    read_fd, write_fd = os.pipe()
    os.close(write_fd)
    monkeypatch.setattr("sys.argv", [
        "tool-executor-launcher", "--start-fd", str(read_fd), "--", "/bin/tool",
    ])
    monkeypatch.setattr(os, "execvpe", lambda *_args: pytest.fail("exec called"))

    with pytest.raises(SystemExit, match="not admitted"):
        tool_executor_launcher.main()


def test_launcher_does_not_accept_obsolete_nofile_argument(monkeypatch):
    read_fd, write_fd = os.pipe()
    try:
        monkeypatch.setattr("sys.argv", [
            "tool-executor-launcher", "--start-fd", str(read_fd),
            "--nofile", "37", "--", "/bin/tool",
        ])
        monkeypatch.setattr(os, "execvpe", lambda *_args: pytest.fail("exec called"))

        with pytest.raises(SystemExit):
            tool_executor_launcher.main()
    finally:
        os.close(read_fd)
        os.close(write_fd)
