"""One-shot isolated Tool Executor process entrypoint.

The parent provides exactly three inherited descriptors: workspace cwd, a
bootstrap request reader, and a response writer.  No control-plane socket or
long-lived owner-worker credential enters this process.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any

from hermes_cli.owner_worker.executor_identity import (
    ExecutorIdentity,
    ExecutorIdentityInvalid,
    ExecutorInvocation,
    ExecutorResourceDecision,
    install_executor_identity,
    parse_egress_profile,
    reset_executor_identity,
)
from hermes_cli.tool_executor_runtime.env import (
    EXECUTOR_BOOTSTRAP_FD,
    EXECUTOR_EGRESS_PROFILE,
    EXECUTOR_RESPONSE_FD,
    EXECUTOR_WORKSPACE_FD,
    ExecutorEnvironmentInvalid,
    validate_executor_environment,
)


class ExecutorRuntimeInvalid(RuntimeError):
    """The isolated executor bootstrap did not meet its admission contract."""


def _read_bootstrap(fd: int) -> dict[str, Any]:
    try:
        with os.fdopen(fd, "rb", closefd=True) as stream:
            raw = stream.read()
        value = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise ExecutorRuntimeInvalid("executor bootstrap is malformed") from exc
    if not isinstance(value, dict):
        raise ExecutorRuntimeInvalid("executor bootstrap is malformed")
    return value


def _write_response(fd: int, result: str) -> None:
    payload = json.dumps({"result": result}, ensure_ascii=False).encode("utf-8")
    with os.fdopen(fd, "wb", closefd=True) as stream:
        stream.write(payload)
        stream.flush()


def _admit_workspace_mount(workspace_fd: int) -> None:
    """Require the passed workspace descriptor to match sandbox `/workspace`."""
    descriptor = os.fstat(workspace_fd)
    mounted = os.stat("/workspace")
    if (descriptor.st_dev, descriptor.st_ino) != (mounted.st_dev, mounted.st_ino):
        raise ExecutorRuntimeInvalid("executor workspace descriptor does not match sandbox mount")
    os.chdir("/workspace")
    os.close(workspace_fd)


def _require_matching_egress_profile(invocation: ExecutorInvocation, environment: dict[str, str]) -> None:
    try:
        environment_profile = parse_egress_profile(
            environment[EXECUTOR_EGRESS_PROFILE], executor_admissible=True
        )
    except ExecutorIdentityInvalid as exc:
        raise ExecutorRuntimeInvalid("executor egress profile is invalid") from exc
    if invocation.egress_profile != environment_profile:
        raise ExecutorRuntimeInvalid("executor egress profile does not match bootstrap")


def invocation_from_payload(payload: dict[str, Any]) -> ExecutorInvocation:
    try:
        identity = ExecutorIdentity.from_payload(payload["identity"])
        return ExecutorInvocation(
            identity=identity,
            tool_name=payload["tool_name"],
            arguments=payload["arguments"],
            tool_call_id=payload["tool_call_id"],
            turn_id=payload["turn_id"],
            api_request_id=payload["api_request_id"],
            invocation_id=payload["invocation_id"],
            egress_profile=payload["egress_profile"],
            resource_decision=ExecutorResourceDecision.from_payload(identity, payload["resource_decision"]),
        )
    except (KeyError, ExecutorIdentityInvalid) as exc:
        raise ExecutorRuntimeInvalid("executor invocation is invalid") from exc


def run_once(environment: dict[str, str] | None = None) -> int:
    """Validate bootstrap, dispatch once directly to the registry, and exit."""
    env = os.environ if environment is None else environment
    try:
        validate_executor_environment(env)
        workspace_fd = int(env[EXECUTOR_WORKSPACE_FD])
        bootstrap_fd = int(env[EXECUTOR_BOOTSTRAP_FD])
        response_fd = int(env[EXECUTOR_RESPONSE_FD])
        # Bubblewrap mounted this already-authorized descriptor at a fixed
        # internal path. Verify that binding before importing tool modules.
        _admit_workspace_mount(workspace_fd)
        invocation = invocation_from_payload(_read_bootstrap(bootstrap_fd))
        _require_matching_egress_profile(invocation, env)
        token = install_executor_identity(invocation.identity)
        try:
            from tools.registry import registry

            result = registry.dispatch(
                invocation.tool_name,
                dict(invocation.arguments),
                task_id=invocation.identity.task_id,
                session_id=invocation.identity.session_id,
                executor_identity=invocation.identity,
                executor_invocation=invocation,
            )
        finally:
            reset_executor_identity(token)
        _write_response(response_fd, str(result))
        return 0
    except (ExecutorEnvironmentInvalid, ExecutorRuntimeInvalid, OSError, ValueError) as exc:
        try:
            response_fd = int(env.get(EXECUTOR_RESPONSE_FD, "-1"))
            if response_fd >= 0:
                _write_response(response_fd, json.dumps({"error": f"executor admission failed: {exc}"}))
        except Exception:
            pass
        return 2


def main() -> None:
    raise SystemExit(run_once())


if __name__ == "__main__":
    main()
