"""Minimal, fail-closed environment for an authenticated Tool Executor.

The executor receives a freshly assembled mapping. This module never starts
from ``os.environ`` because inherited control-plane data is not authority for a
task-bound child runtime. Host runtime paths are admission-only parent inputs;
the child sees fixed sandbox-internal paths exclusively.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping

from hermes_cli.owner_worker.executor_identity import ExecutorIdentity, ExecutorIdentityInvalid, parse_egress_profile


EXECUTOR_RUNTIME_FLAG = "HERMES_EXECUTOR_RUNTIME"
EXECUTOR_HOME = "HERMES_EXECUTOR_HOME"
EXECUTOR_TMP = "HERMES_EXECUTOR_TMP"
EXECUTOR_WORKSPACE_FD = "HERMES_EXECUTOR_WORKSPACE_FD"
EXECUTOR_BOOTSTRAP_FD = "HERMES_EXECUTOR_BOOTSTRAP_FD"
EXECUTOR_RESPONSE_FD = "HERMES_EXECUTOR_RESPONSE_FD"
EXECUTOR_START_GATE_FD = "HERMES_EXECUTOR_START_GATE_FD"
EXECUTOR_GENERATION = "HERMES_EXECUTOR_GENERATION"
EXECUTOR_EGRESS_PROFILE = "HERMES_EXECUTOR_EGRESS_PROFILE"

SANDBOX_EXECUTOR_HOME = "/executor"
SANDBOX_EXECUTOR_TMP = "/executor/tmp"
SANDBOX_RUNTIME_BIN = "/opt/hermes/python/bin"
SANDBOX_SYSTEM_PATH = f"{SANDBOX_RUNTIME_BIN}:/usr/bin:/bin"

_ALLOWED_ENV_KEYS = frozenset({
    "HOME", "TMPDIR", "PATH", "PWD", "LANG", "LC_ALL", "LC_CTYPE", "__CF_USER_TEXT_ENCODING",
    "PYTHONUNBUFFERED", "PYTHONNOUSERSITE",
    EXECUTOR_RUNTIME_FLAG, EXECUTOR_HOME, EXECUTOR_TMP, EXECUTOR_WORKSPACE_FD,
    EXECUTOR_BOOTSTRAP_FD, EXECUTOR_RESPONSE_FD, EXECUTOR_START_GATE_FD,
    EXECUTOR_GENERATION, EXECUTOR_EGRESS_PROFILE,
})

# These names identify parent/control-plane authority. Their presence is a
# configuration failure even if a caller attempts to pass them explicitly.
_FORBIDDEN_EXACT = frozenset({
    "HERMES_CONTROL_HOME", "HERMES_OWNER_KEY", "HERMES_OWNER_WORKER_CAPABILITY_ISSUER",
    "HERMES_OWNER_WORKER_CAPABILITY_PUBLIC_KEY", "HERMES_OWNER_WORKER_CAPABILITY_RETAINED_PUBLIC_KEYS",
    "HERMES_OWNER_WORKER_LEASE", "HERMES_PROFILE", "HERMES_SESSION_PROFILE",
    "HERMES_CONFIG", "HERMES_ENV", "HERMES_WORKSPACE_ROOT", "TERMINAL_CWD",
    "DOCKER_HOST", "DOCKER_SOCK", "SSH_AUTH_SOCK",
})
_FORBIDDEN_PARTS = ("TOKEN", "SECRET", "PRIVATE_KEY", "API_KEY", "PASSWORD", "WEBSOCKET", "REPLAY", "SUPERVISOR")


class ExecutorEnvironmentInvalid(ValueError):
    """Executor environment contains an untrusted or unsafe value."""


def _absolute_under(value: str | Path, parent: str | Path, *, field: str) -> Path:
    path = Path(value).resolve()
    root = Path(parent).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ExecutorEnvironmentInvalid(f"{field} must be beneath executor home") from exc
    return path


def _validate_descriptor_numbers(environment: Mapping[str, str]) -> None:
    values: list[int] = []
    try:
        for key in (
            EXECUTOR_WORKSPACE_FD, EXECUTOR_BOOTSTRAP_FD,
            EXECUTOR_RESPONSE_FD, EXECUTOR_START_GATE_FD,
        ):
            value = int(str(environment[key]))
            if value in {0, 1, 2} or value < 0:
                raise ValueError
            values.append(value)
        if len(set(values)) != len(values) or int(str(environment[EXECUTOR_GENERATION])) < 1:
            raise ValueError
    except (KeyError, ValueError) as exc:
        raise ExecutorEnvironmentInvalid("executor descriptor or generation is invalid") from exc


def validate_executor_environment(environment: Mapping[str, str]) -> None:
    """Reject any non-minimal executor environment before tool imports occur."""
    if not isinstance(environment, Mapping):
        raise ExecutorEnvironmentInvalid("executor environment is invalid")
    keys = {str(key) for key in environment}
    unexpected = keys - _ALLOWED_ENV_KEYS
    if unexpected:
        raise ExecutorEnvironmentInvalid("executor environment contains unallowed variables")
    for key in keys:
        upper = key.upper()
        if key in _FORBIDDEN_EXACT or any(part in upper for part in _FORBIDDEN_PARTS):
            raise ExecutorEnvironmentInvalid("executor environment contains forbidden authority")
    required = {
        EXECUTOR_RUNTIME_FLAG: "1",
        EXECUTOR_HOME: SANDBOX_EXECUTOR_HOME,
        EXECUTOR_TMP: SANDBOX_EXECUTOR_TMP,
        EXECUTOR_WORKSPACE_FD: None,
        EXECUTOR_BOOTSTRAP_FD: None,
        EXECUTOR_RESPONSE_FD: None,
        EXECUTOR_START_GATE_FD: None,
        EXECUTOR_GENERATION: None,
        EXECUTOR_EGRESS_PROFILE: None,
        "HOME": SANDBOX_EXECUTOR_HOME,
        "TMPDIR": SANDBOX_EXECUTOR_TMP,
        "PWD": "/workspace",
    }
    for key, expected in required.items():
        value = str(environment.get(key, "") or "").strip()
        if not value or (expected is not None and value != expected):
            raise ExecutorEnvironmentInvalid(f"executor environment is missing {key}")
    try:
        parse_egress_profile(environment[EXECUTOR_EGRESS_PROFILE], executor_admissible=True)
    except ExecutorIdentityInvalid as exc:
        raise ExecutorEnvironmentInvalid("executor egress profile is invalid") from exc
    _validate_descriptor_numbers(environment)


def build_executor_environment(
    identity: ExecutorIdentity,
    *,
    runtime_home: str | Path,
    workspace_fd: int,
    bootstrap_fd: int,
    response_fd: int,
    start_gate_fd: int,
    egress_profile: str,
    path: str = SANDBOX_SYSTEM_PATH,
    locale: str = "C.UTF-8",
) -> dict[str, str]:
    """Create the complete allowlisted environment for one executor child."""
    Path(runtime_home).resolve()
    try:
        egress_profile = parse_egress_profile(egress_profile, executor_admissible=True).value
    except ExecutorIdentityInvalid as exc:
        raise ExecutorEnvironmentInvalid("egress profile is invalid") from exc
    result = {
        "HOME": SANDBOX_EXECUTOR_HOME,
        "TMPDIR": SANDBOX_EXECUTOR_TMP,
        "PWD": "/workspace",
        "PATH": str(path),
        "LANG": str(locale),
        "PYTHONUNBUFFERED": "1",
        "PYTHONNOUSERSITE": "1",
        EXECUTOR_RUNTIME_FLAG: "1",
        EXECUTOR_HOME: SANDBOX_EXECUTOR_HOME,
        EXECUTOR_TMP: SANDBOX_EXECUTOR_TMP,
        EXECUTOR_WORKSPACE_FD: str(int(workspace_fd)),
        EXECUTOR_BOOTSTRAP_FD: str(int(bootstrap_fd)),
        EXECUTOR_RESPONSE_FD: str(int(response_fd)),
        EXECUTOR_START_GATE_FD: str(int(start_gate_fd)),
        EXECUTOR_GENERATION: str(identity.executor_generation),
        EXECUTOR_EGRESS_PROFILE: str(egress_profile),
    }
    validate_executor_environment(result)
    return result


_NESTED_CHILD_ALLOWED_ENV_KEYS = frozenset({
    "HOME", "TMPDIR", "PATH", "LANG", "LC_ALL", "LC_CTYPE", "__CF_USER_TEXT_ENCODING",
    "PYTHONUNBUFFERED", "PYTHONNOUSERSITE",
})


def build_authenticated_child_environment(environment: Mapping[str, str] | None = None) -> dict[str, str]:
    """Build the only environment allowed for executor terminal descendants.

    Explicit values are rejected rather than filtered so callers cannot use the
    nested process boundary to smuggle authority past the executor admission.
    """
    supplied = {} if environment is None else environment
    if not isinstance(supplied, Mapping):
        raise ExecutorEnvironmentInvalid("authenticated child environment is invalid")
    keys = {str(key) for key in supplied}
    unexpected = keys - _NESTED_CHILD_ALLOWED_ENV_KEYS
    if unexpected:
        raise ExecutorEnvironmentInvalid("authenticated child environment contains unallowed variables")
    for key in keys:
        upper = key.upper()
        if key in _FORBIDDEN_EXACT or key.startswith("HERMES_EXECUTOR_") or any(part in upper for part in _FORBIDDEN_PARTS):
            raise ExecutorEnvironmentInvalid("authenticated child environment contains forbidden authority")
    result = {
        "HOME": SANDBOX_EXECUTOR_HOME,
        "TMPDIR": SANDBOX_EXECUTOR_TMP,
        "PATH": SANDBOX_SYSTEM_PATH,
        "LANG": "C.UTF-8",
        "PYTHONUNBUFFERED": "1",
        "PYTHONNOUSERSITE": "1",
    }
    for key in ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "__CF_USER_TEXT_ENCODING"):
        value = supplied.get(key)
        if value is not None:
            normalized = str(value).strip()
            if not normalized:
                raise ExecutorEnvironmentInvalid("authenticated child environment contains an empty value")
            result[key] = normalized
    return result


def reject_parent_authority_environment(parent: Mapping[str, str] | None = None) -> None:
    """Testable guard: parent authority must never be copied into a child env."""
    source = os.environ if parent is None else parent
    for key in source:
        if key in _FORBIDDEN_EXACT or any(part in str(key).upper() for part in _FORBIDDEN_PARTS):
            continue
