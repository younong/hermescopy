"""Post-spawn Linux attestation for bare-metal Bubblewrap executors."""
from __future__ import annotations

import os
import re
import time
from pathlib import Path, PurePosixPath
from typing import Callable, Mapping

from hermes_cli.owner_worker.tool_executor_sandbox import (
    SandboxMountPolicy,
    SandboxSecurityPolicy,
    SandboxVerificationInvalid,
)

_REQUIRED_NAMESPACES = ("user", "pid", "ipc", "mnt", "net")
_ZERO_CAPABILITIES = ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")
_ATTESTATION_TIMEOUT_SECONDS = 5.0
_ATTESTATION_RETRY_SECONDS = 0.001


def attest_host_bubblewrap_process(
    pid: int,
    *,
    mount_policy: SandboxMountPolicy,
    security_policy: SandboxSecurityPolicy,
    proc_root: str | Path = "/proc",
    read_text: Callable[[Path], str] | None = None,
    read_link: Callable[[Path], str] = os.readlink,
    stat_path: Callable[[Path], os.stat_result] | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Require exact kernel state before an executor start gate is released."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise SandboxVerificationInvalid("sandbox process identity is invalid")
    root = Path(proc_root) / str(pid)
    reader = read_text or (lambda path: path.read_text(encoding="utf-8"))
    stat_reader = stat_path or (lambda path: path.stat())
    status = _await_final_security_status(
        root / "status",
        security_policy,
        reader=reader,
        clock=clock,
        sleep=sleep,
    )
    try:
        mountinfo = reader(root / "mountinfo")
        namespaces = {name: read_link(root / "ns" / name) for name in _REQUIRED_NAMESPACES}
        root_link = read_link(root / "root")
        root_status = stat_reader(root / "root")
    except (OSError, UnicodeError) as exc:
        raise SandboxVerificationInvalid("sandbox process evidence is unavailable") from exc

    _attest_status(status, security_policy)
    _attest_namespaces(pid, namespaces, proc_root=Path(proc_root), read_link=read_link)
    try:
        supervisor_root = stat_reader(Path(proc_root) / "self" / "root")
    except OSError as exc:
        raise SandboxVerificationInvalid("parent root filesystem evidence is unavailable") from exc
    if root_link != "/" or (root_status.st_dev, root_status.st_ino) == (
        supervisor_root.st_dev, supervisor_root.st_ino,
    ):
        raise SandboxVerificationInvalid("sandbox root filesystem identity is invalid")
    _attest_mounts(
        mountinfo,
        mount_policy,
        process_root=root / "root",
        stat_path=stat_reader,
    )


def _await_final_security_status(
    path: Path,
    policy: SandboxSecurityPolicy,
    *,
    reader: Callable[[Path], str],
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> dict[str, str]:
    """Wait for Bubblewrap to finish applying its final child security state."""
    deadline = clock() + _ATTESTATION_TIMEOUT_SECONDS
    last_error: SandboxVerificationInvalid | None = None
    while True:
        try:
            status = _status_fields(reader(path))
            _attest_status(status, policy)
            return status
        except (OSError, UnicodeError) as exc:
            raise SandboxVerificationInvalid("sandbox process evidence is unavailable") from exc
        except SandboxVerificationInvalid as exc:
            last_error = exc
            if status.get("Name") != "bwrap":
                raise
        if clock() >= deadline:
            raise last_error
        sleep(_ATTESTATION_RETRY_SECONDS)


def _status_fields(raw: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in raw.splitlines():
        key, separator, value = line.partition(":")
        if separator:
            result[key] = value.strip()
    return result


def _attest_status(status: Mapping[str, str], policy: SandboxSecurityPolicy) -> None:
    try:
        uid = _status_ids(status["Uid"])
        gid = _status_ids(status["Gid"])
        no_new_privileges = int(status["NoNewPrivs"])
        seccomp = int(status["Seccomp"])
        capabilities = tuple(int(status[name], 16) for name in _ZERO_CAPABILITIES)
    except (KeyError, ValueError) as exc:
        raise SandboxVerificationInvalid("sandbox process status is invalid") from exc
    if uid != (policy.uid,) * 4 or gid != (policy.gid,) * 4:
        raise SandboxVerificationInvalid("sandbox process uid/gid is invalid")
    if no_new_privileges != 1 or seccomp != 2:
        raise SandboxVerificationInvalid("sandbox no-new-privileges or seccomp state is invalid")
    if any(capabilities):
        raise SandboxVerificationInvalid("sandbox capabilities were not fully dropped")


def _status_ids(value: str) -> tuple[int, ...]:
    values = tuple(int(item) for item in value.split())
    if len(values) != 4:
        raise ValueError
    return values


def _attest_namespaces(
    pid: int,
    namespaces: Mapping[str, str],
    *,
    proc_root: Path,
    read_link: Callable[[Path], str],
) -> None:
    for name in _REQUIRED_NAMESPACES:
        value = namespaces.get(name, "")
        if not re.fullmatch(rf"{name}:\[[0-9]+\]", value):
            raise SandboxVerificationInvalid("sandbox namespace evidence is invalid")
        try:
            parent = read_link(proc_root / "self" / "ns" / name)
        except OSError as exc:
            raise SandboxVerificationInvalid("parent namespace evidence is unavailable") from exc
        if parent == value:
            raise SandboxVerificationInvalid("sandbox namespace was not isolated")
    if pid == os.getpid():
        raise SandboxVerificationInvalid("sandbox process identity aliases supervisor")


def _attest_mounts(
    raw: str,
    mount_policy: SandboxMountPolicy,
    *,
    process_root: Path,
    stat_path: Callable[[Path], os.stat_result],
) -> None:
    entries = _mount_entries(raw)
    expected = {
        PurePosixPath("/"): ("ro", "tmpfs"),
        PurePosixPath("/workspace"): ("rw", None),
        PurePosixPath("/executor"): ("rw", None),
        PurePosixPath("/executor/tmp"): ("rw", "tmpfs"),
    }
    expected.update({mount.destination: ("ro", None) for mount in mount_policy.readonly_mounts})
    for destination, (access, filesystem_type) in expected.items():
        entry = entries.get(destination)
        if (
            entry is None
            or access not in entry[0]
            or (filesystem_type is not None and entry[1] != filesystem_type)
        ):
            raise SandboxVerificationInvalid("sandbox mount topology does not match policy")
    for mount in mount_policy.readonly_mounts:
        try:
            source_status = stat_path(mount.source)
            destination_status = stat_path(
                process_root / str(mount.destination).lstrip("/")
            )
        except OSError as exc:
            raise SandboxVerificationInvalid("sandbox mount source evidence is unavailable") from exc
        if (source_status.st_dev, source_status.st_ino) != (
            destination_status.st_dev,
            destination_status.st_ino,
        ):
            raise SandboxVerificationInvalid("sandbox readonly mount source does not match policy")
    for forbidden in (".env", "docker.sock", "ssh-agent", "SSH_AUTH_SOCK"):
        if forbidden in raw:
            raise SandboxVerificationInvalid("sandbox mount topology exposes forbidden authority")


def _mount_entries(raw: str) -> dict[PurePosixPath, tuple[frozenset[str], str, str]]:
    result: dict[PurePosixPath, tuple[frozenset[str], str, str]] = {}
    for line in raw.splitlines():
        fields = line.split()
        if len(fields) < 10 or "-" not in fields:
            raise SandboxVerificationInvalid("sandbox mount information is invalid")
        separator = fields.index("-")
        if separator + 2 >= len(fields):
            raise SandboxVerificationInvalid("sandbox mount information is invalid")
        destination = PurePosixPath(_unescape_mount_field(fields[4]))
        entry = (
            frozenset(fields[5].split(",")),
            _unescape_mount_field(fields[separator + 1]),
            _unescape_mount_field(fields[separator + 2]),
        )
        if destination in result:
            raise SandboxVerificationInvalid("sandbox mount information is ambiguous")
        result[destination] = entry
    return result


def _unescape_mount_field(value: str) -> str:
    return re.sub(
        r"\\(040|011|012|134)",
        lambda match: {"040": " ", "011": "\t", "012": "\n", "134": "\\"}[match.group(1)],
        value,
    )
