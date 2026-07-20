"""One-shot isolated Tool Executor process entrypoint.

The parent provides workspace, bootstrap, response, and start-gate descriptors.
An exact allowlisted owner invocation may additionally receive a one-shot
owner-worker relay descriptor. No control-plane socket or long-lived owner
credential enters this process.
"""
from __future__ import annotations

import json
import os
import stat
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
    EXECUTOR_START_GATE_FD,
    EXECUTOR_OWNER_RELAY_FD,
    EXECUTOR_WORKSPACE_FD,
    ExecutorEnvironmentInvalid,
    validate_executor_environment,
)


class ExecutorRuntimeInvalid(RuntimeError):
    """The isolated executor bootstrap did not meet its admission contract."""


def _await_start_gate(fd: int) -> None:
    """Block until the parent attests this exact sandbox and releases it."""
    try:
        with os.fdopen(fd, "rb", closefd=True) as stream:
            value = stream.read(1)
            trailing = stream.read(1)
    except OSError as exc:
        raise ExecutorRuntimeInvalid("executor start gate is unavailable") from exc
    if value != b"1" or trailing:
        raise ExecutorRuntimeInvalid("executor start gate was not released")


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


def _workspace_mount_status() -> os.stat_result:
    return os.stat("/workspace")


def _admit_workspace_mount(workspace_fd: int) -> None:
    """Enter the workspace mount already attested by the parent process."""
    try:
        os.close(workspace_fd)
    except OSError as exc:
        if exc.errno != 9:
            raise
    mounted = _workspace_mount_status()
    if not stat.S_ISDIR(mounted.st_mode):
        raise ExecutorRuntimeInvalid("executor workspace mount is invalid")
    os.chdir("/workspace")


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
        start_gate_fd = int(env[EXECUTOR_START_GATE_FD])
        # Do not read tool-controlled bootstrap data or import tools until the
        # parent has attested the exact post-spawn sandbox process.
        _await_start_gate(start_gate_fd)
        # Bubblewrap mounted this already-authorized descriptor at a fixed
        # internal path. Verify that binding before importing tool modules.
        _admit_workspace_mount(workspace_fd)
        invocation = invocation_from_payload(_read_bootstrap(bootstrap_fd))
        _require_matching_egress_profile(invocation, env)
        relay_fd_text = str(env.get(EXECUTOR_OWNER_RELAY_FD, "") or "").strip()
        from hermes_cli.owner_worker.owner_tool_relay import OWNER_RELAY_TOOL_NAMES

        if invocation.tool_name in OWNER_RELAY_TOOL_NAMES:
            if not relay_fd_text:
                raise ExecutorRuntimeInvalid("authenticated owner tool relay is unavailable")
            from hermes_cli.owner_worker.owner_tool_relay import (
                OwnerToolRelayError,
                dispatch_owner_tool_over_relay,
            )

            try:
                result = dispatch_owner_tool_over_relay(int(relay_fd_text), invocation)
            except OwnerToolRelayError as exc:
                raise ExecutorRuntimeInvalid("authenticated owner tool relay failed") from exc
        else:
            if relay_fd_text:
                raise ExecutorRuntimeInvalid("unexpected authenticated owner tool relay")
            token = install_executor_identity(invocation.identity)
            try:
                from tools.registry import discover_builtin_tools, registry

                discover_builtin_tools()
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
