"""Trusted, immutable identity for authenticated Tool Executor runtimes.

An executor identity is created from the already-admitted Owner Worker lease.  It
is intentionally separate from tool arguments, ambient cwd, and environment
variables: those sources are never authority inputs in authenticated mode.
"""
from __future__ import annotations

import contextvars
import hashlib
import secrets
from dataclasses import dataclass
from typing import Any, Mapping

from hermes_cli.dashboard_auth.authority import OwnerWorkerAuthorityLease, WorkerLeaseState


class ExecutorIdentityInvalid(ValueError):
    """Executor identity or invocation metadata was incomplete or unsafe."""


def _required(value: str, field: str) -> str:
    normalized = str(value or "").strip()
    if not normalized or "\x00" in normalized:
        raise ExecutorIdentityInvalid(f"{field} is required")
    return normalized


def _nonnegative(value: int, field: str, *, minimum: int = 0) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ExecutorIdentityInvalid(f"{field} is invalid") from exc
    if parsed < minimum:
        raise ExecutorIdentityInvalid(f"{field} is invalid")
    return parsed


@dataclass(frozen=True)
class ExecutorIdentity:
    """Exact authenticated identity of one task-bound executor generation."""

    owner_key: str
    workspace_prefix: str
    worker_id: str
    worker_generation: int
    lease_version: int
    recovery_generation: int
    task_id: str
    session_id: str
    executor_id: str
    executor_generation: int

    def __post_init__(self) -> None:
        _required(self.owner_key, "owner_key")
        prefix = _required(self.workspace_prefix, "workspace_prefix")
        if prefix.startswith("/") or any(part in {"", ".", ".."} for part in prefix.split("/")):
            raise ExecutorIdentityInvalid("workspace_prefix must be a relative controlled path")
        _required(self.worker_id, "worker_id")
        _required(self.task_id, "task_id")
        _required(self.session_id, "session_id")
        _required(self.executor_id, "executor_id")
        _nonnegative(self.worker_generation, "worker_generation", minimum=1)
        _nonnegative(self.lease_version, "lease_version", minimum=1)
        _nonnegative(self.recovery_generation, "recovery_generation")
        _nonnegative(self.executor_generation, "executor_generation", minimum=1)

    @classmethod
    def for_task(
        cls,
        lease: OwnerWorkerAuthorityLease,
        *,
        workspace_prefix: str,
        task_id: str,
        session_id: str,
        executor_id: str | None = None,
        executor_generation: int = 1,
    ) -> "ExecutorIdentity":
        return cls(
            owner_key=lease.owner_key,
            workspace_prefix=workspace_prefix,
            worker_id=lease.worker_id,
            worker_generation=lease.worker_generation,
            lease_version=lease.lease_version,
            recovery_generation=lease.recovery_generation,
            task_id=task_id,
            session_id=session_id,
            executor_id=executor_id or secrets.token_urlsafe(18),
            executor_generation=executor_generation,
        )

    @property
    def owner_digest(self) -> str:
        return hashlib.sha256(self.owner_key.encode("utf-8")).hexdigest()

    @property
    def stable_key(self) -> tuple[str, str, str, int, int, int, str, str, int]:
        return (
            self.owner_key,
            self.workspace_prefix,
            self.worker_id,
            self.worker_generation,
            self.lease_version,
            self.recovery_generation,
            self.task_id,
            self.executor_id,
            self.executor_generation,
        )

    @property
    def lease(self) -> OwnerWorkerAuthorityLease:
        return OwnerWorkerAuthorityLease(
            self.owner_key,
            self.worker_generation,
            self.worker_id,
            # Lease state is verified by the durable authority, never trusted
            # from a child runtime payload.
            WorkerLeaseState.ACTIVE,
            self.lease_version,
            self.recovery_generation,
        )

    def matches(self, other: "ExecutorIdentity | None") -> bool:
        return isinstance(other, ExecutorIdentity) and self == other

    def to_payload(self) -> dict[str, Any]:
        return {
            "owner_key": self.owner_key,
            "workspace_prefix": self.workspace_prefix,
            "worker_id": self.worker_id,
            "worker_generation": self.worker_generation,
            "lease_version": self.lease_version,
            "recovery_generation": self.recovery_generation,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "executor_id": self.executor_id,
            "executor_generation": self.executor_generation,
        }

    @classmethod
    def from_payload(cls, value: Mapping[str, Any]) -> "ExecutorIdentity":
        if not isinstance(value, Mapping):
            raise ExecutorIdentityInvalid("executor identity payload is invalid")
        try:
            return cls(
                owner_key=value["owner_key"],
                workspace_prefix=value["workspace_prefix"],
                worker_id=value["worker_id"],
                worker_generation=value["worker_generation"],
                lease_version=value["lease_version"],
                recovery_generation=value["recovery_generation"],
                task_id=value["task_id"],
                session_id=value["session_id"],
                executor_id=value["executor_id"],
                executor_generation=value["executor_generation"],
            )
        except KeyError as exc:
            raise ExecutorIdentityInvalid("executor identity payload is incomplete") from exc


@dataclass(frozen=True)
class ExecutorInvocation:
    """Explicit request metadata that must cross the executor process boundary."""

    identity: ExecutorIdentity
    tool_name: str
    arguments: Mapping[str, Any]
    tool_call_id: str
    turn_id: str
    api_request_id: str
    invocation_id: str
    egress_profile: str = "tool-none"

    def __post_init__(self) -> None:
        _required(self.tool_name, "tool_name")
        _required(self.tool_call_id, "tool_call_id")
        _required(self.turn_id, "turn_id")
        _required(self.api_request_id, "api_request_id")
        _required(self.invocation_id, "invocation_id")
        _required(self.egress_profile, "egress_profile")
        if not isinstance(self.arguments, Mapping):
            raise ExecutorIdentityInvalid("arguments must be a mapping")

    def to_payload(self) -> dict[str, Any]:
        return {
            "identity": self.identity.to_payload(),
            "tool_name": self.tool_name,
            "arguments": dict(self.arguments),
            "tool_call_id": self.tool_call_id,
            "turn_id": self.turn_id,
            "api_request_id": self.api_request_id,
            "invocation_id": self.invocation_id,
            "egress_profile": self.egress_profile,
        }


_executor_identity: contextvars.ContextVar[ExecutorIdentity | None] = contextvars.ContextVar(
    "authenticated_executor_identity", default=None
)


def current_executor_identity() -> ExecutorIdentity | None:
    """Return the identity installed by a validated executor runtime only."""
    return _executor_identity.get()


def install_executor_identity(identity: ExecutorIdentity) -> contextvars.Token:
    return _executor_identity.set(identity)


def reset_executor_identity(token: contextvars.Token) -> None:
    _executor_identity.reset(token)
