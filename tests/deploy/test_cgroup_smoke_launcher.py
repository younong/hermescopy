from __future__ import annotations

import importlib.util
import os
import pwd
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


LAUNCHER = Path(__file__).parents[2] / "deploy" / "run-cgroup-smoke.py"


def _module():
    spec = importlib.util.spec_from_file_location("cgroup_smoke_launcher", LAUNCHER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _hierarchy(tmp_path: Path, *, service="hermes-dashboard.service"):
    mount = tmp_path / "cgroup"
    service_root = mount / "system.slice" / service
    managed = service_root / "authenticated-owners"
    control = service_root / "control-plane"
    managed.mkdir(parents=True)
    control.mkdir()
    for path, value in (
        (service_root / "cgroup.procs", ""),
        (managed / "cgroup.procs", ""),
        (control / "cgroup.procs", "41\n"),
    ):
        path.write_text(value, encoding="ascii")
    proc_root = tmp_path / "proc"
    (proc_root / "41").mkdir(parents=True)
    expected = "/system.slice/hermes-dashboard.service/control-plane"
    (proc_root / "41" / "cgroup").write_text(f"0::{expected}\n", encoding="ascii")
    return mount, service_root, managed, control, proc_root, expected


def _patch_identity(monkeypatch, launcher, *, uid: int):
    monkeypatch.setattr(launcher.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        launcher.pwd,
        "getpwnam",
        lambda username: pwd.struct_passwd(
            (username, "x", uid, os.getgid(), "", "/opt/hermes/shared", "/bin/bash")
        ),
    )
    monkeypatch.setattr(launcher, "_service_main_pid", lambda _service: 41)


def test_validates_exact_dashboard_delegation_context(tmp_path, monkeypatch):
    launcher = _module()
    mount, service_root, managed, control, proc_root, expected = _hierarchy(tmp_path)
    uid = os.getuid()
    _patch_identity(monkeypatch, launcher, uid=uid)
    monkeypatch.setattr(launcher, "_mountpoint", lambda: mount.resolve())

    context = launcher.validate_launch_context(
        managed,
        service="hermes-dashboard.service",
        username="hermes",
        proc_root=proc_root,
    )

    assert context.service_root == service_root.resolve()
    assert context.control_plane == control.resolve()
    assert context.managed_root == managed.resolve()
    assert context.expected_control_path == expected
    assert context.service_main_pid == 41
    assert context.uid == uid


@pytest.mark.parametrize(
    "mutation,match",
    [
        (lambda service, managed, control, proc: (service / "cgroup.procs").write_text("99\n"), "service cgroup is populated"),
        (lambda service, managed, control, proc: (managed / "cgroup.procs").write_text("99\n"), "managed cgroup root is populated"),
        (lambda service, managed, control, proc: (proc / "41" / "cgroup").write_text("0::/other\n"), "outside control-plane"),
    ],
)
def test_rejects_unsafe_or_mismatched_topology(tmp_path, monkeypatch, mutation, match):
    launcher = _module()
    mount, service_root, managed, control, proc_root, _expected = _hierarchy(tmp_path)
    _patch_identity(monkeypatch, launcher, uid=os.getuid())
    monkeypatch.setattr(launcher, "_mountpoint", lambda: mount.resolve())
    mutation(service_root, managed, control, proc_root)

    with pytest.raises(launcher.CgroupSmokeLaunchError, match=match):
        launcher.validate_launch_context(
            managed,
            service="hermes-dashboard.service",
            username="hermes",
            proc_root=proc_root,
        )


def test_moves_only_the_gated_smoke_pid_and_verifies_membership(tmp_path, monkeypatch):
    launcher = _module()
    _mount, service_root, managed, control, proc_root, expected = _hierarchy(tmp_path)
    pid = 73
    (proc_root / str(pid)).mkdir(parents=True)
    child_membership = proc_root / str(pid) / "cgroup"
    child_membership.write_text("0::/user.slice/deploy.scope\n", encoding="ascii")
    worker = managed / "pool-v1" / "owner-active" / "worker-live"
    reader = managed / "pool-v1" / "owner-active" / "reader-live"
    worker.mkdir(parents=True)
    reader.mkdir()
    (worker / "cgroup.procs").write_text("901\n", encoding="ascii")
    (reader / "cgroup.procs").write_text("902\n", encoding="ascii")
    writes: list[tuple[Path, int]] = []

    def fake_write(path, moved_pid):
        writes.append((path, moved_pid))
        child_membership.write_text(f"0::{expected}\n", encoding="ascii")

    monkeypatch.setattr(launcher, "_write_pid", fake_write)
    context = launcher.LaunchContext(
        mount=service_root.parents[1],
        service_root=service_root,
        control_plane=control,
        managed_root=managed,
        expected_control_path=expected,
        service_main_pid=41,
        uid=os.getuid(),
        gid=os.getgid(),
        username="hermes",
    )

    launcher.move_smoke_process(context, pid, proc_root=proc_root)

    assert writes == [(control / "cgroup.procs", pid)]
    assert (proc_root / "41" / "cgroup").read_text() == f"0::{expected}\n"
    assert (worker / "cgroup.procs").read_text() == "901\n"
    assert (reader / "cgroup.procs").read_text() == "902\n"


def test_membership_mismatch_fails_closed(tmp_path, monkeypatch):
    launcher = _module()
    _mount, service_root, managed, control, proc_root, expected = _hierarchy(tmp_path)
    pid = 73
    (proc_root / str(pid)).mkdir(parents=True)
    (proc_root / str(pid) / "cgroup").write_text("0::/user.slice/deploy.scope\n")
    monkeypatch.setattr(launcher, "_write_pid", lambda _path, _pid: None)
    context = launcher.LaunchContext(
        mount=service_root.parents[1],
        service_root=service_root,
        control_plane=control,
        managed_root=managed,
        expected_control_path=expected,
        service_main_pid=41,
        uid=os.getuid(),
        gid=os.getgid(),
        username="hermes",
    )

    with pytest.raises(launcher.CgroupSmokeLaunchError, match="membership"):
        launcher.move_smoke_process(context, pid, proc_root=proc_root)


def test_child_drops_groups_gid_and_uid_before_exec(monkeypatch):
    launcher = _module()
    calls: list[tuple[object, ...]] = []
    context = SimpleNamespace(username="hermes", uid=123, gid=456)
    monkeypatch.setenv("HOME", "/tmp")
    monkeypatch.setattr(launcher.os, "chdir", lambda path: calls.append(("chdir", path)))
    monkeypatch.setattr(launcher.os, "initgroups", lambda *args: calls.append(("groups", *args)))
    monkeypatch.setattr(launcher.os, "setgid", lambda gid: calls.append(("gid", gid)))
    monkeypatch.setattr(launcher.os, "setuid", lambda uid: calls.append(("uid", uid)))
    monkeypatch.setattr(launcher.os, "umask", lambda mode: calls.append(("umask", mode)))
    monkeypatch.setattr(launcher.os, "geteuid", lambda: 123)
    monkeypatch.setattr(launcher.os, "getegid", lambda: 456)

    def fake_exec(path, command, environment):
        calls.append(("exec", path, tuple(command), environment))
        raise RuntimeError("exec")

    monkeypatch.setattr(launcher.os, "execvpe", fake_exec)
    with pytest.raises(RuntimeError, match="exec"):
        launcher._drop_privileges_and_exec(context, ["/bin/true"])

    assert calls[:5] == [
        ("chdir", Path("/tmp")),
        ("groups", "hermes", 456),
        ("gid", 456),
        ("uid", 123),
        ("umask", 0o077),
    ]
    assert calls[5][:3] == ("exec", "/bin/true", ("/bin/true",))
