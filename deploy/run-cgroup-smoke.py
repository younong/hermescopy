#!/usr/bin/env python3
"""Launch one deployment smoke inside the Dashboard's delegated cgroup."""
from __future__ import annotations

import argparse
import os
import pwd
import re
import signal
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


_SERVICE_RE = re.compile(r"[A-Za-z0-9_.@-]+\.service\Z")


class CgroupSmokeLaunchError(RuntimeError):
    """The smoke cannot be launched through the delegated service cgroup."""


@dataclass(frozen=True)
class LaunchContext:
    mount: Path
    service_root: Path
    control_plane: Path
    managed_root: Path
    expected_control_path: str
    service_main_pid: int
    uid: int
    gid: int
    username: str


def _read_unified_path(path: Path) -> str:
    try:
        lines = path.read_text(encoding="ascii").splitlines()
    except OSError as exc:
        raise CgroupSmokeLaunchError("process cgroup is unavailable") from exc
    matches = [line.split(":", 2)[2] for line in lines if line.startswith("0::")]
    if len(matches) != 1 or not matches[0].startswith("/"):
        raise CgroupSmokeLaunchError("process cgroup is unavailable")
    return matches[0]


def _mountpoint(path: Path = Path("/proc/self/mountinfo")) -> Path:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise CgroupSmokeLaunchError("cgroup v2 mount is unavailable") from exc
    matches: list[Path] = []
    for line in lines:
        fields = line.split()
        try:
            separator = fields.index("-")
        except ValueError:
            continue
        if (
            separator + 1 < len(fields)
            and fields[separator + 1] == "cgroup2"
            and len(fields) >= 5
        ):
            matches.append(Path(fields[4].replace("\\040", " ")))
    if len(matches) != 1:
        raise CgroupSmokeLaunchError("exactly one cgroup v2 mount is required")
    return matches[0].resolve()


def _service_main_pid(service: str) -> int:
    try:
        completed = subprocess.run(
            ["systemctl", "show", service, "--property=MainPID", "--value"],
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
            env={"PATH": "/usr/bin:/bin"},
        )
        pid = int(completed.stdout.strip()) if completed.returncode == 0 else 0
    except (OSError, ValueError, subprocess.SubprocessError):
        pid = 0
    if pid <= 1:
        raise CgroupSmokeLaunchError("Dashboard service has no active main process")
    return pid


def _require_delegated_owner(path: Path, *, uid: int) -> None:
    try:
        metadata = path.stat()
    except OSError as exc:
        raise CgroupSmokeLaunchError("delegated cgroup control is unavailable") from exc
    if metadata.st_uid != uid or not metadata.st_mode & stat.S_IWUSR:
        raise CgroupSmokeLaunchError("delegated cgroup control is not owned by the service user")


def validate_launch_context(
    managed_root: Path,
    *,
    service: str,
    username: str,
    proc_root: Path = Path("/proc"),
) -> LaunchContext:
    if os.geteuid() != 0:
        raise CgroupSmokeLaunchError("cgroup smoke launcher requires root")
    if not _SERVICE_RE.fullmatch(service):
        raise CgroupSmokeLaunchError("service name is invalid")
    try:
        account = pwd.getpwnam(username)
    except KeyError as exc:
        raise CgroupSmokeLaunchError("service user is unavailable") from exc
    try:
        root = managed_root.resolve(strict=True)
    except OSError as exc:
        raise CgroupSmokeLaunchError("managed cgroup root is unavailable") from exc
    if root.name != "authenticated-owners" or root.parent.name != service:
        raise CgroupSmokeLaunchError("managed cgroup root does not match the service")
    mount = _mountpoint()
    if mount != root and mount not in root.parents:
        raise CgroupSmokeLaunchError("managed root is outside cgroup v2")
    service_root = root.parent
    control_plane = service_root / "control-plane"
    if not control_plane.is_dir():
        raise CgroupSmokeLaunchError("Dashboard control-plane cgroup is unavailable")
    expected_control_path = "/" + control_plane.relative_to(mount).as_posix()
    main_pid = _service_main_pid(service)
    if _read_unified_path(proc_root / str(main_pid) / "cgroup") != expected_control_path:
        raise CgroupSmokeLaunchError("Dashboard main process is outside control-plane")
    try:
        if (service_root / "cgroup.procs").read_text(encoding="ascii").strip():
            raise CgroupSmokeLaunchError("delegated service cgroup is populated")
        if (root / "cgroup.procs").read_text(encoding="ascii").strip():
            raise CgroupSmokeLaunchError("managed cgroup root is populated")
    except OSError as exc:
        raise CgroupSmokeLaunchError("delegated cgroup topology cannot be verified") from exc
    _require_delegated_owner(service_root / "cgroup.procs", uid=account.pw_uid)
    _require_delegated_owner(control_plane / "cgroup.procs", uid=account.pw_uid)
    _require_delegated_owner(root / "cgroup.procs", uid=account.pw_uid)
    return LaunchContext(
        mount=mount,
        service_root=service_root,
        control_plane=control_plane,
        managed_root=root,
        expected_control_path=expected_control_path,
        service_main_pid=main_pid,
        uid=account.pw_uid,
        gid=account.pw_gid,
        username=account.pw_name,
    )


def _write_pid(path: Path, pid: int) -> None:
    payload = str(pid).encode("ascii")
    flags = os.O_WRONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
        try:
            written = os.write(descriptor, payload)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise CgroupSmokeLaunchError("smoke process cannot enter control-plane") from exc
    if written != len(payload):
        raise CgroupSmokeLaunchError("smoke process cgroup move was incomplete")


def move_smoke_process(
    context: LaunchContext,
    pid: int,
    *,
    proc_root: Path = Path("/proc"),
) -> None:
    if pid <= 1:
        raise CgroupSmokeLaunchError("smoke process id is invalid")
    _write_pid(context.control_plane / "cgroup.procs", pid)
    actual = _read_unified_path(proc_root / str(pid) / "cgroup")
    if actual != context.expected_control_path:
        raise CgroupSmokeLaunchError("smoke process control-plane membership was not verified")
    dashboard = _read_unified_path(
        proc_root / str(context.service_main_pid) / "cgroup"
    )
    if dashboard != context.expected_control_path:
        raise CgroupSmokeLaunchError("Dashboard main process moved during smoke handoff")


def _drop_privileges_and_exec(context: LaunchContext, command: list[str]) -> None:
    home = Path(os.environ.get("HOME", ""))
    if not home.is_absolute() or not home.is_dir():
        raise CgroupSmokeLaunchError("smoke HOME is invalid")
    try:
        os.chdir(home)
    except OSError as exc:
        raise CgroupSmokeLaunchError("smoke HOME cannot be entered") from exc
    try:
        os.initgroups(context.username, context.gid)
    except OSError as exc:
        raise CgroupSmokeLaunchError("service-user supplementary groups could not be set") from exc
    try:
        os.setgid(context.gid)
    except OSError as exc:
        raise CgroupSmokeLaunchError("service-user group could not be set") from exc
    try:
        os.setuid(context.uid)
    except OSError as exc:
        raise CgroupSmokeLaunchError("service-user identity could not be set") from exc
    os.umask(0o077)
    if os.geteuid() != context.uid or os.getegid() != context.gid:
        raise CgroupSmokeLaunchError("service-user privilege drop was not verified")
    try:
        os.execvpe(command[0], command, os.environ)
    except OSError as exc:
        raise CgroupSmokeLaunchError("smoke command could not be executed") from exc


def _wait_status(pid: int) -> int:
    forwarded = (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)
    previous: dict[signal.Signals, object] = {}

    def forward(signum, _frame):
        try:
            os.kill(pid, signum)
        except ProcessLookupError:
            pass

    try:
        for signum in forwarded:
            previous[signum] = signal.signal(signum, forward)
        while True:
            try:
                _, status = os.waitpid(pid, 0)
                break
            except InterruptedError:
                continue
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    return os.waitstatus_to_exitcode(status)


def launch(context: LaunchContext, command: list[str]) -> int:
    if not command or not Path(command[0]).is_absolute():
        raise CgroupSmokeLaunchError("smoke command must use an absolute executable")
    read_gate, write_gate = os.pipe2(os.O_CLOEXEC)
    pid = os.fork()
    if pid == 0:
        try:
            os.close(write_gate)
            permitted = os.read(read_gate, 1)
            os.close(read_gate)
            if permitted != b"1":
                os._exit(126)
            _drop_privileges_and_exec(context, command)
        except BaseException as exc:
            detail = str(exc) if isinstance(exc, CgroupSmokeLaunchError) else type(exc).__name__
            message = f"cgroup smoke child failed: {detail}\n".encode("ascii")
            try:
                os.write(2, message)
            except OSError:
                pass
            os._exit(126)
    os.close(read_gate)
    try:
        move_smoke_process(context, pid)
        if os.write(write_gate, b"1") != 1:
            raise CgroupSmokeLaunchError("smoke launch gate could not be released")
    except BaseException:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        os.close(write_gate)
        os.waitpid(pid, 0)
        raise
    os.close(write_gate)
    return _wait_status(pid)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--managed-root", required=True, type=Path)
    parser.add_argument("--service", default="hermes-dashboard.service")
    parser.add_argument("--user", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = list(args.command)
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        parser.error("a smoke command is required")
    try:
        context = validate_launch_context(
            args.managed_root,
            service=args.service,
            username=args.user,
        )
        return launch(context, command)
    except CgroupSmokeLaunchError as exc:
        print(f"cgroup smoke launch failed: {exc}", file=sys.stderr)
        return 126


if __name__ == "__main__":
    raise SystemExit(main())
