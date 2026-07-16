"""Narrow de-identified authority audit reporter for Owner Worker decisions."""
from __future__ import annotations

from typing import Callable

from hermes_cli.dashboard_auth.audit import (
    AuthorityAuditEvent,
    AuthorityAuditReason,
    audit_authority,
    new_authority_correlation_id,
)

from .executor_identity import ExecutorIdentity


ExecutorAuditReporter = Callable[[AuthorityAuditEvent, AuthorityAuditReason, ExecutorIdentity], None]


def report_worker_lifecycle(
    event: AuthorityAuditEvent,
    reason: AuthorityAuditReason,
    *,
    worker_generation: int,
) -> None:
    """Best-effort de-identified lifecycle audit for trusted worker boundaries."""
    try:
        audit_authority(
            event,
            correlation_id=new_authority_correlation_id(),
            reason=reason,
            audience_class="browser-ws",
            worker_generation=worker_generation,
        )
    except Exception:
        return


def report_executor_authority_decision(
    event: AuthorityAuditEvent,
    reason: AuthorityAuditReason,
    identity: ExecutorIdentity,
) -> None:
    """Best-effort Control Plane audit without executor input or owner identity."""
    try:
        audit_authority(
            event,
            correlation_id=new_authority_correlation_id(),
            reason=reason,
            audience_class="none",
            worker_generation=identity.worker_generation,
            executor_generation=identity.executor_generation,
        )
    except Exception:
        # Observability cannot weaken an otherwise fail-closed executor decision.
        return
