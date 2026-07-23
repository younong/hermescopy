from __future__ import annotations

import os
import resource

import pytest

from hermes_cli.owner_worker import tool_executor_launcher


def test_launcher_waits_applies_nofile_and_execs(monkeypatch):
    read_fd, write_fd = os.pipe()
    observed = []
    monkeypatch.setattr(resource, "setrlimit", lambda kind, limits: observed.append((kind, limits)))
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
        ["tool-executor-launcher", "--start-fd", str(read_fd), "--nofile", "37", "--", "/bin/tool", "arg"],
    )
    os.write(write_fd, b"1")
    os.close(write_fd)

    with pytest.raises(RuntimeError) as exc_info:
        tool_executor_launcher.main()

    assert exc_info.value.args[0][:2] == ("/bin/tool", ["/bin/tool", "arg"])
    assert exc_info.value.args[0][2] == "trusted"
    assert observed == [(resource.RLIMIT_NOFILE, (37, 37))]


def test_launcher_rejects_closed_start_gate_before_rlimit_or_exec(monkeypatch):
    read_fd, write_fd = os.pipe()
    os.close(write_fd)
    monkeypatch.setattr("sys.argv", [
        "tool-executor-launcher", "--start-fd", str(read_fd), "--nofile", "37", "--", "/bin/tool",
    ])
    monkeypatch.setattr(resource, "setrlimit", lambda *_args: pytest.fail("rlimit applied"))
    monkeypatch.setattr(os, "execvpe", lambda *_args: pytest.fail("exec called"))

    with pytest.raises(SystemExit, match="not admitted"):
        tool_executor_launcher.main()
