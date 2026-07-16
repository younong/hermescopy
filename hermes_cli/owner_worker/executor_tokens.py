"""Typed claims for opaque, broker-issued Tool Executor grants.

Executor grants are deliberately not owner-worker HTTP/WS credentials. The
Owner Worker broker keeps the opaque capability and revocation state locally;
the executor receives only the random bearer string needed for one precise
operation.
"""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass

from .executor_identity import ExecutorIdentity

AUD_EXECUTOR_BOOTSTRAP = "executor-bootstrap"
AUD_EXECUTOR_RPC = "executor-rpc"
AUD_CREDENTIAL_BROKER = "executor-credential-broker"
AUD_PROCESS_REGISTRY = "executor-process-registry"
AUD_CHECKPOINT = "executor-checkpoint"
AUD_TERMINAL = "executor-terminal"


class ExecutorCapabilityInvalid(ValueError):
    """An executor capability is expired, revoked, or scoped incorrectly."""


def _capability_metadata(value: str, field: str) -> str:
    normalized = str(value or "").strip()
    if not normalized or any(ord(character) < 0x20 or ord(character) == 0x7F for character in normalized):
        raise ExecutorCapabilityInvalid("executor capability metadata is invalid")
    if "*" in normalized or normalized.lower() in {"all", "any"}:
        raise ExecutorCapabilityInvalid("executor capability metadata is too broad")
    if field == "scope" and "/" in normalized:
        raise ExecutorCapabilityInvalid("executor capability metadata is too broad")
    return normalized


@dataclass(frozen=True)
class ExecutorCapabilityClaims:
    """Exact short-lived capability claims retained by the owner-side broker."""

    capability: str
    identity: ExecutorIdentity
    task_id: str
    worker_generation: int
    executor_generation: int
    audience: str
    operation: str
    scope: str
    issued_at: int
    expires_at: int
    jti: str

    def __post_init__(self) -> None:
        if not all(str(value or "").strip() for value in (self.capability, self.jti)):
            raise ExecutorCapabilityInvalid("executor capability claims are incomplete")
        _capability_metadata(self.audience, "audience")
        _capability_metadata(self.operation, "operation")
        _capability_metadata(self.scope, "scope")
        if self.expires_at <= self.issued_at:
            raise ExecutorCapabilityInvalid("executor capability expiry is invalid")

    @classmethod
    def issue(
        cls,
        identity: ExecutorIdentity,
        *,
        audience: str,
        operation: str,
        scope: str,
        ttl_seconds: int = 30,
        now: int | None = None,
    ) -> "ExecutorCapabilityClaims":
        issued_at = int(time.time()) if now is None else int(now)
        ttl = int(ttl_seconds)
        if ttl < 1 or ttl > 300:
            raise ExecutorCapabilityInvalid("executor capability ttl is outside the permitted bound")
        return cls(
            capability=secrets.token_urlsafe(32),
            identity=identity,
            task_id=identity.task_id,
            worker_generation=identity.worker_generation,
            executor_generation=identity.executor_generation,
            audience=_capability_metadata(audience, "audience"),
            operation=_capability_metadata(operation, "operation"),
            scope=_capability_metadata(scope, "scope"),
            issued_at=issued_at,
            expires_at=issued_at + ttl,
            jti=secrets.token_urlsafe(18),
        )

    def validate(
        self,
        identity: ExecutorIdentity,
        *,
        audience: str,
        operation: str,
        scope: str,
        now: int | None = None,
    ) -> None:
        current = int(time.time()) if now is None else int(now)
        if current >= self.expires_at:
            raise ExecutorCapabilityInvalid("executor_capability_expired")
        if (
            self.identity != identity
            or self.task_id != identity.task_id
            or self.worker_generation != identity.worker_generation
            or self.executor_generation != identity.executor_generation
        ):
            raise ExecutorCapabilityInvalid("executor_capability_identity_mismatch")
        if (
            self.audience != _capability_metadata(audience, "audience")
            or self.operation != _capability_metadata(operation, "operation")
            or self.scope != _capability_metadata(scope, "scope")
        ):
            raise ExecutorCapabilityInvalid("executor_capability_scope_mismatch")
